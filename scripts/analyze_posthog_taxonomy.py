from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent.parent
load_dotenv(BASE / ".env")

POSTHOG_INGEST_HOST = os.environ.get("POSTHOG_HOST", "").rstrip("/")
POSTHOG_API_HOST = os.environ.get("POSTHOG_API_HOST", "").rstrip("/") or (
    "https://us.posthog.com" if POSTHOG_INGEST_HOST == "https://us.i.posthog.com" else ""
)
POSTHOG_PROJECT_ID = os.environ.get("POSTHOG_PROJECT_ID", "").strip()
POSTHOG_API_KEY = os.environ.get("POSTHOG_API_KEY", "").strip()

OUTPUT_DIR = BASE / "results" / "posthog_analysis"


def run_hogql(query: str, name: str) -> dict[str, Any]:
    payload = json.dumps(
        {
            "query": {
                "kind": "HogQLQuery",
                "query": query,
            },
            "name": name,
        }
    ).encode()
    req = Request(
        f"{POSTHOG_API_HOST}/api/projects/{POSTHOG_PROJECT_ID}/query/",
        data=payload,
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {POSTHOG_API_KEY}")
    req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def write_tsv(path: Path, columns: list[str], rows: list[list[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["\t".join(columns)]
    for row in rows:
        lines.append("\t".join("" if value is None else str(value) for value in row))
    path.write_text("\n".join(lines) + "\n")


def main() -> None:
    if not POSTHOG_API_HOST or not POSTHOG_PROJECT_ID or not POSTHOG_API_KEY:
        raise SystemExit("Missing POSTHOG_API_HOST/POSTHOG_PROJECT_ID/POSTHOG_API_KEY in .env")

    top_events = run_hogql(
        """
        select event, count() as c
        from events
        where timestamp >= now() - interval 180 day
        group by event
        order by c desc
        limit 50
        """,
        "cv-rank-posthog-top-events",
    )
    event_funnel_events = run_hogql(
        """
        select event, count() as c
        from events
        where timestamp >= now() - interval 180 day
          and (event like 'event/%' or event like 'click/event%' or event like 'hackathon/%' or event like 'completion/%')
        group by event
        order by c desc
        limit 100
        """,
        "cv-rank-posthog-event-funnel-taxonomy",
    )
    linkability = run_hogql(
        """
        select
          event,
          count() as c,
          countIf(notEmpty(toString(properties.eventId))) as with_event_id,
          countIf(notEmpty(toString(properties.$current_url))) as with_url,
          countIf(notEmpty(toString(properties.slug))) as with_slug
        from events
        where timestamp >= now() - interval 180 day
          and event in (
            '$pageview',
            'completion/apply',
            'click/event-register-button',
            'event/registration',
            'event/view-guest-list',
            'event/open-messages',
            'event/chat-send-message',
            'event/add-to-calendar',
            'hackathon/view-submission'
          )
        group by event
        order by c desc
        """,
        "cv-rank-posthog-linkable-events",
    )
    top_apply_events = run_hogql(
        """
        select
          coalesce(nullIf(toString(properties.eventId), ''), nullIf(toString(properties.eventTitle), ''), nullIf(toString(properties.url), '')) as event_key,
          count() as c
        from events
        where timestamp >= now() - interval 90 day
          and event = 'completion/apply'
        group by event_key
        having event_key != ''
        order by c desc
        limit 50
        """,
        "cv-rank-posthog-top-apply-events",
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "top_events.json").write_text(json.dumps(top_events, indent=2) + "\n")
    (OUTPUT_DIR / "event_funnel_events.json").write_text(json.dumps(event_funnel_events, indent=2) + "\n")
    (OUTPUT_DIR / "linkability.json").write_text(json.dumps(linkability, indent=2) + "\n")
    (OUTPUT_DIR / "top_apply_events.json").write_text(json.dumps(top_apply_events, indent=2) + "\n")

    write_tsv(OUTPUT_DIR / "top_events.tsv", top_events["columns"], top_events["results"])
    write_tsv(
        OUTPUT_DIR / "event_funnel_events.tsv",
        event_funnel_events["columns"],
        event_funnel_events["results"],
    )
    write_tsv(OUTPUT_DIR / "linkability.tsv", linkability["columns"], linkability["results"])
    write_tsv(OUTPUT_DIR / "top_apply_events.tsv", top_apply_events["columns"], top_apply_events["results"])

    md_lines = [
        "# PostHog Taxonomy Audit",
        "",
        f"- API host: `{POSTHOG_API_HOST}`",
        f"- Project id: `{POSTHOG_PROJECT_ID}`",
        "",
        "## Highest-volume event funnel events",
        "",
    ]
    for event, count in event_funnel_events["results"][:12]:
        md_lines.append(f"- `{event}`: `{count}`")
    md_lines.extend(
        [
            "",
            "## Linkable event coverage",
            "",
            "| event | count | with_event_id | with_url | with_slug |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in linkability["results"]:
        md_lines.append(f"| `{row[0]}` | {row[1]} | {row[2]} | {row[3]} | {row[4]} |")
    md_lines.extend(
        [
            "",
            "## Top `completion/apply` event keys",
            "",
        ]
    )
    for event_key, count in top_apply_events["results"][:15]:
        md_lines.append(f"- `{event_key}`: `{count}`")
    (OUTPUT_DIR / "README.md").write_text("\n".join(md_lines) + "\n")
    print(f"Wrote PostHog audit to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
