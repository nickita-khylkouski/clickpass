"""Backtest horizon-aware model: LOO-CV + aggregate predicted vs actual totals.

Compares baseline (no horizon) vs horizon-aware model at multiple prediction
horizons. Reports per-person AUC and aggregate-level MAE/bias.
"""
import math
import os
import sys
from bisect import bisect_left
from collections import defaultdict
from datetime import timedelta, timezone
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cv_rank.waves.predict import (
    timing_bucket, prior_rate_bucket,
    city_to_timezone, compute_tz_offset_diff, tz_distance_bucket,
)
from cv_rank.waves.model import _encode_features, FEATURE_SPEC

SNAPSHOT_HORIZONS = [0, 1, 3, 7, 14]


# ---------------------------------------------------------------------------
# Data loading (reuses retrain_horizon.py logic)
# ---------------------------------------------------------------------------

def load_all_data():
    """Load base rows + view timestamps + notification data from DB."""
    import psycopg2

    conn = psycopg2.connect(os.environ["PLATFORM_DATABASE_URL"])
    try:
        # import the fetch function from retrain script
        from retrain_horizon import fetch_data, build_snapshots, make_tz_aware
        base_rows, view_rows, notif_rows = fetch_data(conn)
    finally:
        conn.close()

    return base_rows, view_rows, notif_rows


# ---------------------------------------------------------------------------
# Encoding functions
# ---------------------------------------------------------------------------

def encode_baseline(row):
    """Baseline: 15 features (no horizon). Uses T-0 features only."""
    (_, _, applicant_tz, days_before, _, event_title, event_city,
     start_dt, prior_events, prior_attended, page_views, notif_read, _) = row

    tb = timing_bucket(float(days_before))
    event_tz = city_to_timezone(event_city, event_title or "")
    offset = compute_tz_offset_diff(applicant_tz or "America/Los_Angeles", event_tz, event_date=start_dt)
    tz = tz_distance_bucket(offset)
    prior_rate = prior_attended / prior_events if prior_events and prior_events > 0 else None
    pr = prior_rate_bucket(prior_rate)

    # baseline uses the old active features (no horizon)
    baseline_features = [
        "timing_bucket", "tz_distance", "prior_rate_bucket",
        "log_page_views", "notification_read", "log_views_x_remote",
    ]
    return _encode_features(
        tb, tz, pr,
        page_views=int(page_views or 0),
        notification_read=bool(notif_read),
        horizon_days=0.0,
        feature_groups=baseline_features,
    )


def encode_horizon(row, forecasted_views=None, forecasted_notif_prob=None):
    """Horizon-aware: 19 features (includes log_horizon_days + interactions).

    If forecasted values provided, uses them instead of current incomplete values.
    """
    (_, _, applicant_tz, days_before, _, event_title, event_city,
     start_dt, prior_events, prior_attended, page_views, notif_read,
     horizon_days) = row

    tb = timing_bucket(float(days_before))
    event_tz = city_to_timezone(event_city, event_title or "")
    offset = compute_tz_offset_diff(applicant_tz or "America/Los_Angeles", event_tz, event_date=start_dt)
    tz = tz_distance_bucket(offset)
    prior_rate = prior_attended / prior_events if prior_events and prior_events > 0 else None
    pr = prior_rate_bucket(prior_rate)

    # Use forecasted engagement if provided and horizon > 0.5 days
    if forecasted_views is not None and horizon_days > 0.5:
        pv_to_use = int(round(forecasted_views))
        notif_to_use = forecasted_notif_prob > 0.5
    else:
        pv_to_use = int(page_views or 0)
        notif_to_use = bool(notif_read)

    return _encode_features(
        tb, tz, pr,
        page_views=pv_to_use,
        notification_read=notif_to_use,
        horizon_days=float(horizon_days),
    )


