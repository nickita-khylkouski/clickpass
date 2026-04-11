"""End-to-end backtest of three-stage forecasting pipeline.

Tests the complete prediction flow:
1. Stage 0: Forecast final approved count (signup velocity)
2. Stage 1+2: Forecast per-person engagement + predict show probability
3. Stage 3: Estimate expected attendees from future signups

Compares against baseline (no forecasting) and two-stage (engagement only).
"""
import csv
import os
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import psycopg2
import psycopg2.extras
from cv_rank.waves.model import predict_show_probability, forecast_engagement, forecast_signup_count
from cv_rank.waves.predict import (
    timing_bucket,
    prior_rate_bucket,
    city_to_timezone,
    compute_tz_offset_diff,
    tz_distance_bucket,
)


def load_signup_velocity_data():
    """Load signup velocity CSV."""
    csv_path = Path(__file__).resolve().parents[2] / "results" / "signup_velocity.csv"
    events_by_id = {}
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            events_by_id[row['event_id']] = {
                'approved_t21': int(row['approved_t21'] or 0),
                'approved_t14': int(row['approved_t14'] or 0),
                'approved_t7': int(row['approved_t7'] or 0),
                'approved_t3': int(row['approved_t3'] or 0),
                'approved_t1': int(row['approved_t1'] or 0),
                'approved_t0': int(row['approved_t0'] or 0),
                'actual_attended': int(row['actual_attended'] or 0),
            }
    return events_by_id


