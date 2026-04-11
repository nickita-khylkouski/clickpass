from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import psycopg2
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
LOCAL_DATA_DIR = BASE_DIR / "local_data"
AGENT_ANALYSIS_DIR = LOCAL_DATA_DIR / "agent_analysis"
DERIVED_DIR = LOCAL_DATA_DIR / "derived"
RESULTS_DIR = BASE_DIR / "results"

OUTPUT_CATALOG = {
    "events_core": {
        "filename": "events_core.csv",
        "grain": "one row per event",
        "primary_keys": ["event_id"],
        "join_keys": {
            "event_id": ["applicants_core.event_id", "event_acquisition_rollup.event_id"],
            "slug": ["signup_velocity.slug", "posthog_event_velocity.slug"],
        },
        "source_tables": [
            "PlatformEvent",
            "EventApplicant",
            "EventReminder",
            "EventNotificationBlast",
        ],
        "pii": "none",
        "recommended_uses": [
            "event-level modeling",
            "event segmentation",
            "demand and attendance analysis",
        ],
    },
    "applicants_core": {
        "filename": "applicants_core.csv",
        "grain": "one row per applicant",
        "primary_keys": ["applicant_id"],
        "join_keys": {
            "event_id": ["events_core.event_id", "event_acquisition_rollup.event_id"],
            "user_id": ["users_core.user_id", "user_hackathon_history.user_id", "applicant_messaging_rollup.user_id"],
        },
        "source_tables": [
            "EventApplicant",
            "PlatformEvent",
            "UTMTracking",
            "EventQuestionAnswer",
            "Insight",
            "PlatformNotification",
        ],
        "pii": "low",
        "pii_notes": "No email or phone exported; contains user_id and coarse geo inference fields.",
        "recommended_uses": [
            "applicant funnel analysis",
            "per-person attendance modeling",
            "lead-time and attribution analysis",
        ],
    },
    "event_acquisition_rollup": {
        "filename": "event_acquisition_rollup.csv",
        "grain": "one row per event x utm_source x utm_medium x utm_campaign",
        "primary_keys": ["event_id", "utm_source", "utm_medium", "utm_campaign"],
        "join_keys": {
            "event_id": ["events_core.event_id", "applicants_core.event_id"],
        },
        "source_tables": ["EventApplicant", "PlatformEvent", "UTMTracking"],
        "pii": "none",
        "recommended_uses": [
            "channel mix analysis",
            "acquisition cohorting",
            "event-level demand breakdowns",
        ],
    },
    "users_core": {
        "filename": "users_core.csv",
        "grain": "one row per user profile",
        "primary_keys": ["user_id"],
        "join_keys": {
            "user_id": ["applicants_core.user_id", "user_hackathon_history.user_id", "applicant_messaging_rollup.user_id"],
        },
        "source_tables": ["UserProfile"],
        "pii": "low",
        "pii_notes": "Non-PII profile summary only; no email, phone, or birthday exported.",
        "recommended_uses": [
            "user cohort enrichment",
            "profile completeness analysis",
            "hackathon participant segmentation",
        ],
    },
    "user_hackathon_history": {
        "filename": "user_hackathon_history.csv",
        "grain": "one row per user with hackathon history rollups",
        "primary_keys": ["user_id"],
        "join_keys": {
            "user_id": ["applicants_core.user_id", "users_core.user_id"],
        },
        "source_tables": [
            "HackathonTeamMember",
            "HackathonSubmission",
            "HackathonJudgingScore",
        ],
        "pii": "low",
        "pii_notes": "Contains only user_id plus aggregate hackathon history.",
        "recommended_uses": [
            "repeat builder analysis",
            "submission history features",
            "hackathon experience segmentation",
        ],
    },
    "applicant_messaging_rollup": {
        "filename": "applicant_messaging_rollup.csv",
        "grain": "one row per event x user messaging rollup",
        "primary_keys": ["event_id", "user_id"],
        "join_keys": {
            "event_id": ["events_core.event_id", "applicants_core.event_id"],
            "user_id": ["applicants_core.user_id", "users_core.user_id"],
        },
        "source_tables": ["EventApplicationChannel", "EventApplicationChannelMessage"],
        "pii": "low",
        "pii_notes": "Aggregate counts and timestamps only; no message body exported.",
        "recommended_uses": [
            "commitment and response analysis",
            "messaging-to-attendance analysis",
            "application support workload analysis",
        ],
    },
    "signup_velocity.csv": {
        "filename": "signup_velocity.csv",
        "grain": "one row per event with approval-horizon snapshots",
        "primary_keys": ["event_id"],
        "join_keys": {
            "event_id": ["events_core.event_id"],
            "slug": ["events_core.slug"],
        },
        "source_tables": ["derived artifact"],
        "pii": "none",
        "recommended_uses": [
            "Stage 0 modeling",
            "approval curve analysis",
        ],
    },
    "application_velocity.csv": {
        "filename": "application_velocity.csv",
        "grain": "one row per event with application-horizon snapshots",
        "primary_keys": ["event_id"],
        "join_keys": {
            "event_id": ["events_core.event_id"],
            "slug": ["events_core.slug"],
        },
        "source_tables": ["derived artifact"],
        "pii": "none",
        "recommended_uses": [
            "application funnel analysis",
            "approval-lag research",
        ],
    },
    "engagement_profiles.csv": {
        "filename": "engagement_profiles.csv",
        "grain": "one row per person-event snapshot",
        "primary_keys": ["event_id", "user_id"],
        "join_keys": {
            "event_id": ["events_core.event_id", "applicants_core.event_id"],
            "user_id": ["applicants_core.user_id", "users_core.user_id"],
        },
        "source_tables": ["derived artifact"],
        "pii": "low",
        "pii_notes": "Behavioral row set keyed by user_id/event_id; no direct contact info.",
        "recommended_uses": [
            "engagement bucket analysis",
            "attendance feature audits",
        ],
    },
    "posthog_event_velocity.csv": {
        "filename": "posthog_event_velocity.csv",
        "grain": "one row per event with PostHog event-horizon aggregates",
        "primary_keys": ["event_id"],
        "join_keys": {
            "event_id": ["events_core.event_id"],
            "slug": ["events_core.slug"],
        },
        "source_tables": ["derived artifact"],
        "pii": "none",
        "recommended_uses": [
            "Stage 0 demand analysis",
            "PostHog event-signal research",
        ],
    },
}