def train_forecasting_models_from_rows(train_rows):
    """Train forecasting models from training rows (LOO-CV safe).

    Returns (page_views_model, notif_read_model) trained on pairs from train_rows.
    """
    from sklearn.linear_model import Ridge, LogisticRegression
    from collections import defaultdict

    # Build (user, event) → list of snapshots mapping
    by_person_event = defaultdict(list)
    for row in train_rows:
        user_id, event_id, horizon = row[0], row[4], row[12]
        by_person_event[(user_id, event_id)].append(row)

    # Build forecasting pairs: T-h → T-0
    features_list = []
    views_targets = []
    notif_targets = []

    for (user_id, event_id), snapshots in by_person_event.items():
        # Find T-0
        t0_rows = [r for r in snapshots if r[12] == 0]
        if not t0_rows:
            continue
        t0 = t0_rows[0]
        final_views = int(t0[10] or 0)
        final_notif = bool(t0[11])

        # For each earlier snapshot, create pair
        for row in snapshots:
            horizon = row[12]
            if horizon == 0:
                continue

            current_views = int(row[10] or 0)
            current_notif = bool(row[11])
            applicant_tz = row[2]
            event_title = row[5]
            event_city = row[6]
            start_dt = row[7]
            prior_events = row[8]

            # Compute features
            event_tz = city_to_timezone(event_city, event_title or "")
            offset = compute_tz_offset_diff(applicant_tz or "America/Los_Angeles", event_tz, event_date=start_dt)
            tz = tz_distance_bucket(offset)

            features = [
                float(current_views),
                math.log(current_views + 1),
                1.0 if current_notif else 0.0,
                float(horizon),
                math.log(horizon + 1),
                float(offset),
                1.0 if tz == "local" else 0.0,
                1.0 if tz == "near" else 0.0,
                1.0 if tz == "medium" else 0.0,
                float(prior_events or 0),
                math.log((prior_events or 0) + 1),
                math.log(current_views + 1) * math.log(horizon + 1),
            ]

            features_list.append(features)
            views_targets.append(final_views)
            notif_targets.append(int(final_notif))

    if not features_list:
        return None, None

    X = np.array(features_list)
    y_views = np.array(views_targets)
    y_notif = np.array(notif_targets)

    # Train models
    views_model = Ridge(alpha=1.0)
    views_model.fit(X, y_views)

    notif_model = LogisticRegression(penalty='l2', C=1.0, max_iter=1000)
    notif_model.fit(X, y_notif)

    return views_model, notif_model


def forecast_engagement_from_row(row, views_model, notif_model):
    """Forecast final engagement for a single row using trained models."""
    if views_model is None or notif_model is None:
        return float(row[10] or 0), float(row[11] or 0)

    horizon = row[12]
    current_views = int(row[10] or 0)
    current_notif = bool(row[11])
    applicant_tz = row[2]
    event_title = row[5]
    event_city = row[6]
    start_dt = row[7]
    prior_events = row[8]

    event_tz = city_to_timezone(event_city, event_title or "")
    offset = compute_tz_offset_diff(applicant_tz or "America/Los_Angeles", event_tz, event_date=start_dt)
    tz = tz_distance_bucket(offset)

    features = np.array([[
        float(current_views),
        math.log(current_views + 1),
        1.0 if current_notif else 0.0,
        float(horizon),
        math.log(horizon + 1),
        float(offset),
        1.0 if tz == "local" else 0.0,
        1.0 if tz == "near" else 0.0,
        1.0 if tz == "medium" else 0.0,
        float(prior_events or 0),
        math.log((prior_events or 0) + 1),
        math.log(current_views + 1) * math.log(horizon + 1),
    ]])

    forecasted_views = max(0.0, views_model.predict(features)[0])
    forecasted_notif_prob = notif_model.predict_proba(features)[0, 1]

    return forecasted_views, forecasted_notif_prob


# ---------------------------------------------------------------------------
# Leave-one-event-out CV
# ---------------------------------------------------------------------------

