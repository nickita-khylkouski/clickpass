"""Predict upcoming event attendance with Waves v2 artifacts."""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import numpy as np
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import psycopg2
import psycopg2.extras

from cv_rank.waves.v2_pipeline import (
    build_attendance_frame,
    horizon_bucket_name,
    is_hackathon_event,
    load_artifact_bundle,
    predict_attendance_probabilities,
    predict_signup_totals,
)
from cv_rank.waves.event_strength import compute_event_strength_at_cutoffs, load_first_submission_map
from cv_rank.waves.posthog_features import build_live_stage0_features
from cv_rank.waves.stage0_funnel import build_live_signup_row, predict_hybrid_signup_totals


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", default="results/waves_v2/latest")
    parser.add_argument("--days-ahead", type=int, default=14)
    parser.add_argument("--output-csv", default="results/upcoming_predictions_v2.csv")
    parser.add_argument("--include-non-hackathons", action="store_true")
    return parser.parse_args()


def horizon_training_bucket(horizon_days: float) -> int:
    if horizon_days >= 10:
        return 14
    if horizon_days >= 5:
        return 7
    if horizon_days >= 2:
        return 3
    return 1

def fetch_upcoming_events(conn, days_ahead: int) -> list[dict]:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        f"""
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
            pe.id,
            pe.slug,
            pe.title,
            pe.description,
            pe."descriptionSummary",
            pe.details,
            pe.city,
            pe."startDateTime",
            pe."isPlatformHackathon",
            pe."approvalRequired",
            pe.capacity,
            COUNT(*) FILTER (WHERE ea."createdAt" <= NOW()) AS applied_count,
            COUNT(*) FILTER (WHERE ea."createdAt" <= NOW() - INTERVAL '14 days') AS applied_ago_14d,
            COUNT(*) FILTER (WHERE ea."createdAt" <= NOW() - INTERVAL '11 days') AS applied_ago_11d,
            COUNT(*) FILTER (WHERE ea."createdAt" <= NOW() - INTERVAL '7 days') AS applied_ago_7d,
            COUNT(*) FILTER (WHERE ea."createdAt" <= NOW() - INTERVAL '6 days') AS applied_ago_6d,
            COUNT(*) FILTER (WHERE ea."createdAt" <= NOW() - INTERVAL '4 days') AS applied_ago_4d,
            COUNT(*) FILTER (WHERE ea."createdAt" <= NOW() - INTERVAL '2 days') AS applied_ago_2d,
            COUNT(*) FILTER (WHERE at.approved_at <= NOW()) AS approved_count,
            COUNT(*) FILTER (WHERE at.approved_at <= NOW() - INTERVAL '14 days') AS approved_ago_14d,
            COUNT(*) FILTER (WHERE at.approved_at <= NOW() - INTERVAL '11 days') AS approved_ago_11d,
            COUNT(*) FILTER (WHERE at.approved_at <= NOW() - INTERVAL '7 days') AS approved_ago_7d,
            COUNT(*) FILTER (WHERE at.approved_at <= NOW() - INTERVAL '6 days') AS approved_ago_6d,
            COUNT(*) FILTER (WHERE at.approved_at <= NOW() - INTERVAL '4 days') AS approved_ago_4d,
            COUNT(*) FILTER (WHERE at.approved_at <= NOW() - INTERVAL '2 days') AS approved_ago_2d,
            COUNT(*) FILTER (WHERE ea.status = 'approved') AS approved_status_count
        FROM "PlatformEvent" pe
        JOIN "EventApplicant" ea ON ea."eventId" = pe.id
        LEFT JOIN approval_times at
          ON at.event_id = pe.id
         AND at.applicant_user_id = ea."userId"::text
        WHERE pe."startDateTime" >= NOW()
          AND pe."startDateTime" <= NOW() + INTERVAL '{int(days_ahead)} days'
        GROUP BY
            pe.id,
            pe.slug,
            pe.title,
            pe.description,
            pe."descriptionSummary",
            pe.details,
            pe.city,
            pe."startDateTime",
            pe."isPlatformHackathon",
            pe."approvalRequired",
            pe.capacity
        HAVING COUNT(*) FILTER (WHERE ea.status = 'approved') >= 5
        ORDER BY pe."startDateTime"
        """
    )
    rows = cur.fetchall()
    cur.close()
    return list(rows)


