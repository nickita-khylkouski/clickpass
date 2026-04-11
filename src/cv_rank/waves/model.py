"""Logistic regression model for per-person show probability prediction.

Trained on ~8,000 approved applicants from 30 in-person CV hackathons.
Coefficients are hardcoded after one-time training against the platform DB.

Features (6 + 1 interaction = 16 weights):
- timing_bucket: when they applied relative to event date (25.8% spread)
- tz_distance: timezone offset from event location (30.7% spread)
- prior_show_rate_bucket: personal historical attendance rate (20.1% spread)
- log_page_views: log(page_view_count + 1), pre-event views only (62.3% spread — strongest)
- notification_read: whether they read the approval notification (28.5% spread)
- log_views_x_remote: interaction — page views effect amplified for remote attendees
- log_horizon_days: log(days_until_event + 1) at prediction time — compensates for
  incomplete engagement data when predicting before the event

Page views are filtered to pre-event only (no temporal leakage).
The model is natively calibrated (logistic regression predicted probabilities
ARE frequencies), so no post-hoc calibration needed.
"""

from __future__ import annotations

import math
from typing import Any

from .predict import (
    timing_bucket,
    prior_rate_bucket,
    prior_event_count_bucket,
    signup_percentile_bucket,
)


# ---------------------------------------------------------------------------
# Trained model coefficients — HORIZON-AWARE v4 (with critical interactions)
# ---------------------------------------------------------------------------
# Trained via: uv run python scripts/retrain_horizon.py
# Multi-snapshot training: 26,128 rows (5 snapshots × 7,971 applicants at T-0/1/3/7/14)
# GroupKFold CV (AUC 0.714): Proper validation respecting person-level clustering
# L2 regularization (C=1.0): Stabilizes coefficients despite correlated observations
#
# KEY FIX: Added page_views × horizon interaction
#   - Separates early high engagement (T-14, 5 views) from late medium (T-0, 5 views)
#   - notification_read coef now +0.3826 (POSITIVE, matching raw 46.8% vs 20% lift)
#   - Was -0.68 before interactions (Simpson's paradox from confounding)
#
# Calibration: <1% gap across all horizons (T-0 to T-14)

MODEL_COEFFICIENTS: dict[str, Any] | None = {
    "intercept": -2.6226,
    "weights": [
        # timing (reference: 21+ days)
        +0.1485,   # timing_14-21
        +0.3169,   # timing_7-14
        +0.6948,   # timing_3-7
        +1.0964,   # timing_1-3
        +1.4269,   # timing_<1
        # tz_distance (reference: local)
        -0.5083,   # tz_near
        -1.5900,   # tz_medium
        -1.2860,   # tz_far
        # prior_rate (reference: no_history)
        -0.4912,   # prior_0-25%
        -0.1394,   # prior_25-50%
        -0.3151,   # prior_50-75%
        +0.2183,   # prior_75-100%
        # engagement features (v4 - with critical interactions)
        +1.2459,   # log_page_views — main effect (boosted from +0.71 due to interactions)
        +0.3826,   # notification_read — POSITIVE! Fixed via interactions (was -0.68)
        +0.4498,   # log_views_x_remote — interaction: views amplified for remote attendees
        # prediction horizon (v4) — with full interaction modeling
        +0.4950,   # log_horizon_days — positive: compensates for incomplete engagement
        -0.2704,   # notif_read_x_horizon — negative: early readers are self-selected high-engagement
        -0.1906,   # page_views_x_horizon — CRITICAL: early high engagement less predictive than late
        -0.5608,   # notif_x_pageviews — notification helps low-engagement users more
    ],
    "feature_names": [
        "timing_bucket_14-21", "timing_bucket_7-14", "timing_bucket_3-7",
        "timing_bucket_1-3", "timing_bucket_<1",
        "tz_distance_near", "tz_distance_medium", "tz_distance_far",
        "prior_rate_bucket_0-25%", "prior_rate_bucket_25-50%",
        "prior_rate_bucket_50-75%", "prior_rate_bucket_75-100%",
        "log_page_views", "notification_read", "log_views_x_remote",
        "log_horizon_days", "notif_read_x_horizon",
        "page_views_x_horizon", "notif_x_pageviews",
    ],
    "n_samples": 26128,
    "positive_rate": 0.422,
}

