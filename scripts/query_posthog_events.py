from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from cv_rank.waves.posthog_features import EVENT_SLUG_EXPR, _run_hogql, load_posthog_config


BASE_DIR = Path(__file__).resolve().parent.parent


def load_env() -> None:
    load_dotenv(BASE_DIR / ".env")


def top_events(*, days: int, limit: int) -> dict:
    config = load_posthog_config()
    if config is None:
        raise SystemExit("Missing PostHog configuration")
    query = f"""
    select
      event,
      count() as total_events
    from events
    where timestamp >= now() - interval {int(days)} day
    group by event
    order by total_events desc
    limit {int(limit)}
    """
    return _run_hogql(query, name="cv-rank-posthog-top-events-live", config=config)


def sample_event_rows(*, event_name: str, days: int, limit: int, slug: str | None) -> dict:
    config = load_posthog_config()
    if config is None:
        raise SystemExit("Missing PostHog configuration")
    slug_clause = f"and {EVENT_SLUG_EXPR} = '{slug.lower()}'" if slug else ""
    event_literal = event_name.replace("\\", "\\\\").replace("'", "\\'")
    query = f"""
    select
      timestamp,
      event,
      distinct_id,
      coalesce(toString(properties['$user_id']), toString(properties['userId']), '') as user_id,
      coalesce(toString(properties.eventId), '') as raw_event_id,
      {EVENT_SLUG_EXPR} as event_slug,
      coalesce(
        toString(properties.url),
        toString(properties.eventUrl),
        toString(properties.$current_url),
        toString(properties.$pathname),
        ''
      ) as url
    from events
    where timestamp >= now() - interval {int(days)} day
      and event = '{event_literal}'
      {slug_clause}
    order by timestamp desc
    limit {int(limit)}
    """
    return _run_hogql(query, name="cv-rank-posthog-sample-event-rows", config=config)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Quick PostHog sampler for agents.")
    parser.add_argument("--top", action="store_true", help="Show top PostHog events.")
    parser.add_argument("--event", help="Sample rows for a single PostHog event name.")
    parser.add_argument("--slug", help="Optional event slug filter when using --event.")
    parser.add_argument("--days", type=int, default=30, help="Lookback window in days.")
    parser.add_argument("--limit", type=int, default=10, help="Result limit.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_env()
    try:
        if args.top:
            payload = top_events(days=args.days, limit=args.limit)
        elif args.event:
            payload = sample_event_rows(
                event_name=args.event,
                days=args.days,
                limit=args.limit,
                slug=args.slug,
            )
        else:
            raise SystemExit("Use --top or --event EVENT_NAME")
    except Exception as exc:
        payload = {"error": str(exc), "event": args.event, "top": args.top}
    print(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    main()
