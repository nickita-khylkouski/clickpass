"""Temporal held-out evaluation for the three-stage attendance model.

This script is intentionally conservative:
- Split historical events by event_start (oldest train, newest test)
- Derive Stage 3 future-attendance rates from training events only
- Evaluate baseline, Stage 1+2, and full Stage 0+1+2+3 on held-out events

It avoids the failure mode we hit earlier: choosing a correction on the same
events we later call "validation".
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import psycopg2
import psycopg2.extras

from cv_rank.waves.model import forecast_engagement, forecast_signup_count, predict_show_probability
from cv_rank.waves.predict import city_to_timezone, compute_tz_offset_diff, tz_distance_bucket


HORIZON_BUCKETS = {
    7: ("T-7 to T-3", "T-3 to T-1", "T-1 to T-0"),
    3: ("T-3 to T-1", "T-1 to T-0"),
    1: ("T-1 to T-0",),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--horizon", type=int, choices=[7, 3, 1], default=7)
    parser.add_argument("--test-events", type=int, default=10)
    parser.add_argument("--min-current-approved", type=int, default=20)
    parser.add_argument(
        "--include-suspect-labels",
        action="store_true",
        help="Include events with implausibly low actual_attended labels (default: exclude).",
    )
    return parser.parse_args()


def load_signup_velocity_rows() -> list[dict[str, object]]:
    path = Path(__file__).parent.parent / "results" / "signup_velocity.csv"
    rows: list[dict[str, object]] = []
    with path.open() as f:
        for row in csv.DictReader(f):
            if not row["event_start"]:
                continue
            rows.append(
                {
                    "event_id": row["event_id"],
                    "title": row["title"],
                    "city": row["city"] or "",
                    "event_start": datetime.fromisoformat(row["event_start"]),
                    "approved_t21": int(row["approved_t21"] or 0),
                    "approved_t14": int(row["approved_t14"] or 0),
                    "approved_t7": int(row["approved_t7"] or 0),
                    "approved_t3": int(row["approved_t3"] or 0),
                    "approved_t1": int(row["approved_t1"] or 0),
                    "approved_t0": int(row["approved_t0"] or 0),
                    "actual_attended": int(row["actual_attended"] or 0),
                }
            )
    rows.sort(key=lambda row: row["event_start"])
    return rows


def derive_future_show_rate(train_event_ids: set[str], horizon: int) -> float:
    path = Path(__file__).parent.parent / "results" / "engagement_profiles.csv"
    buckets = set(HORIZON_BUCKETS[horizon])
    kept = 0
    showed = 0

    with path.open() as f:
        for row in csv.DictReader(f):
            if row["event_id"] not in train_event_ids:
                continue
            if row["signup_bucket"] not in buckets:
                continue
            kept += 1
            showed += int(row["showed_up"])

    if kept == 0:
        raise RuntimeError("No engagement rows found for training events")

    return showed / kept


def fetch_snapshot_rows(event_ids: list[str], horizon: int) -> list[dict[str, object]]:
    dsn = os.environ["PLATFORM_DATABASE_URL"]
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        query = f"""
            WITH prior_agg AS (
                SELECT ea2."userId", pe2.id AS event_id, pe2."startDateTime" AS event_start,
                    COUNT(*) FILTER (WHERE ea2.status = 'approved') AS prior_events,
                    COALESCE(SUM(ea2."checkedIn"::int) FILTER (WHERE ea2.status = 'approved'), 0) AS prior_attended
                FROM "EventApplicant" ea2
                JOIN "PlatformEvent" pe2 ON pe2.id = ea2."eventId"
                WHERE pe2."startDateTime" IS NOT NULL
                GROUP BY ea2."userId", pe2.id, pe2."startDateTime"
            ),
            page_view_snapshots AS (
                SELECT i."userId", i.properties->>'eventId' AS event_id, COUNT(*) AS view_count
                FROM "Insight" i
                JOIN "PlatformEvent" pe_pv ON pe_pv.id::text = i.properties->>'eventId'
                WHERE i."eventName" = 'PAGE_VIEW'
                  AND i.properties->>'eventId' IS NOT NULL
                  AND pe_pv.id::text = ANY(%s)
                  AND i."createdAt" <= pe_pv."startDateTime" - INTERVAL '{horizon} days'
                GROUP BY i."userId", i.properties->>'eventId'
            ),
            notif_snapshots AS (
                SELECT
                    pn."userId",
                    pn.data->>'eventId' AS event_id,
                    BOOL_OR(pn.read) AS notif_read
                FROM "PlatformNotification" pn
                JOIN "PlatformEvent" pe_pn ON pe_pn.id::text = pn.data->>'eventId'
                WHERE pn.type = 'event_application_status_change'
                  AND pn.data->>'status' = 'approved'
                  AND pe_pn.id::text = ANY(%s)
                  AND pn."createdAt" <= pe_pn."startDateTime" - INTERVAL '{horizon} days'
                GROUP BY pn."userId", pn.data->>'eventId'
            )
            SELECT
                pe.id::text AS event_id,
                pe.title,
                pe.city,
                pe."startDateTime" AS event_start,
                ea."userId",
                ea."appliedFromTimeZone" AS applicant_tz,
                EXTRACT(EPOCH FROM (pe."startDateTime" - ea."createdAt")) / 86400.0 AS days_before,
                ea."checkedIn" AS showed_up,
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
                COALESCE(pv.view_count, 0) AS page_views_snapshot,
                COALESCE(ns.notif_read, false) AS notif_read_snapshot
            FROM "EventApplicant" ea
            JOIN "PlatformEvent" pe ON pe.id = ea."eventId"
            LEFT JOIN page_view_snapshots pv ON pv."userId" = ea."userId" AND pv.event_id = pe.id::text
            LEFT JOIN notif_snapshots ns ON ns."userId" = ea."userId" AND ns.event_id = pe.id::text
            WHERE pe.id::text = ANY(%s)
              AND ea.status = 'approved'
              AND ea."createdAt" IS NOT NULL
              AND ea."createdAt" <= pe."startDateTime" - INTERVAL '{horizon} days'
            ORDER BY pe."startDateTime", ea."createdAt"
        """
        cur.execute(query, (event_ids, event_ids, event_ids))
        rows = cur.fetchall()
        cur.close()
        return list(rows)
    finally:
        conn.close()


def summarize(name: str, predicted: list[float], actual: list[float]) -> dict[str, float]:
    errors = np.array(predicted) - np.array(actual)
    return {
        "mae": float(np.mean(np.abs(errors))),
        "bias": float(np.mean(errors)),
        "rmse": float(np.sqrt(np.mean(errors ** 2))),
    }


def has_suspect_attendance_label(event: dict[str, object]) -> bool:
    """Large recent events with 0-1 check-ins are usually incomplete labels."""
    return int(event["approved_t0"]) >= 50 and int(event["actual_attended"]) <= 1


def main() -> None:
    args = parse_args()
    all_events = load_signup_velocity_rows()
    if len(all_events) <= args.test_events:
        raise SystemExit("Not enough events for requested temporal split")

    train_events = all_events[:-args.test_events]
    test_events = all_events[-args.test_events:]
    train_event_ids = {row["event_id"] for row in train_events}
    test_event_ids = [row["event_id"] for row in test_events]
    signup_by_event = {row["event_id"]: row for row in all_events}

    future_rate = derive_future_show_rate(train_event_ids, args.horizon)
    snapshot_rows = fetch_snapshot_rows(test_event_ids, args.horizon)
    by_event: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in snapshot_rows:
        by_event[row["event_id"]].append(row)

    print("=" * 100)
    print("TEMPORAL THREE-STAGE EVALUATION")
    print("=" * 100)
    print(
        f"Horizon: T-{args.horizon} | "
        f"Train events: {len(train_events)} | Test events: {len(test_events)} | "
        f"Derived future approval show rate: {future_rate:.2%}"
    )
    print(f"Train range: {train_events[0]['event_start']} → {train_events[-1]['event_start']}")
    print(f"Test range:  {test_events[0]['event_start']} → {test_events[-1]['event_start']}")
    if args.include_suspect_labels:
        print("Label filter: OFF (including suspect actual_attended labels)")
    else:
        print("Label filter: ON  (excluding events with approved_t0 >= 50 and actual_attended <= 1)")

    predicted = {"baseline": [], "two_stage": [], "three_stage": []}
    actual = []
    excluded_events: list[str] = []

    print("\nPer-event results:")
    print(
        f"{'Event':<32} | {'Current':>7} | {'Actual':>6} | "
        f"{'Baseline':>8} | {'TwoStage':>8} | {'ThreeStage':>10} | {'Future+':>7}"
    )
    print(f"{'-' * 32}-+-{'-' * 7}-+-{'-' * 6}-+-{'-' * 8}-+-{'-' * 8}-+-{'-' * 10}-+-{'-' * 7}")

    for event_id in test_event_ids:
        people = by_event.get(event_id, [])
        signup = signup_by_event[event_id]
        current_approved = int(signup[f"approved_t{args.horizon}"])
        if current_approved < args.min_current_approved or not people:
            continue
        if not args.include_suspect_labels and has_suspect_attendance_label(signup):
            excluded_events.append(str(signup["title"]))
            continue

        event_title = (signup["title"] or "")[:32]
        event_city = signup["city"] or ""
        event_tz = city_to_timezone(event_city, signup["title"] or "")
        event_start = signup["event_start"]

        baseline_sum = 0.0
        two_stage_sum = 0.0

        for person in people:
            applicant_tz = person["applicant_tz"] or "America/Los_Angeles"
            prior_events = int(person["prior_events"] or 0)
            prior_attended = int(person["prior_attended"] or 0)
            prior_rate = prior_attended / prior_events if prior_events > 0 else None
            page_views = int(person["page_views_snapshot"] or 0)
            notif_read = bool(person["notif_read_snapshot"])
            days_before = float(person["days_before"])

            offset = compute_tz_offset_diff(applicant_tz, event_tz, event_date=event_start)
            tz_distance = tz_distance_bucket(offset)

            baseline_sum += predict_show_probability(
                days_before_event=days_before,
                prior_attendance_rate=prior_rate,
                tz_distance=tz_distance,
                prior_events=prior_events,
                page_views=page_views,
                notification_read=notif_read,
                horizon_days=float(args.horizon),
            )

            forecasted_views, forecasted_notif_prob = forecast_engagement(
                current_page_views=page_views,
                current_notif_read=notif_read,
                horizon_days=float(args.horizon),
                tz_offset_hrs=offset,
                tz_distance=tz_distance,
                prior_events=prior_events,
            )
            two_stage_sum += predict_show_probability(
                days_before_event=days_before,
                prior_attendance_rate=prior_rate,
                tz_distance=tz_distance,
                prior_events=prior_events,
                page_views=int(round(forecasted_views)),
                notification_read=forecasted_notif_prob > 0.5,
                horizon_days=0.0,
            )

        forecasted_total = forecast_signup_count(
            current_approved=current_approved,
            horizon_days=float(args.horizon),
            approved_t14=int(signup["approved_t14"]),
            approved_t21=int(signup["approved_t21"]),
        )
        additional_approved = max(0.0, forecasted_total - current_approved)
        three_stage_sum = two_stage_sum + additional_approved * future_rate

        actual_attended = int(signup["actual_attended"])
        actual.append(actual_attended)
        predicted["baseline"].append(baseline_sum)
        predicted["two_stage"].append(two_stage_sum)
        predicted["three_stage"].append(three_stage_sum)

        print(
            f"{event_title:<32} | {current_approved:>7} | {actual_attended:>6} | "
            f"{baseline_sum:>8.1f} | {two_stage_sum:>8.1f} | {three_stage_sum:>10.1f} | {additional_approved:>7.1f}"
        )

    print("\nAggregate metrics:")
    for name in ("baseline", "two_stage", "three_stage"):
        stats = summarize(name, predicted[name], actual)
        print(
            f"  {name:<10} "
            f"MAE={stats['mae']:.1f}  Bias={stats['bias']:+.1f}  RMSE={stats['rmse']:.1f}"
        )
    if excluded_events:
        print("\nExcluded suspect-label events:")
        for title in excluded_events:
            print(f"  - {title}")


if __name__ == "__main__":
    main()