# ---------------------------------------------------------------------------
# STAGE 1: Engagement Forecasting Models (TWO-STAGE APPROACH)
# ---------------------------------------------------------------------------
# Predicts FINAL engagement (at T-0) from CURRENT incomplete engagement (at T-h)
#
# Problem: When predicting at T-8, people have 2-3 views (current), but by T-0
# they'll have 6-7 views (final). Using current incomplete engagement causes
# systematic underprediction (-65 people bias at T-7).
#
# Solution: Learn how engagement grows from multi-snapshot training data, then
# forecast final engagement before predicting show probability.
#
# Features (12):
#   0. current_page_views (raw count)
#   1. log(current_views + 1)
#   2. current_notif_read (1 if read, 0 otherwise)
#   3. horizon (days until event)
#   4. log(horizon + 1)
#   5. tz_offset_hrs (timezone offset from event)
#   6-8. tz_local, tz_near, tz_medium (one-hot)
#   9. prior_events (count)
#   10. log(prior_events + 1)
#   11. log(current_views + 1) × log(horizon + 1) (interaction)
#
# Trained on ~20K paired observations (same person at T-h and T-0).

FORECASTING_MODELS: dict[str, Any] | None = {
    "page_views": {
        "intercept": 2.4845,
        "weights": [
            +1.1368,   # current_page_views
            -2.3653,   # log_current_views
            -1.5805,   # current_notif_read
            +0.1241,   # horizon
            -2.4987,   # log_horizon
            -0.0429,   # tz_offset_hrs
            +0.4258,   # tz_local
            -0.6279,   # tz_near
            -0.0790,   # tz_medium
            +0.1430,   # prior_events
            -1.2506,   # log_prior_events
            +3.1895,   # log_views_x_log_horizon (CRITICAL interaction)
        ],
    },
    "notif_read": {
        "intercept": -4.5052,
        "weights": [
            -0.0152,   # current_page_views
            +0.6664,   # log_current_views
            +7.3142,   # current_notif_read (if read now, likely stay read)
            -0.0925,   # horizon
            +1.2663,   # log_horizon
            +0.0311,   # tz_offset_hrs
            +0.5128,   # tz_local
            +0.0203,   # tz_near
            +0.3201,   # tz_medium
            +0.0368,   # prior_events
            -0.9945,   # log_prior_events
            -0.1194,   # log_views_x_log_horizon
        ],
    },
    "feature_names": [
        "current_page_views", "log_current_views", "current_notif_read",
        "horizon", "log_horizon", "tz_offset_hrs",
        "tz_local", "tz_near", "tz_medium",
        "prior_events", "log_prior_events",
        "log_views_x_log_horizon",
    ],
}

# ---------------------------------------------------------------------------
# STAGE 0: Signup Forecasting Models (THREE-STAGE APPROACH)
# ---------------------------------------------------------------------------
# Predicts FINAL approved count (at T-0) from CURRENT approved count (at T-h)
#
# Problem: At T-7, event has 238 approved, but by T-0 it will have 500 approved.
# Predicting on 238 people with avg P_show=45% gives 107 expected, but actual is 225.
# This is the root cause of -47 bias at T-7 (missing signup velocity).
#
# Solution: Forecast how many MORE people will sign up from T-h to T-0, then predict
# on the full forecasted population (both current and future signups).
#
# Features (11):
#   0. current_approved (raw count at T-h)
#   1. log(current_approved + 1)
#   2. velocity (signups per day in recent window)
#   3. log(|velocity| + 1) × sign(velocity)
#   4. acceleration (change in velocity)
#   5. log(|acceleration| + 1) × sign(acceleration)
#   6. horizon (days until event)
#   7. log(horizon + 1)
#   8. current_approved × horizon (interaction)
#   9. log(current_approved + 1) × log(horizon + 1) (interaction)
#   10. velocity × horizon (interaction)
#
# Trained on 60 historical events with LOO cross-validation:
#   T-7: MAE=64.5, Bias=-1.8, MAPE=31.3% (predicting 2.20x growth)
#   T-3: MAE=35.6, Bias=-0.6, MAPE=21.5% (predicting 1.50x growth)
#   T-1: MAE=21.1, Bias=+0.9, MAPE=12.2% (predicting 1.14x growth)

