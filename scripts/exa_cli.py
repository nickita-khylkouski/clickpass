#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from cv_rank.research.exa_person_research import call_exa_with_rotation, get_exa_api_keys

try:
    from exa_py import Exa
except ImportError as exc:  # pragma: no cover
    raise SystemExit(f"exa_py unavailable: {exc}")


PRESET_DEFAULTS: dict[str, dict[str, Any]] = {
    "general": {"type": "auto", "category": None, "user_location": "US"},
    "people": {"type": "fast", "category": "people", "user_location": "US"},
    "company": {"type": "fast", "category": "company", "user_location": "US"},
    "code": {"type": "neural", "category": None, "user_location": "US"},
    "news": {"type": "fast", "category": "news", "user_location": "US"},
    "papers": {"type": "fast", "category": "research paper", "user_location": "US"},
    "reports": {"type": "fast", "category": "financial report", "user_location": "US"},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Unified Exa CLI for search, contents, answer, and similar-page lookups."
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--preset", choices=sorted(PRESET_DEFAULTS), default="general")
    common.add_argument("--json-output", action="store_true", help="Render full JSON payload.")
    common.add_argument("--output", help="Optional output path for the rendered result.")
    common.add_argument("--compact", action="store_true", help="Compact JSON instead of indented JSON.")

    for action in common._actions:
        if action.dest != "help":
            parser._add_action(action)

    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search", help="Search Exa.", parents=[common])
    search.add_argument("query", nargs="+")
    search.add_argument("--num-results", type=int, default=5)
    search.add_argument("--type", dest="search_type")
    search.add_argument("--category")
    search.add_argument("--include-domain", action="append", default=[])
    search.add_argument("--exclude-domain", action="append", default=[])
    search.add_argument("--include-text", action="append", default=[])
    search.add_argument("--exclude-text", action="append", default=[])
    search.add_argument("--additional-query", action="append", default=[])
    search.add_argument("--user-location")
    search.add_argument("--text-chars", type=int, default=1200)
    search.add_argument("--summary", action="store_true")
    search.add_argument("--highlights", action="store_true")
    search.add_argument("--highlights-query")
    search.add_argument("--summary-query")
    search.add_argument("--start-published-date")
    search.add_argument("--end-published-date")
    search.add_argument("--start-crawl-date")
    search.add_argument("--end-crawl-date")

    contents = subparsers.add_parser("contents", help="Fetch one or more URLs through Exa.", parents=[common])
    contents.add_argument("url", nargs="+")
    contents.add_argument("--text-chars", type=int, default=4000)
    contents.add_argument("--summary", action="store_true")
    contents.add_argument("--subpages", type=int)
    contents.add_argument("--subpage-target", action="append", default=[])
    contents.add_argument("--livecrawl", choices=["always", "fallback", "never", "auto", "preferred"])
    contents.add_argument("--max-age-hours", type=int)

    similar = subparsers.add_parser("similar", help="Find pages similar to a URL.", parents=[common])
    similar.add_argument("url")
    similar.add_argument("--num-results", type=int, default=5)
    similar.add_argument("--category")
    similar.add_argument("--include-domain", action="append", default=[])
    similar.add_argument("--exclude-domain", action="append", default=[])
    similar.add_argument("--include-text", action="append", default=[])
    similar.add_argument("--exclude-text", action="append", default=[])
    similar.add_argument("--exclude-source-domain", action="store_true")
    similar.add_argument("--text-chars", type=int, default=1200)
    similar.add_argument("--summary", action="store_true")

    answer = subparsers.add_parser("answer", help="Use Exa answer with citations.", parents=[common])
    answer.add_argument("query", nargs="+")
    answer.add_argument("--model", choices=["exa", "exa-pro"])
    answer.add_argument("--text", action="store_true", help="Include citation text.")
    answer.add_argument("--user-location")

    return parser.parse_args()


def ensure_exa() -> None:
    if not get_exa_api_keys():
        raise SystemExit("EXA_API_KEY unavailable")


def clip_text(value: str, limit: int) -> str:
    value = " ".join((value or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def write_output(rendered: str, output_path: str | None) -> None:
    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)


def render_json(payload: Any, compact: bool) -> str:
    if compact:
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"
    return json.dumps(payload, ensure_ascii=True, indent=2) + "\n"


def preset_value(args: argparse.Namespace, key: str, explicit: Any = None) -> Any:
    if explicit not in (None, [], ""):
        return explicit
    return PRESET_DEFAULTS[args.preset].get(key)


def contents_options(*, text_chars: int, summary: bool, highlights: bool, highlights_query: str | None, summary_query: str | None) -> dict[str, Any]:
    options: dict[str, Any] = {
        "text": {"max_characters": text_chars},
    }
    if summary:
        summary_opts: dict[str, Any] = True if not summary_query else {"query": summary_query}
        options["summary"] = summary_opts
    if highlights:
        highlight_opts: dict[str, Any] = {"max_characters": min(text_chars, 1200)}
        if highlights_query:
            highlight_opts["query"] = highlights_query
        options["highlights"] = highlight_opts
    return options


def do_search(args: argparse.Namespace) -> dict[str, Any]:
    query = " ".join(args.query).strip()
    exa_args: dict[str, Any] = {
        "query": query,
        "num_results": args.num_results,
        "type": preset_value(args, "type", args.search_type),
        "category": preset_value(args, "category", args.category),
        "include_domains": args.include_domain or None,
        "exclude_domains": args.exclude_domain or None,
        "include_text": args.include_text or None,
        "exclude_text": args.exclude_text or None,
        "additional_queries": args.additional_query or None,
        "user_location": preset_value(args, "user_location", args.user_location),
        "start_published_date": args.start_published_date,
        "end_published_date": args.end_published_date,
        "start_crawl_date": args.start_crawl_date,
        "end_crawl_date": args.end_crawl_date,
        "contents": contents_options(
            text_chars=args.text_chars,
            summary=args.summary,
            highlights=args.highlights,
            highlights_query=args.highlights_query,
            summary_query=args.summary_query,
        ),
    }
    result = call_exa_with_rotation("search", lambda exa: exa.search(**exa_args))
    rows = []
    for item in getattr(result, "results", []) or []:
        rows.append(
            {
                "title": getattr(item, "title", "") or "",
                "url": getattr(item, "url", "") or "",
                "published_date": getattr(item, "published_date", None),
                "author": getattr(item, "author", None),
                "text": clip_text(getattr(item, "text", "") or "", args.text_chars),
                "summary": getattr(item, "summary", None),
                "highlights": getattr(item, "highlights", None),
                "score": getattr(item, "score", None),
            }
        )
    return {
        "command": "search",
        "preset": args.preset,
        "query": query,
        "resolved_search_type": getattr(result, "resolved_search_type", None),
        "results": rows,
    }


def do_contents(args: argparse.Namespace) -> dict[str, Any]:
    exa_args: dict[str, Any] = {
        "urls": args.url,
        "text": {"max_characters": args.text_chars},
        "summary": True if args.summary else None,
        "subpages": args.subpages,
        "subpage_target": args.subpage_target or None,
        "livecrawl": args.livecrawl,
        "max_age_hours": args.max_age_hours,
    }
    result = call_exa_with_rotation("get_contents", lambda exa: exa.get_contents(**exa_args))
    rows = []
    for item in getattr(result, "results", []) or []:
        rows.append(
            {
                "title": getattr(item, "title", "") or "",
                "url": getattr(item, "url", "") or "",
                "published_date": getattr(item, "published_date", None),
                "author": getattr(item, "author", None),
                "text": clip_text(getattr(item, "text", "") or "", args.text_chars),
                "summary": getattr(item, "summary", None),
            }
        )
    return {"command": "contents", "urls": args.url, "results": rows}


def do_similar(args: argparse.Namespace) -> dict[str, Any]:
    exa_args: dict[str, Any] = {
        "url": args.url,
        "num_results": args.num_results,
        "category": preset_value(args, "category", args.category),
        "include_domains": args.include_domain or None,
        "exclude_domains": args.exclude_domain or None,
        "include_text": args.include_text or None,
        "exclude_text": args.exclude_text or None,
        "exclude_source_domain": args.exclude_source_domain or None,
        "contents": {
            "text": {"max_characters": args.text_chars},
            **({"summary": True} if args.summary else {}),
        },
    }
    result = call_exa_with_rotation("find_similar", lambda exa: exa.find_similar(**exa_args))
    rows = []
    for item in getattr(result, "results", []) or []:
        rows.append(
            {
                "title": getattr(item, "title", "") or "",
                "url": getattr(item, "url", "") or "",
                "published_date": getattr(item, "published_date", None),
                "author": getattr(item, "author", None),
                "text": clip_text(getattr(item, "text", "") or "", args.text_chars),
                "summary": getattr(item, "summary", None),
            }
        )
    return {"command": "similar", "url": args.url, "results": rows}


def do_answer(args: argparse.Namespace) -> dict[str, Any]:
    query = " ".join(args.query).strip()
    response = call_exa_with_rotation(
        "answer",
        lambda exa: exa.answer(
            query,
            model=args.model,
            text=args.text,
            user_location=preset_value(args, "user_location", args.user_location),
        ),
    )
    citations = []
    for item in getattr(response, "citations", []) or []:
        citations.append(
            {
                "title": getattr(item, "title", "") or "",
                "url": getattr(item, "url", "") or "",
                "published_date": getattr(item, "published_date", None),
                "author": getattr(item, "author", None),
                "text": getattr(item, "text", None),
            }
        )
    return {
        "command": "answer",
        "query": query,
        "answer": getattr(response, "answer", "") or "",
        "citations": citations,
    }


def render_human(payload: dict[str, Any]) -> str:
    command = payload["command"]
    lines: list[str] = []
    if command == "answer":
        lines.append(payload.get("answer") or "")
        for citation in payload.get("citations", []) or []:
            lines.append(f"- {citation.get('title') or 'Untitled'} | {citation.get('url') or ''}")
        return "\n".join(line for line in lines if line).strip() + "\n"

    if command == "contents":
        for item in payload.get("results", []) or []:
            lines.append(f"- {item.get('title') or 'Untitled'} | {item.get('url') or ''}")
            if item.get("summary"):
                lines.append(f"  summary: {clip_text(item['summary'], 240)}")
            if item.get("text"):
                lines.append(f"  text: {clip_text(item['text'], 240)}")
        return "\n".join(lines).strip() + ("\n" if lines else "")

    for item in payload.get("results", []) or []:
        lines.append(f"- {item.get('title') or 'Untitled'} | {item.get('url') or ''}")
        text = item.get("summary") or item.get("text")
        if text:
            lines.append(f"  text: {clip_text(text, 220)}")
    return "\n".join(lines).strip() + ("\n" if lines else "")


def main() -> int:
    args = parse_args()
    ensure_exa()
    if args.command == "search":
        payload = do_search(args)
    elif args.command == "contents":
        payload = do_contents(args)
    elif args.command == "similar":
        payload = do_similar(args)
    elif args.command == "answer":
        payload = do_answer(args)
    else:  # pragma: no cover
        raise SystemExit(f"Unsupported command: {args.command}")

    rendered = render_json(payload, compact=args.compact) if args.json_output else render_human(payload)
    write_output(rendered, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
