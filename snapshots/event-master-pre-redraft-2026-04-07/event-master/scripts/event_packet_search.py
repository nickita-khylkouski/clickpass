#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


WORKSPACE_ROOT = Path("/Users/nickita/.superset/worktrees/start/second-handstand")
FINAL_PACKETS = WORKSPACE_ROOT / ".research/agent-evidence/final"
NODE_EVIDENCE = WORKSPACE_ROOT / "web/src/data/node-evidence.json"


def normalize(text: str | None) -> str:
    return (text or "").strip()


def tokenize(text: str) -> list[str]:
    return [token for token in re.split(r"[^A-Za-z0-9]+", text.lower()) if token]


def load_node_evidence():
    blob = json.loads(NODE_EVIDENCE.read_text())
    return blob.get("evidenceByNodeId", {})


def score_text(query_tokens: list[str], text: str) -> tuple[int, list[str]]:
    lower = text.lower()
    score = 0
    reasons: list[str] = []
    phrase = " ".join(query_tokens).strip()
    if phrase and phrase in lower:
        score += 10
        reasons.append(f"phrase:{phrase}")
    for token in query_tokens:
        if token in lower:
            score += 1
            reasons.append(token)
    return score, reasons


def packet_hits(query: str, limit: int):
    tokens = tokenize(query)
    hits = []
    for path in FINAL_PACKETS.glob("*.md"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        score, reasons = score_text(tokens, f"{path.name}\n{text}")
        if score <= 0:
            continue
        excerpt = ""
        lower = text.lower()
        for token in tokens:
            idx = lower.find(token)
            if idx >= 0:
                start = max(0, idx - 180)
                end = min(len(text), idx + 420)
                excerpt = text[start:end].strip()
                break
        hits.append(
            {
                "score": score,
                "path": str(path),
                "name": path.name,
                "reasons": reasons[:8],
                "excerpt": excerpt,
            }
        )
    hits.sort(key=lambda item: (-item["score"], item["name"]))
    return hits[:limit]


def node_hits(query: str, limit: int):
    tokens = tokenize(query)
    evidence = load_node_evidence()
    hits = []
    for node_id, entry in evidence.items():
        text_parts = [
            normalize(entry.get("label")),
            normalize(entry.get("whatThisEvidenceShows")),
            " ".join(entry.get("bullets", []) or []),
            " ".join(entry.get("quotes", []) or []),
        ]
        for section in entry.get("packetSections", []) or []:
            text_parts.append(normalize(section.get("heading")))
            text_parts.append(normalize(section.get("summary")))
        combined = "\n".join(part for part in text_parts if part)
        score, reasons = score_text(tokens, combined)
        if score <= 0:
            continue
        packet_sections = entry.get("packetSections", []) or []
        excerpt = ""
        for section in packet_sections:
            blob = f'{section.get("heading","")}\n{section.get("summary","")}'
            section_score, _ = score_text(tokens, blob)
            if section_score > 0:
                excerpt = blob[:500].strip()
                break
        if not excerpt and entry.get("quotes"):
            excerpt = normalize(entry["quotes"][0])
        hits.append(
            {
                "score": score,
                "nodeId": node_id,
                "label": entry.get("label"),
                "whatThisEvidenceShows": entry.get("whatThisEvidenceShows"),
                "reasons": reasons[:8],
                "excerpt": excerpt,
                "references": (entry.get("references") or [])[:4],
            }
        )
    hits.sort(key=lambda item: (-item["score"], item["nodeId"]))
    return hits[:limit]


def main():
    parser = argparse.ArgumentParser(description="Search event packets and node evidence for operational quotes/context")
    parser.add_argument("query")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    payload = {
        "query": args.query,
        "packet_hits": packet_hits(args.query, args.limit),
        "node_hits": node_hits(args.query, args.limit),
    }

    if args.json:
        print(json.dumps(payload, indent=2))
        return

    print(f'Query: {args.query}\n')
    print("Packet hits:")
    for item in payload["packet_hits"]:
        print(f'- [{item["score"]}] {item["name"]}')
        print(f'  path: {item["path"]}')
        if item["reasons"]:
            print(f'  reasons: {", ".join(item["reasons"])}')
        if item["excerpt"]:
            print(f'  excerpt: {item["excerpt"][:500]}')
        print()

    print("Node evidence hits:")
    for item in payload["node_hits"]:
        print(f'- [{item["score"]}] {item["nodeId"]} — {item["label"]}')
        if item["whatThisEvidenceShows"]:
            print(f'  summary: {item["whatThisEvidenceShows"]}')
        if item["reasons"]:
            print(f'  reasons: {", ".join(item["reasons"])}')
        if item["excerpt"]:
            print(f'  excerpt: {item["excerpt"][:500]}')
        if item["references"]:
            print(f'  refs: {" | ".join(item["references"])}')
        print()


if __name__ == "__main__":
    main()
