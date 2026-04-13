#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _stale_cutoff(seconds: int) -> str:
    return (datetime.now(tz=UTC) - timedelta(seconds=max(1, int(seconds)))).isoformat()


def connect_task_queue(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ordinal INTEGER NOT NULL UNIQUE,
            task_id TEXT NOT NULL UNIQUE,
            task_type TEXT NOT NULL,
            prompt_text TEXT NOT NULL,
            prompt_ref TEXT NOT NULL DEFAULT '',
            input_ref TEXT NOT NULL DEFAULT '',
            output_ref TEXT NOT NULL DEFAULT '',
            workdir TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            claimed_by TEXT,
            claimed_at TEXT,
            finished_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            last_run_id TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_queue_status_ordinal
        ON items(status, ordinal, id)
        """
    )
    conn.commit()
    return conn


def seed_task_queue(db_path: Path, tasks: list[dict[str, Any]]) -> None:
    conn = connect_task_queue(db_path)
    now = utc_now()
    try:
        conn.execute("DELETE FROM items")
        for ordinal, task in enumerate(tasks, start=1):
            metadata_payload = {
                "metadata": dict(task.get("metadata") or {}),
                "agent_mode": str(task.get("agent_mode") or "ask"),
                "session_alias": str(task.get("session_alias") or ""),
                "session_id": str(task.get("session_id") or ""),
                "create_session": bool(task.get("create_session") or False),
                "append_output": bool(task.get("append_output") or False),
                "allowed_tools": str(task.get("allowed_tools") or ""),
                "disallowed_tools": str(task.get("disallowed_tools") or ""),
            }
            conn.execute(
                """
                INSERT INTO items (
                    ordinal, task_id, task_type, prompt_text, prompt_ref, input_ref, output_ref,
                    workdir, metadata_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    ordinal,
                    str(task["task_id"]),
                    str(task.get("task_type") or "prompt"),
                    str(task["prompt_text"]),
                    str(task.get("prompt_ref") or ""),
                    str(task.get("input_ref") or ""),
                    str(task.get("output_ref") or ""),
                    str(task.get("workdir") or ""),
                    json.dumps(metadata_payload, ensure_ascii=True, sort_keys=True),
                    now,
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def claim_next_task(
    db_path: Path,
    *,
    worker_id: str,
    run_id: str,
    stale_after_seconds: int,
) -> dict[str, Any] | None:
    conn = connect_task_queue(db_path)
    try:
        cutoff = _stale_cutoff(stale_after_seconds)
        now = utc_now()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id, ordinal, task_id, task_type, prompt_text, prompt_ref, input_ref, output_ref,
                   workdir, metadata_json, status, attempts
            FROM items
            WHERE status = 'pending'
               OR (status = 'running' AND claimed_at IS NOT NULL AND claimed_at < ?)
            ORDER BY ordinal ASC
            LIMIT 1
            """,
            (cutoff,),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        conn.execute(
            """
            UPDATE items
            SET status = 'running',
                updated_at = ?,
                claimed_by = ?,
                claimed_at = ?,
                attempts = attempts + 1,
                last_run_id = ?
            WHERE id = ?
            """,
            (now, worker_id, now, run_id, int(row["id"])),
        )
        conn.commit()
        metadata_payload = json.loads(str(row["metadata_json"] or "{}"))
        if not isinstance(metadata_payload, dict):
            metadata_payload = {}
        return {
            "id": int(row["id"]),
            "ordinal": int(row["ordinal"]),
            "task_id": str(row["task_id"]),
            "task_type": str(row["task_type"]),
            "prompt_text": str(row["prompt_text"]),
            "prompt_ref": str(row["prompt_ref"]),
            "input_ref": str(row["input_ref"]),
            "output_ref": str(row["output_ref"]),
            "workdir": str(row["workdir"]),
            "metadata": dict(metadata_payload.get("metadata") or {}),
            "agent_mode": str(metadata_payload.get("agent_mode") or "ask"),
            "session_alias": str(metadata_payload.get("session_alias") or ""),
            "session_id": str(metadata_payload.get("session_id") or ""),
            "create_session": bool(metadata_payload.get("create_session") or False),
            "append_output": bool(metadata_payload.get("append_output") or False),
            "allowed_tools": str(metadata_payload.get("allowed_tools") or ""),
            "disallowed_tools": str(metadata_payload.get("disallowed_tools") or ""),
            "attempts": int(row["attempts"]) + 1,
        }
    finally:
        conn.close()


def mark_task_completed(db_path: Path, *, item_id: int, run_id: str) -> None:
    conn = connect_task_queue(db_path)
    try:
        now = utc_now()
        conn.execute(
            """
            UPDATE items
            SET status = 'completed',
                updated_at = ?,
                finished_at = ?,
                last_run_id = ?
            WHERE id = ?
            """,
            (now, now, run_id, int(item_id)),
        )
        conn.commit()
    finally:
        conn.close()


def mark_task_failed(
    db_path: Path,
    *,
    item_id: int,
    run_id: str,
    error_text: str,
    requeue: bool,
) -> None:
    conn = connect_task_queue(db_path)
    try:
        now = utc_now()
        status = "pending" if requeue else "failed"
        finished_at = None if requeue else now
        conn.execute(
            """
            UPDATE items
            SET status = ?,
                updated_at = ?,
                finished_at = ?,
                last_error = ?,
                last_run_id = ?
            WHERE id = ?
            """,
            (status, now, finished_at, error_text[:4000], run_id, int(item_id)),
        )
        conn.commit()
    finally:
        conn.close()


def queue_counts(db_path: Path) -> dict[str, int]:
    conn = connect_task_queue(db_path)
    try:
        rows = conn.execute(
            "SELECT status, count(*) AS c FROM items GROUP BY status ORDER BY status"
        ).fetchall()
        return {str(row["status"]): int(row["c"]) for row in rows}
    finally:
        conn.close()


def cancel_unfinished_tasks(
    db_path: Path,
    *,
    reason: str,
    run_id: str = "",
) -> dict[str, int]:
    conn = connect_task_queue(db_path)
    try:
        now = utc_now()
        cursor = conn.execute(
            """
            UPDATE items
            SET status = 'cancelled',
                updated_at = ?,
                finished_at = COALESCE(finished_at, ?),
                last_error = ?,
                last_run_id = CASE
                    WHEN ? <> '' THEN ?
                    ELSE last_run_id
                END
            WHERE status IN ('pending', 'running')
            """,
            (now, now, reason[:4000], run_id, run_id),
        )
        conn.commit()
        return {"cancelled": int(cursor.rowcount)}
    finally:
        conn.close()
