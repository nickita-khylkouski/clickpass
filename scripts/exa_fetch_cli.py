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
    parser = argparse.ArgumentParser(description="Fetch one or more URLs through Exa and print JSON.")
    parser.add_argument("--url", action="append", required=True, dest="urls")
    parser.add_argument("--text-chars", type=int, default=4000)
    parser.add_argument("--output", help="Optional output JSON path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_keys = get_exa_api_keys()
    if not api_keys:
        raise SystemExit("EXA_API_KEY unavailable")
    contents = call_exa_with_rotation(
        "get_contents",
        lambda exa: exa.get_contents(args.urls, text=True),
    )
    rows = []
    for idx, item in enumerate(contents.results or []):
        rows.append(
            {
                "url": getattr(item, "url", "") or (args.urls[idx] if idx < len(args.urls) else ""),
                "title": getattr(item, "title", "") or "",
                "author": getattr(item, "author", None),
                "published_date": getattr(item, "published_date", None),
                "text": (getattr(item, "text", "") or "")[: args.text_chars],
            }
        )
    payload = {"urls": args.urls, "results": rows}
    rendered = json.dumps(payload, indent=2) + "\n"
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
