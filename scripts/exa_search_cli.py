#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from cv_rank.research.exa_person_research import call_exa_with_rotation, get_exa_api_keys

try:
    from exa_py import Exa
except ImportError as exc:  # pragma: no cover
    raise SystemExit(f"exa_py unavailable: {exc}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search Exa and print compact JSON results.")
    parser.add_argument("--query", required=True)
    parser.add_argument("--num-results", type=int, default=5)
    parser.add_argument("--type", default="auto")
    parser.add_argument("--text-chars", type=int, default=1200)
    parser.add_argument("--highlights", action="store_true")
    parser.add_argument("--output", help="Optional output JSON path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_keys = get_exa_api_keys()
    if not api_keys:
        raise SystemExit("EXA_API_KEY unavailable")
    result = call_exa_with_rotation(
        "search",
        lambda exa: exa.search(
            args.query,
            type=args.type,
            num_results=args.num_results,
        ),
    )
    rows = []
    for item in getattr(result, "results", []) or []:
        rows.append(
            {
                "title": getattr(item, "title", "") or "",
                "url": getattr(item, "url", "") or "",
                "published_date": getattr(item, "published_date", None),
                "author": getattr(item, "author", None),
                "snippet": ((getattr(item, "text", "") or getattr(item, "summary", "") or getattr(item, "snippet", "") or ""))[: args.text_chars],
                "highlights": getattr(item, "highlights", None) if args.highlights else None,
            }
        )
    payload = {"query": args.query, "results": rows}
    rendered = json.dumps(payload, indent=2) + "\n"
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
