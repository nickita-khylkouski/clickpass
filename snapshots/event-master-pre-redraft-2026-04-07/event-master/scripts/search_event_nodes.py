#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


WORKSPACE_ROOT = Path("/Users/nickita/.superset/worktrees/start/second-handstand")
NODE_CATALOG = WORKSPACE_ROOT / "web/src/data/node-catalog.json"
NODE_EVIDENCE = WORKSPACE_ROOT / "web/src/data/node-evidence.json"


def normalize(value: str | None) -> str:
    return (value or "").strip()


def tokenize(value: str) -> list[str]:
    return [token for token in re.split(r"[^A-Za-z0-9]+", value.lower()) if token]


def join_fields(node: dict, evidence: dict | None, include_sections: bool) -> str:
    fields = [
        normalize(node.get("nodeId")),
        normalize(node.get("label")),
        normalize(node.get("title")),
        normalize(node.get("summary")),
        " ".join(node.get("bullets", []) or []),
        " ".join(node.get("connectedLabels", []) or []),
        " ".join(node.get("topQuotes", []) or []),
    ]
    if evidence:
        fields.extend(
            [
                " ".join(evidence.get("bullets", []) or []),
                " ".join(evidence.get("quotes", []) or []),
            ]
        )
        if include_sections:
            for section in evidence.get("packetSections", []) or []:
                fields.append(normalize(section.get("heading")))
                fields.append(normalize(section.get("summary")))
    return "\n".join(part for part in fields if part)


def score_node(query_tokens: list[str], node: dict, evidence: dict | None, include_sections: bool) -> tuple[int, list[str]]:
    text = join_fields(node, evidence, include_sections).lower()
    title = normalize(node.get("title")).lower()
    label = normalize(node.get("label")).lower()
    summary = normalize(node.get("summary")).lower()
    score = 0
    reasons: list[str] = []
    for token in query_tokens:
        if token in title:
            score += 8
            reasons.append(f"title:{token}")
        elif token in label:
            score += 6
            reasons.append(f"label:{token}")
        elif token in summary:
            score += 4
            reasons.append(f"summary:{token}")
        elif token in text:
            score += 2
            reasons.append(f"body:{token}")
    return score, reasons


def main() -> None:
    parser = argparse.ArgumentParser(description="Search CV event workflow nodes")
    parser.add_argument("query")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--include-sections", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    catalog = json.loads(NODE_CATALOG.read_text())
    evidence_blob = json.loads(NODE_EVIDENCE.read_text())
    nodes = catalog["nodes"]
    evidence_map = evidence_blob.get("evidenceByNodeId", {})
    query_tokens = tokenize(args.query)

    ranked = []
    for node in nodes:
        evidence = evidence_map.get(node["nodeId"])
        score, reasons = score_node(query_tokens, node, evidence, args.include_sections)
        if score <= 0:
            continue
        ranked.append(
            {
                "score": score,
                "reasons": reasons[:8],
                "nodeId": node["nodeId"],
                "label": node.get("label"),
                "summary": node.get("summary"),
                "bullets": (node.get("bullets") or [])[:4],
                "connectedLabels": (node.get("connectedLabels") or [])[:6],
                "topReferences": (node.get("topReferences") or [])[:4],
                "packetSectionHeadings": (node.get("packetSectionHeadings") or [])[:8],
            }
        )

    ranked.sort(key=lambda item: (-item["score"], item["nodeId"]))
    results = ranked[: args.limit]

    if args.json:
        print(json.dumps(results, indent=2))
        return

    for item in results:
        print(f'[{item["score"]}] {item["nodeId"]} — {item["label"]}')
        if item["summary"]:
            print(f'  summary: {item["summary"]}')
        if item["reasons"]:
            print(f'  reasons: {", ".join(item["reasons"])}')
        if item["bullets"]:
            print(f'  bullets: {" | ".join(item["bullets"])}')
        if item["connectedLabels"]:
            print(f'  neighbors: {" | ".join(item["connectedLabels"])}')
        if item["packetSectionHeadings"]:
            print(f'  sections: {" | ".join(item["packetSectionHeadings"])}')
        if item["topReferences"]:
            print(f'  refs: {" | ".join(item["topReferences"])}')
        print()


if __name__ == "__main__":
    main()