SIGNUP_FORECASTING_MODELS: dict[str, Any] | None = {
    "t7": {
        "intercept": 19.9048,
        "weights": [0.0257, 2.507, -0.0314, -4.4062, 3.6016, -1.3356, 0.0, 0.0, 0.18, 5.2131, -0.2198],
    },
    "t3": {
        "intercept": -15.8553,
        "weights": [0.1033, 5.1613, 0.0563, -8.2055, -0.4758, 3.1379, 0.0, -0.0, 0.3099, 7.155, 0.1688],
    },
    "t1": {
        "intercept": -12.3834,
        "weights": [0.5165, 7.9259, 0.1318, -15.8972, -0.2198, 6.9625, 0.0, 0.0, 0.5165, 5.4938, 0.1318],
    },
}

# ---------------------------------------------------------------------------
# Future approved-attendee conversion rates (Stage 3)
# ---------------------------------------------------------------------------
# These are descriptive pooled historical show rates from engagement_profiles.csv,
# not optimized calibration targets. They answer:
#   "Of the people who are approved during the remaining horizon window, what
#    fraction historically checked in?"
#
# Horizon buckets:
#   t7: approvals still to come from T-7→T-0
#   t3: approvals still to come from T-3→T-0
#   t1: approvals still to come from T-1→T-0
#
# Full-history pooled rates:
#   T-7→T-0 mix  = 30.47%
#   T-3→T-0 mix  = 33.56%
#   T-1→T-0 only = 37.92%
#
# Keep these separate from Stage 0 forecasting quality. Stage 0 predicts how
# many additional approved applicants arrive; Stage 3 converts that expected
# approved volume into expected attendance.
FUTURE_APPROVED_SHOW_RATES: dict[str, float] = {
    "t7": 0.3047,
    "t3": 0.3356,
    "t1": 0.3792,
}

# Feature encoding: one-hot categories and their column indices.
# prior_event_count and signup_percentile are available for future use
# but not in the active model (they're redundant with existing features).
FEATURE_SPEC = {
    "timing_bucket": ["21+", "14-21", "7-14", "3-7", "1-3", "<1"],
    "tz_distance": ["local", "near", "medium", "far"],
    "prior_rate_bucket": ["no_history", "0-25%", "25-50%", "50-75%", "75-100%"],
    "prior_event_count": ["none", "1", "2-3", "4+"],
    "signup_percentile": ["first_10", "10-25", "25-50", "50-75", "75-90", "last_10"],
}


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    ex = math.exp(x)
    return ex / (1.0 + ex)


# The active feature groups used by the hardcoded model.
# Categorical features are one-hot encoded via FEATURE_SPEC; "log_page_views"
# and "notification_read" are appended as continuous/binary scalars.
_ACTIVE_FEATURES = [
    "timing_bucket", "tz_distance", "prior_rate_bucket",
    "log_page_views", "notification_read",
    "log_views_x_remote",  # interaction: page views effect amplified for remote attendees
    "log_horizon_days",  # prediction horizon: compensates for incomplete engagement data
    "notif_read_x_horizon",  # interaction: notification effect varies by horizon (early read = strong signal)
    "page_views_x_horizon",  # CRITICAL: 5 views at T-14 ≠ 5 views at T-0 (engagement decay)
    "notif_x_pageviews",  # interaction: notification effectiveness varies by engagement level
]


def _encode_features(
    timing: str,
    tz_dist: str = "local",
    prior_rate: str = "no_history",
    prior_count: str = "none",
    signup_pct: str = "25-50",
    *,
    page_views: int = 0,
    notification_read: bool = False,
    horizon_days: float = 0.0,
    feature_groups: list[str] | None = None,
) -> list[float]:
    """Encode features into a weight vector for logistic regression.

    Categorical features are one-hot encoded (dropping the reference category).
    Continuous/binary features are appended as scalar values.
    """
    vec: list[float] = []
    vals = {
        "timing_bucket": timing,
        "tz_distance": tz_dist,
        "prior_rate_bucket": prior_rate,
        "prior_event_count": prior_count,
        "signup_percentile": signup_pct,
    }

    groups = feature_groups or _ACTIVE_FEATURES
    for feature_name in groups:
        if feature_name == "log_page_views":
            vec.append(math.log(page_views + 1))
        elif feature_name == "notification_read":
            vec.append(1.0 if notification_read else 0.0)
        elif feature_name == "log_views_x_remote":
            is_remote = 1.0 if tz_dist in ("medium", "far") else 0.0
            vec.append(math.log(page_views + 1) * is_remote)
        elif feature_name == "log_horizon_days":
            vec.append(math.log(horizon_days + 1))
        elif feature_name == "notif_read_x_horizon":
            notif_val = 1.0 if notification_read else 0.0
            vec.append(notif_val * math.log(horizon_days + 1))
        elif feature_name == "page_views_x_horizon":
            # CRITICAL interaction: separates early high engagement from late low engagement
            vec.append(math.log(page_views + 1) * math.log(horizon_days + 1))
        elif feature_name == "notif_x_pageviews":
            # Notification effectiveness varies by engagement level
            notif_val = 1.0 if notification_read else 0.0
            vec.append(notif_val * math.log(page_views + 1))
        elif feature_name in FEATURE_SPEC:
            categories = FEATURE_SPEC[feature_name]
            val = vals.get(feature_name, categories[0])
            if val not in categories:
                val = categories[0]
            for cat in categories[1:]:
                vec.append(1.0 if val == cat else 0.0)

    return vec


