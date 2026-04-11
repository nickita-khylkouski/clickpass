"""Retrain LogReg with horizon-aware multi-snapshot training data.

For each historical (applicant, event) pair, generates training rows at
multiple prediction horizons (T-0, T-1, T-3, T-7, T-14 days before event).
At each snapshot, page_views and notification_read are computed using only
data available at that point in time.

This teaches the model that incomplete engagement at T-7 is normal and
adjusts predictions upward accordingly.

Outputs hardcoded coefficients for model.py (16 weights, was 15).
"""
import math
import os
import sys
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cv_rank.waves.predict import (
    timing_bucket, prior_rate_bucket,
    city_to_timezone, compute_tz_offset_diff, tz_distance_bucket,
)
from cv_rank.waves.model import _encode_features, _ACTIVE_FEATURES, FEATURE_SPEC

SNAPSHOT_HORIZONS = [0, 1, 3, 7, 14]  # days before event


def fetch_data(conn):
    """Fetch base rows, page view timestamps, and notification data."""
    cur = conn.cursor()

    # Query 1: Base applicant data (returns raw applied_at timestamp)
    print("  Fetching base applicant data...")
    cur.execute("""
    WITH prior_agg AS (
        SELECT ea2."userId", pe2.id AS event_id, pe2."startDateTime" AS event_start,
            COUNT(*) FILTER (WHERE ea2.status = 'approved') AS prior_events,
            COALESCE(SUM(ea2."checkedIn"::int) FILTER (WHERE ea2.status = 'approved'), 0) AS prior_attended
        FROM "EventApplicant" ea2
        JOIN "PlatformEvent" pe2 ON ea2."eventId" = pe2.id
        WHERE pe2."startDateTime" IS NOT NULL
        GROUP BY ea2."userId", pe2.id, pe2."startDateTime"
    )
    SELECT
        ea."userId",
        ea."checkedIn"::int AS checked_in,
        ea."appliedFromTimeZone" AS applicant_tz,
        ea."createdAt" AS applied_at,
        pe.id AS event_id,
        pe.title AS event_title,
        pe.city AS event_city,
        pe."startDateTime",
        (SELECT COALESCE(SUM(pa.prior_events), 0) FROM prior_agg pa
         WHERE pa."userId" = ea."userId" AND pa.event_start < pe."startDateTime" AND pa.event_id != pe.id) AS prior_events,
        (SELECT COALESCE(SUM(pa.prior_attended), 0) FROM prior_agg pa
         WHERE pa."userId" = ea."userId" AND pa.event_start < pe."startDateTime" AND pa.event_id != pe.id) AS prior_attended
    FROM "EventApplicant" ea
    JOIN "PlatformEvent" pe ON ea."eventId" = pe.id
    WHERE ea.status = 'approved'
      AND pe."startDateTime" IS NOT NULL
      AND ea."createdAt" IS NOT NULL
      AND EXTRACT(EPOCH FROM (pe."startDateTime" - ea."createdAt")) / 86400.0 > 0.25
      AND pe.id IN (
          SELECT "eventId" FROM "EventApplicant" WHERE "checkedIn" = true
          GROUP BY "eventId" HAVING COUNT(*) >= 10
      )
    ORDER BY pe."startDateTime", ea."createdAt"
    """)
    base_rows = cur.fetchall()
    print(f"    {len(base_rows)} base rows")

    # Query 2: All page view timestamps (pre-event only)
    print("  Fetching page view timestamps...")
    cur.execute("""
    SELECT i."userId", i.properties->>'eventId' AS event_id, i."createdAt" AS view_time
    FROM "Insight" i
    JOIN "PlatformEvent" pe ON pe.id::text = i.properties->>'eventId'
    WHERE i."eventName" = 'PAGE_VIEW'
      AND i.properties->>'eventId' IS NOT NULL
      AND i."createdAt" < pe."startDateTime"
    ORDER BY i."userId", i.properties->>'eventId', i."createdAt"
    """)
    view_rows = cur.fetchall()
    print(f"    {len(view_rows)} page view records")

    # Query 3: Notification timestamps
    print("  Fetching notification data...")
    cur.execute("""
    SELECT DISTINCT ON (pn."userId", pn.data->>'eventId')
        pn."userId", pn.data->>'eventId' AS event_id,
        pn.read AS notif_read, pn."createdAt" AS notif_created, pn."updatedAt" AS notif_updated
    FROM "PlatformNotification" pn
    WHERE pn.type = 'event_application_status_change'
      AND pn.data->>'status' = 'approved'
    ORDER BY pn."userId", pn.data->>'eventId', pn."createdAt" DESC
    """)
    notif_rows = cur.fetchall()
    print(f"    {len(notif_rows)} notification records")

    cur.close()
    return base_rows, view_rows, notif_rows


