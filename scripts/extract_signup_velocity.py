"""Extract signup velocity data: approved counts at T-14, T-7, T-3, T-1, T-0 for all events."""
# ruff: noqa: E402
import csv
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import psycopg2
import psycopg2.extras

from cv_rank.waves.event_strength import compute_event_strength_at_cutoffs, load_first_submission_map


OUTPUT_HORIZONS = (21, 14, 7, 3, 1, 0)


def fetch_applicant_strength_rows(conn, event_ids: list[str]) -> list[dict]:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT
            ea."eventId"::text AS event_id,
            ea."userId"::text AS user_id,
            ea."createdAt" AS applied_at,
            (ea."utmTrackingId" IS NOT NULL) AS tracked_application
        FROM "EventApplicant" ea
        WHERE ea."eventId"::text = ANY(%s)
          AND ea."createdAt" IS NOT NULL
        """,
        (event_ids,),
    )
    rows = cur.fetchall()
    cur.close()
    return list(rows)


def fetch_event_invitation_rows(conn, event_ids: list[str]) -> list[dict]:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT
            ei."eventId"::text AS event_id,
            ei."userId"::text AS user_id,
            ei."createdAt" AS invited_at
        FROM "EventInvitation" ei
        WHERE ei."eventId"::text = ANY(%s)
          AND ei."invitationType"::text = 'attend'
          AND ei."createdAt" IS NOT NULL
        """,
        (event_ids,),
    )
    rows = cur.fetchall()
    cur.close()
    return list(rows)