def main():
    dsn = os.environ["PLATFORM_DATABASE_URL"]
    conn = psycopg2.connect(dsn)

    # Load signup velocity data
    signup_data = load_signup_velocity_data()
    print(f"Loaded signup velocity data for {len(signup_data)} events\n")

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Get historical events with person-level data at T-7
        # (Simplified: only testing at T-7 horizon for now)
        horizon = 7

        print("="*100)
        print(f"END-TO-END THREE-STAGE BACKTEST AT T-{horizon}")
        print("="*100)
        print("\nFetching historical event data...\n")

        # Get events with sufficient data
        event_ids = list(signup_data.keys())
        event_ids_str = "','".join(event_ids)

        query = f"""
            WITH prior_agg AS (
                SELECT ea2."userId", pe2.id AS event_id, pe2."startDateTime" AS event_start,
                    COUNT(*) FILTER (WHERE ea2.status = 'approved') AS prior_events,
                    COALESCE(SUM(ea2."checkedIn"::int) FILTER (WHERE ea2.status = 'approved'), 0) AS prior_attended
                FROM "EventApplicant" ea2
                JOIN "PlatformEvent" pe2 ON ea2."eventId" = pe2.id
                WHERE pe2."startDateTime" IS NOT NULL
                GROUP BY ea2."userId", pe2.id, pe2."startDateTime"
            ),
            page_view_counts AS (
                SELECT i."userId", i.properties->>'eventId' AS event_id,
                       i."createdAt", COUNT(*) AS view_count
                FROM "Insight" i
                JOIN "PlatformEvent" pe_pv ON pe_pv.id::text = i.properties->>'eventId'
                WHERE i."eventName" = 'PAGE_VIEW'
                  AND i.properties->>'eventId' IS NOT NULL
                  AND i."createdAt" < pe_pv."startDateTime"
                GROUP BY i."userId", i.properties->>'eventId', i."createdAt"
            ),
            notif_reads AS (
                SELECT DISTINCT ON (pn."userId", pn.data->>'eventId')
                    pn."userId", pn.data->>'eventId' AS event_id,
                    pn.read AS notif_read, pn."createdAt"
                FROM "PlatformNotification" pn
                WHERE pn.type = 'event_application_status_change'
                  AND pn.data->>'status' = 'approved'
                ORDER BY pn."userId", pn.data->>'eventId', pn."createdAt" DESC
            )
            SELECT
                pe.id AS event_id,
                pe.title,
                pe.city,
                pe."startDateTime" AS event_start,
                ea."userId",
                ea."appliedFromTimeZone" AS applicant_tz,
                EXTRACT(EPOCH FROM (pe."startDateTime" - ea."createdAt")) / 86400.0 AS days_before,
                ea."checkedIn" AS showed_up,
                (SELECT COALESCE(SUM(pa.prior_events), 0) FROM prior_agg pa
                 WHERE pa."userId" = ea."userId" AND pa.event_start < pe."startDateTime") AS prior_events,
                (SELECT COALESCE(SUM(pa.prior_attended), 0) FROM prior_agg pa
                 WHERE pa."userId" = ea."userId" AND pa.event_start < pe."startDateTime") AS prior_attended,
                COALESCE((SELECT COUNT(*) FROM page_view_counts pv
                          WHERE pv."userId" = ea."userId" AND pv.event_id = pe.id::text
                          AND pv."createdAt" <= pe."startDateTime" - INTERVAL '{horizon} days'), 0) AS page_views_t{horizon},
                COALESCE((SELECT notif_read FROM notif_reads nr
                          WHERE nr."userId" = ea."userId" AND nr.event_id = pe.id::text
                          AND nr."createdAt" <= pe."startDateTime" - INTERVAL '{horizon} days'), false) AS notif_read_t{horizon}
            FROM "EventApplicant" ea
            JOIN "PlatformEvent" pe ON pe.id = ea."eventId"
            WHERE pe.id IN ('{event_ids_str}')
              AND ea.status = 'approved'
              AND ea."createdAt" IS NOT NULL
              AND ea."createdAt" <= pe."startDateTime" - INTERVAL '{horizon} days'
            ORDER BY pe."startDateTime", ea."createdAt"
        """

        cur.execute(query)
        rows = cur.fetchall()
        cur.close()

        print(f"Fetched {len(rows)} person-event observations at T-{horizon}\n")

        # Group by event
        by_event = defaultdict(list)
        for row in rows:
            by_event[row['event_id']].append(row)

        # Run predictions event-by-event
        results = {
            'baseline': {'predicted': [], 'actual': []},
            'two_stage': {'predicted': [], 'actual': []},
            'three_stage': {'predicted': [], 'actual': []},
        }

        for event_id, people in by_event.items():
            if len(people) < 20:  # Skip small events
                continue

            event_title = people[0]['title'] or 'Unknown'
            event_city = people[0]['city'] or 'Unknown'
            event_start = people[0]['event_start']
            event_tz = city_to_timezone(event_city, event_title)

            # Get signup data
            signup = signup_data.get(event_id)
            if not signup:
                continue

            actual_attended = signup['actual_attended']
            approved_t7 = signup['approved_t7']
            approved_t14 = signup['approved_t14']
            approved_t21 = signup['approved_t21']
            approved_t0 = signup['approved_t0']

            # Stage 0: Forecast final approved count
            forecasted_total = forecast_signup_count(
                current_approved=approved_t7,
                horizon_days=float(horizon),
                approved_t14=approved_t14,
                approved_t21=approved_t21,
            )
            additional_signups = max(0, int(forecasted_total - approved_t7))

            # Calculate predictions for each person at T-7
            baseline_p_shows = []
            two_stage_p_shows = []

            for person in people:
                applicant_tz = person['applicant_tz'] or 'America/Los_Angeles'
                days_before = float(person['days_before'])
                prior_events = int(person['prior_events'] or 0)
                prior_attended = int(person['prior_attended'] or 0)
                pv_t7 = int(person[f'page_views_t{horizon}'] or 0)
                notif_t7 = bool(person[f'notif_read_t{horizon}'])

                prior_rate = prior_attended / prior_events if prior_events > 0 else None
                offset = compute_tz_offset_diff(applicant_tz, event_tz, event_date=event_start)
                tz_dist = tz_distance_bucket(offset)

                # BASELINE: Use current incomplete engagement (no forecasting)
                p_show_baseline = predict_show_probability(
                    days_before_event=days_before,
                    prior_attendance_rate=prior_rate,
                    tz_distance=tz_dist,
                    prior_events=prior_events,
                    page_views=pv_t7,
                    notification_read=notif_t7,
                    horizon_days=float(horizon),
                )
                baseline_p_shows.append(p_show_baseline)

                # TWO-STAGE: Forecast engagement, then predict (Stage 1+2)
                forecasted_views, forecasted_notif_prob = forecast_engagement(
                    current_page_views=pv_t7,
                    current_notif_read=notif_t7,
                    horizon_days=float(horizon),
                    tz_offset_hrs=offset,
                    tz_distance=tz_dist,
                    prior_events=prior_events,
                )
                pv_for_prediction = int(round(forecasted_views))
                notif_for_prediction = forecasted_notif_prob > 0.5

                p_show_two_stage = predict_show_probability(
                    days_before_event=days_before,
                    prior_attendance_rate=prior_rate,
                    tz_distance=tz_dist,
                    prior_events=prior_events,
                    page_views=pv_for_prediction,
                    notification_read=notif_for_prediction,
                    horizon_days=0,  # Using forecasted engagement, so horizon=0
                )
                two_stage_p_shows.append(p_show_two_stage)

            # Calculate expected attendees
            expected_baseline = sum(baseline_p_shows)
            expected_two_stage = sum(two_stage_p_shows)

            # THREE-STAGE: Add estimate for future signups (Stage 3)
            late_signup_show_rate = 0.30  # From engagement profiles at T-7
            expected_from_future = additional_signups * late_signup_show_rate
            expected_three_stage = expected_two_stage + expected_from_future

            # Store results
            results['baseline']['predicted'].append(expected_baseline)
            results['baseline']['actual'].append(actual_attended)
            results['two_stage']['predicted'].append(expected_two_stage)
            results['two_stage']['actual'].append(actual_attended)
            results['three_stage']['predicted'].append(expected_three_stage)
            results['three_stage']['actual'].append(actual_attended)

        # Calculate aggregate metrics
        print("\n" + "="*100)
        print("RESULTS COMPARISON")
        print("="*100)

        for approach in ['baseline', 'two_stage', 'three_stage']:
            import numpy as np
            preds = np.array(results[approach]['predicted'])
            actuals = np.array(results[approach]['actual'])
            errors = preds - actuals

            mae = np.mean(np.abs(errors))
            bias = np.mean(errors)
            rmse = np.sqrt(np.mean(errors ** 2))

            print(f"\n{approach.upper().replace('_', ' ')} (n={len(errors)} events):")
            print(f"  MAE:  {mae:.1f} people")
            print(f"  Bias: {bias:+.1f} people {'✅ FIXED!' if abs(bias) < 10 else '❌ Still biased'}")
            print(f"  RMSE: {rmse:.1f} people")

        print("\n" + "="*100)
        print("BIAS IMPROVEMENT:")
        baseline_bias = np.mean(np.array(results['baseline']['predicted']) - np.array(results['baseline']['actual']))
        two_stage_bias = np.mean(np.array(results['two_stage']['predicted']) - np.array(results['two_stage']['actual']))
        three_stage_bias = np.mean(np.array(results['three_stage']['predicted']) - np.array(results['three_stage']['actual']))

        print(f"  Baseline → Two-Stage:   {baseline_bias:+.1f} → {two_stage_bias:+.1f} ({two_stage_bias - baseline_bias:+.1f})")
        print(f"  Two-Stage → Three-Stage: {two_stage_bias:+.1f} → {three_stage_bias:+.1f} ({three_stage_bias - two_stage_bias:+.1f})")
        print(f"  Overall Improvement:     {baseline_bias:+.1f} → {three_stage_bias:+.1f} ({three_stage_bias - baseline_bias:+.1f})")
        print("="*100)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
