"""Retrain LogReg with 5 features on full dataset. Outputs hardcoded coefficients."""
import math, os, sys
from pathlib import Path
import numpy as np
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cv_rank.waves.predict import (
    timing_bucket, prior_rate_bucket,
    city_to_timezone, compute_tz_offset_diff, tz_distance_bucket,
)
from cv_rank.waves.model import _encode_features, _ACTIVE_FEATURES, FEATURE_SPEC


def encode(row):
    """Encode a training row using the same _encode_features as production."""
    _, _, applicant_tz, days_before, _, event_title, event_city, start_dt, prior_events, prior_attended, page_views, notif_read = row

    tb = timing_bucket(float(days_before))
    event_tz = city_to_timezone(event_city, event_title or "")
    offset = compute_tz_offset_diff(applicant_tz or "America/Los_Angeles", event_tz, event_date=start_dt)
    tz = tz_distance_bucket(offset)
    prior_rate = prior_attended / prior_events if prior_events and prior_events > 0 else None
    pr = prior_rate_bucket(prior_rate)

    return _encode_features(
        tb, tz, pr,
        page_views=int(page_views or 0),
        notification_read=bool(notif_read),
    )


def main():
    import psycopg2
    from sklearn.linear_model import LogisticRegression

    conn = psycopg2.connect(os.environ["PLATFORM_DATABASE_URL"])
    try:
        cur = conn.cursor()
        # NOTE: this query mirrors model.py:train_model() — keep in sync
        cur.execute("""
    WITH prior_agg AS (
        SELECT ea2."userId", pe2.id AS event_id, pe2."startDateTime" AS event_start,
            COUNT(*) FILTER (WHERE ea2.status = 'approved') AS prior_events,
            COALESCE(SUM(ea2."checkedIn"::int) FILTER (WHERE ea2.status = 'approved'), 0) AS prior_attended
        FROM "EventApplicant" ea2 JOIN "PlatformEvent" pe2 ON ea2."eventId" = pe2.id
        WHERE pe2."startDateTime" IS NOT NULL
        GROUP BY ea2."userId", pe2.id, pe2."startDateTime"
    ),
    page_view_counts AS (
        SELECT i."userId", i.properties->>'eventId' AS event_id, COUNT(*) AS view_count
        FROM "Insight" i
        JOIN "PlatformEvent" pe_pv ON pe_pv.id::text = i.properties->>'eventId'
        WHERE i."eventName" = 'PAGE_VIEW' AND i.properties->>'eventId' IS NOT NULL
        AND i."createdAt" < pe_pv."startDateTime"
        GROUP BY i."userId", i.properties->>'eventId'
    ),
    notif_reads AS (
        SELECT DISTINCT ON (pn."userId", pn.data->>'eventId')
            pn."userId", pn.data->>'eventId' AS event_id, pn.read AS notif_read
        FROM "PlatformNotification" pn
        WHERE pn.type = 'event_application_status_change' AND pn.data->>'status' = 'approved'
        ORDER BY pn."userId", pn.data->>'eventId', pn."createdAt" DESC
    )
    SELECT
        ea."userId", ea."checkedIn"::int AS checked_in,
        ea."appliedFromTimeZone" AS applicant_tz,
        EXTRACT(EPOCH FROM (pe."startDateTime" - ea."createdAt")) / 86400.0 AS days_before,
        pe.id AS event_id, pe.title AS event_title, pe.city AS event_city, pe."startDateTime",
        (SELECT COALESCE(SUM(pa.prior_events), 0) FROM prior_agg pa
         WHERE pa."userId" = ea."userId" AND pa.event_start < pe."startDateTime" AND pa.event_id != pe.id) AS prior_events,
        (SELECT COALESCE(SUM(pa.prior_attended), 0) FROM prior_agg pa
         WHERE pa."userId" = ea."userId" AND pa.event_start < pe."startDateTime" AND pa.event_id != pe.id) AS prior_attended,
        COALESCE(pv.view_count, 0) AS page_views,
        COALESCE(nr.notif_read, false) AS notif_read
    FROM "EventApplicant" ea
    JOIN "PlatformEvent" pe ON ea."eventId" = pe.id
    LEFT JOIN page_view_counts pv ON pv."userId" = ea."userId" AND pv.event_id = pe.id::text
    LEFT JOIN notif_reads nr ON nr."userId" = ea."userId" AND nr.event_id = pe.id::text
    WHERE ea.status = 'approved' AND pe."startDateTime" IS NOT NULL AND ea."createdAt" IS NOT NULL
      AND EXTRACT(EPOCH FROM (pe."startDateTime" - ea."createdAt")) / 86400.0 > 0.25
      AND pe.id IN (
          SELECT "eventId" FROM "EventApplicant" WHERE "checkedIn" = true GROUP BY "eventId" HAVING COUNT(*) >= 10
      )
    ORDER BY pe."startDateTime", ea."createdAt"
    """)
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    X = np.array([encode(r) for r in rows])
    y = np.array([int(r[1]) for r in rows])
    print(f"Training on {len(rows)} rows, {X.shape[1]} features, positive rate: {y.mean():.1%}")

    model = LogisticRegression(penalty="l2", C=1.0, max_iter=1000, solver="lbfgs")
    model.fit(X, y)

    # Build feature names from model's _ACTIVE_FEATURES (single source of truth)
    feature_names = []
    for feat_name in _ACTIVE_FEATURES:
        if feat_name in FEATURE_SPEC:
            for cat in FEATURE_SPEC[feat_name][1:]:
                feature_names.append(f"{feat_name}_{cat}")
        else:
            feature_names.append(feat_name)

    print(f"\nIntercept: {model.intercept_[0]:.4f}")
    print(f"\nWeights:")
    for name, w in zip(feature_names, model.coef_[0]):
        print(f"  {name:<30} {w:+.4f}")

    # Print Python-ready format for hardcoding
    print(f"\n# --- Copy-paste into model.py ---")
    print(f'MODEL_COEFFICIENTS = {{')
    print(f'    "intercept": {model.intercept_[0]:.4f},')
    print(f'    "weights": [')
    for name, w in zip(feature_names, model.coef_[0]):
        print(f'        {w:+.4f},   # {name}')
    print(f'    ],')
    print(f'    "feature_names": {feature_names},')
    print(f'    "n_samples": {len(rows)},')
    print(f'    "positive_rate": {y.mean():.3f},')
    print(f'}}')

    # Quick calibration check
    preds = model.predict_proba(X)[:, 1]
    print(f"\nCalibration check (full dataset):")
    order = np.argsort(preds)
    n = len(y)
    for i in range(10):
        lo, hi = i * n // 10, (i + 1) * n // 10
        idx = order[lo:hi]
        print(f"  Decile {i}: predicted={preds[idx].mean():.1%}, actual={y[idx].mean():.1%}")

    # Spot checks
    from cv_rank.waves.model import _sigmoid
    def predict(vec):
        return _sigmoid(model.intercept_[0] + sum(w * f for w, f in zip(model.coef_[0], vec)))

    # Best: local + late + high prior + 15 views + read
    best = encode((None, 0, "America/Los_Angeles", 0.5, None, "", "San Francisco", None, 5, 5, 15, True))
    worst = encode((None, 0, "Asia/Kolkata", 25, None, "", "San Francisco", None, 0, 0, 0, False))
    print(f"\nSpot checks:")
    print(f"  Best profile (local/late/high-prior/15views/read): {predict(best):.1%}")
    print(f"  Worst profile (far/early/no-history/0views/unread): {predict(worst):.1%}")


if __name__ == "__main__":
    main()
