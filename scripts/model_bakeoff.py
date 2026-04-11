"""Bakeoff: compare LogReg vs XGBoost with old and new features.

Leave-one-event-out cross-validation on 8,244 approved applicants across 30 events.
Trains 4 model variants, reports AUC-ROC, log-loss, and calibration.
"""
import math
import os
import sys
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_training_data():
    """Query all training data with 5 features from platform DB."""
    import psycopg2

    dsn = os.environ["PLATFORM_DATABASE_URL"]
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()

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

        print("Querying training data...")
        cur.execute(query)
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    print(f"  Loaded {len(rows)} rows")
    return rows


# ---------------------------------------------------------------------------
# Feature encoding
# ---------------------------------------------------------------------------

# Reuse bucket functions from the codebase
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from cv_rank.waves.predict import (
    timing_bucket,
    prior_rate_bucket,
    city_to_timezone,
    compute_tz_offset_diff,
    tz_distance_bucket,
)

# One-hot specs (reference category = first, dropped)
TIMING_CATS = ["21+", "14-21", "7-14", "3-7", "1-3", "<1"]
TZ_CATS = ["local", "near", "medium", "far"]
PRIOR_CATS = ["no_history", "0-25%", "25-50%", "50-75%", "75-100%"]


def one_hot(value, categories):
    """One-hot encode, dropping first category as reference."""
    return [1.0 if value == cat else 0.0 for cat in categories[1:]]


def encode_row_A(row):
    """Model A: 3 original features, one-hot bucketed (12 weights)."""
    _, _, applicant_tz, days_before, _, event_title, event_city, start_dt, prior_events, prior_attended, _, _ = row

    tb = timing_bucket(float(days_before))
    event_tz = city_to_timezone(event_city, event_title or "")
    offset = compute_tz_offset_diff(applicant_tz or "America/Los_Angeles", event_tz, event_date=start_dt)
    tz = tz_distance_bucket(offset)
    prior_rate = prior_attended / prior_events if prior_events and prior_events > 0 else None
    pr = prior_rate_bucket(prior_rate)

    return one_hot(tb, TIMING_CATS) + one_hot(tz, TZ_CATS) + one_hot(pr, PRIOR_CATS)


def encode_row_B(row):
    """Model B: 5 features, one-hot buckets + log(views) continuous + binary notif."""
    base = encode_row_A(row)
    page_views = int(row[10] or 0)
    notif_read = bool(row[11])
    return base + [math.log(page_views + 1), 1.0 if notif_read else 0.0]


def encode_row_C(row):
    """Model C: 5 features, all raw continuous for XGBoost."""
    _, _, applicant_tz, days_before, _, event_title, event_city, start_dt, prior_events, prior_attended, page_views, notif_read = row

    event_tz = city_to_timezone(event_city, event_title or "")
    offset = compute_tz_offset_diff(applicant_tz or "America/Los_Angeles", event_tz, event_date=start_dt)
    prior_rate = prior_attended / prior_events if prior_events and prior_events > 0 else -1.0

    return [
        float(days_before),
        float(offset),
        float(prior_rate),
        math.log(int(page_views or 0) + 1),
        1.0 if notif_read else 0.0,
    ]


# ---------------------------------------------------------------------------
# Leave-one-event-out cross-validation
# ---------------------------------------------------------------------------

def loo_cv(rows, encode_fn, model_cls, model_kwargs=None):
    """Leave-one-event-out CV. Returns per-fold AUC, log-loss.

    Note: No eval_set/early stopping — using the held-out fold for early
    stopping and then evaluating on the same fold would leak information.
    """
    from sklearn.metrics import roc_auc_score, log_loss

    # group rows by event_id
    events = {}
    for row in rows:
        eid = row[4]
        events.setdefault(eid, []).append(row)

    event_ids = sorted(events.keys(), key=lambda e: len(events[e]))
    fold_aucs = []
    fold_losses = []
    all_y_true = []
    all_y_pred = []

    for held_out in event_ids:
        train_rows = []
        test_rows = []
        for eid, erows in events.items():
            if eid == held_out:
                test_rows.extend(erows)
            else:
                train_rows.extend(erows)

        if len(test_rows) < 10:
            continue

        X_train = np.array([encode_fn(r) for r in train_rows])
        y_train = np.array([int(r[1]) for r in train_rows])
        X_test = np.array([encode_fn(r) for r in test_rows])
        y_test = np.array([int(r[1]) for r in test_rows])

        if y_test.sum() == 0 or y_test.sum() == len(y_test):
            continue  # skip if no variance

        kwargs = dict(model_kwargs or {})
        model = model_cls(**kwargs)
        model.fit(X_train, y_train)

        y_prob = model.predict_proba(X_test)[:, 1]
        try:
            auc = roc_auc_score(y_test, y_prob)
        except ValueError:
            continue
        ll = log_loss(y_test, y_prob)

        fold_aucs.append(auc)
        fold_losses.append(ll)
        all_y_true.extend(y_test.tolist())
        all_y_pred.extend(y_prob.tolist())

    return {
        "auc_mean": np.mean(fold_aucs),
        "auc_std": np.std(fold_aucs),
        "logloss_mean": np.mean(fold_losses),
        "logloss_std": np.std(fold_losses),
        "n_folds": len(fold_aucs),
        "y_true": all_y_true,
        "y_pred": all_y_pred,
    }


