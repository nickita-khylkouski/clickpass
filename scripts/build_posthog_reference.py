from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from query_posthog_events import sample_event_rows, top_events


BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "local_data" / "posthog" / "portal"
KEY_EVENTS = [
    "$pageview",
    "completion/apply",
    "click/event-register-button",
    "event/registration",
    "event/view-guest-list",
    "event/open-messages",
    "event/add-to-calendar",
    "event/chat-send-message",
]


def sanitize(name: str) -> str:
    return name.replace("/", "_").replace("$", "dollar_").replace("-", "_")


def main() -> None:
    load_dotenv(BASE_DIR / ".env")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    samples_dir = OUTPUT_DIR / "samples"
    samples_dir.mkdir(parents=True, exist_ok=True)

    outputs: dict[str, dict] = {}
    warnings: list[str] = []
    top = {"columns": [], "results": []}
    try:
        top = top_events(days=30, limit=25)
    except Exception as exc:
        warnings.append(f"top_events_failed: {exc}")
    (OUTPUT_DIR / "top_events_30d.json").write_text(json.dumps(top, indent=2) + "\n")

    for event_name in KEY_EVENTS:
        try:
            result = sample_event_rows(event_name=event_name, days=90, limit=10, slug=None)
            filename = f"{sanitize(event_name)}.json"
            path = samples_dir / filename
            path.write_text(json.dumps(result, indent=2, default=str) + "\n")
            outputs[event_name] = {
                "path": str(path.relative_to(BASE_DIR)),
                "rows": len(result.get("results", [])),
                "columns": result.get("columns", []),
            }
        except Exception as exc:
            warnings.append(f"sample_failed:{event_name}:{exc}")

    index = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "top_events_30d": str((OUTPUT_DIR / "top_events_30d.json").relative_to(BASE_DIR)),
        "sample_events": outputs,
        "warnings": warnings,
        "notes": [
            "These are lightweight saved PostHog event samples for agent discovery.",
            "Use scripts/query_posthog_events.py for fresh live sampling.",
        ],
    }
    (OUTPUT_DIR / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    readme_lines = [
        "# PostHog Portal",
        "",
        "Saved PostHog discovery artifacts for agents.",
        "",
        "## Files",
        "",
        "- `index.json`: entrypoint with sample-event file map",
        "- `top_events_30d.json`: top recent events",
        "- `samples/*.json`: 10 example rows for key PostHog event families",
        "",
        "## Refresh",
        "",
        "```bash",
        "uv run python scripts/build_posthog_reference.py",
        "```",
        "",
        "For fresh live samples instead of saved artifacts:",
        "",
        "```bash",
        "uv run python scripts/query_posthog_events.py --top --days 30 --limit 20",
        "uv run python scripts/query_posthog_events.py --event 'completion/apply' --days 90 --limit 10",
        "```",
    ]
    (OUTPUT_DIR / "README.md").write_text("\n".join(readme_lines) + "\n")


if __name__ == "__main__":
    main()
