"""Extract engagement profiles by signup timing: Do late signups engage less?"""
import csv
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import psycopg2
import psycopg2.extras


def signup_bucket(days_before_event):
    """Classify signup timing into buckets."""
    if days_before_event >= 14:
        return "T-14+"
    elif days_before_event >= 7:
        return "T-14 to T-7"
    elif days_before_event >= 3:
        return "T-7 to T-3"
    elif days_before_event >= 1:
        return "T-3 to T-1"
    else:
        return "T-1 to T-0"


def main():
    dsn = os.environ["PLATFORM_DATABASE_URL"]
    conn = psycopg2.connect(dsn)

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Get per-person engagement by signup timing
        cur.execute("""
            WITH page_view_counts AS (
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
                pe.id AS event_id,
                pe.title AS event_title,
                pe."startDateTime" AS event_start,
                ea."userId",
                ea."createdAt" AS signup_time,
                EXTRACT(EPOCH FROM (pe."startDateTime" - ea."createdAt")) / 86400.0 AS days_before_event,
                COALESCE(pv.view_count, 0) AS page_views,
                COALESCE(nr.notif_read, false) AS notif_read,
                ea."checkedIn" AS showed_up
            FROM "EventApplicant" ea
            JOIN "PlatformEvent" pe ON pe.id = ea."eventId"
            LEFT JOIN page_view_counts pv ON pv."userId" = ea."userId" AND pv.event_id = pe.id::text
            LEFT JOIN notif_reads nr ON nr."userId" = ea."userId" AND nr.event_id = pe.id::text
            WHERE ea.status = 'approved'
              AND pe."startDateTime" IS NOT NULL
              AND pe."startDateTime" < NOW()
              AND ea."createdAt" IS NOT NULL
              AND ea."createdAt" < pe."startDateTime"
            ORDER BY pe."startDateTime" DESC, ea."createdAt"
        """)

        rows = cur.fetchall()
        cur.close()

        print(f"Extracted {len(rows)} person-event observations\n")

        # Classify into signup timing buckets
        by_bucket = {
            "T-14+": [],
            "T-14 to T-7": [],
            "T-7 to T-3": [],
            "T-3 to T-1": [],
            "T-1 to T-0": [],
        }

        person_records = []
        for row in rows:
            days_before = row['days_before_event']
            bucket = signup_bucket(days_before)

            views = int(row['page_views'] or 0)
            notif = bool(row['notif_read'])
            showed = bool(row['showed_up'])

            record = {
                'event_id': row['event_id'],
                'event_title': row['event_title'],
                'signup_bucket': bucket,
                'days_before_event': round(days_before, 1),
                'page_views': views,
                'notif_read': 1 if notif else 0,
                'showed_up': 1 if showed else 0,
            }

            by_bucket[bucket].append(record)
            person_records.append(record)

        # Calculate aggregate statistics by bucket
        print("="*100)
        print("ENGAGEMENT PROFILES BY SIGNUP TIMING")
        print("="*100)
        print(f"\n{'Bucket':<15} {'Count':>7} {'Avg Views':>10} {'Notif %':>8} {'Show %':>8}")
        print("-"*100)

        for bucket in ["T-14+", "T-14 to T-7", "T-7 to T-3", "T-3 to T-1", "T-1 to T-0"]:
            records = by_bucket[bucket]
            if not records:
                continue

            count = len(records)
            avg_views = sum(r['page_views'] for r in records) / count
            notif_pct = sum(r['notif_read'] for r in records) / count
            show_pct = sum(r['showed_up'] for r in records) / count

            print(f"{bucket:<15} {count:>7} {avg_views:>10.2f} {notif_pct:>8.1%} {show_pct:>8.1%}")

        # Save detailed records
        output_path = Path(__file__).parent.parent / "results" / "engagement_profiles.csv"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        fields = ['event_id', 'event_title', 'signup_bucket', 'days_before_event',
                  'page_views', 'notif_read', 'showed_up']

        with open(output_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(person_records)

        print(f"\n{'='*100}")
        print(f"Saved engagement profiles to: {output_path}")
        print(f"Total records: {len(person_records)}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
