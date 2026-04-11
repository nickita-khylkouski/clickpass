"""Extract application funnel snapshots plus explicit approval proxy metrics.

Applications are counted from ``EventApplicant.createdAt``.

``approved_notification_t*`` columns are incomplete proxy counts based on the
first approval notification timestamp when available.

``approved_t0`` is the production-aligned final label: the current count of
applicants whose status is ``approved`` at T0.

We intentionally do not emit backfilled "approved by horizon" status counts
from final status plus apply time because that logic is temporally invalid.
"""
# ruff: noqa: E402

from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import psycopg2
import psycopg2.extras


HORIZONS = (21, 14, 7, 3, 1)


def main() -> None:
    dsn = os.environ["PLATFORM_DATABASE_URL"]
    conn = psycopg2.connect(dsn)

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
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
                pe.type AS event_type,
                COALESCE(pe."isPlatformHackathon", false) AS is_platform_hackathon,
                pe.city,
                pe."startDateTime" AS event_start,
                COUNT(*) FILTER (
                    WHERE ea."createdAt" <= pe."startDateTime" - INTERVAL '21 days'
                ) AS applied_t21,
                COUNT(*) FILTER (
                    WHERE ea."createdAt" <= pe."startDateTime" - INTERVAL '14 days'
                ) AS applied_t14,
                COUNT(*) FILTER (
                    WHERE ea."createdAt" <= pe."startDateTime" - INTERVAL '7 days'
                ) AS applied_t7,
                COUNT(*) FILTER (
                    WHERE ea."createdAt" <= pe."startDateTime" - INTERVAL '3 days'
                ) AS applied_t3,
                COUNT(*) FILTER (
                    WHERE ea."createdAt" <= pe."startDateTime" - INTERVAL '1 day'
                ) AS applied_t1,
                COUNT(*) FILTER (
                    WHERE ea."createdAt" < pe."startDateTime"
                ) AS applied_t0,
                COUNT(*) FILTER (
                    WHERE at.approved_at <= pe."startDateTime" - INTERVAL '21 days'
                ) AS approved_notification_t21,
                COUNT(*) FILTER (
                    WHERE at.approved_at <= pe."startDateTime" - INTERVAL '14 days'
                ) AS approved_notification_t14,
                COUNT(*) FILTER (
                    WHERE at.approved_at <= pe."startDateTime" - INTERVAL '7 days'
                ) AS approved_notification_t7,
                COUNT(*) FILTER (
                    WHERE at.approved_at <= pe."startDateTime" - INTERVAL '3 days'
                ) AS approved_notification_t3,
                COUNT(*) FILTER (
                    WHERE at.approved_at <= pe."startDateTime" - INTERVAL '1 day'
                ) AS approved_notification_t1,
                COUNT(*) FILTER (
                    WHERE at.approved_at < pe."startDateTime"
                ) AS approved_notification_t0,
                COUNT(*) FILTER (
                    WHERE ea.status = 'approved'
                      AND ea."createdAt" < pe."startDateTime"
                ) AS approved_t0,
                COUNT(*) FILTER (
                    WHERE ea.status = 'approved'
                      AND ea."checkedIn" = true
                      AND ea."createdAt" < pe."startDateTime"
                ) AS actual_attended
            FROM "PlatformEvent" pe
            JOIN "EventApplicant" ea
              ON ea."eventId" = pe.id
            LEFT JOIN approval_times at
              ON at.event_id = pe.id
             AND at.applicant_user_id = ea."userId"::text
            WHERE pe."startDateTime" IS NOT NULL
              AND pe."startDateTime" < NOW()
              AND ea."createdAt" IS NOT NULL
            GROUP BY pe.id, pe.title, pe.type, pe."isPlatformHackathon", pe.city, pe."startDateTime"
            HAVING COUNT(*) FILTER (
                WHERE ea."createdAt" < pe."startDateTime"
            ) >= 20
            ORDER BY pe."startDateTime" ASC
            """
        )

        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    print(f"Extracted application/approval funnel data for {len(rows)} events")

    output_path = Path(__file__).parent.parent / "results" / "application_velocity.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    enriched_rows: list[dict[str, object]] = []
    for row in rows:
        applied = {f"t{h}": int(row[f"applied_t{h}"] or 0) for h in HORIZONS}
        applied["t0"] = int(row["applied_t0"] or 0)
        approved_notification = {
            f"t{h}": int(row[f"approved_notification_t{h}"] or 0) for h in HORIZONS
        }
        approved_notification["t0"] = int(row["approved_notification_t0"] or 0)
        final_approved = int(row["approved_t0"] or 0)

        enriched: dict[str, object] = {
            "event_id": row["event_id"],
            "title": row["title"],
            "event_type": row["event_type"],
            "is_platform_hackathon": bool(row["is_platform_hackathon"]),
            "city": row["city"],
            "event_start": row["event_start"],
            "actual_attended": int(row["actual_attended"] or 0),
            "approved_t0": final_approved,
            "approved_notification_t0": approved_notification["t0"],
            "approval_notification_gap_t0": final_approved - approved_notification["t0"],
            "approval_count_gap_t0": final_approved - approved_notification["t0"],
        }

        for horizon in (*HORIZONS, 0):
            applied_key = f"applied_t{horizon}"
            applied_count = applied[f"t{horizon}"]
            enriched[applied_key] = applied_count
            if horizon != 0:
                approved_count = approved_notification[f"t{horizon}"]
                enriched[f"approved_notification_t{horizon}"] = approved_count
                enriched[f"approval_notification_rate_t{horizon}"] = (
                    round(approved_count / applied_count, 4) if applied_count > 0 else 0.0
                )

        enriched["approval_rate_t0"] = (
            round(final_approved / applied["t0"], 4) if applied["t0"] > 0 else 0.0
        )
        enriched["approval_notification_rate_t0"] = (
            round(approved_notification["t0"] / applied["t0"], 4) if applied["t0"] > 0 else 0.0
        )

        enriched["applied_growth_t7_to_t0"] = (
            round(applied["t0"] / applied["t7"], 4) if applied["t7"] > 0 else 0.0
        )
        enriched["approved_notification_to_final_growth_t7_to_t0"] = (
            round(final_approved / approved_notification["t7"], 4)
            if approved_notification["t7"] > 0
            else 0.0
        )
        enriched["applied_growth_t3_to_t0"] = (
            round(applied["t0"] / applied["t3"], 4) if applied["t3"] > 0 else 0.0
        )
        enriched["approved_notification_to_final_growth_t3_to_t0"] = (
            round(final_approved / approved_notification["t3"], 4)
            if approved_notification["t3"] > 0
            else 0.0
        )
        enriched["show_rate_from_final_approved_t0"] = (
            round(int(row["actual_attended"] or 0) / final_approved, 4) if final_approved > 0 else 0.0
        )

        enriched_rows.append(enriched)

    enriched_rows.sort(key=lambda item: item["event_start"])

    fieldnames = [
        "event_id",
        "title",
        "event_type",
        "is_platform_hackathon",
        "city",
        "event_start",
        "actual_attended",
        "applied_t21",
        "applied_t14",
        "applied_t7",
        "applied_t3",
        "applied_t1",
        "applied_t0",
        "approved_t0",
        "approved_notification_t21",
        "approved_notification_t14",
        "approved_notification_t7",
        "approved_notification_t3",
        "approved_notification_t1",
        "approved_notification_t0",
        "approval_notification_gap_t0",
        "approval_count_gap_t0",
        "approval_rate_t0",
        "approval_notification_rate_t21",
        "approval_notification_rate_t14",
        "approval_notification_rate_t7",
        "approval_notification_rate_t3",
        "approval_notification_rate_t1",
        "approval_notification_rate_t0",
        "applied_growth_t7_to_t0",
        "approved_notification_to_final_growth_t7_to_t0",
        "applied_growth_t3_to_t0",
        "approved_notification_to_final_growth_t3_to_t0",
        "show_rate_from_final_approved_t0",
    ]

    with output_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(enriched_rows)

    large_gap = [
        row for row in enriched_rows
        if abs(int(row["approval_notification_gap_t0"])) > 3
    ]
    print(f"Saved funnel artifact to {output_path}")
    print(f"Events with >3 approval-count gap between notifications and current status: {len(large_gap)}")
    for row in sorted(
        large_gap,
        key=lambda item: abs(int(item["approval_notification_gap_t0"])),
        reverse=True,
    )[:10]:
        print(
            f"  {str(row['title'])[:50]:<50} "
            f"notif_proxy_approved={row['approved_notification_t0']:<4} "
            f"final_status_approved={row['approved_t0']:<4} "
            f"gap={row['approval_notification_gap_t0']:+d}"
        )


if __name__ == "__main__":
    main()