def make_tz_aware(dt):
    """Ensure datetime is timezone-aware (UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def build_snapshots(base_rows, view_rows, notif_rows):
    """Generate multi-snapshot training rows."""
    # Index view timestamps: (userId, eventId) -> sorted list of datetimes
    views_index = defaultdict(list)
    for user_id, event_id, view_time in view_rows:
        views_index[(str(user_id), str(event_id))].append(make_tz_aware(view_time))

    # Sort each list for bisect
    for key in views_index:
        views_index[key].sort()

    # Index notification data: (userId, eventId) -> (read, created, updated)
    notif_index = {}
    for user_id, event_id, read, created, updated in notif_rows:
        notif_index[(str(user_id), str(event_id))] = (
            bool(read), make_tz_aware(created), make_tz_aware(updated)
        )

    training_rows = []
    skipped = defaultdict(int)

    for row in base_rows:
        user_id, checked_in, applicant_tz, applied_at, event_id, \
            event_title, event_city, event_start, prior_events, prior_attended = row

        applied_at = make_tz_aware(applied_at)
        event_start = make_tz_aware(event_start)
        key = (str(user_id), str(event_id))

        for horizon in SNAPSHOT_HORIZONS:
            snapshot_time = event_start - timedelta(days=horizon)

            # applicant must have applied before snapshot
            if applied_at >= snapshot_time:
                skipped[horizon] += 1
                continue

            # windowed page views: count views before snapshot_time
            all_views = views_index.get(key, [])
            page_views = bisect_left(all_views, snapshot_time)

            # Time-windowed notification read (no leakage!)
            notif = notif_index.get(key)
            if notif is not None:
                read, notif_created, notif_updated = notif
                # Only count as read if they read it BEFORE this snapshot time
                notification_read = (
                    read
                    and notif_created is not None and notif_created < snapshot_time
                    and notif_updated is not None and notif_updated <= snapshot_time
                )
            else:
                notification_read = False

            days_before = (event_start - applied_at).total_seconds() / 86400.0

            training_rows.append((
                user_id, checked_in, applicant_tz, days_before,
                event_id, event_title, event_city, event_start,
                prior_events, prior_attended,
                page_views, notification_read,
                float(horizon),
            ))

    print(f"\n  Snapshot generation:")
    print(f"    Total training rows: {len(training_rows)}")
    for h in SNAPSHOT_HORIZONS:
        n = sum(1 for r in training_rows if r[12] == h)
        s = skipped.get(h, 0)
        print(f"    T-{h:>2}: {n:>6} rows ({s} skipped — applied after snapshot)")

    return training_rows


def build_forecasting_pairs(training_rows):
    """Build paired data for engagement forecasting.

    For each person with observations at both T-h and T-0, create a training row
    showing how engagement grew from T-h to T-0.

    Returns list of (current_features, final_page_views, final_notif_read) tuples.
    """
    # Group by (user_id, event_id)
    from collections import defaultdict
    by_person_event = defaultdict(list)

    for row in training_rows:
        user_id, _, _, _, event_id = row[0], row[1], row[2], row[3], row[4]
        horizon = row[12]
        by_person_event[(user_id, event_id)].append(row)

    forecasting_pairs = []

    for (user_id, event_id), rows in by_person_event.items():
        # Find T-0 row (final engagement)
        t0_rows = [r for r in rows if r[12] == 0]
        if not t0_rows:
            continue
        t0_row = t0_rows[0]
        final_page_views = int(t0_row[10] or 0)
        final_notif_read = bool(t0_row[11])

        # For each other horizon, create a forecasting pair
        for row in rows:
            horizon = row[12]
            if horizon == 0:
                continue  # skip T-0 itself

            # Current engagement at this horizon
            current_page_views = int(row[10] or 0)
            current_notif_read = bool(row[11])
            applicant_tz = row[2]
            days_before = row[3]
            event_title = row[5]
            event_city = row[6]
            start_dt = row[7]
            prior_events = row[8]
            prior_attended = row[9]

            # Compute basic features for forecasting
            tb = timing_bucket(float(days_before))
            event_tz = city_to_timezone(event_city, event_title or "")
            offset = compute_tz_offset_diff(applicant_tz or "America/Los_Angeles", event_tz, event_date=start_dt)
            tz = tz_distance_bucket(offset)
            prior_rate = prior_attended / prior_events if prior_events and prior_events > 0 else None
            pr = prior_rate_bucket(prior_rate)

            # Features: current engagement + horizon + context
            features = [
                float(current_page_views),
                math.log(current_page_views + 1),
                1.0 if current_notif_read else 0.0,
                float(horizon),
                math.log(horizon + 1),
                offset,  # tz offset hours
                1.0 if tz == "local" else 0.0,
                1.0 if tz == "near" else 0.0,
                1.0 if tz == "medium" else 0.0,
                float(prior_events or 0),
                math.log((prior_events or 0) + 1),
                # Interaction: current views × horizon (critical!)
                math.log(current_page_views + 1) * math.log(horizon + 1),
            ]

            forecasting_pairs.append((features, final_page_views, final_notif_read))

    return forecasting_pairs


def train_forecasting_models(forecasting_pairs):
    """Train Stage 1: Engagement forecasting models.

    Returns (page_views_model, notif_read_model) coefficients.
    """
    from sklearn.linear_model import Ridge, LogisticRegression

    if not forecasting_pairs:
        print("  WARNING: No forecasting pairs found!")
        return None, None

    X = np.array([p[0] for p in forecasting_pairs])
    y_views = np.array([p[1] for p in forecasting_pairs])
    y_notif = np.array([int(p[2]) for p in forecasting_pairs])

    print(f"\n=== STAGE 1: ENGAGEMENT FORECASTING ===\n")
    print(f"  Training on {len(forecasting_pairs)} paired observations")
    print(f"  (same person at T-h and T-0, showing engagement growth)")

    # Model 1: Forecast final page_views
    print(f"\n  Training page_views forecasting model...")
    views_model = Ridge(alpha=1.0)
    views_model.fit(X, y_views)

    # Validate
    views_pred = views_model.predict(X)
    mae = np.abs(views_pred - y_views).mean()
    r2 = 1 - np.sum((y_views - views_pred)**2) / np.sum((y_views - y_views.mean())**2)
    print(f"    MAE: {mae:.2f} views")
    print(f"    R²: {r2:.3f}")

    # Model 2: Forecast final notif_read probability
    print(f"\n  Training notif_read forecasting model...")
    notif_model = LogisticRegression(penalty='l2', C=1.0, max_iter=1000)
    notif_model.fit(X, y_notif)

    # Validate
    from sklearn.metrics import roc_auc_score
    notif_pred = notif_model.predict_proba(X)[:, 1]
    auc = roc_auc_score(y_notif, notif_pred)
    print(f"    AUC: {auc:.3f}")
    print(f"    Currently read: {(X[:, 2] > 0.5).mean():.1%} → Final read: {y_notif.mean():.1%}")

    # Print growth statistics
    print(f"\n  Engagement growth patterns:")
    horizons = X[:, 3]
    current_views = X[:, 0]
    for h in [1, 3, 7, 14]:
        mask = horizons == h
        if mask.sum() == 0:
            continue
        current_avg = current_views[mask].mean()
        final_avg = y_views[mask].mean()
        growth = (final_avg / current_avg - 1) * 100 if current_avg > 0 else 0
        print(f"    T-{h:>2}: avg {current_avg:.1f} views → {final_avg:.1f} views ({growth:+.0f}% growth)")

    return views_model, notif_model


def encode_horizon(row):
    """Encode a training row including horizon_days (time-windowed, no leakage)."""
    (_, _, applicant_tz, days_before, _, event_title, event_city,
     start_dt, prior_events, prior_attended, page_views, notif_read,
     horizon_days) = row

    tb = timing_bucket(float(days_before))
    event_tz = city_to_timezone(event_city, event_title or "")
    offset = compute_tz_offset_diff(applicant_tz or "America/Los_Angeles", event_tz, event_date=start_dt)
    tz = tz_distance_bucket(offset)
    prior_rate = prior_attended / prior_events if prior_events and prior_events > 0 else None
    pr = prior_rate_bucket(prior_rate)

    return _encode_features(
        tb, tz, pr,
        page_views=int(page_views or 0),
        notification_read=bool(notif_read),  # Time-windowed: read BY snapshot time
        horizon_days=float(horizon_days),
    )


def sanity_check(conn):
    """Print page view accumulation curve and notification read timing."""
    cur = conn.cursor()

    print("\n=== SANITY CHECK: Page View Accumulation ===\n")
    cur.execute("""
    WITH view_timing AS (
        SELECT
            EXTRACT(EPOCH FROM (pe."startDateTime" - i."createdAt")) / 86400.0 AS days_before_view
        FROM "Insight" i
        JOIN "PlatformEvent" pe ON pe.id::text = i.properties->>'eventId'
        WHERE i."eventName" = 'PAGE_VIEW'
          AND i.properties->>'eventId' IS NOT NULL
          AND i."createdAt" < pe."startDateTime"
    )
    SELECT
        CASE
            WHEN days_before_view < 1 THEN '0: <1d'
            WHEN days_before_view < 3 THEN '1: 1-3d'
            WHEN days_before_view < 7 THEN '2: 3-7d'
            WHEN days_before_view < 14 THEN '3: 7-14d'
            ELSE '4: 14d+'
        END AS window,
        COUNT(*) AS n_views,
        ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER(), 1) AS pct
    FROM view_timing
    GROUP BY 1
    ORDER BY 1
    """)
    rows = cur.fetchall()
    for window, n, pct in rows:
        bar = "#" * int(pct / 2)
        print(f"  {window[3:]:<8} {n:>8} views ({pct:>5}%)  {bar}")

    last_7 = sum(n for w, n, _ in rows if w < "3:")
    total = sum(n for _, n, _ in rows)
    print(f"\n  Views in last 7 days: {last_7/total:.0%} of total")

    print("\n=== SANITY CHECK: Notification Read Timing ===\n")
    cur.execute("""
    SELECT
        CASE
            WHEN NOT pn.read THEN '5: never_read'
            WHEN EXTRACT(EPOCH FROM (pn."updatedAt" - pn."createdAt")) / 3600 < 1 THEN '0: <1hr'
            WHEN EXTRACT(EPOCH FROM (pn."updatedAt" - pn."createdAt")) / 3600 < 24 THEN '1: 1hr-1d'
            WHEN EXTRACT(EPOCH FROM (pn."updatedAt" - pn."createdAt")) / 3600 < 72 THEN '2: 1-3d'
            WHEN EXTRACT(EPOCH FROM (pn."updatedAt" - pn."createdAt")) / 3600 < 168 THEN '3: 3-7d'
            ELSE '4: 7d+'
        END AS read_speed,
        COUNT(*) AS n,
        ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER(), 1) AS pct
    FROM "PlatformNotification" pn
    WHERE pn.type = 'event_application_status_change'
      AND pn.data->>'status' = 'approved'
    GROUP BY 1
    ORDER BY 1
    """)
    rows = cur.fetchall()
    for speed, n, pct in rows:
        bar = "#" * int(pct / 2)
        print(f"  {speed[3:]:<12} {n:>6} ({pct:>5}%)  {bar}")

    cur.close()


def main():
    import psycopg2
    from sklearn.linear_model import LogisticRegression

    do_sanity = "--sanity-check" in sys.argv

    conn = psycopg2.connect(os.environ["PLATFORM_DATABASE_URL"])
    try:
        if do_sanity:
            sanity_check(conn)
            if "--sanity-check" in sys.argv and len(sys.argv) == 2:
                return  # only sanity check requested

        print("\n=== FETCHING DATA ===\n")
        base_rows, view_rows, notif_rows = fetch_data(conn)
    finally:
        conn.close()

    print("\n=== BUILDING SNAPSHOTS ===")
    training_rows = build_snapshots(base_rows, view_rows, notif_rows)

    # NEW: Train engagement forecasting models (Stage 1)
    forecasting_pairs = build_forecasting_pairs(training_rows)
    views_model, notif_model = train_forecasting_models(forecasting_pairs)

    print("\n=== ENCODING FEATURES ===\n")
    X = np.array([encode_horizon(r) for r in training_rows])
    y = np.array([int(r[1]) for r in training_rows])

    print(f"  Shape: {X.shape}")
    print(f"  Positive rate: {y.mean():.1%}")

    # Show feature stats by horizon
    horizons_arr = np.array([r[12] for r in training_rows])
    page_views_arr = np.array([int(r[10] or 0) for r in training_rows])
    notif_arr = np.array([bool(r[11]) for r in training_rows])  # time-windowed notification_read

    print(f"\n  Feature stats by horizon:")
    print(f"  {'Horizon':>8} | {'N':>6} | {'ShowRate':>8} | {'AvgViews':>8} | {'NotifRead':>9}")
    print(f"  {'─'*8}─┼─{'─'*6}─┼─{'─'*8}─┼─{'─'*8}─┼─{'─'*9}")
    print(f"  Note: 'NotifRead' is time-windowed (read BY snapshot time) - no leakage")
    for h in SNAPSHOT_HORIZONS:
        mask = horizons_arr == h
        if mask.sum() == 0:
            continue
        n = mask.sum()
        sr = y[mask].mean()
        avg_v = page_views_arr[mask].mean()
        nr = notif_arr[mask].mean()
        print(f"  T-{h:>5} | {n:>6} | {sr:>7.1%} | {avg_v:>8.1f} | {nr:>8.1%}")

    print(f"\n=== TRAINING ===\n")

    # Extract person_ids for GroupKFold (same person's snapshots stay together)
    person_ids = np.array([r[0] for r in training_rows])  # user_id is first element

    # Use L2 regularization C=1.0 to stabilize coefficients with correlated rows
    # (C=1.0 is moderate strength - balances stability vs fitting)
    model = LogisticRegression(penalty='l2', C=1.0, max_iter=1000, solver='lbfgs')

    # Validate with GroupKFold CV first (prevents leakage from same person in train/val)
    from sklearn.model_selection import GroupKFold, cross_val_score

    print(f"  Running GroupKFold CV (5-fold, grouped by person)...")
    group_kfold = GroupKFold(n_splits=5)
    cv_scores = cross_val_score(
        model, X, y,
        cv=group_kfold,
        groups=person_ids,
        scoring='roc_auc',
        n_jobs=-1
    )
    print(f"  GroupKFold CV AUC: {cv_scores.mean():.4f} ± {cv_scores.std():.4f}")
    print(f"  (Each fold: {', '.join(f'{s:.3f}' for s in cv_scores)})")

    # Train final model on all data
    print(f"\n  Training final model on all data...")
    model.fit(X, y)

    # Build feature names
    feature_names = []
    for feat_name in _ACTIVE_FEATURES:
        if feat_name in FEATURE_SPEC:
            for cat in FEATURE_SPEC[feat_name][1:]:
                feature_names.append(f"{feat_name}_{cat}")
        else:
            feature_names.append(feat_name)

    print(f"  Intercept: {model.intercept_[0]:.4f}")
    print(f"\n  Weights:")
    for name, w in zip(feature_names, model.coef_[0]):
        print(f"    {name:<30} {w:+.4f}")

    # Print Python-ready format
    print(f"\n# --- Copy-paste into model.py ---")
    print(f'MODEL_COEFFICIENTS = {{')
    print(f'    "intercept": {model.intercept_[0]:.4f},')
    print(f'    "weights": [')
    for name, w in zip(feature_names, model.coef_[0]):
        print(f'        {w:+.4f},   # {name}')
    print(f'    ],')
    print(f'    "feature_names": {feature_names},')
    print(f'    "n_samples": {len(training_rows)},')
    print(f'    "positive_rate": {y.mean():.3f},')
    print(f'}}')

    # Calibration by horizon
    preds = model.predict_proba(X)[:, 1]
    print(f"\n=== CALIBRATION BY HORIZON ===\n")
    print(f"  {'Horizon':>8} | {'Predicted':>9} | {'Actual':>8} | {'N':>6} | {'Gap':>6}")
    print(f"  {'─'*8}─┼─{'─'*9}─┼─{'─'*8}─┼─{'─'*6}─┼─{'─'*6}")
    for h in SNAPSHOT_HORIZONS:
        mask = horizons_arr == h
        if mask.sum() == 0:
            continue
        pred_mean = preds[mask].mean()
        actual_mean = y[mask].mean()
        gap = actual_mean - pred_mean
        print(f"  T-{h:>5} | {pred_mean:>8.1%} | {actual_mean:>7.1%} | {mask.sum():>6} | {gap:>+.1%}")

    # Calibration by decile (overall)
    print(f"\n=== CALIBRATION BY DECILE (overall) ===\n")
    order = np.argsort(preds)
    n = len(y)
    for i in range(10):
        lo, hi = i * n // 10, (i + 1) * n // 10
        idx = order[lo:hi]
        pred_mean = preds[idx].mean()
        actual_mean = y[idx].mean()
        print(f"  Decile {i}: predicted={pred_mean:.1%}, actual={actual_mean:.1%}")

    # Spot checks
    from cv_rank.waves.model import _sigmoid
    def predict(vec):
        return _sigmoid(model.intercept_[0] + sum(w * f for w, f in zip(model.coef_[0], vec)))

    print(f"\n=== SPOT CHECKS ===\n")
    # Best at T-0: local + late + high prior + 15 views + read + horizon=0
    best_t0 = encode_horizon((None, 0, "America/Los_Angeles", 0.5, None, "", "San Francisco", None, 5, 5, 15, True, 0))
    # Same person at T-7: fewer views, likely hasn't read yet
    best_t7 = encode_horizon((None, 0, "America/Los_Angeles", 0.5, None, "", "San Francisco", None, 5, 5, 3, False, 7))
    # Worst at T-0
    worst_t0 = encode_horizon((None, 0, "Asia/Kolkata", 25, None, "", "San Francisco", None, 0, 0, 0, False, 0))
    # Same at T-7
    worst_t7 = encode_horizon((None, 0, "Asia/Kolkata", 25, None, "", "San Francisco", None, 0, 0, 0, False, 7))
    # Average person at T-7 vs T-0
    avg_t0 = encode_horizon((None, 0, "America/Los_Angeles", 7, None, "", "San Francisco", None, 1, 1, 5, True, 0))
    avg_t7 = encode_horizon((None, 0, "America/Los_Angeles", 7, None, "", "San Francisco", None, 1, 1, 2, False, 7))

    print(f"  Best profile (local/late/high-prior):")
    print(f"    T-0  (15 views, read notif):  {predict(best_t0):.1%}")
    print(f"    T-7  (3 views, unread notif):  {predict(best_t7):.1%}")
    print(f"  Worst profile (far/early/no-history):")
    print(f"    T-0  (0 views, unread):        {predict(worst_t0):.1%}")
    print(f"    T-7  (0 views, unread):        {predict(worst_t7):.1%}")
    print(f"  Average profile (local/7d/1-prior):")
    print(f"    T-0  (5 views, read):          {predict(avg_t0):.1%}")
    print(f"    T-7  (2 views, unread):        {predict(avg_t7):.1%}")

    # Print forecasting model coefficients
    if views_model and notif_model:
        print(f"\n\n# --- FORECASTING MODEL COEFFICIENTS (copy to model.py) ---\n")
        print(f'FORECASTING_MODELS = {{')
        print(f'    "page_views": {{')
        print(f'        "intercept": {views_model.intercept_:.4f},')
        print(f'        "weights": [')
        for w in views_model.coef_:
            print(f'            {w:+.4f},')
        print(f'        ],')
        print(f'    }},')
        print(f'    "notif_read": {{')
        print(f'        "intercept": {notif_model.intercept_[0]:.4f},')
        print(f'        "weights": [')
        for w in notif_model.coef_[0]:
            print(f'            {w:+.4f},')
        print(f'        ],')
        print(f'    }},')
        print(f'    "feature_names": [')
        print(f'        "current_page_views", "log_current_views", "current_notif_read",')
        print(f'        "horizon", "log_horizon", "tz_offset_hrs",')
        print(f'        "tz_local", "tz_near", "tz_medium",')
        print(f'        "prior_events", "log_prior_events",')
        print(f'        "log_views_x_log_horizon",')
        print(f'    ],')
        print(f'}}')


if __name__ == "__main__":
    main()