def augment_upcoming_events_with_strength_features(
    conn,
    events: list[dict],
    *,
    reference_time: datetime,
) -> list[dict]:
    if not events:
        return events

    event_ids = [str(event["id"]) for event in events]
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
    applicant_rows = cur.fetchall()
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
    invite_rows = cur.fetchall()
    cur.close()

    applicants_by_event: dict[str, list[dict]] = {}
    for row in applicant_rows:
        applicants_by_event.setdefault(str(row["event_id"]), []).append(row)

    invites_by_event: dict[str, list[dict]] = {}
    for row in invite_rows:
        invites_by_event.setdefault(str(row["event_id"]), []).append(row)

    first_submission_map = load_first_submission_map()
    cutoffs = {
        "now": reference_time,
        "ago_14d": reference_time - pd.Timedelta(days=14),
        "ago_11d": reference_time - pd.Timedelta(days=11),
        "ago_7d": reference_time - pd.Timedelta(days=7),
        "ago_6d": reference_time - pd.Timedelta(days=6),
        "ago_4d": reference_time - pd.Timedelta(days=4),
        "ago_2d": reference_time - pd.Timedelta(days=2),
    }

    for event in events:
        event.update(
            compute_event_strength_at_cutoffs(
                applicants_by_event.get(str(event["id"]), []),
                invites_by_event.get(str(event["id"]), []),
                cutoffs=cutoffs,
                first_submission_map=first_submission_map,
            )
        )
    return events