def forecast_engagement(
    current_page_views: int,
    current_notif_read: bool,
    horizon_days: float,
    tz_offset_hrs: float,
    tz_distance: str,
    prior_events: int,
) -> tuple[float, float]:
    """Forecast final engagement (at T-0) from current incomplete engagement (at T-h).

    Uses trained Ridge and LogisticRegression models to predict what engagement
    will be at event time (T-0) given current partial engagement at T-h.

    Args:
        current_page_views: Current page view count at T-h
        current_notif_read: Whether notification is read at T-h
        horizon_days: Days until event (T-h)
        tz_offset_hrs: Timezone offset from event location (hours)
        tz_distance: Timezone distance bucket ("local", "near", "medium", "far")
        prior_events: Number of prior approved events

    Returns:
        (forecasted_page_views, forecasted_notif_read_probability)
    """
    if FORECASTING_MODELS is None:
        # No forecasting model available, return current values
        return float(current_page_views), 1.0 if current_notif_read else 0.0

    # Encode features for forecasting
    features = [
        float(current_page_views),                                    # 0
        math.log(current_page_views + 1),                             # 1
        1.0 if current_notif_read else 0.0,                           # 2
        float(horizon_days),                                          # 3
        math.log(horizon_days + 1),                                   # 4
        float(tz_offset_hrs),                                         # 5
        1.0 if tz_distance == "local" else 0.0,                       # 6
        1.0 if tz_distance == "near" else 0.0,                        # 7
        1.0 if tz_distance == "medium" else 0.0,                      # 8
        float(prior_events),                                          # 9
        math.log(prior_events + 1),                                   # 10
        math.log(current_page_views + 1) * math.log(horizon_days + 1), # 11
    ]

    # Forecast page_views (Ridge regression)
    pv_model = FORECASTING_MODELS["page_views"]
    pv_pred = pv_model["intercept"]
    for w, f in zip(pv_model["weights"], features):
        pv_pred += w * f
    forecasted_views = max(0.0, pv_pred)  # can't be negative

    # Forecast notif_read probability (Logistic regression)
    nr_model = FORECASTING_MODELS["notif_read"]
    nr_logit = nr_model["intercept"]
    for w, f in zip(nr_model["weights"], features):
        nr_logit += w * f
    forecasted_notif_prob = _sigmoid(nr_logit)

    return forecasted_views, forecasted_notif_prob


