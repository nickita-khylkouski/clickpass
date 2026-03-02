from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any


class RecipeStore:
    def __init__(self, db_path: str | Path = "ghostapi/recipes.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recipes (
                    host TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    analysis_json TEXT NOT NULL,
                    endpoints_count INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.commit()

    def upsert(self, host: str, title: str, analysis: dict[str, Any]) -> None:
        payload = json.dumps(analysis)
        endpoints_count = int((analysis.get("summary") or {}).get("endpoints_found", 0))
        updated_at = time.time()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO recipes(host, title, analysis_json, endpoints_count, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(host) DO UPDATE SET
                    title=excluded.title,
                    analysis_json=excluded.analysis_json,
                    endpoints_count=excluded.endpoints_count,
                    updated_at=excluded.updated_at
                """,
                (host, title, payload, endpoints_count, updated_at),
            )
            conn.commit()

    def get(self, host: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT host, title, analysis_json, endpoints_count, updated_at FROM recipes WHERE host = ?",
                (host,),
            ).fetchone()
        if not row:
            return None
        return {
            "host": row[0],
            "title": row[1],
            "analysis": json.loads(row[2]),
            "endpoints_count": row[3],
            "updated_at": row[4],
        }

    def list_all(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT host, title, endpoints_count, updated_at FROM recipes ORDER BY updated_at DESC"
            ).fetchall()
        return [
            {
                "host": row[0],
                "title": row[1],
                "endpoints_count": row[2],
                "updated_at": row[3],
            }
            for row in rows
        ]
