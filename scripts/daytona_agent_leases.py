#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _stale_cutoff(seconds: int) -> str:
    return (datetime.now(tz=UTC) - timedelta(seconds=max(1, int(seconds)))).isoformat()


def connect_leases(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pools (
            pool_name TEXT PRIMARY KEY,
            sandbox_limit INTEGER NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS launches (
            launch_id TEXT PRIMARY KEY,
            pool_name TEXT NOT NULL,
            launch_kind TEXT NOT NULL,
            status TEXT NOT NULL,
            requested_sandboxes INTEGER NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_heartbeat_at TEXT NOT NULL,
            released_at TEXT,
            FOREIGN KEY(pool_name) REFERENCES pools(pool_name)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sandboxes (
            sandbox_name TEXT PRIMARY KEY,
            pool_name TEXT NOT NULL,
            launch_id TEXT NOT NULL,
            lane TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            released_at TEXT,
            FOREIGN KEY(pool_name) REFERENCES pools(pool_name),
            FOREIGN KEY(launch_id) REFERENCES launches(launch_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_launches_pool_status
        ON launches(pool_name, status, released_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sandboxes_pool_status
        ON sandboxes(pool_name, status, released_at)
        """
    )
    conn.commit()
    return conn


@contextmanager
def lease_txn(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect_leases(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_pool(conn: sqlite3.Connection, *, pool_name: str, sandbox_limit: int, metadata: dict[str, object] | None = None) -> None:
    now = utc_now()
    metadata_json = json.dumps(metadata or {}, ensure_ascii=True, sort_keys=True)
    existing = conn.execute(
        "SELECT pool_name FROM pools WHERE pool_name = ?",
        (pool_name,),
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO pools (pool_name, sandbox_limit, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (pool_name, int(sandbox_limit), metadata_json, now, now),
        )
        return
    conn.execute(
        """
        UPDATE pools
        SET sandbox_limit = ?, metadata_json = ?, updated_at = ?
        WHERE pool_name = ?
        """,
        (int(sandbox_limit), metadata_json, now, pool_name),
    )


def pool_capacity(conn: sqlite3.Connection, *, pool_name: str) -> dict[str, int]:
    row = conn.execute(
        "SELECT sandbox_limit FROM pools WHERE pool_name = ?",
        (pool_name,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"Unknown pool: {pool_name}")
    used = conn.execute(
        """
        SELECT count(*) AS c
        FROM sandboxes
        WHERE pool_name = ? AND released_at IS NULL
        """,
        (pool_name,),
    ).fetchone()
    sandbox_limit = int(row["sandbox_limit"])
    in_use = int(used["c"] if used is not None else 0)
    return {
        "sandbox_limit": sandbox_limit,
        "reserved": in_use,
        "available": max(0, sandbox_limit - in_use),
    }


def reserve_launch(
    db_path: Path,
    *,
    pool_name: str,
    sandbox_limit: int,
    launch_id: str,
    launch_kind: str,
    sandbox_specs: list[dict[str, object]],
    metadata: dict[str, object] | None = None,
    pool_metadata: dict[str, object] | None = None,
) -> dict[str, int]:
    with lease_txn(db_path) as conn:
        ensure_pool(conn, pool_name=pool_name, sandbox_limit=sandbox_limit, metadata=pool_metadata)
        capacity = pool_capacity(conn, pool_name=pool_name)
        requested = len(sandbox_specs)
        if capacity["available"] < requested:
            raise SystemExit(
                f"Pool '{pool_name}' only has {capacity['available']} free sandboxes; "
                f"requested {requested}. Increase --pool-limit or wait for other launches to finish."
            )
        now = utc_now()
        try:
            conn.execute(
                """
                INSERT INTO launches (
                    launch_id, pool_name, launch_kind, status, requested_sandboxes,
                    metadata_json, created_at, updated_at, last_heartbeat_at
                )
                VALUES (?, ?, ?, 'reserved', ?, ?, ?, ?, ?)
                """,
                (
                    launch_id,
                    pool_name,
                    launch_kind,
                    requested,
                    json.dumps(metadata or {}, ensure_ascii=True, sort_keys=True),
                    now,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise SystemExit(
                f"Launch '{launch_id}' already exists in pool '{pool_name}'. "
                "Choose a new --launch-id or stop/reap the existing launch first."
            ) from exc
        for spec in sandbox_specs:
            sandbox_name = str(spec["sandbox_name"])
            try:
                conn.execute(
                    """
                    INSERT INTO sandboxes (
                        sandbox_name, pool_name, launch_id, lane, status, metadata_json,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, 'reserved', ?, ?, ?)
                    """,
                    (
                        sandbox_name,
                        pool_name,
                        launch_id,
                        str(spec.get("lane", "") or ""),
                        json.dumps(spec.get("metadata") or {}, ensure_ascii=True, sort_keys=True),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise SystemExit(
                    f"Sandbox name collision for '{sandbox_name}'. "
                    "This usually means two launches generated the same sandbox prefix. "
                    "Retry with a different --launch-id or --sandbox-prefix."
                ) from exc
        return pool_capacity(conn, pool_name=pool_name)


def activate_launch(db_path: Path, *, launch_id: str, metadata_patch: dict[str, object] | None = None) -> None:
    with lease_txn(db_path) as conn:
        now = utc_now()
        row = conn.execute("SELECT metadata_json FROM launches WHERE launch_id = ?", (launch_id,)).fetchone()
        if row is None:
            raise SystemExit(f"Unknown launch_id: {launch_id}")
        metadata = json.loads(str(row["metadata_json"] or "{}"))
        if metadata_patch:
            metadata.update(metadata_patch)
        conn.execute(
            """
            UPDATE launches
            SET status = 'active', metadata_json = ?, updated_at = ?, last_heartbeat_at = ?
            WHERE launch_id = ?
            """,
            (json.dumps(metadata, ensure_ascii=True, sort_keys=True), now, now, launch_id),
        )
        conn.execute(
            """
            UPDATE sandboxes
            SET status = 'active', updated_at = ?
            WHERE launch_id = ? AND released_at IS NULL
            """,
            (now, launch_id),
        )


def heartbeat_launch(db_path: Path, *, launch_id: str, status: str | None = None) -> None:
    with lease_txn(db_path) as conn:
        now = utc_now()
        if status:
            conn.execute(
                "UPDATE launches SET status = ?, updated_at = ?, last_heartbeat_at = ? WHERE launch_id = ?",
                (status, now, now, launch_id),
            )
        else:
            conn.execute(
                "UPDATE launches SET updated_at = ?, last_heartbeat_at = ? WHERE launch_id = ?",
                (now, now, launch_id),
            )


def release_launch(db_path: Path, *, launch_id: str, final_status: str = "released") -> None:
    with lease_txn(db_path) as conn:
        now = utc_now()
        conn.execute(
            """
            UPDATE launches
            SET status = ?, updated_at = ?, last_heartbeat_at = ?, released_at = COALESCE(released_at, ?)
            WHERE launch_id = ?
            """,
            (final_status, now, now, now, launch_id),
        )
        conn.execute(
            """
            UPDATE sandboxes
            SET status = ?, updated_at = ?, released_at = COALESCE(released_at, ?)
            WHERE launch_id = ? AND released_at IS NULL
            """,
            (final_status, now, now, launch_id),
        )


def stale_launch_ids(db_path: Path, *, stale_after_seconds: int) -> list[str]:
    conn = connect_leases(db_path)
    try:
        cutoff = _stale_cutoff(stale_after_seconds)
        rows = conn.execute(
            """
            SELECT launch_id
            FROM launches
            WHERE released_at IS NULL
              AND status IN ('reserved', 'active', 'running')
              AND last_heartbeat_at < ?
            ORDER BY last_heartbeat_at ASC, launch_id ASC
            """,
            (cutoff,),
        ).fetchall()
        return [str(row["launch_id"]) for row in rows]
    finally:
        conn.close()


def list_launches(db_path: Path, *, only_active: bool = False) -> list[dict[str, object]]:
    conn = connect_leases(db_path)
    try:
        sql = (
            "SELECT launch_id, pool_name, launch_kind, status, requested_sandboxes, metadata_json, "
            "created_at, updated_at, last_heartbeat_at, released_at "
            "FROM launches "
        )
        if only_active:
            sql += "WHERE released_at IS NULL "
        sql += "ORDER BY created_at DESC, launch_id DESC"
        rows = conn.execute(sql).fetchall()
        out: list[dict[str, object]] = []
        for row in rows:
            out.append(
                {
                    "launch_id": str(row["launch_id"]),
                    "pool_name": str(row["pool_name"]),
                    "launch_kind": str(row["launch_kind"]),
                    "status": str(row["status"]),
                    "requested_sandboxes": int(row["requested_sandboxes"]),
                    "metadata": json.loads(str(row["metadata_json"] or "{}")),
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                    "last_heartbeat_at": str(row["last_heartbeat_at"]),
                    "released_at": str(row["released_at"] or ""),
                }
            )
        return out
    finally:
        conn.close()


def list_sandboxes(db_path: Path, *, pool_name: str | None = None, only_active: bool = False) -> list[dict[str, object]]:
    conn = connect_leases(db_path)
    try:
        clauses: list[str] = []
        params: list[object] = []
        if pool_name:
            clauses.append("pool_name = ?")
            params.append(pool_name)
        if only_active:
            clauses.append("released_at IS NULL")
        sql = (
            "SELECT sandbox_name, pool_name, launch_id, lane, status, metadata_json, "
            "created_at, updated_at, released_at FROM sandboxes"
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY pool_name ASC, launch_id ASC, sandbox_name ASC"
        rows = conn.execute(sql, params).fetchall()
        out: list[dict[str, object]] = []
        for row in rows:
            out.append(
                {
                    "sandbox_name": str(row["sandbox_name"]),
                    "pool_name": str(row["pool_name"]),
                    "launch_id": str(row["launch_id"]),
                    "lane": str(row["lane"]),
                    "status": str(row["status"]),
                    "metadata": json.loads(str(row["metadata_json"] or "{}")),
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                    "released_at": str(row["released_at"] or ""),
                }
            )
        return out
    finally:
        conn.close()


def pool_snapshot(db_path: Path) -> list[dict[str, object]]:
    conn = connect_leases(db_path)
    try:
        rows = conn.execute(
            "SELECT pool_name, sandbox_limit, metadata_json FROM pools ORDER BY pool_name ASC"
        ).fetchall()
        out: list[dict[str, object]] = []
        for row in rows:
            cap = pool_capacity(conn, pool_name=str(row["pool_name"]))
            out.append(
                {
                    "pool_name": str(row["pool_name"]),
                    "sandbox_limit": int(row["sandbox_limit"]),
                    "reserved": int(cap["reserved"]),
                    "available": int(cap["available"]),
                    "metadata": json.loads(str(row["metadata_json"] or "{}")),
                }
            )
        return out
    finally:
        conn.close()