def fetch_current_attendance_rows(conn, event_id: str) -> pd.DataFrame:
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
              AND pn.data->>'eventId' = %s::text
            GROUP BY 1, 2
        ),
        prior_agg AS (
            SELECT ea2."userId", pe2.id AS event_id, pe2."startDateTime" AS event_start,
                COUNT(*) FILTER (WHERE ea2.status = 'approved') AS prior_events,
                COALESCE(SUM(ea2."checkedIn"::int) FILTER (WHERE ea2.status = 'approved'), 0) AS prior_attended
            FROM "EventApplicant" ea2
            JOIN "PlatformEvent" pe2 ON pe2.id = ea2."eventId"
            WHERE pe2."startDateTime" IS NOT NULL
            GROUP BY ea2."userId", pe2.id, pe2."startDateTime"
        ),
        page_view_events AS (
            SELECT
                i."userId"::text AS user_id,
                i.properties->>'eventId' AS event_id,
                i."createdAt" AS view_at
            FROM "Insight" i
            WHERE i."eventName" = 'PAGE_VIEW'
              AND i.properties->>'eventId' = %s::text
              AND i."createdAt" <= NOW()
        ),
        page_view_counts AS (
            SELECT
                pve.user_id,
                pve.event_id,
                at.approved_at,
                COUNT(*) AS page_views,
                COUNT(*) FILTER (WHERE pve.view_at >= NOW() - INTERVAL '7 days') AS recent_view_count_7d,
                COUNT(*) FILTER (WHERE pve.view_at >= at.approved_at) AS views_after_approval,
                EXTRACT(EPOCH FROM (NOW() - MAX(pve.view_at))) / 86400.0 AS days_since_last_view,
                COUNT(DISTINCT DATE(pve.view_at)) AS distinct_active_view_days,
                EXTRACT(EPOCH FROM (MIN(pve.view_at) FILTER (WHERE pve.view_at >= at.approved_at) - at.approved_at)) / 3600.0 AS approval_to_first_view_hours
            FROM page_view_events pve
            JOIN approval_times at
              ON at.event_id::text = pve.event_id
             AND at.applicant_user_id = pve.user_id
            GROUP BY pve.user_id, pve.event_id, at.approved_at
        ),
        notif_reads AS (
            SELECT
                COALESCE(pn.data->>'applicantUserId', pn."userId"::text) AS user_id,
                pn.data->>'eventId' AS event_id,
                BOOL_OR(
                    pn.read
                    AND pn."updatedAt" IS NOT NULL
                    AND pn."updatedAt" <= NOW()
                ) AS notification_read,
                EXTRACT(EPOCH FROM (
                    MIN(pn."updatedAt") FILTER (WHERE pn.read AND pn."updatedAt" <= NOW())
                    - MIN(pn."createdAt")
                )) / 3600.0 AS notification_read_latency_hours
            FROM "PlatformNotification" pn
            WHERE pn.type = 'event_application_status_change'
              AND pn.data->>'status' = 'approved'
              AND pn.data->>'eventId' = %s::text
              AND pn."createdAt" <= NOW()
            GROUP BY 1, 2
        ),
        invite_links AS (
            SELECT
                ei."eventId"::text AS event_id,
                ei."userId"::text AS user_id,
                MIN(ei."createdAt") AS first_invited_at
            FROM "EventInvitation" ei
            WHERE ei."eventId" = %s::uuid
              AND ei."userId" IS NOT NULL
              AND ei."invitationType"::text = 'attend'
              AND ei."createdAt" <= NOW()
            GROUP BY 1, 2
        )
        SELECT
            pe.id::text AS event_id,
            pe.slug AS event_slug,
            pe.title,
            pe.city,
            pe."startDateTime" AS event_start,
            ea."userId"::text AS user_id,
            ea."createdAt" AS applied_at,
            (ea."utmTrackingId" IS NOT NULL) AS tracked_application_flag,
            ea."appliedFromTimeZone" AS applicant_tz,
            EXTRACT(EPOCH FROM (pe."startDateTime" - ea."createdAt")) / 86400.0 AS days_before_event,
            0 AS showed_up,
            at.approved_at,
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
            COALESCE(pv.page_views, 0) AS page_views,
            COALESCE(pv.recent_view_count_7d, 0) AS recent_view_count_7d,
            COALESCE(pv.views_after_approval, 0) AS views_after_approval,
            COALESCE(pv.days_since_last_view, 9999.0) AS days_since_last_view,
            COALESCE(pv.distinct_active_view_days, 0) AS distinct_active_view_days,
            COALESCE(pv.approval_to_first_view_hours, 9999.0) AS approval_to_first_view_hours,
            COALESCE(nr.notification_read, false) AS notification_read,
            COALESCE(nr.notification_read_latency_hours, 9999.0) AS notification_read_latency_hours,
            COALESCE(il.first_invited_at IS NOT NULL, false) AS invited_user_flag
        FROM "EventApplicant" ea
        JOIN "PlatformEvent" pe ON pe.id = ea."eventId"
        JOIN approval_times at
          ON at.event_id = pe.id
         AND at.applicant_user_id = ea."userId"::text
        LEFT JOIN page_view_counts pv ON pv.user_id = ea."userId"::text AND pv.event_id = pe.id::text
        LEFT JOIN notif_reads nr ON nr.user_id = ea."userId"::text AND nr.event_id = pe.id::text
        LEFT JOIN invite_links il ON il.user_id = ea."userId"::text AND il.event_id = pe.id::text
        WHERE pe.id = %s
          AND ea.status = 'approved'
          AND at.approved_at <= NOW()
        ORDER BY ea."createdAt"
        """,
        (event_id, event_id, event_id, event_id, event_id),
    )
    rows = cur.fetchall()
    cur.close()
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame["notification_read_latency_hours"] = pd.to_numeric(
        frame["notification_read_latency_hours"], errors="coerce"
    ).fillna(9999.0)
    frame["tracked_application_flag"] = pd.to_numeric(
        frame["tracked_application_flag"], errors="coerce"
    ).fillna(0.0)
    frame["invited_user_flag"] = pd.to_numeric(
        frame["invited_user_flag"], errors="coerce"
    ).fillna(0.0)
    frame["approval_to_first_view_hours"] = pd.to_numeric(
        frame["approval_to_first_view_hours"], errors="coerce"
    ).fillna(9999.0)
    frame["days_since_last_view"] = pd.to_numeric(frame["days_since_last_view"], errors="coerce").fillna(9999.0)
    frame["notification_read_bucket_same_day"] = (
        frame["notification_read"].astype(bool)
        & (frame["notification_read_latency_hours"] <= 24.0)
    ).astype(int)
    frame["notification_read_bucket_one_to_three_days"] = (
        frame["notification_read"].astype(bool)
        & (frame["notification_read_latency_hours"] > 24.0)
        & (frame["notification_read_latency_hours"] <= 72.0)
    ).astype(int)
    frame["notification_read_bucket_after_three_days"] = (
        frame["notification_read"].astype(bool)
        & (frame["notification_read_latency_hours"] > 72.0)
    ).astype(int)
    return frame


def main() -> None:
    args = parse_args()
    bundle = load_artifact_bundle(Path(args.artifact_dir))
    future_show_rates = bundle["metadata"]["future_show_rates"]
    dsn = os.environ["PLATFORM_DATABASE_URL"]
    conn = psycopg2.connect(dsn)
    try:
        events = fetch_upcoming_events(conn, args.days_ahead)
        if not args.include_non_hackathons:
            events = [
                event
                for event in events
                if is_hackathon_event(
                    event.get("title"),
                    is_platform_hackathon=bool(event.get("isPlatformHackathon")),
                )
            ]
        if not events:
            print("No upcoming hackathons found.")
            return

        now_utc = datetime.now(timezone.utc)
        events = augment_upcoming_events_with_strength_features(conn, events, reference_time=now_utc)
        slug_df = build_live_stage0_features(
            event_slugs=[str(event.get("slug") or "").strip().lower() for event in events if str(event.get("slug") or "").strip()],
            reference_time=now_utc,
        )
        slug_lookup = {
            str(row["slug"]).strip().lower(): row
            for row in slug_df.to_dict("records")
        } if not slug_df.empty else {}

        output_rows = []
        for event in events:
            event_start = event["startDateTime"]
            event_start_aware = event_start.replace(tzinfo=timezone.utc) if event_start.tzinfo is None else event_start
            horizon_days = max(0.0, (event_start_aware - now_utc).total_seconds() / 86400.0)
            event["horizon_days"] = horizon_days
            slug = str(event.get("slug") or "").strip().lower()
            if slug and slug in slug_lookup:
                event.update(slug_lookup[slug])
            current_rows = fetch_current_attendance_rows(conn, str(event["id"]))
            current_approved = float(len(current_rows.index))
            event_for_signup = dict(event)
            event_for_signup["approved_count"] = current_approved
            signup_row = pd.DataFrame([build_live_signup_row(event_for_signup)])
            pred_total_approved = float(
                predict_hybrid_signup_totals(
                    signup_row,
                    bundle["signup"],
                    direct_predict_fn=predict_signup_totals,
                )[0]
            )
            additional_approved = max(0.0, pred_total_approved - current_approved)

            attendance_frame = build_attendance_frame(
                current_rows.assign(showed_up=0),
                horizon_days=horizon_days,
            )
            probs = predict_attendance_probabilities(attendance_frame, bundle["attendance"])
            pred_current_attendance = float(np.sum(probs))

            future_rate = float(future_show_rates[horizon_bucket_name(horizon_training_bucket(horizon_days))])
            pred_future_attendance = additional_approved * future_rate
            pred_total_attendance = pred_current_attendance + pred_future_attendance

            output_rows.append(
                {
                    "event_id": str(event["id"]),
                    "event_title": event["title"] or "",
                    "is_platform_hackathon": bool(event.get("isPlatformHackathon")),
                    "passes_hackathon_filter": is_hackathon_event(
                        event.get("title"),
                        is_platform_hackathon=bool(event.get("isPlatformHackathon")),
                    ),
                    "city": event["city"] or "",
                    "event_start": event_start,
                    "horizon_days": round(horizon_days, 2),
                    "training_horizon_bucket": horizon_training_bucket(horizon_days),
                    "current_approved": int(current_approved),
                    "pred_total_approved": round(pred_total_approved, 1),
                    "pred_additional_approved": round(additional_approved, 1),
                    "pred_attendance_current_approved": round(pred_current_attendance, 1),
                    "future_show_rate": round(future_rate, 4),
                    "pred_attendance_future_approved": round(pred_future_attendance, 1),
                    "pred_total_attendance": round(pred_total_attendance, 1),
                }
            )

        output_df = pd.DataFrame(output_rows)
        output_path = Path(args.output_csv)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_df.to_csv(output_path, index=False)
        print(output_df.to_string(index=False))
        print(f"\nSaved predictions to {output_path}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