def load_env() -> None:
    load_dotenv(BASE_DIR / ".env")


def connect() -> psycopg2.extensions.connection:
    dsn = os.environ.get("PLATFORM_DATABASE_URL", "").strip()
    if not dsn:
        raise SystemExit("Missing PLATFORM_DATABASE_URL")
    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout TO 0")
    return conn


def read_sql_frame(conn: psycopg2.extensions.connection, query: str) -> pd.DataFrame:
    return pd.read_sql_query(query, conn)


def derived_path(filename: str) -> Path:
    local = DERIVED_DIR / filename
    if local.exists():
        return local
    return RESULTS_DIR / filename


def build_events_core(conn: psycopg2.extensions.connection) -> pd.DataFrame:
    query = """
    with applicant_stats as (
      select
        ea."eventId"::text as event_id,
        count(*) as applicant_count,
        count(*) filter (where ea.status::text = 'approved') as approved_count,
        count(*) filter (where ea."checkedIn" = true) as checkedin_count,
        count(*) filter (where ea.status::text = 'waitlisted') as waitlisted_count,
        count(*) filter (where ea.status::text = 'rejected') as rejected_count,
        count(*) filter (where ea.status::text = 'pending') as pending_count,
        min(ea."createdAt") as first_apply_at,
        max(ea."createdAt") as last_apply_at
      from "EventApplicant" ea
      group by 1
    ),
    reminder_stats as (
      select
        er."eventId"::text as event_id,
        count(*) as reminder_count,
        count(*) filter (where er."isEmail" = true) as email_reminder_count,
        count(*) filter (where er."isText" = true) as text_reminder_count
      from "EventReminder" er
      group by 1
    ),
    blast_stats as (
      select
        enb."eventId"::text as event_id,
        count(*) as blast_count,
        count(*) filter (where enb."isEmail" = true) as email_blast_count,
        count(*) filter (where enb."isSMS" = true) as sms_blast_count,
        min(enb."scheduledAt") as first_blast_at,
        max(coalesce(enb."sentAt", enb."scheduledAt")) as last_blast_at
      from "EventNotificationBlast" enb
      group by 1
    )
    select
      pe.id::text as event_id,
      pe.slug,
      pe.title,
      pe."startDateTime" as event_start,
      pe."endDateTime" as event_end,
      pe.city,
      pe.type,
      pe."approvalRequired" as approval_required,
      pe.capacity,
      pe."isPlatformHackathon" as is_platform_hackathon,
      pe.published,
      pe."registrationClosed" as registration_closed,
      pe."showGuestListBeforeApproval" as show_guest_list_before_approval,
      pe."showLocationBeforeApproval" as show_location_before_approval,
      coalesce(a.applicant_count, 0) as applicant_count,
      coalesce(a.approved_count, 0) as approved_count,
      coalesce(a.checkedin_count, 0) as checkedin_count,
      coalesce(a.waitlisted_count, 0) as waitlisted_count,
      coalesce(a.rejected_count, 0) as rejected_count,
      coalesce(a.pending_count, 0) as pending_count,
      a.first_apply_at,
      a.last_apply_at,
      coalesce(r.reminder_count, 0) as reminder_count,
      coalesce(r.email_reminder_count, 0) as email_reminder_count,
      coalesce(r.text_reminder_count, 0) as text_reminder_count,
      coalesce(b.blast_count, 0) as blast_count,
      coalesce(b.email_blast_count, 0) as email_blast_count,
      coalesce(b.sms_blast_count, 0) as sms_blast_count,
      b.first_blast_at,
      b.last_blast_at
    from "PlatformEvent" pe
    left join applicant_stats a on a.event_id = pe.id::text
    left join reminder_stats r on r.event_id = pe.id::text
    left join blast_stats b on b.event_id = pe.id::text
    where coalesce(a.applicant_count, 0) > 0
    order by pe."startDateTime" asc
    """
    frame = read_sql_frame(conn, query)

    for filename in ["signup_velocity.csv", "application_velocity.csv", "posthog_event_velocity.csv"]:
        path = derived_path(filename)
        if path.exists():
            extra = pd.read_csv(path)
            if "event_id" in extra.columns:
                frame = frame.merge(extra, on="event_id", how="left", suffixes=("", "_dup"))
            elif "slug" in extra.columns:
                frame = frame.merge(extra, on="slug", how="left", suffixes=("", "_dup"))
            dup_cols = [col for col in frame.columns if col.endswith("_dup")]
            if dup_cols:
                frame = frame.drop(columns=dup_cols)
    return frame


