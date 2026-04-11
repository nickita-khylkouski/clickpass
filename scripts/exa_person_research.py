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

from cv_rank.research.exa_person_research import collect_person_evidence


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deterministic Exa-first person research. Search and fetch evidence without relying on Wafer tool calls."
    )
    parser.add_argument("--name", required=True)
    parser.add_argument("--email", default="")
    parser.add_argument("--company", default="")
    parser.add_argument("--title", default="")
    parser.add_argument("--linkedin-url", default="")
    parser.add_argument("--github-url", default="")
    parser.add_argument("--x-handle", default="")
    parser.add_argument("--personal-website", default="")
    parser.add_argument("--latest-checked-in-event", default="")
    parser.add_argument("--exa-api-key-file", help="Path to a file containing EXA_API_KEY.")
    parser.add_argument("--output", help="Optional output JSON path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.exa_api_key_file:
        os.environ["EXA_API_KEY"] = Path(args.exa_api_key_file).read_text(encoding="utf-8").strip()
    person = {
        "name": args.name,
        "email": args.email,
        "company": args.company,
        "title": args.title,
        "linkedin_url": args.linkedin_url,
        "github_url": args.github_url,
        "x_handle": args.x_handle,
        "personal_website": args.personal_website,
        "latest_checked_in_event": args.latest_checked_in_event,
    }
    payload = collect_person_evidence(person)
    rendered = json.dumps(payload, indent=2) + "\n"
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