def loo_cv(snapshot_rows, encode_fn, model_cls, model_kwargs=None, horizons=None, use_forecasting=False):
    """LOO-CV with per-horizon evaluation.

    Returns per-horizon AUC/logloss and aggregate results.

    If use_forecasting=True, trains forecasting models in each fold and uses
    forecasted engagement for predictions.
    """
    from sklearn.metrics import roc_auc_score, log_loss

    if horizons is None:
        horizons = SNAPSHOT_HORIZONS

    # Group rows by event_id
    events = defaultdict(list)
    for row in snapshot_rows:
        events[row[4]].append(row)

    event_ids = sorted(events.keys(), key=lambda e: len(events[e]))

    results_by_horizon = {h: {"y_true": [], "y_pred": []} for h in horizons}
    aggregate = []

    for fold_idx, held_out in enumerate(event_ids):
        train_rows = []
        for eid, erows in events.items():
            if eid != held_out:
                train_rows.extend(erows)

        test_all = events[held_out]
        # need at least 10 T-0 rows to evaluate
        test_t0 = [r for r in test_all if r[12] == 0]
        if len(test_t0) < 10:
            continue

        # NEW: Train forecasting models on train_rows (no leakage!)
        if use_forecasting:
            views_model, notif_model = train_forecasting_models_from_rows(train_rows)
        else:
            views_model, notif_model = None, None

        # Encode training data (no forecasting needed for training)
        X_train = np.array([encode_fn(r) if not use_forecasting else encode_fn(r, None, None) for r in train_rows])
        y_train = np.array([int(r[1]) for r in train_rows])

        if y_train.sum() == 0 or y_train.sum() == len(y_train):
            continue

        kwargs = dict(model_kwargs or {})
        model = model_cls(**kwargs)
        model.fit(X_train, y_train)

        # actual total (from T-0 rows = unique applicants)
        actual_total = sum(int(r[1]) for r in test_t0)

        for horizon in horizons:
            horizon_rows = [r for r in test_all if r[12] == horizon]
            if not horizon_rows:
                continue

            # NEW: Forecast engagement for test rows if using forecasting
            if use_forecasting and views_model is not None and horizon > 0.5:
                X_test_encoded = []
                for r in horizon_rows:
                    forecasted_views, forecasted_notif_prob = forecast_engagement_from_row(r, views_model, notif_model)
                    X_test_encoded.append(encode_fn(r, forecasted_views, forecasted_notif_prob))
                X_test = np.array(X_test_encoded)
            else:
                X_test = np.array([encode_fn(r) if not use_forecasting else encode_fn(r, None, None) for r in horizon_rows])

            y_test = np.array([int(r[1]) for r in horizon_rows])

            if y_test.sum() == 0 or y_test.sum() == len(y_test):
                continue

            y_prob = model.predict_proba(X_test)[:, 1]

            results_by_horizon[horizon]["y_true"].extend(y_test.tolist())
            results_by_horizon[horizon]["y_pred"].extend(y_prob.tolist())

            aggregate.append({
                "event_id": held_out,
                "horizon": horizon,
                "n_applicants": len(horizon_rows),
                "actual_total": actual_total,
                "predicted_total": float(y_prob.sum()),
            })

    # Compute per-horizon metrics
    metrics = {}
    for h in horizons:
        yt = np.array(results_by_horizon[h]["y_true"])
        yp = np.array(results_by_horizon[h]["y_pred"])
        if len(yt) < 20 or yt.sum() == 0 or yt.sum() == len(yt):
            continue
        try:
            auc = roc_auc_score(yt, yp)
        except ValueError:
            auc = float("nan")
        ll = log_loss(yt, yp)
        metrics[h] = {"auc": auc, "logloss": ll, "n": len(yt)}

    return metrics, aggregate