def build_applicants_core(conn: psycopg2.extensions.connection) -> pd.DataFrame:
    query = """
    with answer_stats as (
      select
        eqa."applicantId"::text as applicant_id,
        count(*) as answer_count,
        sum(length(coalesce(eqa.answer, ''))) as answer_chars,
        avg(length(coalesce(eqa.answer, ''))) as avg_answer_chars,
        max(length(coalesce(eqa.answer, ''))) as max_answer_chars
      from "EventQuestionAnswer" eqa
      group by 1
    ),
    insight_stats as (
      select
        i."userId" as user_id,
        i.properties->>'eventId' as event_id,
        count(*) as insight_count,
        count(*) filter (where i."eventName"::text = 'PAGE_VIEW') as page_view_count,
        count(distinct date(i."createdAt")) as active_view_days,
        min(i."createdAt") as first_view_at,
        max(i."createdAt") as last_view_at
      from "Insight" i
      where coalesce(i."userId", '') <> ''
        and coalesce(i.properties->>'eventId', '') <> ''
      group by 1, 2
    ),
    notif_stats as (
      select
        coalesce(nullif(pn.data->>'applicantUserId', ''), pn."userId") as user_id,
        pn.data->>'eventId' as event_id,
        count(*) as notif_count,
        count(*) filter (where pn.read = true) as notif_read_count,
        min(pn."createdAt") as first_notif_at,
        max(pn."createdAt") as last_notif_at,
        min(pn."createdAt") filter (
          where pn.type::text = 'event_application_status_change'
            and coalesce(pn.data->>'status', '') = 'approved'
        ) as first_approval_notif_at,
        min(pn."updatedAt") filter (where pn.read = true) as first_notif_read_at
      from "PlatformNotification" pn
      where coalesce(pn.data->>'eventId', '') <> ''
        and coalesce(nullif(pn.data->>'applicantUserId', ''), pn."userId", '') <> ''
      group by 1, 2
    )
    select
      ea.id::text as applicant_id,
      ea."eventId"::text as event_id,
      pe.slug as event_slug,
      pe.title as event_title,
      pe."startDateTime" as event_start,
      pe.city as event_city,
      pe.type as event_type,
      pe."isPlatformHackathon" as event_is_platform_hackathon,
      ea."userId" as user_id,
      ea.status::text as status,
      ea."checkedIn" as checked_in,
      ea."createdAt" as applied_at,
      ea."updatedAt" as applicant_updated_at,
      ea."appliedFromTimeZone" as applied_from_time_zone,
      ea."inferredCountryCode" as inferred_country_code,
      ea."inferredRegion" as inferred_region,
      ea."inferredCity" as inferred_city,
      ut.utm_source,
      ut.utm_medium,
      ut.utm_campaign,
      ut.utm_term,
      ut.utm_content,
      coalesce(ans.answer_count, 0) as answer_count,
      coalesce(ans.answer_chars, 0) as answer_chars,
      coalesce(ans.avg_answer_chars, 0) as avg_answer_chars,
      coalesce(ans.max_answer_chars, 0) as max_answer_chars,
      coalesce(ins.insight_count, 0) as insight_count,
      coalesce(ins.page_view_count, 0) as page_view_count,
      coalesce(ins.active_view_days, 0) as active_view_days,
      ins.first_view_at,
      ins.last_view_at,
      coalesce(ns.notif_count, 0) as notif_count,
      coalesce(ns.notif_read_count, 0) as notif_read_count,
      ns.first_notif_at,
      ns.last_notif_at,
      ns.first_approval_notif_at,
      ns.first_notif_read_at,
      extract(epoch from (pe."startDateTime" - ea."createdAt")) / 86400.0 as apply_lead_days
    from "EventApplicant" ea
    join "PlatformEvent" pe on pe.id = ea."eventId"
    left join "UTMTracking" ut on ut.id = ea."utmTrackingId"
    left join answer_stats ans on ans.applicant_id = ea.id::text
    left join insight_stats ins on ins.user_id = ea."userId" and ins.event_id = ea."eventId"::text
    left join notif_stats ns on ns.user_id = ea."userId" and ns.event_id = ea."eventId"::text
    order by pe."startDateTime" asc, ea."createdAt" asc
    """
    return read_sql_frame(conn, query)