def forecast_signup_count(
    current_approved: int,
    horizon_days: float,
    approved_t14: int = 0,
    approved_t21: int = 0,
) -> float:
    """Forecast final approved count (at T-0) from current count (at T-h).

    Uses trained Ridge regression to predict how many MORE people will sign up
    and be approved from T-h to T-0 based on approval velocity and acceleration
    patterns.

    Args:
        current_approved: Current approved count at T-h
        horizon_days: Days until event (T-h)
        approved_t14: Approved count at T-14 (for velocity calculation)
        approved_t21: Approved count at T-21 (for acceleration calculation)

    Returns:
        forecasted_total_approved: Predicted final approved count at T-0
    """
    if SIGNUP_FORECASTING_MODELS is None:
        # No forecasting model available, return current count
        return float(current_approved)

    # Determine which horizon model to use
    if horizon_days >= 6:
        model_key = "t7"
        # Calculate velocity and acceleration for T-7
        velocity_7d = (current_approved - approved_t14) / 7 if approved_t14 > 0 else 0
        velocity_14d = (approved_t14 - approved_t21) / 7 if approved_t21 > 0 else 0
        acceleration = velocity_7d - velocity_14d
    elif horizon_days >= 2:
        model_key = "t3"
        # Calculate velocity and acceleration for T-3
        t7_approx = current_approved - (current_approved - approved_t14) * (3.0 / 7.0) if approved_t14 > 0 else current_approved
        velocity_3d = (current_approved - t7_approx) / 4 if t7_approx > 0 else 0
        velocity_7d = (t7_approx - approved_t14) / 7 if approved_t14 > 0 else 0
        acceleration = velocity_3d - velocity_7d
    else:
        model_key = "t1"
        # Calculate velocity and acceleration for T-1
        t3_approx = current_approved * 0.8  # rough estimate
        velocity_1d = (current_approved - t3_approx) / 2 if t3_approx > 0 else 0
        velocity_3d = 0  # not enough history
        acceleration = velocity_1d - velocity_3d

    model = SIGNUP_FORECASTING_MODELS[model_key]

    # Build feature vector (11 features)
    velocity = velocity_7d if horizon_days >= 6 else (velocity_3d if horizon_days >= 2 else velocity_1d)
    features = [
        float(current_approved),                                    # 0
        math.log(current_approved + 1),                             # 1
        float(velocity),                                            # 2
        math.log(abs(velocity) + 1) * (1 if velocity >= 0 else -1), # 3
        float(acceleration),                                        # 4
        math.log(abs(acceleration) + 1) * (1 if acceleration >= 0 else -1), # 5
        float(horizon_days),                                        # 6
        math.log(horizon_days + 1),                                 # 7
        float(current_approved) * float(horizon_days),              # 8
        math.log(current_approved + 1) * math.log(horizon_days + 1), # 9
        float(velocity) * float(horizon_days),                      # 10
    ]

    # Predict (Ridge regression)
    pred = model["intercept"]
    for w, f in zip(model["weights"], features):
        pred += w * f

    forecasted_total = max(float(current_approved), pred)  # can't be less than current
    return forecasted_total


def future_approved_show_rate(horizon_days: float) -> float:
    """Return pooled historical show rate for approvals still expected after T-h."""
    if horizon_days >= 6:
        return FUTURE_APPROVED_SHOW_RATES["t7"]
    if horizon_days >= 2:
        return FUTURE_APPROVED_SHOW_RATES["t3"]
    return FUTURE_APPROVED_SHOW_RATES["t1"]


def expected_future_attendance(additional_approved: float, horizon_days: float) -> float:
    """Convert expected future approved volume into expected future attendance."""
    if additional_approved <= 0:
        return 0.0
    return additional_approved * future_approved_show_rate(horizon_days)


def predict_show_probability(
    days_before_event: float,
    prior_attendance_rate: float | None = None,
    tz_distance: str = "local",
    prior_events: int = 0,
    signup_percentile: float = 0.5,
    *,
    page_views: int = 0,
    notification_read: bool = False,
    horizon_days: float = 0.0,
) -> float:
    """Predict P(show) for one person using logistic regression.

    Args:
        days_before_event: days between application and event
        prior_attendance_rate: personal historical show rate (None = first timer)
        tz_distance: timezone distance bucket ("local", "near", "medium", "far")
        prior_events: number of prior events this person was approved for
        signup_percentile: where they fall in this event's signup order (0-1)
        page_views: number of event page views by this user (0 = none)
        notification_read: whether user read the approval notification
        horizon_days: days until event at prediction time (0 = event day)
    """
    timing = timing_bucket(days_before_event)
    prior = prior_rate_bucket(prior_attendance_rate)
    prior_count = prior_event_count_bucket(prior_events)
    signup_pct = signup_percentile_bucket(signup_percentile)

    if MODEL_COEFFICIENTS is not None:
        return _predict_with_model(
            timing, tz_distance, prior, prior_count, signup_pct,
            page_views=page_views, notification_read=notification_read,
            horizon_days=horizon_days,
        )

    # fallback: empirical lookup (no trained model)
    from .predict import (
        SHOW_RATES_BY_TIMING,
        PRIOR_RATE_MULTIPLIERS,
        TZ_DISTANCE_MULTIPLIERS,
        OVERALL_SHOW_RATE,
    )
    base = SHOW_RATES_BY_TIMING.get(timing, {}).get("show_rate", OVERALL_SHOW_RATE)
    p = base * PRIOR_RATE_MULTIPLIERS.get(prior, 1.0) * TZ_DISTANCE_MULTIPLIERS.get(tz_distance, 1.0)
    return max(0.01, min(0.99, p))