def main():
    dsn = os.environ["PLATFORM_DATABASE_URL"]
    conn = psycopg2.connect(dsn)

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Get signup counts at different horizons for all events
        cur.execute("""
            WITH approval_times AS (
                SELECT
                    (pn.data->>'eventId')::uuid AS event_id,
                    COALESCE(
                        pn.data->>'applicantUserId',
                        pn."userId"::text
                    ) AS applicant_user_id,
                    MIN(pn."createdAt") AS approved_at
                FROM "PlatformNotification" pn
                WHERE pn.type = 'event_application_status_change'
                  AND pn.data->>'status' = 'approved'
                  AND pn.data->>'eventId' IS NOT NULL
                GROUP BY 1, 2
            )
            SELECT
                pe.id AS event_id,
                pe.title,
                pe.description,
                pe."descriptionSummary" AS description_summary,
                pe.details,
                pe.type AS event_type,
                COALESCE(pe."isPlatformHackathon", false) AS is_platform_hackathon,
                COALESCE(pe."approvalRequired", false) AS approval_required,
                COALESCE(pe.capacity, 0) AS capacity,
                pe.city,
                pe."startDateTime" AS event_start,
                COUNT(*) FILTER (WHERE at.approved_at <= pe."startDateTime" - INTERVAL '21 days') AS approved_t21,
                COUNT(*) FILTER (WHERE at.approved_at <= pe."startDateTime" - INTERVAL '14 days') AS approved_t14,
                COUNT(*) FILTER (WHERE at.approved_at <= pe."startDateTime" - INTERVAL '7 days') AS approved_t7,
                COUNT(*) FILTER (WHERE at.approved_at <= pe."startDateTime" - INTERVAL '3 days') AS approved_t3,
                COUNT(*) FILTER (WHERE at.approved_at <= pe."startDateTime" - INTERVAL '1 day') AS approved_t1,
                COUNT(*) FILTER (WHERE at.approved_at < pe."startDateTime") AS approved_t0,
                COUNT(*) FILTER (
                    WHERE ea.status = 'approved'
                      AND ea."checkedIn" = true
                      AND ea."createdAt" < pe."startDateTime"
                ) AS actual_attended
            FROM "PlatformEvent" pe
            JOIN "EventApplicant" ea ON ea."eventId" = pe.id
            LEFT JOIN approval_times at
              ON at.event_id = pe.id
             AND at.applicant_user_id = ea."userId"::text
            WHERE ea.status = 'approved'
              AND pe."startDateTime" IS NOT NULL
              AND pe."startDateTime" < NOW()  -- Only past events
              AND ea."createdAt" IS NOT NULL
            GROUP BY
                pe.id,
                pe.title,
                pe.description,
                pe."descriptionSummary",
                pe.details,
                pe.type,
                pe."isPlatformHackathon",
                pe."approvalRequired",
                pe.capacity,
                pe.city,
                pe."startDateTime"
            HAVING COUNT(*) FILTER (WHERE at.approved_at < pe."startDateTime") >= 20  -- Events with 20+ approvals
            ORDER BY pe."startDateTime" DESC
        """)

        events = cur.fetchall()
        cur.close()

        print(f"Extracted signup velocity data for {len(events)} events\n")

        event_ids = [str(event["event_id"]) for event in events]
        applicant_rows = fetch_applicant_strength_rows(conn, event_ids)
        invitation_rows = fetch_event_invitation_rows(conn, event_ids)
        first_submission_map = load_first_submission_map()

        applicants_by_event: dict[str, list[dict]] = {}
        for row in applicant_rows:
            applicants_by_event.setdefault(str(row["event_id"]), []).append(row)

        invites_by_event: dict[str, list[dict]] = {}
        for row in invitation_rows:
            invites_by_event.setdefault(str(row["event_id"]), []).append(row)

        # Calculate velocity and acceleration metrics
        enriched_events = []
        for ev in events:
            t21 = ev['approved_t21'] or 0
            t14 = ev['approved_t14'] or 0
            t7 = ev['approved_t7'] or 0
            t3 = ev['approved_t3'] or 0
            t1 = ev['approved_t1'] or 0
            t0 = ev['approved_t0'] or 0
            attended = ev['actual_attended'] or 0

            # Velocity (signups per day)
            velocity_21_to_14 = (t14 - t21) / 7 if t21 > 0 else 0
            velocity_14_to_7 = (t7 - t14) / 7 if t14 > 0 else 0
            velocity_7_to_3 = (t3 - t7) / 4 if t7 > 0 else 0
            velocity_3_to_1 = (t1 - t3) / 2 if t3 > 0 else 0
            velocity_1_to_0 = (t0 - t1) / 1 if t1 > 0 else 0

            # Acceleration (change in velocity)
            accel_14_to_7 = velocity_14_to_7 - velocity_21_to_14 if velocity_21_to_14 > 0 else 0

            # Growth multipliers
            growth_t14_to_t0 = t0 / t14 if t14 > 0 else 0
            growth_t7_to_t0 = t0 / t7 if t7 > 0 else 0
            growth_t3_to_t0 = t0 / t3 if t3 > 0 else 0

            # Show rate
            show_rate = attended / t0 if t0 > 0 else 0

            event_start = pd.Timestamp(ev["event_start"])
            cutoffs = {
                f"t{horizon}": event_start if horizon == 0 else event_start - pd.Timedelta(days=horizon)
                for horizon in OUTPUT_HORIZONS
            }
            strength_counts = compute_event_strength_at_cutoffs(
                applicants_by_event.get(str(ev["event_id"]), []),
                invites_by_event.get(str(ev["event_id"]), []),
                cutoffs=cutoffs,
                first_submission_map=first_submission_map,
            )

            row = {
                'event_id': ev['event_id'],
                'title': ev['title'],
                'description': ev['description'] or "",
                'description_summary': ev['description_summary'] or "",
                'details': ev['details'] or "",
                'event_type': ev['event_type'],
                'is_platform_hackathon': bool(ev['is_platform_hackathon']),
                'approval_required': bool(ev['approval_required']),
                'capacity': ev['capacity'] or 0,
                'city': ev['city'],
                'event_start': ev['event_start'],
                'approved_t21': t21,
                'approved_t14': t14,
                'approved_t7': t7,
                'approved_t3': t3,
                'approved_t1': t1,
                'approved_t0': t0,
                'actual_attended': attended,
                'show_rate': round(show_rate, 3),
                'velocity_21_to_14': round(velocity_21_to_14, 2),
                'velocity_14_to_7': round(velocity_14_to_7, 2),
                'velocity_7_to_3': round(velocity_7_to_3, 2),
                'velocity_3_to_1': round(velocity_3_to_1, 2),
                'velocity_1_to_0': round(velocity_1_to_0, 2),
                'accel_14_to_7': round(accel_14_to_7, 2),
                'growth_t14_to_t0': round(growth_t14_to_t0, 2),
                'growth_t7_to_t0': round(growth_t7_to_t0, 2),
                'growth_t3_to_t0': round(growth_t3_to_t0, 2),
                'additional_t7_to_t0': t0 - t7,
                'additional_t3_to_t0': t0 - t3,
            }
            row.update(strength_counts)
            enriched_events.append(row)

        # Print summary statistics
        print("="*100)
        print("SIGNUP VELOCITY ANALYSIS")
        print("="*100)

        # Overall growth patterns
        growth_t7_to_t0_values = [e['growth_t7_to_t0'] for e in enriched_events if e['approved_t7'] >= 50]
        growth_t3_to_t0_values = [e['growth_t3_to_t0'] for e in enriched_events if e['approved_t3'] >= 50]

        print("\nGrowth Multipliers (events with 50+ approvals at horizon):")
        print(f"  T-7 → T-0:  mean={np.mean(growth_t7_to_t0_values):.2f}, median={np.median(growth_t7_to_t0_values):.2f}, std={np.std(growth_t7_to_t0_values):.2f}")
        print(f"  T-3 → T-0:  mean={np.mean(growth_t3_to_t0_values):.2f}, median={np.median(growth_t3_to_t0_values):.2f}, std={np.std(growth_t3_to_t0_values):.2f}")

        # Top 10 events by size
        print("\nTop 10 Events by Final Approved Count:")
        print(f"{'Title':<40} {'T-7':>5} {'T-3':>5} {'T-1':>5} {'T-0':>5} {'Growth':>7} {'Attended':>8} {'Show%':>6}")
        print("-"*100)
        for ev in sorted(enriched_events, key=lambda x: x['approved_t0'], reverse=True)[:10]:
            title = (ev['title'] or 'Unknown')[:38]
            print(f"{title:<40} {ev['approved_t7']:>5} {ev['approved_t3']:>5} {ev['approved_t1']:>5} {ev['approved_t0']:>5} "
                  f"{ev['growth_t7_to_t0']:>7.2f} {ev['actual_attended']:>8} {ev['show_rate']:>6.1%}")

        # Save to CSV
        output_path = Path(__file__).parent.parent / "results" / "signup_velocity.csv"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        fields = [
            'event_id', 'title', 'description', 'description_summary', 'details', 'event_type',
            'is_platform_hackathon', 'approval_required', 'capacity', 'city', 'event_start',
            'approved_t21', 'approved_t14', 'approved_t7', 'approved_t3', 'approved_t1', 'approved_t0',
            'actual_attended', 'show_rate',
            'velocity_21_to_14', 'velocity_14_to_7', 'velocity_7_to_3', 'velocity_3_to_1', 'velocity_1_to_0',
            'accel_14_to_7',
            'growth_t14_to_t0', 'growth_t7_to_t0', 'growth_t3_to_t0',
            'additional_t7_to_t0', 'additional_t3_to_t0',
        ]
        for prefix in (
            "invited",
            "linked_invited",
            "linked_invited_applicant",
            "tracked_applied",
            "repeat_builder_applied",
        ):
            for horizon in OUTPUT_HORIZONS:
                fields.append(f"{prefix}_t{horizon}")

        with open(output_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(enriched_events)

        print(f"\n{'='*100}")
        print(f"Saved signup velocity data to: {output_path}")
        print(f"Total events: {len(enriched_events)}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
