"""Extract event-level PostHog demand/intent snapshots for Stage 0 modeling."""
# ruff: noqa: E402

from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pandas as pd
import psycopg2
import psycopg2.extras

from cv_rank.waves.posthog_features import (
    POSTHOG_EVENT_VELOCITY_EVENTS,
    build_event_velocity_features,
    fetch_hourly_stage0_counts,
)


def main() -> None:
    dsn = os.environ["PLATFORM_DATABASE_URL"]
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT
                pe.id::text AS event_id,
                pe.slug,
                pe.title,
                pe."startDateTime" AS event_start
            FROM "PlatformEvent" pe
            JOIN "EventApplicant" ea ON ea."eventId" = pe.id
            WHERE pe."startDateTime" IS NOT NULL
              AND pe."startDateTime" < NOW()
            GROUP BY pe.id, pe.slug, pe.title, pe."startDateTime"
            HAVING COUNT(*) FILTER (WHERE ea."createdAt" < pe."startDateTime") >= 20
            ORDER BY pe."startDateTime" ASC
            """
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    events_df = pd.DataFrame(rows)
    if events_df.empty:
        raise SystemExit("No historical events found for PostHog extraction")

    events_df["slug"] = events_df["slug"].fillna("").astype(str).str.strip().str.lower()
    events_df = events_df[events_df["slug"] != ""].copy()
    events_df["event_start"] = pd.to_datetime(events_df["event_start"], utc=True)

    since = events_df["event_start"].min() - pd.Timedelta(days=30)
    hourly_counts = fetch_hourly_stage0_counts(
        event_slugs=events_df["slug"].dropna().astype(str).tolist(),
        since=since.to_pydatetime(),
        event_map=POSTHOG_EVENT_VELOCITY_EVENTS,
    )
    features_df = build_event_velocity_features(
        events_df,
        hourly_counts,
        event_map=POSTHOG_EVENT_VELOCITY_EVENTS,
    )
    output_path = Path(__file__).parent.parent / "results" / "posthog_event_velocity.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    features_df.to_csv(output_path, index=False, quoting=csv.QUOTE_MINIMAL)
    print(f"Wrote {len(features_df)} PostHog event rows to {output_path}")


if __name__ == "__main__":
    main()