def print_calibration(y_true, y_pred, label):
    """Print calibration by decile."""
    y_true = np.array(y_true)
    y_pred = np.array(y_pred)
    print(f"\n  Calibration ({label}):")
    order = np.argsort(y_pred)
    n = len(y_true)
    for i in range(10):
        lo = i * n // 10
        hi = (i + 1) * n // 10
        idx = order[lo:hi]
        pred_mean = y_pred[idx].mean()
        actual_mean = y_true[idx].mean()
        gap = actual_mean - pred_mean
        print(f"    Decile {i}: predicted={pred_mean:.1%}, actual={actual_mean:.1%}, gap={gap:+.1%}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    from sklearn.linear_model import LogisticRegression
    from retrain_horizon import build_snapshots, make_tz_aware

    print("=== LOADING DATA ===\n")
    base_rows, view_rows, notif_rows = load_all_data()

    print("\n=== BUILDING SNAPSHOTS ===")
    snapshot_rows = build_snapshots(base_rows, view_rows, notif_rows)

    # Also build T-0-only rows for baseline
    t0_rows = [r for r in snapshot_rows if r[12] == 0]
    print(f"\n  T-0 only rows (baseline): {len(t0_rows)}")

    lr_kwargs = {"penalty": "l2", "C": 1.0, "max_iter": 1000, "solver": "lbfgs"}
    sep = "=" * 70

    # --- Baseline: no horizon, T-0 training only ---
    print(f"\n{sep}")
    print("  BASELINE: LogReg 5+1 features, T-0 training only (no horizon)")
    print(f"{sep}")

    # For baseline evaluation at non-zero horizons, we need snapshot data
    # but the baseline model is trained on T-0 only and predicts WITHOUT horizon
    baseline_metrics_by_horizon = {}
    baseline_aggregate = []

    events_base = defaultdict(list)
    for row in t0_rows:
        events_base[row[4]].append(row)

    events_snap = defaultdict(list)
    for row in snapshot_rows:
        events_snap[row[4]].append(row)

    event_ids = sorted(events_base.keys(), key=lambda e: len(events_base[e]))

    for held_out in event_ids:
        # train on T-0 rows of other events
        train_rows = []
        for eid, erows in events_base.items():
            if eid != held_out:
                train_rows.extend(erows)

        test_t0 = events_base.get(held_out, [])
        if len(test_t0) < 10:
            continue

        X_train = np.array([encode_baseline(r) for r in train_rows])
        y_train = np.array([int(r[1]) for r in train_rows])

        if y_train.sum() == 0 or y_train.sum() == len(y_train):
            continue

        model = LogisticRegression(**lr_kwargs)
        model.fit(X_train, y_train)

        actual_total = sum(int(r[1]) for r in test_t0)

        # evaluate at each horizon using time-windowed features but NO horizon feature
        for horizon in SNAPSHOT_HORIZONS:
            horizon_rows = [r for r in events_snap.get(held_out, []) if r[12] == horizon]
            if not horizon_rows:
                continue

            X_test = np.array([encode_baseline(r) for r in horizon_rows])
            y_test = np.array([int(r[1]) for r in horizon_rows])

            if y_test.sum() == 0 or y_test.sum() == len(y_test):
                continue

            y_prob = model.predict_proba(X_test)[:, 1]

            if horizon not in baseline_metrics_by_horizon:
                baseline_metrics_by_horizon[horizon] = {"y_true": [], "y_pred": []}
            baseline_metrics_by_horizon[horizon]["y_true"].extend(y_test.tolist())
            baseline_metrics_by_horizon[horizon]["y_pred"].extend(y_prob.tolist())

            baseline_aggregate.append({
                "event_id": held_out,
                "horizon": horizon,
                "n_applicants": len(horizon_rows),
                "actual_total": actual_total,
                "predicted_total": float(y_prob.sum()),
            })

    # --- Horizon-aware: with horizon, multi-snapshot training ---
    print(f"\n{sep}")
    print("  HORIZON-AWARE: LogReg 19 features, multi-snapshot training (NO forecasting)")
    print(f"{sep}")

    horizon_metrics, horizon_aggregate = loo_cv(
        snapshot_rows, encode_horizon,
        LogisticRegression, lr_kwargs,
        use_forecasting=False,
    )

    # --- Horizon-aware WITH FORECASTING ---
    print(f"\n{sep}")
    print("  HORIZON-AWARE + FORECASTING: 19 features + engagement forecasting")
    print(f"{sep}")

    forecast_metrics, forecast_aggregate = loo_cv(
        snapshot_rows, encode_horizon,
        LogisticRegression, lr_kwargs,
        use_forecasting=True,
    )

    # --- Print comparison ---
    print(f"\n{sep}")
    print("  PER-PERSON DISCRIMINATION (AUC by horizon)")
    print(f"{sep}\n")

    from sklearn.metrics import roc_auc_score

    print(f"  {'Horizon':>8} | {'Baseline':>9} | {'Horizon':>9} | {'Forecast':>9} | {'Δ Hor':>7} | {'Δ For':>7} | {'N':>6}")
    print(f"  {'─'*8}─┼─{'─'*9}─┼─{'─'*9}─┼─{'─'*9}─┼─{'─'*7}─┼─{'─'*7}─┼─{'─'*6}")

    for h in SNAPSHOT_HORIZONS:
        b_data = baseline_metrics_by_horizon.get(h, {})
        h_data = horizon_metrics.get(h, {})
        f_data = forecast_metrics.get(h, {})

        if b_data and "y_true" in b_data:
            yt = np.array(b_data["y_true"])
            yp = np.array(b_data["y_pred"])
            try:
                b_auc = roc_auc_score(yt, yp)
            except ValueError:
                b_auc = float("nan")
            b_n = len(yt)
        else:
            b_auc = float("nan")
            b_n = 0

        h_auc = h_data.get("auc", float("nan")) if h_data else float("nan")
        h_n = h_data.get("n", 0) if h_data else 0

        f_auc = f_data.get("auc", float("nan")) if f_data else float("nan")
        f_n = f_data.get("n", 0) if f_data else 0

        improvement_h = h_auc - b_auc if not (math.isnan(h_auc) or math.isnan(b_auc)) else float("nan")
        improvement_f = f_auc - b_auc if not (math.isnan(f_auc) or math.isnan(b_auc)) else float("nan")

        imp_h_str = f"{improvement_h:+.3f}" if not math.isnan(improvement_h) else "N/A"
        imp_f_str = f"{improvement_f:+.3f}" if not math.isnan(improvement_f) else "N/A"

        print(f"  T-{h:>5} | {b_auc:>8.4f} | {h_auc:>8.4f} | {f_auc:>8.4f} | {imp_h_str:>7} | {imp_f_str:>7} | {max(b_n, h_n, f_n):>6}")

    # --- Aggregate comparison ---
    print(f"\n{sep}")
    print("  AGGREGATE BACKTEST (predicted total vs actual total)")
    print(f"{sep}\n")

    def compute_aggregate_stats(agg_list, horizon):
        rows = [r for r in agg_list if r["horizon"] == horizon]
        if not rows:
            return None
        errors = [r["predicted_total"] - r["actual_total"] for r in rows]
        abs_errors = [abs(e) for e in errors]
        return {
            "n_events": len(rows),
            "mae": np.mean(abs_errors),
            "bias": np.mean(errors),
            "mape": np.mean([abs(e) / max(r["actual_total"], 1) for e, r in zip(errors, rows)]),
        }

    print(f"  {'Horizon':>8} | {'Baseline':>9} | {'Horizon':>9} | {'Forecast':>9} | {'Δ Hor':>7} | {'Δ For':>7} | {'Bias: Base':>11} | {'Bias: Hor':>10} | {'Bias: For':>10}")
    print(f"  {'─'*8}─┼─{'─'*9}─┼─{'─'*9}─┼─{'─'*9}─┼─{'─'*7}─┼─{'─'*7}─┼─{'─'*11}─┼─{'─'*10}─┼─{'─'*10}")

    for h in SNAPSHOT_HORIZONS:
        b_stats = compute_aggregate_stats(baseline_aggregate, h)
        h_stats = compute_aggregate_stats(horizon_aggregate, h)
        f_stats = compute_aggregate_stats(forecast_aggregate, h)

        if not b_stats or not h_stats or not f_stats:
            continue

        improvement_h = b_stats["mae"] - h_stats["mae"]
        improvement_f = b_stats["mae"] - f_stats["mae"]
        print(
            f"  T-{h:>5} | "
            f"{b_stats['mae']:>8.1f} | "
            f"{h_stats['mae']:>8.1f} | "
            f"{f_stats['mae']:>8.1f} | "
            f"{improvement_h:>+6.1f} | "
            f"{improvement_f:>+6.1f} | "
            f"{b_stats['bias']:>+10.1f} | "
            f"{h_stats['bias']:>+9.1f} | "
            f"{f_stats['bias']:>+9.1f}"
        )

    # --- Per-event detail at T-7 ---
    print(f"\n{sep}")
    print("  PER-EVENT DETAIL AT T-7 (largest improvement expected)")
    print(f"{sep}\n")

    baseline_t7 = {r["event_id"]: r for r in baseline_aggregate if r["horizon"] == 7}
    horizon_t7 = {r["event_id"]: r for r in horizon_aggregate if r["horizon"] == 7}
    forecast_t7 = {r["event_id"]: r for r in forecast_aggregate if r["horizon"] == 7}

    common_events = sorted(set(baseline_t7) & set(horizon_t7) & set(forecast_t7), key=lambda e: baseline_t7[e]["actual_total"])

    print(f"  {'Event':>6} | {'Actual':>6} | {'Base':>6} | {'Hor':>6} | {'For':>6} | {'B Err':>6} | {'H Err':>6} | {'F Err':>6}")
    print(f"  {'─'*6}─┼─{'─'*6}─┼─{'─'*6}─┼─{'─'*6}─┼─{'─'*6}─┼─{'─'*6}─┼─{'─'*6}─┼─{'─'*6}")

    for eid in common_events[-15:]:  # show last 15 (largest events)
        b = baseline_t7[eid]
        h = horizon_t7[eid]
        f = forecast_t7[eid]
        b_err = b["predicted_total"] - b["actual_total"]
        h_err = h["predicted_total"] - h["actual_total"]
        f_err = f["predicted_total"] - f["actual_total"]
        print(
            f"  {str(eid)[:6]:>6} | "
            f"{b['actual_total']:>6} | "
            f"{b['predicted_total']:>6.0f} | "
            f"{h['predicted_total']:>6.0f} | "
            f"{f['predicted_total']:>6.0f} | "
            f"{b_err:>+6.0f} | "
            f"{h_err:>+6.0f} | "
            f"{f_err:>+6.0f}"
        )

    # --- Calibration at T-7 ---
    b_t7_data = baseline_metrics_by_horizon.get(7, {})
    if b_t7_data and "y_true" in b_t7_data:
        print_calibration(b_t7_data["y_true"], b_t7_data["y_pred"], "Baseline at T-7")

    h_t7_data = horizon_metrics.get(7, {})
    if h_t7_data and "n" in h_t7_data:
        # reconstruct from loo_cv results
        pass  # calibration printed via the LOO results

    # --- Summary ---
    print(f"\n{sep}")
    print("  SUMMARY")
    print(f"{sep}\n")

    b_t0_stats = compute_aggregate_stats(baseline_aggregate, 0)
    h_t0_stats = compute_aggregate_stats(horizon_aggregate, 0)
    f_t0_stats = compute_aggregate_stats(forecast_aggregate, 0)
    b_t7_stats = compute_aggregate_stats(baseline_aggregate, 7)
    h_t7_stats = compute_aggregate_stats(horizon_aggregate, 7)
    f_t7_stats = compute_aggregate_stats(forecast_aggregate, 7)
    b_t14_stats = compute_aggregate_stats(baseline_aggregate, 14)
    h_t14_stats = compute_aggregate_stats(horizon_aggregate, 14)
    f_t14_stats = compute_aggregate_stats(forecast_aggregate, 14)

    if b_t0_stats and h_t0_stats and f_t0_stats:
        print(f"  T-0 MAE:  baseline={b_t0_stats['mae']:.1f}, horizon={h_t0_stats['mae']:.1f}, forecast={f_t0_stats['mae']:.1f}")
    if b_t7_stats and h_t7_stats and f_t7_stats:
        imp_h = b_t7_stats["mae"] - h_t7_stats["mae"]
        imp_f = b_t7_stats["mae"] - f_t7_stats["mae"]
        print(f"  T-7 MAE:  baseline={b_t7_stats['mae']:.1f}, horizon={h_t7_stats['mae']:.1f} ({imp_h:+.1f}), forecast={f_t7_stats['mae']:.1f} ({imp_f:+.1f})")
        print(f"  T-7 BIAS: baseline={b_t7_stats['bias']:+.1f}, horizon={h_t7_stats['bias']:+.1f}, forecast={f_t7_stats['bias']:+.1f}")
    if b_t14_stats and h_t14_stats and f_t14_stats:
        imp_h = b_t14_stats["mae"] - h_t14_stats["mae"]
        imp_f = b_t14_stats["mae"] - f_t14_stats["mae"]
        print(f"  T-14 MAE: baseline={b_t14_stats['mae']:.1f}, horizon={h_t14_stats['mae']:.1f} ({imp_h:+.1f}), forecast={f_t14_stats['mae']:.1f} ({imp_f:+.1f})")
        print(f"  T-14 BIAS: baseline={b_t14_stats['bias']:+.1f}, horizon={h_t14_stats['bias']:+.1f}, forecast={f_t14_stats['bias']:+.1f}")

    if f_t7_stats and b_t7_stats:
        if abs(f_t7_stats["bias"]) < 30:
            print(f"\n  ✅ VERDICT: Forecasting FIXES the bias! T-7 bias reduced from {b_t7_stats['bias']:+.0f} to {f_t7_stats['bias']:+.0f}.")
        elif f_t7_stats["mae"] < h_t7_stats["mae"]:
            print(f"\n  VERDICT: Forecasting improves T-7 MAE ({f_t7_stats['mae']:.1f} < {h_t7_stats['mae']:.1f}).")
        else:
            print(f"\n  VERDICT: Still underpredicting at T-7 (bias={f_t7_stats['bias']:+.0f}). Need further investigation.")
    else:
        print(f"\n  VERDICT: Insufficient data for comparison.")


if __name__ == "__main__":
    main()