def print_calibration(y_true, y_pred, label):
    """Print calibration by decile."""
    y_true = np.array(y_true)
    y_pred = np.array(y_pred)
    print(f"\n  Calibration ({label}):")
    print(f"  {'Decile':<12} | {'Predicted':>9} | {'Actual':>9} | {'N':>5} | {'Gap':>5}")
    print(f"  {'─'*12}─┼─{'─'*9}─┼─{'─'*9}─┼─{'─'*5}─┼─{'─'*5}")

    # sort by predicted probability, split into 10 bins
    order = np.argsort(y_pred)
    n = len(y_true)
    for i in range(10):
        lo = i * n // 10
        hi = (i + 1) * n // 10
        idx = order[lo:hi]
        pred_mean = y_pred[idx].mean()
        actual_mean = y_true[idx].mean()
        gap = actual_mean - pred_mean
        print(f"  {f'{i*10}-{(i+1)*10}%':<12} | {pred_mean:>8.1%} | {actual_mean:>8.1%} | {len(idx):>5} | {gap:>+.1%}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    from sklearn.linear_model import LogisticRegression
    from xgboost import XGBClassifier

    rows = load_training_data()
    y_all = np.array([int(r[1]) for r in rows])
    pos_rate = y_all.mean()
    print(f"  Positive rate: {pos_rate:.1%}")
    print(f"  Events: {len(set(r[4] for r in rows))}")

    # Page views distribution
    views = [int(r[10] or 0) for r in rows]
    notifs = [bool(r[11]) for r in rows]
    print(f"  Page views: mean={np.mean(views):.1f}, median={np.median(views):.0f}, "
          f"max={max(views)}, zero={sum(1 for v in views if v == 0)}")
    print(f"  Notification read: {sum(notifs)}/{len(notifs)} ({sum(notifs)/len(notifs):.0%})")

    sep = "═" * 70

    # --- Model A: LogReg 3-feature baseline ---
    print(f"\n{sep}")
    print("  Model A: LogReg, 3 original features (timing + tz + prior)")
    print(sep)
    results_a = loo_cv(
        rows, encode_row_A,
        LogisticRegression,
        {"penalty": "l2", "C": 1.0, "max_iter": 1000, "solver": "lbfgs"},
    )
    print(f"  AUC:     {results_a['auc_mean']:.4f} ± {results_a['auc_std']:.4f}")
    print(f"  LogLoss: {results_a['logloss_mean']:.4f} ± {results_a['logloss_std']:.4f}")
    print(f"  Folds:   {results_a['n_folds']}")
    print_calibration(results_a["y_true"], results_a["y_pred"], "Model A")

    # --- Model B: LogReg 5-feature ---
    print(f"\n{sep}")
    print("  Model B: LogReg, 5 features (+log_views, +notif_read)")
    print(sep)
    results_b = loo_cv(
        rows, encode_row_B,
        LogisticRegression,
        {"penalty": "l2", "C": 1.0, "max_iter": 1000, "solver": "lbfgs"},
    )
    print(f"  AUC:     {results_b['auc_mean']:.4f} ± {results_b['auc_std']:.4f}")
    print(f"  LogLoss: {results_b['logloss_mean']:.4f} ± {results_b['logloss_std']:.4f}")
    print(f"  Folds:   {results_b['n_folds']}")
    print_calibration(results_b["y_true"], results_b["y_pred"], "Model B")

    # --- Model C: XGBoost 5-feature default ---
    print(f"\n{sep}")
    print("  Model C: XGBoost, 5 raw features (default hyperparams)")
    print(sep)
    scale = (1 - pos_rate) / pos_rate
    results_c = loo_cv(
        rows, encode_row_C,
        XGBClassifier,
        {
            "n_estimators": 200,
            "max_depth": 4,
            "learning_rate": 0.1,
            "scale_pos_weight": scale,
            "eval_metric": "logloss",
            "verbosity": 0,
        },
    )
    print(f"  AUC:     {results_c['auc_mean']:.4f} ± {results_c['auc_std']:.4f}")
    print(f"  LogLoss: {results_c['logloss_mean']:.4f} ± {results_c['logloss_std']:.4f}")
    print(f"  Folds:   {results_c['n_folds']}")
    print_calibration(results_c["y_true"], results_c["y_pred"], "Model C")

    # --- Model D: XGBoost 5-feature tuned ---
    print(f"\n{sep}")
    print("  Model D: XGBoost, 5 raw features (conservative tuning)")
    print(sep)
    results_d = loo_cv(
        rows, encode_row_C,  # same encoding
        XGBClassifier,
        {
            "n_estimators": 300,
            "max_depth": 3,
            "learning_rate": 0.05,
            "min_child_weight": 50,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "scale_pos_weight": scale,
            "eval_metric": "logloss",
            "verbosity": 0,
        },
    )
    print(f"  AUC:     {results_d['auc_mean']:.4f} ± {results_d['auc_std']:.4f}")
    print(f"  LogLoss: {results_d['logloss_mean']:.4f} ± {results_d['logloss_std']:.4f}")
    print(f"  Folds:   {results_d['n_folds']}")
    print_calibration(results_d["y_true"], results_d["y_pred"], "Model D")

    # --- Summary ---
    print(f"\n{'═' * 70}")
    print("  SUMMARY")
    print(f"{'═' * 70}")
    results = [
        ("A: LogReg 3-feat", results_a),
        ("B: LogReg 5-feat", results_b),
        ("C: XGBoost default", results_c),
        ("D: XGBoost tuned", results_d),
    ]
    print(f"  {'Model':<22} | {'AUC':>12} | {'LogLoss':>12} | {'Folds':>5}")
    print(f"  {'─'*22}─┼─{'─'*12}─┼─{'─'*12}─┼─{'─'*5}")
    best_auc = 0
    best_name = ""
    for name, r in results:
        auc_str = f"{r['auc_mean']:.4f}±{r['auc_std']:.3f}"
        ll_str = f"{r['logloss_mean']:.4f}±{r['logloss_std']:.3f}"
        print(f"  {name:<22} | {auc_str:>12} | {ll_str:>12} | {r['n_folds']:>5}")
        if r["auc_mean"] > best_auc:
            best_auc = r["auc_mean"]
            best_name = name

    print(f"\n  Winner: {best_name} (AUC {best_auc:.4f})")

    # Decision
    auc_b = results_b["auc_mean"]
    auc_best_xgb = max(results_c["auc_mean"], results_d["auc_mean"])
    xgb_advantage = auc_best_xgb - auc_b
    if xgb_advantage >= 0.02:
        print(f"  Recommendation: Ship XGBoost (+{xgb_advantage:.3f} AUC over LogReg)")
    else:
        print(f"  Recommendation: Ship LogReg (XGBoost advantage only {xgb_advantage:+.3f}, <0.02 threshold)")

    # Feature importance from full XGBoost
    print(f"\n  XGBoost feature importances (Model D, full training):")
    X_all = np.array([encode_row_C(r) for r in rows])
    final_model = XGBClassifier(
        n_estimators=300, max_depth=3, learning_rate=0.05,
        min_child_weight=50, subsample=0.8, colsample_bytree=0.8,
        scale_pos_weight=scale, verbosity=0,
    )
    final_model.fit(X_all, y_all)
    feat_names = ["days_before", "tz_offset_hours", "prior_rate", "log_views", "notif_read"]
    importances = final_model.feature_importances_
    for name, imp in sorted(zip(feat_names, importances), key=lambda x: -x[1]):
        bar = "█" * int(imp * 50)
        print(f"    {name:<18} {imp:.3f}  {bar}")


if __name__ == "__main__":
    main()
