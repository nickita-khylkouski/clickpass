#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import psycopg2
from dotenv import dotenv_values


ENV_PATH = Path("/Users/nickita/cv-rank/.env")


def connect():
    vals = dotenv_values(ENV_PATH)
    url = vals.get("PLATFORM_DATABASE_URL")
    if not url:
        raise SystemExit("PLATFORM_DATABASE_URL missing from /Users/nickita/cv-rank/.env")
    return psycopg2.connect(url, connect_timeout=10)


def row_to_dict(cur, row):
    if row is None:
        return None
    return {desc.name: value for desc, value in zip(cur.description, row)}


def list_recent(cur, limit: int):
    cur.execute(
        """
        SELECT
          "id",
          "title",
          "slug",
          "city",
          "venue",
          "startDateTime",
          "endDateTime",
          "approvalRequired",
          "registrationClosed",
          "showHackathonGallery",
          "hackathonJudgingOpen"
        FROM "PlatformEvent"
        ORDER BY "startDateTime" DESC NULLS LAST, "createdAt" DESC
        LIMIT %s
        """,
        (limit,),
    )
    return [row_to_dict(cur, row) for row in cur.fetchall()]


def fetch_event(cur, slug: str | None, title_like: str | None, event_id: str | None):
    if event_id:
        cur.execute(
            """
            SELECT *
            FROM "PlatformEvent"
            WHERE "id" = %s
            LIMIT 1
            """,
            (event_id,),
        )
    elif slug:
        cur.execute(
            """
            SELECT *
            FROM "PlatformEvent"
            WHERE "slug" = %s
            LIMIT 1
            """,
            (slug,),
        )
    else:
        cur.execute(
            """
            SELECT *
            FROM "PlatformEvent"
            WHERE "title" ILIKE %s
            ORDER BY "startDateTime" DESC NULLS LAST, "createdAt" DESC
            LIMIT 1
            """,
            (f"%{title_like}%",),
        )
    return row_to_dict(cur, cur.fetchone())


def applicant_status_counts(cur, event_id: str):
    cur.execute(
        """
        SELECT COALESCE("status"::text, 'NULL') AS status, COUNT(*)
        FROM "EventApplicant"
        WHERE "eventId" = %s
        GROUP BY 1
        ORDER BY 2 DESC, 1
        """,
        (event_id,),
    )
    return {status: count for status, count in cur.fetchall()}


def applicant_extra_counts(cur, event_id: str):
    cur.execute(
        """
        SELECT
          COUNT(*) AS total,
          COUNT(*) FILTER (WHERE "checkedIn" = true) AS checked_in,
          COUNT(*) FILTER (WHERE "agreedToWaiver" = true) AS agreed_to_waiver,
          COUNT(DISTINCT "utmTrackingId") FILTER (WHERE "utmTrackingId" IS NOT NULL) AS distinct_utm_ids
        FROM "EventApplicant"
        WHERE "eventId" = %s
        """,
        (event_id,),
    )
    return row_to_dict(cur, cur.fetchone())


def reminders(cur, event_id: str):
    cur.execute(
        """
        SELECT
          "id",
          "reminderOffsetMinutes",
          "sent",
          "isText",
          "isEmail",
          "createdAt",
          "updatedAt"
        FROM "EventReminder"
        WHERE "eventId" = %s
        ORDER BY "reminderOffsetMinutes" ASC NULLS LAST, "createdAt" ASC
        """,
        (event_id,),
    )
    rows = [row_to_dict(cur, row) for row in cur.fetchall()]
    summary = {
        "count": len(rows),
        "sent_count": sum(1 for row in rows if row["sent"]),
        "email_count": sum(1 for row in rows if row["isEmail"]),
        "text_count": sum(1 for row in rows if row["isText"]),
        "rows": rows[:20],
    }
    return summary


def notification_blasts(cur, event_id: str):
    cur.execute(
        """
        SELECT
          "id",
          "content",
          "isEmail",
          "isSMS",
          "targetStatus",
          "scheduledAt",
          "sentAt",
          "createdAt",
          "updatedAt",
          "sendingHostUserId"
        FROM "EventNotificationBlast"
        WHERE "eventId" = %s
        ORDER BY COALESCE("sentAt", "scheduledAt", "createdAt") DESC
        """,
        (event_id,),
    )
    rows = [row_to_dict(cur, row) for row in cur.fetchall()]
    summary = {
        "count": len(rows),
        "sent_count": sum(1 for row in rows if row["sentAt"] is not None),
        "scheduled_count": sum(1 for row in rows if row["scheduledAt"] is not None),
        "email_count": sum(1 for row in rows if row["isEmail"]),
        "sms_count": sum(1 for row in rows if row["isSMS"]),
        "rows": rows[:20],
    }
    return summary


def submissions(cur, event_id: str):
    cur.execute(
        """
        SELECT
          "id",
          "teamName",
          "teamNumber",
          "placement",
          "publicVotePlacement",
          "createdAt",
          "updatedAt"
        FROM "HackathonSubmission"
        WHERE "eventId" = %s
        ORDER BY
          CASE WHEN "placement" IS NULL THEN 1 ELSE 0 END,
          "placement" ASC NULLS LAST,
          "createdAt" ASC
        """,
        (event_id,),
    )
    rows = [row_to_dict(cur, row) for row in cur.fetchall()]
    placement_counts: dict[str, int] = defaultdict(int)
    for row in rows:
        key = str(row["placement"]) if row["placement"] is not None else "unplaced"
        placement_counts[key] += 1
    summary = {
        "count": len(rows),
        "placement_counts": dict(placement_counts),
        "rows": rows[:30],
    }
    return summary


def build_snapshot(slug: str | None, title_like: str | None, event_id: str | None):
    conn = connect()
    try:
        cur = conn.cursor()
        event = fetch_event(cur, slug=slug, title_like=title_like, event_id=event_id)
        if not event:
            return {"status": "not_found", "slug": slug, "title_like": title_like, "event_id": event_id}
        eid = event["id"]
        return {
            "status": "ok",
            "event": event,
            "applicants": {
                "status_counts": applicant_status_counts(cur, eid),
                "extra_counts": applicant_extra_counts(cur, eid),
            },
            "reminders": reminders(cur, eid),
            "notification_blasts": notification_blasts(cur, eid),
            "submissions": submissions(cur, eid),
        }
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Read-only CV platform event snapshot")
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--slug")
    group.add_argument("--title-like")
    group.add_argument("--event-id")
    parser.add_argument("--recent", type=int, help="List the N most recent PlatformEvent rows")
    args = parser.parse_args()

    conn = connect()
    try:
        cur = conn.cursor()
        if args.recent:
            payload = {"status": "ok", "recent_events": list_recent(cur, args.recent)}
        else:
            payload = build_snapshot(args.slug, args.title_like, args.event_id)
    finally:
        conn.close()
    print(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    main()