def build_users_core(conn: psycopg2.extensions.connection) -> pd.DataFrame:
    query = """
    select
      up."userId" as user_id,
      up."createdAt" as profile_created_at,
      up.location,
      up."isClaimed" as is_claimed,
      up."isOrganizationAccount" as is_organization_account,
      up."isAdmin" as is_admin,
      up."emailVerified" as email_verified,
      up."phoneNumberVerified" as phone_verified,
      (coalesce(up.handle, '') <> '') as has_handle,
      (coalesce(up."githubUsername", '') <> '') as has_github,
      (coalesce(up."linkedinUsername", '') <> '') as has_linkedin,
      (coalesce(up."xHandle", '') <> '') as has_x,
      length(coalesce(up.description, '')) as description_chars,
      case
        when jsonb_typeof(up."externalLinks") = 'array' then jsonb_array_length(up."externalLinks")
        else 0
      end as external_link_count
    from "UserProfile" up
    order by up."createdAt" asc
    """
    return read_sql_frame(conn, query)


def build_event_acquisition_rollup(conn: psycopg2.extensions.connection) -> pd.DataFrame:
    query = """
    select
      ea."eventId"::text as event_id,
      pe.slug as event_slug,
      coalesce(ut.utm_source, '') as utm_source,
      coalesce(ut.utm_medium, '') as utm_medium,
      coalesce(ut.utm_campaign, '') as utm_campaign,
      count(*) as applicant_count,
      count(*) filter (where ea.status::text = 'approved') as approved_count,
      count(*) filter (where ea."checkedIn" = true) as checkedin_count
    from "EventApplicant" ea
    join "PlatformEvent" pe on pe.id = ea."eventId"
    left join "UTMTracking" ut on ut.id = ea."utmTrackingId"
    group by 1, 2, 3, 4, 5
    having count(*) > 0
    order by pe.slug, applicant_count desc
    """
    return read_sql_frame(conn, query)


