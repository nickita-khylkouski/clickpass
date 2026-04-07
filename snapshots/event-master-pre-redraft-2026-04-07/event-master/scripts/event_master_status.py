#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path


WORKSPACE_ROOT = Path("/Users/nickita/.superset/worktrees/start/second-handstand")
EVENTS_ROOT = Path(
    "/Users/nickita/Library/CloudStorage/GoogleDrive-nickita@cerebralvalley.ai/My Drive/Events"
)
EVENTS_DB = WORKSPACE_ROOT / ".research/events-index/events.db"
EVENTS_TREE = WORKSPACE_ROOT / ".research/events-index/tree.txt"
EVENTS_STATS = WORKSPACE_ROOT / ".research/events-index/stats.json"
NODE_CATALOG = WORKSPACE_ROOT / "web/src/data/node-catalog.json"
NODE_EVIDENCE = WORKSPACE_ROOT / "web/src/data/node-evidence.json"
FINAL_PACKETS = WORKSPACE_ROOT / ".research/agent-evidence/final"
GRANOLA_CACHE = Path.home() / "Library/Application Support/Granola/cache-v6.json"
GRANOLA_EXPORT = WORKSPACE_ROOT / ".research/granola-export"
GOOGLE_EXPORT = WORKSPACE_ROOT / ".research/events-google-export"
GOOGLE_EXPORT_TOKEN = GOOGLE_EXPORT / "auth" / "token.json"
PACKET_PROMPT = WORKSPACE_ROOT / ".research/prompt-training/14_3545-description-updated/prompt_v3.md"
PACKET_GUIDE = WORKSPACE_ROOT / ".research/process-node-mapping/NODE_SUMMARY_GUIDE.md"
CV_RANK_ENV = Path("/Users/nickita/cv-rank/.env")


def db_counts(path: Path) -> dict[str, int] | None:
    if not path.exists():
        return None
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    counts = {}
    for table in ["files", "file_texts", "chunks", "chunk_embeddings"]:
        try:
            counts[table] = cur.execute(f"select count(*) from {table}").fetchone()[0]
        except sqlite3.Error:
            counts[table] = -1
    conn.close()
    return counts


def json_shape(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    out: dict[str, object] = {"exists": True, "type": type(data).__name__}
    if isinstance(data, dict):
        out["keys"] = list(data)[:10]
        if "nodes" in data and isinstance(data["nodes"], list):
            out["nodes"] = len(data["nodes"])
        if "evidenceByNodeId" in data and isinstance(data["evidenceByNodeId"], dict):
            out["evidenceByNodeId"] = len(data["evidenceByNodeId"])
    elif isinstance(data, list):
        out["items"] = len(data)
    return out


def count_meeting_dirs(path: Path) -> int | None:
    meetings = path / "meetings"
    if not meetings.exists():
        return None
    return sum(1 for child in meetings.iterdir() if child.is_dir())


def main() -> None:
    payload = {
        "workspace_root": str(WORKSPACE_ROOT),
        "events_root_exists": EVENTS_ROOT.exists(),
        "events_root": str(EVENTS_ROOT),
        "events_db": {
            "path": str(EVENTS_DB),
            "exists": EVENTS_DB.exists(),
            "counts": db_counts(EVENTS_DB),
        },
        "events_tree_exists": EVENTS_TREE.exists(),
        "events_stats_exists": EVENTS_STATS.exists(),
        "node_catalog": {
            "path": str(NODE_CATALOG),
            "shape": json_shape(NODE_CATALOG),
        },
        "node_evidence": {
            "path": str(NODE_EVIDENCE),
            "shape": json_shape(NODE_EVIDENCE),
        },
        "final_packets": {
            "path": str(FINAL_PACKETS),
            "exists": FINAL_PACKETS.exists(),
            "count": len(list(FINAL_PACKETS.glob("*.md"))) if FINAL_PACKETS.exists() else None,
        },
        "granola_cache": {
            "path": str(GRANOLA_CACHE),
            "exists": GRANOLA_CACHE.exists(),
        },
        "granola_export": {
            "path": str(GRANOLA_EXPORT),
            "exists": GRANOLA_EXPORT.exists(),
            "meeting_dirs": count_meeting_dirs(GRANOLA_EXPORT) if GRANOLA_EXPORT.exists() else None,
        },
        "google_export": {
            "path": str(GOOGLE_EXPORT),
            "exists": GOOGLE_EXPORT.exists(),
            "token_exists": GOOGLE_EXPORT_TOKEN.exists(),
            "exported_doc_dirs": sum(1 for child in GOOGLE_EXPORT.iterdir() if child.is_dir() and child.name != "auth") if GOOGLE_EXPORT.exists() else None,
        },
        "packet_prompt": {
            "path": str(PACKET_PROMPT),
            "exists": PACKET_PROMPT.exists(),
        },
        "packet_guide": {
            "path": str(PACKET_GUIDE),
            "exists": PACKET_GUIDE.exists(),
        },
        "cv_rank_env": {
            "path": str(CV_RANK_ENV),
            "exists": CV_RANK_ENV.exists(),
        },
        "openai_api_key_present": bool(os.getenv("OPENAI_API_KEY")),
    }
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