def _predict_with_model(
    timing: str,
    tz_dist: str,
    prior_rate: str,
    prior_count: str = "none",
    signup_pct: str = "25-50",
    *,
    page_views: int = 0,
    notification_read: bool = False,
    horizon_days: float = 0.0,
) -> float:
    """Predict using trained logistic regression coefficients."""
    assert MODEL_COEFFICIENTS is not None
    features = _encode_features(
        timing, tz_dist, prior_rate, prior_count, signup_pct,
        page_views=page_views, notification_read=notification_read,
        horizon_days=horizon_days,
    )
    intercept = MODEL_COEFFICIENTS["intercept"]
    weights = MODEL_COEFFICIENTS["weights"]

    if len(weights) != len(features):
        raise ValueError(
            f"Weight/feature length mismatch: {len(weights)} weights vs {len(features)} features"
        )
    logit = intercept + sum(w * f for w, f in zip(weights, features))
    return _sigmoid(logit)


def predict_batch(
    applicants: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Predict P(show) for a list of applicants.

    Each applicant dict should have:
    - days_before_event: float (required)
    - prior_attendance_rate: float or None (optional)
    - tz_distance: str ("local"/"near"/"medium"/"far") (optional, default "local")
    - page_views: int (optional, default 0)
    - notification_read: bool (optional, default False)
    - email: str (optional, for identification)

    Returns list of dicts with {email, p_show, timing_bucket, prior_bucket, tz_distance}.
    """
    # compute signup percentiles from days_before_event ordering
    # (higher days = earlier signup = lower percentile)
    # NOTE: signup_percentile is informational-only (not in _ACTIVE_FEATURES),
    # included in output for display but does not affect p_show predictions.
    days_list = []
    for app in applicants:
        d = app.get("days_before_event")
        days_list.append(d if d is not None else 7)
    n = len(days_list)
    if n > 1:
        # rank by days descending (earliest signup = rank 0, latest = rank n-1)
        sorted_indices = sorted(range(n), key=lambda i: -days_list[i])
        ranks = [0] * n
        for rank, idx in enumerate(sorted_indices):
            ranks[idx] = rank / (n - 1)  # 0.0 = earliest, 1.0 = latest
    else:
        ranks = [0.5] * n

    results = []
    for i, app in enumerate(applicants):
        days = days_list[i]
        if app.get("days_before_event") is None:
            import warnings
            warnings.warn("days_before_event missing, defaulting to 7", stacklevel=2)
        prior = app.get("prior_attendance_rate", None)
        tz_dist = app.get("tz_distance", "local")
        prior_events = app.get("prior_events", 0)
        email = app.get("email", "")
        pv = app.get("page_views", 0)
        nr = app.get("notification_read", False)
        horizon = app.get("horizon_days", 0.0)

        p = predict_show_probability(
            days_before_event=days,
            prior_attendance_rate=prior,
            tz_distance=tz_dist,
            prior_events=prior_events,
            signup_percentile=ranks[i],
            page_views=pv,
            notification_read=nr,
            horizon_days=horizon,
        )

        results.append({
            "email": email,
            "p_show": round(p, 4),
            "timing_bucket": timing_bucket(days),
            "prior_bucket": prior_rate_bucket(prior),
            "tz_distance": tz_dist,
            "prior_event_count": prior_event_count_bucket(prior_events),
            "signup_percentile": signup_percentile_bucket(ranks[i]),
        })

    return results


# ---------------------------------------------------------------------------
# Training (one-time, against platform DB)
# ---------------------------------------------------------------------------

def train_model(db_url: str) -> dict[str, Any]:
    """Train logistic regression on platform DB data.

    Queries approved applicants with check-in data + prior attendance + timezone,
    fits sklearn LogisticRegression, returns coefficients as JSON-serializable dict.

    Features: timing (5 one-hot) + tz_distance (3 one-hot) + prior_rate (4 one-hot)
    + log_page_views (continuous) + notification_read (binary)
    + log_views_x_remote (interaction) = 15 weights total.

    Requires: psycopg2, sklearn (optional deps).
    """
    try:
        import psycopg2
    except ImportError:
        raise RuntimeError("psycopg2 required for training. Install: pip install psycopg2-binary")

    try:
        from sklearn.linear_model import LogisticRegression
    except ImportError:
        raise RuntimeError("scikit-learn required for training. Install: pip install scikit-learn")

    import numpy as np
    from .predict import (
        city_to_timezone,
        compute_tz_offset_diff,
        tz_distance_bucket,
    )

    # CTE-based query avoids statement timeout from correlated subqueries.
    # Includes page views + notification read for 5-feature model.
    query = """
    WITH prior_agg AS (
        SELECT
            ea2."userId",
            pe2.id AS event_id,
            pe2."startDateTime" AS event_start,
            COUNT(*) FILTER (WHERE ea2.status = 'approved') AS prior_events,
            COALESCE(SUM(ea2."checkedIn"::int) FILTER (WHERE ea2.status = 'approved'), 0) AS prior_attended
        FROM "EventApplicant" ea2
        JOIN "PlatformEvent" pe2 ON ea2."eventId" = pe2.id
        WHERE pe2."startDateTime" IS NOT NULL
        GROUP BY ea2."userId", pe2.id, pe2."startDateTime"
    ),
    page_view_counts AS (
        SELECT i."userId", i.properties->>'eventId' AS event_id, COUNT(*) AS view_count
        FROM "Insight" i
        JOIN "PlatformEvent" pe_pv ON pe_pv.id::text = i.properties->>'eventId'
        WHERE i."eventName" = 'PAGE_VIEW'
        AND i.properties->>'eventId' IS NOT NULL
        AND i."createdAt" < pe_pv."startDateTime"
        GROUP BY i."userId", i.properties->>'eventId'
    ),
    notif_reads AS (
        SELECT DISTINCT ON (pn."userId", pn.data->>'eventId')
            pn."userId", pn.data->>'eventId' AS event_id, pn.read AS notif_read
        FROM "PlatformNotification" pn
        WHERE pn.type = 'event_application_status_change'
        AND pn.data->>'status' = 'approved'
        ORDER BY pn."userId", pn.data->>'eventId', pn."createdAt" DESC
    )
    SELECT
        ea."userId",
        ea."checkedIn"::int AS checked_in,
        ea."appliedFromTimeZone" AS applicant_tz,
        EXTRACT(EPOCH FROM (pe."startDateTime" - ea."createdAt")) / 86400.0 AS days_before,
        pe.id AS event_id,
        pe.title AS event_title,
        pe.city AS event_city,
        pe."startDateTime",
        (SELECT COALESCE(SUM(pa.prior_events), 0)
         FROM prior_agg pa
         WHERE pa."userId" = ea."userId"
           AND pa.event_start < pe."startDateTime"
           AND pa.event_id != pe.id) AS prior_events,
        (SELECT COALESCE(SUM(pa.prior_attended), 0)
         FROM prior_agg pa
         WHERE pa."userId" = ea."userId"
           AND pa.event_start < pe."startDateTime"
           AND pa.event_id != pe.id) AS prior_attended,
        COALESCE(pv.view_count, 0) AS page_views,
        COALESCE(nr.notif_read, false) AS notif_read
    FROM "EventApplicant" ea
    JOIN "PlatformEvent" pe ON ea."eventId" = pe.id
    LEFT JOIN page_view_counts pv ON pv."userId" = ea."userId" AND pv.event_id = pe.id::text
    LEFT JOIN notif_reads nr ON nr."userId" = ea."userId" AND nr.event_id = pe.id::text
    WHERE ea.status = 'approved'
      AND pe."startDateTime" IS NOT NULL
      AND ea."createdAt" IS NOT NULL
      AND EXTRACT(EPOCH FROM (pe."startDateTime" - ea."createdAt")) / 86400.0 > 0.25
      AND pe.id IN (
          SELECT "eventId" FROM "EventApplicant"
          WHERE "checkedIn" = true
          GROUP BY "eventId"
          HAVING COUNT(*) >= 10
      )
    ORDER BY pe."startDateTime", ea."createdAt"
    """

    conn = psycopg2.connect(db_url)
    try:
        cur = conn.cursor()
        cur.execute(query)
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    if len(rows) < 100:
        raise RuntimeError(f"Only {len(rows)} training rows found, need at least 100")

    print(f"Training on {len(rows)} approved applicants...")

    # build feature matrix
    y = []
    X_rows = []

    for (user_id, checked_in, applicant_tz, days_before,
         event_id, event_title, event_city, event_start,
         prior_events, prior_attended, page_views, notif_read) in rows:
        y.append(int(checked_in))

        # timing
        tb = timing_bucket(float(days_before))

        # timezone distance
        event_tz = city_to_timezone(event_city, event_title or "")
        offset = compute_tz_offset_diff(applicant_tz or "America/Los_Angeles", event_tz, event_date=event_start)
        tz_dist = tz_distance_bucket(offset)

        # prior attendance rate
        if prior_events and prior_events > 0:
            prior_rate = prior_attended / prior_events
            prior_bucket = prior_rate_bucket(prior_rate)
        else:
            prior_bucket = "no_history"

        # prior event count (serial RSVPer signal)
        prior_count = prior_event_count_bucket(int(prior_events or 0))

        # signup percentile — removed from active features but kept for compatibility
        signup_pct_bucket = "25-50"

        features = _encode_features(
            tb, tz_dist, prior_bucket, prior_count, signup_pct_bucket,
            page_views=int(page_views or 0),
            notification_read=bool(notif_read),
            horizon_days=0.0,
        )
        X_rows.append(features)

    X = np.array(X_rows, dtype=np.float64)
    y_arr = np.array(y, dtype=np.float64)

    print(f"  Features: {X.shape[1]} columns (12 one-hot + 3 continuous/binary/interaction)")
    print(f"  Positive rate: {y_arr.mean():.1%}")

    model = LogisticRegression(
        C=1.0,
        max_iter=1000,
        solver="lbfgs",
    )
    model.fit(X, y_arr)

    # build feature names (only for active features used in training)
    feature_names = []
    for feat_name in _ACTIVE_FEATURES:
        if feat_name in FEATURE_SPEC:
            categories = FEATURE_SPEC[feat_name]
            for cat in categories[1:]:
                feature_names.append(f"{feat_name}_{cat}")
        else:
            # continuous/binary features: single weight, name = feature name
            feature_names.append(feat_name)

    coefficients = {
        "intercept": float(model.intercept_[0]),
        "weights": [float(w) for w in model.coef_[0]],
        "feature_names": feature_names,
        "n_samples": len(rows),
        "positive_rate": float(y_arr.mean()),
    }

    # print coefficients
    print(f"\n  Intercept: {coefficients['intercept']:.4f}")
    print(f"  Coefficients:")
    for name, w in zip(feature_names, coefficients["weights"]):
        print(f"    {name:<30} {w:+.4f}")

    # calibration check per feature group
    preds = model.predict_proba(X)[:, 1]

    print(f"\n  Calibration by timing:")
    for bucket_name in FEATURE_SPEC["timing_bucket"]:
        mask = np.array([
            timing_bucket(float(r[3])) == bucket_name for r in rows
        ])
        if mask.sum() > 0:
            actual = y_arr[mask].mean()
            predicted = preds[mask].mean()
            print(f"    {bucket_name:<10}: actual={actual:.1%}, predicted={predicted:.1%}, n={mask.sum()}")

    print(f"\n  Calibration by tz_distance:")
    for bucket_name in FEATURE_SPEC["tz_distance"]:
        mask = np.array([
            tz_distance_bucket(compute_tz_offset_diff(
                r[2] or "America/Los_Angeles",
                city_to_timezone(r[6], r[5] or ""),
                event_date=r[7],
            )) == bucket_name for r in rows
        ])
        if mask.sum() > 0:
            actual = y_arr[mask].mean()
            predicted = preds[mask].mean()
            print(f"    {bucket_name:<10}: actual={actual:.1%}, predicted={predicted:.1%}, n={mask.sum()}")

    print(f"\n  Calibration by prior_rate:")
    for bucket_name in FEATURE_SPEC["prior_rate_bucket"]:
        mask = np.array([
            prior_rate_bucket(
                r[9] / r[8] if r[8] and r[8] > 0 else None
            ) == bucket_name for r in rows
        ])
        if mask.sum() > 0:
            actual = y_arr[mask].mean()
            predicted = preds[mask].mean()
            print(f"    {bucket_name:<10}: actual={actual:.1%}, predicted={predicted:.1%}, n={mask.sum()}")

    # page views calibration
    from .predict import page_views_bucket
    print(f"\n  Calibration by page_views:")
    for bucket_name in ["0", "1", "2-3", "4-5", "6-10", "11+"]:
        mask = np.array([
            page_views_bucket(int(r[10] or 0)) == bucket_name for r in rows
        ])
        if mask.sum() > 0:
            actual = y_arr[mask].mean()
            predicted = preds[mask].mean()
            print(f"    {bucket_name:<10}: actual={actual:.1%}, predicted={predicted:.1%}, n={mask.sum()}")

    return coefficients