def build_user_hackathon_history(conn: psycopg2.extensions.connection) -> pd.DataFrame:
    query = """
    with submission_history as (
      select
        htm."userId" as user_id,
        count(distinct htm."submissionId") as submission_count,
        count(distinct hs."eventId") as submission_event_count,
        min(hs."createdAt") as first_submission_at,
        max(hs."createdAt") as last_submission_at
      from "HackathonTeamMember" htm
      join "HackathonSubmission" hs on hs.id = htm."submissionId"
      group by 1
    ),
    score_history as (
      select
        htm."userId" as user_id,
        count(*) as judging_score_count,
        avg(hjs.score::float) as avg_judging_score,
        max(hjs.score::float) as max_judging_score
      from "HackathonTeamMember" htm
      join "HackathonJudgingScore" hjs on hjs."submissionId" = htm."submissionId"
      group by 1
    )
    select
      coalesce(sh.user_id, sc.user_id) as user_id,
      coalesce(sh.submission_count, 0) as submission_count,
      coalesce(sh.submission_event_count, 0) as submission_event_count,
      sh.first_submission_at,
      sh.last_submission_at,
      coalesce(sc.judging_score_count, 0) as judging_score_count,
      coalesce(sc.avg_judging_score, 0) as avg_judging_score,
      coalesce(sc.max_judging_score, 0) as max_judging_score
    from submission_history sh
    full outer join score_history sc on sc.user_id = sh.user_id
    order by 1
    """
    return read_sql_frame(conn, query)


def build_applicant_messaging_rollup(conn: psycopg2.extensions.connection) -> pd.DataFrame:
    query = """
    select
      eac."eventId"::text as event_id,
      eac."userId" as user_id,
      count(*) as message_count,
      count(*) filter (where eacm.role::text = 'USER') as user_message_count,
      count(*) filter (where eacm.role::text <> 'USER') as non_user_message_count,
      min(eacm."createdAt") as first_message_at,
      max(eacm."createdAt") as last_message_at
    from "EventApplicationChannel" eac
    join "EventApplicationChannelMessage" eacm on eacm."channelId" = eac.id
    group by 1, 2
    order by 1, 2
    """
    return read_sql_frame(conn, query)


def write_frame(df: pd.DataFrame, path: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return {
        "path": str(path.relative_to(BASE_DIR)),
        "rows": int(len(df)),
        "columns": int(len(df.columns)),
        "bytes": path.stat().st_size,
    }


def copy_if_exists(src: Path, dst: Path) -> dict | None:
    if not src.exists():
        return None
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return {
        "path": str(dst.relative_to(BASE_DIR)),
        "bytes": dst.stat().st_size,
    }


def write_catalog(present_outputs: dict[str, dict], derived_exports: dict[str, dict]) -> None:
    catalog_entries: dict[str, dict] = {}
    for name in list(present_outputs.keys()) + list(derived_exports.keys()):
        if name not in OUTPUT_CATALOG:
            continue
        entry = dict(OUTPUT_CATALOG[name])
        payload = present_outputs.get(name) or derived_exports.get(name) or {}
        entry["path"] = payload.get("path")
        if "rows" in payload:
            entry["rows"] = payload["rows"]
        if "columns" in payload:
            entry["columns"] = payload["columns"]
        entry["bytes"] = payload.get("bytes")
        catalog_entries[name] = entry

    def existing(names: list[str]) -> list[str]:
        return [name for name in names if name in catalog_entries]

    catalog = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": catalog_entries,
        "recommended_start_points": {
            "event_level": existing([
                "events_core",
                "signup_velocity.csv",
                "application_velocity.csv",
                "posthog_event_velocity.csv",
                "event_acquisition_rollup",
            ]),
            "person_level": existing([
                "applicants_core",
                "users_core",
                "user_hackathon_history",
                "engagement_profiles.csv",
            ]),
            "messaging_focus": existing([
                "applicant_messaging_rollup",
                "applicants_core",
                "events_core",
            ]),
        },
        "notes": [
            "Start with agent_analysis CSVs before using raw platform table exports.",
            "Use event_id and user_id as the canonical join keys.",
            "Only load optional files like users_core or applicant_messaging_rollup when the analysis needs them.",
        ],
    }
    (AGENT_ANALYSIS_DIR / "catalog.json").write_text(json.dumps(catalog, indent=2) + "\n")

    lines = [
        "# Agent Analysis Catalog",
        "",
        "Machine-readable map for analysis agents.",
        "",
    ]
    for name, entry in catalog_entries.items():
        lines.extend(
            [
                f"## {name}",
                f"- file: `{entry['filename']}`",
                f"- grain: {entry['grain']}",
                f"- primary keys: {', '.join(entry.get('primary_keys', [])) or 'n/a'}",
                f"- path: `{entry.get('path', '')}`",
                f"- rows: {entry.get('rows', 'n/a')}",
                f"- columns: {entry.get('columns', 'n/a')}",
                f"- pii: {entry.get('pii', 'unknown')}",
                f"- source tables: {', '.join(entry.get('source_tables', []))}",
                f"- join keys: {json.dumps(entry.get('join_keys', {}), ensure_ascii=True)}",
                f"- recommended uses: {', '.join(entry.get('recommended_uses', []))}",
                "",
            ]
        )
    (AGENT_ANALYSIS_DIR / "catalog.md").write_text("\n".join(lines) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a compact local pack for agent-driven data analysis.")
    parser.add_argument("--include-users", action="store_true", help="Export non-PII user profile summary rows.")
    parser.add_argument("--include-hackathon-history", action="store_true", help="Export per-user hackathon history rollups.")
    parser.add_argument("--include-messaging", action="store_true", help="Export applicant messaging rollups.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_env()
    AGENT_ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        outputs: dict[str, dict] = {}
        outputs["events_core"] = write_frame(build_events_core(conn), AGENT_ANALYSIS_DIR / "events_core.csv")
        outputs["applicants_core"] = write_frame(build_applicants_core(conn), AGENT_ANALYSIS_DIR / "applicants_core.csv")
        outputs["event_acquisition_rollup"] = write_frame(
            build_event_acquisition_rollup(conn),
            AGENT_ANALYSIS_DIR / "event_acquisition_rollup.csv",
        )
        if args.include_users:
            outputs["users_core"] = write_frame(build_users_core(conn), AGENT_ANALYSIS_DIR / "users_core.csv")
        if args.include_hackathon_history:
            outputs["user_hackathon_history"] = write_frame(
                build_user_hackathon_history(conn),
                AGENT_ANALYSIS_DIR / "user_hackathon_history.csv",
            )
        if args.include_messaging:
            outputs["applicant_messaging_rollup"] = write_frame(
                build_applicant_messaging_rollup(conn),
                AGENT_ANALYSIS_DIR / "applicant_messaging_rollup.csv",
            )
    finally:
        conn.close()

    derived_exports: dict[str, dict] = {}
    for filename in ["signup_velocity.csv", "application_velocity.csv", "engagement_profiles.csv", "posthog_event_velocity.csv"]:
        exported = copy_if_exists(derived_path(filename), AGENT_ANALYSIS_DIR / filename)
        if exported is not None:
            derived_exports[filename] = exported

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "outputs": outputs,
        "derived_exports": derived_exports,
        "notes": {
            "events_core": "One row per event with event metadata, applicant/approval/checkin counts, reminder/blast metadata, and merged derived velocity/PostHog snapshots.",
            "applicants_core": "One row per applicant with event context, UTM attribution, question-answer rollups, page-view rollups, notification rollups, and application lead time.",
            "event_acquisition_rollup": "Event-level applicant counts by UTM source/medium/campaign.",
            "users_core": "Optional non-PII user profile summary rows.",
            "user_hackathon_history": "Optional per-user submission and judging history rollups.",
            "applicant_messaging_rollup": "Optional per-event per-user application-channel messaging summary.",
        },
    }
    manifest_path = AGENT_ANALYSIS_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    write_catalog(outputs, derived_exports)
    print(f"Wrote {manifest_path}")


if __name__ == "__main__":
    main()
