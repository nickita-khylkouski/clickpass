#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from build_attendee_dossiers import build_seed_people, enrich_people, hydrate_enrichment_config
from cv_rank.config import load_config


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "outputs" / "attendee_dossier_queue" / "queue.sqlite3"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "outputs" / "attendee_dossier_queue"
DEFAULT_PROMPT = REPO_ROOT / "prompts" / "wafer_attendee_dossier_system_prompt.txt"
DEFAULT_DOSSIER_SCRIPT = REPO_ROOT / "scripts" / "build_attendee_dossiers.py"
DEFAULT_DAYTONA_RUNNER = Path("/Users/nickita/.superset/worktrees/start/beaded-mind/daytona_cv_rank_remote.py")
DEFAULT_CONFIG = REPO_ROOT / "config.yaml"


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def utc_after(seconds: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + max(0, int(seconds))))


def load_env() -> None:
    load_dotenv(REPO_ROOT / ".env")


def connect_platform_db() -> psycopg2.extensions.connection:
    dsn = os.environ.get("PLATFORM_DATABASE_URL", "").strip()
    if not dsn:
        raise SystemExit("Missing PLATFORM_DATABASE_URL in /Users/nickita/cv-rank/.env")
    conn = psycopg2.connect(dsn)
    return conn


def connect_queue(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            queue_name TEXT NOT NULL,
            priority_rank INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            run_id TEXT,
            run_dir TEXT,
            user_id TEXT,
            email TEXT NOT NULL,
            name TEXT NOT NULL,
            linkedin_username TEXT,
            github_username TEXT,
            x_handle TEXT,
            site_url TEXT,
            description TEXT,
            details_json TEXT,
            location TEXT,
            latest_checked_in_event TEXT,
            total_applications INTEGER NOT NULL DEFAULT 0,
            total_checkins INTEGER NOT NULL DEFAULT 0,
            unique_events_signed_up INTEGER NOT NULL DEFAULT 0,
            unique_events_checked_in INTEGER NOT NULL DEFAULT 0,
            latest_application_at TEXT,
            UNIQUE(queue_name, email)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_jobs_queue_status_priority
        ON jobs(queue_name, status, priority_rank, id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_jobs_queue_priority
        ON jobs(queue_name, priority_rank, id)
        """
    )
    _ensure_job_columns(
        conn,
        {
            "backend": "TEXT",
            "requested_model": "TEXT",
            "web_mode": "TEXT",
            "worker_pid": "INTEGER",
            "worker_host": "TEXT",
            "last_exit_code": "INTEGER",
            "failure_bucket": "TEXT",
            "available_after": "TEXT",
        },
    )
    conn.commit()
    return conn


def _ensure_job_columns(conn: sqlite3.Connection, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    for name, type_sql in columns.items():
        if name in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {type_sql}")
            conn.commit()
            existing.add(name)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" in str(exc).lower():
                existing.add(name)
                continue
            raise


def ranked_people_query() -> str:
    return """
        WITH per_person AS (
            SELECT
                up."userId"::text AS user_id,
                LOWER(up.email) AS email,
                COALESCE(
                    NULLIF(TRIM(CONCAT(COALESCE(up."firstName", ''), ' ', COALESCE(up."lastName", ''))), ''),
                    LOWER(up.email)
                ) AS name,
                up."firstName" AS first_name,
                up."lastName" AS last_name,
                up."linkedinUsername" AS linkedin_username,
                up."githubUsername" AS github_username,
                up."xHandle" AS x_handle,
                up."siteUrl" AS site_url,
                up.description,
                up.details,
                up.location,
                COUNT(*)::int AS total_applications,
                COUNT(*) FILTER (WHERE ea."checkedIn" = TRUE)::int AS total_checkins,
                COUNT(DISTINCT ea."eventId")::int AS unique_events_signed_up,
                COUNT(DISTINCT ea."eventId") FILTER (WHERE ea."checkedIn" = TRUE)::int AS unique_events_checked_in,
                MAX(pe.title) FILTER (WHERE ea."checkedIn" = TRUE) AS latest_checked_in_event,
                MAX(ea."createdAt") AS latest_application_at
            FROM "UserProfile" up
            JOIN "EventApplicant" ea ON ea."userId" = up."userId"
            JOIN "PlatformEvent" pe ON pe.id = ea."eventId"
            WHERE up.email IS NOT NULL AND up.email <> ''
            GROUP BY
                up."userId",
                up.email,
                up."firstName",
                up."lastName",
                up."linkedinUsername",
                up."githubUsername",
                up."xHandle",
                up."siteUrl",
                up.description,
                up.details,
                up.location
        )
        SELECT
            *,
            ROW_NUMBER() OVER (
                ORDER BY
                    unique_events_signed_up DESC,
                    unique_events_checked_in DESC,
                    total_applications DESC,
                    total_checkins DESC,
                    latest_application_at DESC
            )::int AS priority_rank
        FROM per_person
        ORDER BY priority_rank
    """


def fetch_ranked_people(limit: int | None, offset: int) -> list[dict[str, Any]]:
    conn = connect_platform_db()
    try:
        query = ranked_people_query()
        if limit is not None:
            query += " LIMIT %s OFFSET %s"
            params = [limit, offset]
        else:
            query += " OFFSET %s"
            params = [offset]
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def normalize_people_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for idx, row in enumerate(rows, start=1):
        normalized.append(
            {
                "priority_rank": int(row.get("priority_rank") or idx),
                "user_id": row.get("user_id") or row.get("userId"),
                "email": (row.get("email") or "").strip().lower(),
                "name": (
                    row.get("name")
                    or " ".join(
                        part for part in [row.get("first_name") or row.get("firstName"), row.get("last_name") or row.get("lastName")] if part
                    ).strip()
                    or (row.get("email") or "").strip().lower()
                ),
                "linkedin_username": row.get("linkedin_username") or row.get("linkedinUsername"),
                "github_username": row.get("github_username") or row.get("githubUsername"),
                "x_handle": row.get("x_handle") or row.get("xHandle"),
                "site_url": row.get("site_url") or row.get("siteUrl"),
                "description": row.get("description"),
                "details": row.get("details"),
                "location": row.get("location") or row.get("profile_location") or row.get("profileLocation"),
                "latest_checked_in_event": row.get("latest_checked_in_event") or row.get("latestCheckedInEvent"),
                "total_applications": int(row.get("total_applications") or row.get("total_ny_applications") or 0),
                "total_checkins": int(row.get("total_checkins") or row.get("total_ny_checkins") or 0),
                "unique_events_signed_up": int(row.get("unique_events_signed_up") or row.get("unique_ny_events_signed_up") or 0),
                "unique_events_checked_in": int(row.get("unique_events_checked_in") or row.get("unique_ny_events_checked_in") or 0),
                "latest_application_at": row.get("latest_application_at") or row.get("latest_ny_application_at"),
            }
        )
    return normalized


def load_people_file(path: Path) -> list[dict[str, Any]]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise SystemExit(f"Expected list in people file: {path}")
    normalized = normalize_people_rows([dict(row) for row in rows])
    normalized.sort(key=lambda row: int(row["priority_rank"]))
    for idx, row in enumerate(normalized, start=1):
        row["priority_rank"] = idx
    return normalized


def upsert_jobs(conn: sqlite3.Connection, queue_name: str, rows: list[dict[str, Any]]) -> int:
    inserted = 0
    now = utc_now()
    for row in rows:
        details_json = json.dumps(row.get("details"), default=str) if row.get("details") is not None else None
        cursor = conn.execute(
            """
            INSERT INTO jobs (
                queue_name, priority_rank, status, created_at, updated_at,
                user_id, email, name, linkedin_username, github_username, x_handle,
                site_url, description, details_json, location, latest_checked_in_event,
                total_applications, total_checkins, unique_events_signed_up, unique_events_checked_in,
                latest_application_at
            )
            VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(queue_name, email) DO UPDATE SET
                priority_rank=excluded.priority_rank,
                updated_at=excluded.updated_at,
                user_id=excluded.user_id,
                name=excluded.name,
                linkedin_username=excluded.linkedin_username,
                github_username=excluded.github_username,
                x_handle=excluded.x_handle,
                site_url=excluded.site_url,
                description=excluded.description,
                details_json=excluded.details_json,
                location=excluded.location,
                latest_checked_in_event=excluded.latest_checked_in_event,
                total_applications=excluded.total_applications,
                total_checkins=excluded.total_checkins,
                unique_events_signed_up=excluded.unique_events_signed_up,
                unique_events_checked_in=excluded.unique_events_checked_in,
                latest_application_at=excluded.latest_application_at
            """,
            (
                queue_name,
                int(row["priority_rank"]),
                now,
                now,
                row.get("user_id"),
                row.get("email"),
                row.get("name"),
                row.get("linkedin_username"),
                row.get("github_username"),
                row.get("x_handle"),
                row.get("site_url"),
                row.get("description"),
                details_json,
                row.get("location"),
                row.get("latest_checked_in_event"),
                int(row.get("total_applications") or 0),
                int(row.get("total_checkins") or 0),
                int(row.get("unique_events_signed_up") or 0),
                int(row.get("unique_events_checked_in") or 0),
                row.get("latest_application_at"),
            ),
        )
        if cursor.rowcount:
            inserted += 1
    conn.commit()
    return inserted


@dataclass
class QueueJob:
    id: int
    queue_name: str
    priority_rank: int
    email: str
    name: str
    attempts: int
    row: dict[str, Any]


def claim_next_job(conn: sqlite3.Connection, queue_name: str, *, max_attempts: int) -> QueueJob | None:
    while True:
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = utc_now()
            row = conn.execute(
                """
                SELECT *
                FROM jobs
                WHERE queue_name = ? AND status = 'pending'
                  AND attempts < ?
                  AND (available_after IS NULL OR available_after <= ?)
                ORDER BY priority_rank ASC, id ASC
                LIMIT 1
                """,
                (queue_name, max_attempts, now),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = 'running',
                    started_at = ?,
                    updated_at = ?,
                    attempts = attempts + 1,
                    available_after = NULL
                WHERE id = ? AND status = 'pending'
                """,
                (now, now, row["id"]),
            )
            if cursor.rowcount == 0:
                conn.rollback()
                continue
            conn.commit()
            attempts = int(row["attempts"]) + 1
            return QueueJob(
                id=int(row["id"]),
                queue_name=str(row["queue_name"]),
                priority_rank=int(row["priority_rank"]),
                email=str(row["email"]),
                name=str(row["name"]),
                attempts=attempts,
                row=dict(row),
            )
        except Exception:
            conn.rollback()
            raise


def finish_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    status: str,
    run_id: str | None = None,
    run_dir: str | None = None,
    error: str | None = None,
) -> None:
    now = utc_now()
    conn.execute(
        """
        UPDATE jobs
        SET status = ?,
            finished_at = ?,
            updated_at = ?,
            run_id = COALESCE(?, run_id),
            run_dir = COALESCE(?, run_dir),
            last_error = ?
        WHERE id = ?
        """,
        (status, now, now, run_id, run_dir, error, job_id),
    )
    conn.commit()


def set_job_run_metadata(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    run_id: str,
    run_dir: str,
    backend: str | None = None,
    requested_model: str | None = None,
    web_mode: str | None = None,
    worker_pid: int | None = None,
    worker_host: str | None = None,
) -> None:
    now = utc_now()
    conn.execute(
        """
        UPDATE jobs
        SET updated_at = ?,
            run_id = ?,
            run_dir = ?,
            backend = COALESCE(?, backend),
            requested_model = COALESCE(?, requested_model),
            web_mode = COALESCE(?, web_mode),
            worker_pid = COALESCE(?, worker_pid),
            worker_host = COALESCE(?, worker_host)
        WHERE id = ?
        """,
        (now, run_id, run_dir, backend, requested_model, web_mode, worker_pid, worker_host, job_id),
    )
    conn.commit()


def requeue_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    error: str | None = None,
    delay_seconds: int = 0,
) -> None:
    now = utc_now()
    available_after = utc_after(delay_seconds) if delay_seconds > 0 else None
    conn.execute(
        """
        UPDATE jobs
        SET status = 'pending',
            started_at = NULL,
            finished_at = NULL,
            updated_at = ?,
            last_error = ?,
            last_exit_code = NULL,
            failure_bucket = NULL,
            available_after = ?
        WHERE id = ?
        """,
        (now, error, available_after, job_id),
    )
    conn.commit()


def build_run_id(job: QueueJob) -> str:
    return f"{job.queue_name}-{job.priority_rank:05d}"


def build_run_dir(queue_root: Path, run_id: str) -> Path:
    return queue_root / "runs" / run_id


def reset_run_dir(run_dir: Path) -> None:
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)


def recover_stale_running_jobs(
    conn: sqlite3.Connection,
    *,
    queue_name: str,
    stale_after_minutes: int,
    max_attempts: int,
) -> int:
    rows = conn.execute(
        """
        SELECT id, attempts, started_at
        FROM jobs
        WHERE queue_name = ? AND status = 'running'
        """,
        (queue_name,),
    ).fetchall()
    recovered = 0
    now_ts = time.time()
    threshold = stale_after_minutes * 60
    for row in rows:
        started_at = row["started_at"]
        stale = False
        if not started_at:
            stale = True
        else:
            try:
                started_ts = time.mktime(time.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ"))
                stale = (now_ts - started_ts) >= threshold
            except Exception:
                stale = True
        if not stale:
            continue
        attempts = int(row["attempts"] or 0)
        if attempts < max_attempts:
            requeue_job(
                conn,
                int(row["id"]),
                error=f"Automatically requeued stale running job after {stale_after_minutes} minutes.",
            )
        else:
            finish_job(
                conn,
                int(row["id"]),
                status="failed",
                error=f"Marked failed after stale running recovery; attempts={attempts}.",
            )
        recovered += 1
    return recovered


def row_to_person_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        row = dict(row)
    details_json = row.get("details_json")
    details = json.loads(details_json) if details_json else None
    name = str(row.get("name") or row.get("email") or "").strip()
    first_name = ""
    last_name = ""
    if name and "@" not in name and " " in name:
        parts = name.split()
        first_name = parts[0]
        last_name = " ".join(parts[1:])
    return {
        "user_id": row.get("user_id"),
        "email": row.get("email"),
        "name": name,
        "first_name": first_name,
        "last_name": last_name,
        "linkedin_username": row.get("linkedin_username"),
        "github_username": row.get("github_username"),
        "x_handle": row.get("x_handle"),
        "site_url": row.get("site_url"),
        "description": row.get("description"),
        "details": details,
        "location": row.get("location"),
        "latest_checked_in_event": row.get("latest_checked_in_event"),
        "company": "",
        "title": "",
    }


def build_prefetched_people_payload(row: sqlite3.Row | dict[str, Any], config_path: Path | None) -> list[dict[str, Any]]:
    seed_row = row_to_person_payload(row)
    config = hydrate_enrichment_config(
        load_config(str(config_path) if config_path and config_path.exists() else None)
    )
    enrichment_cfg = config.setdefault("enrichment", {})
    supabase_cfg = enrichment_cfg.setdefault("supabase", {})
    github_cfg = enrichment_cfg.setdefault("github", {})
    platform_cfg = enrichment_cfg.setdefault("platform_db", {})
    exa_cfg = enrichment_cfg.setdefault("exa", {})

    # Queue prefetch should be a narrow local enrichment step, not a broad
    # directory scrape. Default to Postgres + GitHub and keep Supabase off
    # unless explicitly re-enabled for a special backfill.
    if os.environ.get("CV_RANK_PREFETCH_SUPABASE", "").strip().lower() not in {"1", "true", "yes"}:
        supabase_cfg["enabled"] = False
    github_cfg["enabled"] = bool(github_cfg.get("token"))
    platform_cfg["enabled"] = bool(platform_cfg.get("dsn"))
    exa_cfg["enabled"] = False

    people = enrich_people(build_seed_people([seed_row]), config)
    people[0]["_prefetched_enrichment"] = {
        "supabase": bool(supabase_cfg.get("enabled") and supabase_cfg.get("url") and supabase_cfg.get("key")),
        "github": bool(github_cfg.get("enabled") and github_cfg.get("token")),
        "platform_db": bool(platform_cfg.get("enabled") and platform_cfg.get("dsn")),
    }
    return people


def load_or_build_prefetched_people_payload(
    people_file: Path,
    row: sqlite3.Row | dict[str, Any],
    config_path: Path | None,
) -> list[dict[str, Any]]:
    expected_email = str((dict(row) if not isinstance(row, dict) else row).get("email") or "").strip().lower()
    if people_file.exists():
        try:
            payload = json.loads(people_file.read_text(encoding="utf-8"))
            if (
                isinstance(payload, list)
                and len(payload) == 1
                and isinstance(payload[0], dict)
                and str(payload[0].get("email") or "").strip().lower() == expected_email
                and isinstance(payload[0].get("_prefetched_enrichment"), dict)
            ):
                return payload
        except Exception:
            pass
    return build_prefetched_people_payload(row, config_path)


def run_single_dossier(
    job: QueueJob,
    *,
    queue_root: Path,
    prompt_file: Path,
    dossier_script: Path,
    config_path: Path,
    wafer_api_key_file: str | None,
    exa_api_key_file: str | None,
    wafer_web_mode: str,
    wafer_timeout_seconds: int,
    fallback_model: str | None,
    claude_model: str | None,
    effort: str | None,
    claude_output_format: str,
    wafer_synthesis_only: bool,
    wafer_expansion_pass: bool,
    wafer_braindump_pass: bool,
) -> tuple[int, str, str, str]:
    people_dir = queue_root / "people"
    people_dir.mkdir(parents=True, exist_ok=True)
    slug = job.email.replace("@", "-at-").replace("/", "-")
    people_file = people_dir / f"{job.priority_rank:05d}-{slug}.json"
    people_payload = load_or_build_prefetched_people_payload(people_file, job.row, config_path)
    people_file.write_text(json.dumps(people_payload, indent=2, default=str) + "\n", encoding="utf-8")

    run_id = build_run_id(job)
    output_dir = queue_root / "runs"
    run_dir = output_dir / run_id
    reset_run_dir(run_dir)

    cmd = [
        sys.executable,
        str(dossier_script),
        "--people-file",
        str(people_file),
        "--limit",
        "1",
        "--output-dir",
        str(output_dir),
        "--run-id",
        run_id,
        "--system-prompt-file",
        str(prompt_file),
        "--config",
        str(config_path),
        "--wafer-web-mode",
        wafer_web_mode,
        "--wafer-timeout-seconds",
        str(wafer_timeout_seconds),
    ]
    if fallback_model:
        cmd.extend(["--fallback-model", fallback_model])
    if claude_model:
        cmd.extend(["--claude-model", claude_model])
    if effort:
        cmd.extend(["--effort", effort])
    if claude_output_format:
        cmd.extend(["--claude-output-format", claude_output_format])
    cmd.append("--wafer-synthesis-only" if wafer_synthesis_only else "--no-wafer-synthesis-only")
    cmd.append("--wafer-expansion-pass" if wafer_expansion_pass else "--no-wafer-expansion-pass")
    cmd.append("--wafer-braindump-pass" if wafer_braindump_pass else "--no-wafer-braindump-pass")
    if wafer_api_key_file:
        cmd.extend(["--wafer-api-key-file", wafer_api_key_file])
    if exa_api_key_file:
        cmd.extend(["--exa-api-key-file", exa_api_key_file])

    result = subprocess.run(cmd, text=True, capture_output=True, check=False)
    return result.returncode, result.stdout, result.stderr, str(run_dir)


def run_single_dossier_daytona(
    job: QueueJob,
    *,
    queue_root: Path,
    prompt_file: Path,
    daytona_runner: Path,
    config_path: Path | None,
    wafer_web_mode: str,
    wafer_timeout_seconds: int,
    claude_model: str | None,
    effort: str | None,
    wafer_synthesis_only: bool,
    wafer_expansion_pass: bool,
    wafer_braindump_pass: bool,
) -> tuple[int, str, str, str]:
    people_dir = queue_root / "people"
    people_dir.mkdir(parents=True, exist_ok=True)
    slug = job.email.replace("@", "-at-").replace("/", "-")
    people_file = people_dir / f"{job.priority_rank:05d}-{slug}.json"
    people_payload = load_or_build_prefetched_people_payload(people_file, job.row, config_path)
    people_file.write_text(json.dumps(people_payload, indent=2, default=str) + "\n", encoding="utf-8")

    run_id = build_run_id(job)
    output_root = queue_root / "runs"
    run_dir = output_root / run_id
    reset_run_dir(run_dir)

    cmd = [
        os.environ.get("DAYTONA_PYTHON_BIN") or sys.executable,
        str(daytona_runner),
        "--local-repo-root",
        str(REPO_ROOT),
        "--local-people-file",
        str(people_file),
        "--local-system-prompt",
        str(prompt_file),
        "--local-output-root",
        str(output_root),
        "--run-id",
        run_id,
        "--limit",
        "1",
        "--model",
        claude_model or "gpt-5.4",
        "--web-mode",
        wafer_web_mode,
        "--timeout-seconds",
        str(wafer_timeout_seconds),
        "--effort",
        effort or "low",
    ]
    cmd.append("--wafer-synthesis-only" if wafer_synthesis_only else "--no-wafer-synthesis-only")
    cmd.append("--wafer-expansion-pass" if wafer_expansion_pass else "--no-wafer-expansion-pass")
    cmd.append("--wafer-braindump-pass" if wafer_braindump_pass else "--no-wafer-braindump-pass")
    if config_path and config_path.exists():
        cmd.extend(["--local-config", str(config_path)])

    result = subprocess.run(cmd, text=True, capture_output=True, check=False)
    return result.returncode, result.stdout, result.stderr, str(run_dir)


def _looks_like_bad_terminal_output(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    needles = [
        "you've hit your limit",
        "you have hit your limit",
        "rate_limit_event",
        "ratelimittype",
        "resets 8pm",
        "resets ",
        "failed to authenticate",
        "invalid authentication credentials",
        "authentication_error",
        "insufficient_quota",
        "exceeded your current quota",
        "credit balance is too low",
        "quota exceeded",
        "billing_hard_limit",
        "rate limit exceeded",
        "429 too many requests",
        "status code 429",
        "status code 529",
        "traffic is currently high",
        "try again shortly",
        "try again later",
        "overloaded_error",
        "all minimax api keys are on cooldown",
        "token plan is designed for individual",
    ]
    return any(needle in lowered for needle in needles)


def _classify_failure_text(text: str) -> str | None:
    lowered = (text or "").lower()
    if not lowered:
        return None
    checks = [
        ("Codex usage limit shell", ["you've hit your limit", "you have hit your limit", "resets 8pm", "resets "]),
        ("Exa credits exhausted", ["no_more_credits", "status code 402"]),
        ("Exa invalid API key", ["invalid_api_key", "status code 401"]),
        ("Claude authentication failure", ["failed to authenticate", "invalid authentication credentials", "authentication_error"]),
        ("Provider hard quota exhausted", [
            "insufficient_quota",
            "exceeded your current quota",
            "credit balance is too low",
            "quota exceeded",
            "billing_hard_limit",
        ]),
        ("Provider transient rate limit", [
            "rate limit exceeded",
            "429 too many requests",
            "status code 429",
            "status 429",
            "status code 529",
            "status 529",
            "traffic is currently high",
            "try again shortly",
            "try again later",
            "overloaded_error",
            "all minimax api keys are on cooldown",
            "token plan is designed for individual",
        ]),
        ("Daytona apt/dpkg lock during bootstrap", ["could not get lock", "unable to acquire the dpkg frontend lock"]),
        ("Remote output archive missing", ["cannot stat", "failed to archive remote output"]),
        ("Missing shell/runtime path", ["/bin/zsh", "filenotfounderror"]),
        ("Root permission / launcher mismatch", ["dangerously-skip-permissions cannot be used with root"]),
        ("MiniMax native web-search parameter error", ["invalid params, function name or parameters is empty", "invalid_request_error"]),
        ("Supabase enrichment HTTP 400", ["supabase x: http 400"]),
    ]
    for label, needles in checks:
        if any(needle in lowered for needle in needles):
            return label
    return None


def _is_terminal_failure_bucket(bucket: str | None) -> bool:
    return bucket in {
        "Codex usage limit shell",
        "Exa credits exhausted",
        "Exa invalid API key",
        "Claude authentication failure",
        "Provider hard quota exhausted",
    }


def infer_backend(
    *,
    execution_backend: str,
    daytona_runner: Path | None,
    claude_model: str | None,
) -> str:
    if execution_backend == "daytona" and daytona_runner is not None:
        name = daytona_runner.name.lower()
        if "minimax" in name:
            return "minimax"
        if "wafer" in name:
            return "wafer"
        if "plain" in name:
            return "claude"
        if "codex" in name or "cv_rank" in name:
            if claude_model and "gpt-5" in claude_model.lower():
                return "codex"
            return "codex"
    if claude_model:
        lowered = claude_model.lower()
        if "minimax" in lowered:
            return "minimax"
        if "qwen" in lowered:
            return "wafer"
        if "gpt-5" in lowered:
            return "codex"
    return "claude"


def write_attempt_telemetry(
    run_dir: Path,
    *,
    job: QueueJob,
    backend: str,
    execution_backend: str,
    requested_model: str | None,
    web_mode: str,
    command: list[str],
    return_code: int | None,
    stdout: str,
    stderr: str,
    validation_error: str | None,
    failure_bucket: str | None,
    final_status: str,
    terminal_failure: bool,
) -> None:
    telemetry_dir = run_dir / "telemetry"
    telemetry_dir.mkdir(parents=True, exist_ok=True)
    stdout_tail = (stdout or "")[-4000:]
    stderr_tail = (stderr or "")[-4000:]
    artifact_counts = {}
    for subdir in ("dossiers", "usage", "telemetry", "raw_wafer", "contexts", "seed_evidence"):
        path = run_dir / subdir
        artifact_counts[subdir] = len(list(path.iterdir())) if path.exists() and path.is_dir() else 0
    payload = {
        "queue_name": job.queue_name,
        "job_id": job.id,
        "priority_rank": job.priority_rank,
        "run_id": run_dir.name,
        "name": job.name,
        "email": job.email,
        "attempt": job.attempts,
        "backend": backend,
        "execution_backend": execution_backend,
        "requested_model": requested_model,
        "web_mode": web_mode,
        "worker_pid": os.getpid(),
        "worker_host": socket.gethostname(),
        "command": command,
        "return_code": return_code,
        "validation_error": validation_error,
        "failure_bucket": failure_bucket,
        "final_status": final_status,
        "terminal_failure": terminal_failure,
        "stdout_chars": len(stdout or ""),
        "stderr_chars": len(stderr or ""),
        "stdout_sha256": hashlib.sha256((stdout or "").encode("utf-8", errors="ignore")).hexdigest() if stdout is not None else None,
        "stderr_sha256": hashlib.sha256((stderr or "").encode("utf-8", errors="ignore")).hexdigest() if stderr is not None else None,
        "artifact_counts": artifact_counts,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "written_at": utc_now(),
    }
    (telemetry_dir / "queue_attempt.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _build_failure_summary(run_dir: Path, stdout: str, stderr: str, validation_error: str | None = None) -> str:
    parts: list[str] = []
    combined = "\n".join(filter(None, [validation_error or "", stderr or "", stdout or ""]))
    runner_stdout = run_dir / "daytona-runner.stdout.txt"
    if runner_stdout.exists():
        try:
            combined = "\n".join([combined, runner_stdout.read_text(encoding="utf-8", errors="replace")])
        except Exception:
            pass
    label = _classify_failure_text(combined)
    if label:
        parts.append(label)
    if validation_error:
        parts.append(validation_error)
    excerpt = (stderr or stdout or "").strip()
    if not excerpt and runner_stdout.exists():
        try:
            excerpt = runner_stdout.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            excerpt = ""
    excerpt = excerpt[-2500:].strip()
    if excerpt:
        parts.append(excerpt)
    return "\n".join(part for part in parts if part).strip() or "Unknown runner failure"


def _payload_has_real_substance(payload: dict[str, Any]) -> bool:
    raw_agent_text = str(
        payload.get("agent_returned_text")
        or payload.get("_agent_returned_text")
        or ""
    ).strip()
    if len(raw_agent_text) >= 400 and not _looks_like_bad_terminal_output(raw_agent_text):
        return True

    subject = payload.get("subject")
    current_snapshot = payload.get("current_snapshot")
    research_gaps = payload.get("research_gaps")
    possible_updates = payload.get("possible_updates")
    confirmed_updates = payload.get("confirmed_updates")
    public_outputs = payload.get("public_outputs")
    evidence_clips = payload.get("evidence_clips")

    if isinstance(subject, dict):
        identity_status = str(subject.get("identity_status") or "").strip().lower()
        identity_summary = str(subject.get("identity_summary") or "").strip().lower()
        if identity_status == "error":
            return False
        if "wafer research failed" in identity_summary or "file not founderror" in identity_summary:
            return False
        for key in ("identity_summary", "identity_verification", "notes"):
            value = subject.get(key)
            if isinstance(value, str) and len(value.strip()) >= 120:
                return True
            if isinstance(value, dict) and len(json.dumps(value, default=str)) >= 120:
                return True
    if isinstance(current_snapshot, dict):
        dense_values = [str(v).strip() for v in current_snapshot.values() if isinstance(v, str) and str(v).strip()]
        if dense_values:
            return True
    for section in (research_gaps, possible_updates, confirmed_updates, public_outputs, evidence_clips):
        if isinstance(section, list) and section:
            return True

    return len(json.dumps(payload, default=str)) >= 800


def _payload_signal_counts(payload: dict[str, Any]) -> tuple[int, int, int, int]:
    confirmed_updates = payload.get("confirmed_updates")
    public_outputs = payload.get("public_outputs")
    evidence_clips = payload.get("evidence_clips")
    possible_updates = payload.get("possible_updates")
    return (
        len(confirmed_updates) if isinstance(confirmed_updates, list) else 0,
        len(public_outputs) if isinstance(public_outputs, list) else 0,
        len(evidence_clips) if isinstance(evidence_clips, list) else 0,
        len(possible_updates) if isinstance(possible_updates, list) else 0,
    )


def validate_completed_run(run_dir: Path) -> str | None:
    dossier_dir = run_dir / "dossiers"
    if not dossier_dir.exists():
        return f"Missing dossier directory: {dossier_dir}"

    agent_files = sorted(dossier_dir.glob("*.agent.txt"))
    for path in agent_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        if _looks_like_bad_terminal_output(text):
            return f"Rate-limit shell output detected in {path.name}"

    json_files = sorted(dossier_dir.glob("*.json"))
    if not json_files:
        return f"Missing dossier JSON output in {dossier_dir}"

    for path in json_files:
        raw_text = path.read_text(encoding="utf-8", errors="replace")
        if len(raw_text.strip()) < 300:
            return f"Dossier JSON too short in {path.name}"
        try:
            payload = json.loads(raw_text)
        except Exception as exc:
            return f"Failed to parse {path.name}: {exc}"

        subject = payload.get("subject") or {}
        identity_status = payload.get("identity_status") or subject.get("identity_status")
        identity_summary = payload.get("identity_summary") or subject.get("identity_summary") or ""
        research_gaps = " ".join(str(item) for item in (payload.get("research_gaps") or []))
        combined_text = f"{identity_summary}\n{research_gaps}"
        confirmed_count, public_count, evidence_count, possible_count = _payload_signal_counts(payload)
        current_snapshot = payload.get("current_snapshot") if isinstance(payload.get("current_snapshot"), dict) else {}
        snapshot_summary = str(current_snapshot.get("summary") or "").strip().lower()
        if _looks_like_bad_terminal_output(combined_text):
            return f"Rate-limit shell summary detected in {path.name}"
        if (
            "wafer research failed" in combined_text.lower()
            and _classify_failure_text(combined_text) == "Provider transient rate limit"
        ):
            return f"Provider transient rate limit in {path.name}"
        if identity_status in (None, "", "error") and not _payload_has_real_substance(payload):
            return f"Zero-signal dossier output in {path.name}"
        if (
            confirmed_count == 0
            and public_count == 0
            and evidence_count <= 1
            and possible_count <= 1
        ):
            return f"Thin dossier output in {path.name} ({confirmed_count}/{public_count}/{evidence_count}/{possible_count})"
        if (
            "cannot be externally verified" in snapshot_summary
            and confirmed_count == 0
            and public_count == 0
            and evidence_count < 3
        ):
            return f"Thin unverified dossier output in {path.name} ({confirmed_count}/{public_count}/{evidence_count}/{possible_count})"

        md_path = dossier_dir / f"{path.stem}.md"
        if md_path.exists():
            md_text = md_path.read_text(encoding="utf-8", errors="replace")
            if len(md_text.strip()) < 300:
                return f"Dossier markdown too short in {md_path.name}"

    return None


def parse_iso8601_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def read_usage_aggregate(run_dir: Path) -> dict[str, Any] | None:
    usage_dir = run_dir / "usage"
    if not usage_dir.exists():
        return None
    for path in sorted(usage_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        aggregate = payload.get("aggregate")
        if isinstance(aggregate, dict):
            usage = aggregate.get("usage")
            if isinstance(usage, dict):
                return usage
    return None


def read_run_quality(run_dir: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "confirmed_updates": None,
        "public_outputs": None,
        "evidence_clips": None,
        "possible_updates": None,
        "dossier_json": None,
    }
    dossier_dir = run_dir / "dossiers"
    if not dossier_dir.exists():
        return result
    for path in sorted(dossier_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        confirmed_count, public_count, evidence_count, possible_count = _payload_signal_counts(payload)
        result.update(
            {
                "confirmed_updates": confirmed_count,
                "public_outputs": public_count,
                "evidence_clips": evidence_count,
                "possible_updates": possible_count,
                "dossier_json": str(path),
            }
        )
        return result
    return result


def compute_completed_metrics(
    conn: sqlite3.Connection,
    queue_name: str,
    *,
    limit: int,
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT priority_rank, backend, requested_model, run_id, run_dir, started_at, finished_at
        FROM jobs
        WHERE queue_name = ? AND status = 'completed'
        ORDER BY finished_at DESC
        LIMIT ?
        """,
        (queue_name, limit),
    ).fetchall()
    durations: list[float] = []
    cost_values: list[float] = []
    input_values: list[int] = []
    cache_read_values: list[int] = []
    output_values: list[int] = []
    lane_stats: dict[str, dict[str, float | int | None]] = {}
    for row in rows:
        started = parse_iso8601_utc(row["started_at"])
        finished = parse_iso8601_utc(row["finished_at"])
        duration_seconds: float | None = None
        if started and finished:
            duration_seconds = max(0.0, (finished - started).total_seconds())
            durations.append(duration_seconds)
        backend = row["backend"] or "unknown"
        lane = lane_stats.setdefault(
            backend,
            {
                "completed_recent": 0,
                "duration_seconds_total": 0.0,
                "duration_seconds_count": 0,
                "cost_usd_total": 0.0,
                "cost_usd_count": 0,
                "input_tokens_total": 0,
                "input_tokens_count": 0,
                "cache_read_input_tokens_total": 0,
                "cache_read_input_tokens_count": 0,
                "output_tokens_total": 0,
                "output_tokens_count": 0,
            },
        )
        lane["completed_recent"] += 1
        if duration_seconds is not None:
            lane["duration_seconds_total"] += duration_seconds
            lane["duration_seconds_count"] += 1
        run_dir_value = row["run_dir"]
        if not run_dir_value:
            continue
        usage = read_usage_aggregate(Path(run_dir_value))
        if not usage:
            continue
        cost = usage.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            cost_float = float(cost)
            cost_values.append(cost_float)
            lane["cost_usd_total"] += cost_float
            lane["cost_usd_count"] += 1
        input_tokens = usage.get("input_tokens")
        if isinstance(input_tokens, int):
            input_values.append(input_tokens)
            lane["input_tokens_total"] += input_tokens
            lane["input_tokens_count"] += 1
        cache_read_input_tokens = usage.get("cache_read_input_tokens")
        if isinstance(cache_read_input_tokens, int):
            cache_read_values.append(cache_read_input_tokens)
            lane["cache_read_input_tokens_total"] += cache_read_input_tokens
            lane["cache_read_input_tokens_count"] += 1
        output_tokens = usage.get("output_tokens")
        if isinstance(output_tokens, int):
            output_values.append(output_tokens)
            lane["output_tokens_total"] += output_tokens
            lane["output_tokens_count"] += 1
    failed_recent_by_lane = {
        (row["backend"] or "unknown"): int(row["c"] or 0)
        for row in conn.execute(
            """
            SELECT backend, COUNT(*) c
            FROM jobs
            WHERE queue_name = ? AND status = 'failed' AND backend IS NOT NULL
              AND finished_at >= datetime('now', '-2 hours')
            GROUP BY backend
            """,
            (queue_name,),
        ).fetchall()
    }
    for lane_name, stats in lane_stats.items():
        stats["failed_recent"] = failed_recent_by_lane.get(lane_name, 0)
        duration_count = int(stats["duration_seconds_count"] or 0)
        cost_count = int(stats["cost_usd_count"] or 0)
        input_count = int(stats["input_tokens_count"] or 0)
        cache_read_count = int(stats["cache_read_input_tokens_count"] or 0)
        output_count = int(stats["output_tokens_count"] or 0)
        stats["avg_seconds"] = round(float(stats["duration_seconds_total"]) / duration_count, 1) if duration_count else None
        stats["avg_cost_usd"] = round(float(stats["cost_usd_total"]) / cost_count, 4) if cost_count else None
        stats["avg_input_tokens"] = round(int(stats["input_tokens_total"]) / input_count) if input_count else None
        stats["avg_cache_read_input_tokens"] = round(int(stats["cache_read_input_tokens_total"]) / cache_read_count) if cache_read_count else None
        stats["avg_output_tokens"] = round(int(stats["output_tokens_total"]) / output_count) if output_count else None
    median_seconds = None
    if durations:
        ordered = sorted(durations)
        median_seconds = round(ordered[len(ordered) // 2], 1)
    return {
        "sample_size": len(rows),
        "avg_seconds_per_applicant": round(sum(durations) / len(durations), 1) if durations else None,
        "median_seconds_per_applicant": median_seconds,
        "avg_cost_usd_per_applicant": round(sum(cost_values) / len(cost_values), 4) if cost_values else None,
        "avg_input_tokens_per_applicant": round(sum(input_values) / len(input_values)) if input_values else None,
        "avg_cache_read_input_tokens_per_applicant": round(sum(cache_read_values) / len(cache_read_values)) if cache_read_values else None,
        "avg_output_tokens_per_applicant": round(sum(output_values) / len(output_values)) if output_values else None,
        "lane_metrics": lane_stats,
    }


def command_enqueue(args: argparse.Namespace) -> int:
    load_env()
    conn = connect_queue(Path(args.db))
    total_fetched = 0
    total_upserted = 0
    top_row: dict[str, Any] | None = None

    if args.people_file:
        rows = load_people_file(Path(args.people_file))
        start = args.offset
        end = None if args.all else start + args.limit
        sliced = rows[start:end]
        if sliced:
            top_row = sliced[0]
        total_fetched = len(sliced)
        total_upserted = upsert_jobs(conn, args.queue_name, sliced)
    else:
        offset = args.offset
        remaining = None if args.all else args.limit
        batch_size = args.batch_size

        while True:
            current_limit = batch_size if remaining is None else min(batch_size, remaining)
            if current_limit <= 0:
                break
            rows = fetch_ranked_people(limit=current_limit, offset=offset)
            if not rows:
                break
            if top_row is None:
                top_row = rows[0]
            total_fetched += len(rows)
            total_upserted += upsert_jobs(conn, args.queue_name, rows)
            offset += len(rows)
            if remaining is not None:
                remaining -= len(rows)
                if remaining <= 0:
                    break
            if len(rows) < current_limit:
                break

    print(f"queue: {args.queue_name}")
    print(f"rows_fetched: {total_fetched}")
    print(f"rows_upserted: {total_upserted}")
    if top_row:
        print(
            f"top: {top_row['name']} <{top_row['email']}> "
            f"signed_up={top_row['unique_events_signed_up']} checked_in={top_row['unique_events_checked_in']}"
        )
    return 0


def command_status(args: argparse.Namespace) -> int:
    conn = connect_queue(Path(args.db))
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS n
        FROM jobs
        WHERE queue_name = ?
        GROUP BY status
        ORDER BY status
        """,
        (args.queue_name,),
    ).fetchall()
    print(f"queue: {args.queue_name}")
    for row in rows:
        print(f"{row['status']}: {row['n']}")
    pending = conn.execute(
        """
        SELECT priority_rank, name, email, unique_events_signed_up, unique_events_checked_in
        FROM jobs
        WHERE queue_name = ? AND status = 'pending'
        ORDER BY priority_rank ASC
        LIMIT ?
        """,
        (args.queue_name, args.limit),
    ).fetchall()
    if pending:
        print("\nnext:")
        for row in pending:
            print(
                f"- #{row['priority_rank']} {row['name']} <{row['email']}> "
                f"signed_up={row['unique_events_signed_up']} checked_in={row['unique_events_checked_in']}"
            )
    return 0


def command_overview(args: argparse.Namespace) -> int:
    conn = connect_queue(Path(args.db))
    counts = {
        row["status"]: int(row["c"] or 0)
        for row in conn.execute(
            """
            SELECT status, COUNT(*) AS c
            FROM jobs
            WHERE queue_name = ?
            GROUP BY status
            ORDER BY status
            """,
            (args.queue_name,),
        ).fetchall()
    }
    running_by_lane = {
        (row["backend"] or "unknown"): int(row["c"] or 0)
        for row in conn.execute(
            """
            SELECT backend, COUNT(*) AS c
            FROM jobs
            WHERE queue_name = ? AND status = 'running'
            GROUP BY backend
            ORDER BY backend
            """,
            (args.queue_name,),
        ).fetchall()
    }
    failure_buckets = conn.execute(
        """
        SELECT COALESCE(failure_bucket, 'unknown') AS bucket, COUNT(*) AS c
        FROM jobs
        WHERE queue_name = ? AND status = 'failed'
        GROUP BY COALESCE(failure_bucket, 'unknown')
        ORDER BY c DESC, bucket ASC
        LIMIT ?
        """,
        (args.queue_name, args.limit),
    ).fetchall()
    metrics = compute_completed_metrics(conn, args.queue_name, limit=args.metrics_limit)
    print(f"queue: {args.queue_name}")
    print(
        "counts: "
        + ", ".join(
            f"{status}={counts.get(status, 0)}"
            for status in ("completed", "failed", "pending", "running")
        )
    )
    print(
        "recent_completed_metrics: "
        + f"sample={metrics['sample_size']} "
        + f"avg_seconds={metrics['avg_seconds_per_applicant']} "
        + f"median_seconds={metrics['median_seconds_per_applicant']} "
        + f"avg_cost_usd={metrics['avg_cost_usd_per_applicant']} "
        + f"avg_input_tokens={metrics['avg_input_tokens_per_applicant']} "
        + f"avg_cache_read_input_tokens={metrics['avg_cache_read_input_tokens_per_applicant']} "
        + f"avg_output_tokens={metrics['avg_output_tokens_per_applicant']}"
    )
    if running_by_lane:
        print("\nrunning_by_lane:")
        for lane, count in sorted(running_by_lane.items()):
            print(f"- {lane}: {count}")
    if failure_buckets:
        print("\nfailed_buckets:")
        for row in failure_buckets:
            print(f"- {row['bucket']}: {row['c']}")
    return 0


def command_lanes(args: argparse.Namespace) -> int:
    conn = connect_queue(Path(args.db))
    metrics = compute_completed_metrics(conn, args.queue_name, limit=args.metrics_limit)
    running_by_lane = {
        (row["backend"] or "unknown"): int(row["c"] or 0)
        for row in conn.execute(
            """
            SELECT backend, COUNT(*) AS c
            FROM jobs
            WHERE queue_name = ? AND status = 'running'
            GROUP BY backend
            """,
            (args.queue_name,),
        ).fetchall()
    }
    failed_by_lane = {
        (row["backend"] or "unknown"): int(row["c"] or 0)
        for row in conn.execute(
            """
            SELECT backend, COUNT(*) AS c
            FROM jobs
            WHERE queue_name = ? AND status = 'failed'
            GROUP BY backend
            """,
            (args.queue_name,),
        ).fetchall()
    }
    recent_failed_by_lane = {
        (row["backend"] or "unknown"): int(row["c"] or 0)
        for row in conn.execute(
            """
            SELECT backend, COUNT(*) AS c
            FROM jobs
            WHERE queue_name = ? AND status = 'failed' AND finished_at >= datetime('now', ?)
            GROUP BY backend
            """,
            (args.queue_name, f"-{args.failed_window_hours} hours"),
        ).fetchall()
    }
    lanes = sorted(
        set(running_by_lane)
        | set(failed_by_lane)
        | set(metrics["lane_metrics"].keys())
        | set(recent_failed_by_lane)
    )
    print(f"queue: {args.queue_name}")
    for lane in lanes:
        lane_metrics = metrics["lane_metrics"].get(lane, {})
        print(
            f"{lane}: "
            + f"running={running_by_lane.get(lane, 0)} "
            + f"completed_recent={lane_metrics.get('completed_recent', 0)} "
            + f"failed_total={failed_by_lane.get(lane, 0)} "
            + f"failed_recent={recent_failed_by_lane.get(lane, 0)} "
            + f"avg_seconds={lane_metrics.get('avg_seconds')} "
            + f"avg_cost_usd={lane_metrics.get('avg_cost_usd')} "
            + f"avg_input_tokens={lane_metrics.get('avg_input_tokens')} "
            + f"avg_cache_read_input_tokens={lane_metrics.get('avg_cache_read_input_tokens')} "
            + f"avg_output_tokens={lane_metrics.get('avg_output_tokens')}"
        )
    return 0


def command_running(args: argparse.Namespace) -> int:
    conn = connect_queue(Path(args.db))
    rows = conn.execute(
        """
        SELECT id, priority_rank, name, email, attempts, backend, requested_model, web_mode,
               worker_pid, worker_host, started_at, run_id, run_dir
        FROM jobs
        WHERE queue_name = ? AND status = 'running'
        ORDER BY priority_rank ASC, id ASC
        LIMIT ?
        """,
        (args.queue_name, args.limit),
    ).fetchall()
    for row in rows:
        print(
            f"#{row['priority_rank']} job_id={row['id']} {row['name']} <{row['email']}> "
            f"attempts={row['attempts']} backend={row['backend'] or ''} model={row['requested_model'] or ''} "
            f"web={row['web_mode'] or ''} pid={row['worker_pid'] or ''} host={row['worker_host'] or ''}"
        )
        print(f"  started={row['started_at'] or ''} run_id={row['run_id'] or ''}")
        print(f"  run_dir={row['run_dir'] or ''}")
    return 0


def command_recent_completions(args: argparse.Namespace) -> int:
    conn = connect_queue(Path(args.db))
    rows = conn.execute(
        """
        SELECT id, priority_rank, name, email, attempts, backend, requested_model, web_mode,
               started_at, finished_at, run_id, run_dir
        FROM jobs
        WHERE queue_name = ? AND status = 'completed'
        ORDER BY finished_at DESC, id DESC
        LIMIT ?
        """,
        (args.queue_name, args.limit),
    ).fetchall()
    for row in rows:
        started = parse_iso8601_utc(row["started_at"])
        finished = parse_iso8601_utc(row["finished_at"])
        duration_seconds = None
        if started and finished:
            duration_seconds = round(max(0.0, (finished - started).total_seconds()), 1)
        run_dir = Path(str(row["run_dir"] or ""))
        usage = read_usage_aggregate(run_dir) if run_dir else None
        quality = read_run_quality(run_dir) if run_dir else {}
        print(
            f"#{row['priority_rank']} job_id={row['id']} {row['name']} <{row['email']}> "
            + f"backend={row['backend'] or ''} model={row['requested_model'] or ''} web={row['web_mode'] or ''} "
            + f"attempts={row['attempts']} duration={duration_seconds if duration_seconds is not None else ''}"
        )
        if usage:
            print(
                "  usage="
                + f"cost={usage.get('total_cost_usd')} "
                + f"input={usage.get('input_tokens')} "
                + f"cache_read={usage.get('cache_read_input_tokens')} "
                + f"output={usage.get('output_tokens')}"
            )
        if quality:
            print(
                "  quality="
                + f"confirmed={quality.get('confirmed_updates')} "
                + f"outputs={quality.get('public_outputs')} "
                + f"clips={quality.get('evidence_clips')} "
                + f"possible={quality.get('possible_updates')}"
            )
        print(f"  finished={row['finished_at'] or ''} run_id={row['run_id'] or ''}")
        print(f"  run_dir={row['run_dir'] or ''}")
    return 0


def command_failures(args: argparse.Namespace) -> int:
    conn = connect_queue(Path(args.db))
    rows = conn.execute(
        """
        SELECT id, priority_rank, name, email, attempts, backend, requested_model, web_mode,
               finished_at, run_id, run_dir, failure_bucket, last_exit_code, last_error
        FROM jobs
        WHERE queue_name = ? AND status = 'failed'
        ORDER BY finished_at DESC, id DESC
        LIMIT ?
        """,
        (args.queue_name, args.limit),
    ).fetchall()
    for row in rows:
        print(
            f"#{row['priority_rank']} job_id={row['id']} {row['name']} <{row['email']}> "
            f"backend={row['backend'] or ''} model={row['requested_model'] or ''} web={row['web_mode'] or ''} "
            f"attempts={row['attempts']} exit={row['last_exit_code'] if row['last_exit_code'] is not None else ''}"
        )
        print(f"  bucket={row['failure_bucket'] or ''} finished={row['finished_at'] or ''} run_id={row['run_id'] or ''}")
        print(f"  run_dir={row['run_dir'] or ''}")
        if row["last_error"]:
            print("  error=" + str(row["last_error"]).replace("\n", " | ")[:500])
    return 0


def command_inspect(args: argparse.Namespace) -> int:
    conn = connect_queue(Path(args.db))
    row = None
    if args.job_id is not None:
        row = conn.execute(
            "SELECT * FROM jobs WHERE queue_name = ? AND id = ?",
            (args.queue_name, args.job_id),
        ).fetchone()
    elif args.run_id:
        row = conn.execute(
            "SELECT * FROM jobs WHERE queue_name = ? AND run_id = ?",
            (args.queue_name, args.run_id),
        ).fetchone()
    elif args.email:
        row = conn.execute(
            "SELECT * FROM jobs WHERE queue_name = ? AND email = ?",
            (args.queue_name, args.email.strip().lower()),
        ).fetchone()
    if row is None:
        raise SystemExit("No matching job found.")
    data = dict(row)
    print(json.dumps(data, indent=2, default=str))
    run_dir = Path(str(data.get("run_dir") or ""))
    if run_dir and run_dir.exists():
        print("\n[run_dir]")
        print(run_dir)
        for rel in [
            Path("telemetry/queue_attempt.json"),
            Path("telemetry/run_summary.json"),
            Path("daytona-runner.stdout.txt"),
            Path("index.json"),
        ]:
            p = run_dir / rel
            print(f"{rel}: {'yes' if p.exists() else 'no'}")
        usage_dir = run_dir / "usage"
        if usage_dir.exists():
            print("usage_files:")
            for p in sorted(usage_dir.glob("*.json"))[:10]:
                print(f"  {p.name}")
                try:
                    payload = json.loads(p.read_text(encoding="utf-8"))
                    aggregate = payload.get("aggregate") or {}
                    print(
                        "    "
                        + f"model={payload.get('requested_model') or ''} "
                        + f"input={aggregate.get('input_tokens') or 0} "
                        + f"output={aggregate.get('output_tokens') or 0} "
                        + f"cost={aggregate.get('total_cost_usd') if aggregate.get('total_cost_usd') is not None else ''}"
                    )
                except Exception:
                    pass
    return 0


def command_work(args: argparse.Namespace) -> int:
    load_env()
    conn = connect_queue(Path(args.db))
    queue_root = Path(args.output_dir) / args.queue_name
    queue_root.mkdir(parents=True, exist_ok=True)
    recovered = recover_stale_running_jobs(
        conn,
        queue_name=args.queue_name,
        stale_after_minutes=args.stale_after_minutes,
        max_attempts=args.max_attempts,
    )
    if recovered:
        print(f"recovered_stale_jobs: {recovered}")
    processed = 0
    while args.max_jobs is None or processed < args.max_jobs:
        job = claim_next_job(conn, args.queue_name, max_attempts=args.max_attempts)
        if job is None:
            print("no pending jobs")
            break
        run_id = build_run_id(job)
        run_dir_path = build_run_dir(queue_root, run_id)
        backend = infer_backend(
            execution_backend=args.execution_backend,
            daytona_runner=Path(args.daytona_runner) if args.execution_backend == "daytona" else None,
            claude_model=args.claude_model,
        )
        set_job_run_metadata(
            conn,
            job.id,
            run_id=run_id,
            run_dir=str(run_dir_path),
            backend=backend,
            requested_model=args.claude_model,
            web_mode=args.wafer_web_mode,
            worker_pid=os.getpid(),
            worker_host=socket.gethostname(),
        )
        print(
            f"processing #{job.priority_rank} {job.name} <{job.email}> "
            f"signed_up={job.row.get('unique_events_signed_up')} checked_in={job.row.get('unique_events_checked_in')}"
        )
        if args.execution_backend == "daytona":
            code, stdout, stderr, run_dir = run_single_dossier_daytona(
                job,
                queue_root=queue_root,
                prompt_file=Path(args.prompt_file),
                daytona_runner=Path(args.daytona_runner),
                config_path=Path(args.config) if args.config else None,
                wafer_web_mode=args.wafer_web_mode,
                wafer_timeout_seconds=args.wafer_timeout_seconds,
                claude_model=args.claude_model,
                effort=args.effort,
                wafer_synthesis_only=args.wafer_synthesis_only,
                wafer_expansion_pass=args.wafer_expansion_pass,
                wafer_braindump_pass=args.wafer_braindump_pass,
            )
            executed_cmd = [
                os.environ.get("DAYTONA_PYTHON_BIN") or sys.executable,
                str(Path(args.daytona_runner)),
                "--run-id",
                run_id,
                "--model",
                args.claude_model or "",
                "--web-mode",
                args.wafer_web_mode,
            ]
        else:
            code, stdout, stderr, run_dir = run_single_dossier(
                job,
                queue_root=queue_root,
                prompt_file=Path(args.prompt_file),
                dossier_script=Path(args.dossier_script),
                config_path=Path(args.config),
                wafer_api_key_file=args.wafer_api_key_file,
                exa_api_key_file=args.exa_api_key_file,
                wafer_web_mode=args.wafer_web_mode,
                wafer_timeout_seconds=args.wafer_timeout_seconds,
                fallback_model=args.fallback_model,
                claude_model=args.claude_model,
                effort=args.effort,
                claude_output_format=args.claude_output_format,
                wafer_synthesis_only=args.wafer_synthesis_only,
                wafer_expansion_pass=args.wafer_expansion_pass,
                wafer_braindump_pass=args.wafer_braindump_pass,
            )
            executed_cmd = [
                sys.executable,
                str(Path(args.dossier_script)),
                "--run-id",
                run_id,
                "--claude-model",
                args.claude_model or "",
                "--wafer-web-mode",
                args.wafer_web_mode,
            ]
        log_dir = queue_root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{job.priority_rank:05d}-{job.email.replace('@', '-at-')}"
        (log_dir / f"{stem}.stdout.txt").write_text(stdout, encoding="utf-8")
        (log_dir / f"{stem}.stderr.txt").write_text(stderr, encoding="utf-8")
        validation_error: str | None = None
        if code == 0:
            validation_error = validate_completed_run(Path(run_dir))
            if validation_error:
                code = 1
                stderr = f"{stderr}\n{validation_error}".strip() if stderr else validation_error
        if code == 0:
            finish_job(conn, job.id, status="completed", run_id=Path(run_dir).name, run_dir=run_dir)
            conn.execute(
                "UPDATE jobs SET last_exit_code = ?, failure_bucket = NULL WHERE id = ?",
                (code, job.id),
            )
            conn.commit()
            write_attempt_telemetry(
                Path(run_dir),
                job=job,
                backend=backend,
                execution_backend=args.execution_backend,
                requested_model=args.claude_model,
                web_mode=args.wafer_web_mode,
                command=executed_cmd,
                return_code=code,
                stdout=stdout,
                stderr=stderr,
                validation_error=None,
                failure_bucket=None,
                final_status="completed",
                terminal_failure=False,
            )
            print(f"completed: {run_dir}")
        else:
            error_text = _build_failure_summary(Path(run_dir), stdout, stderr, validation_error)[-4000:]
            failure_bucket = _classify_failure_text(error_text) or (validation_error or "runner_failure")
            terminal_failure = _is_terminal_failure_bucket(failure_bucket)
            requeue_delay_seconds = 45 if failure_bucket == "Provider transient rate limit" else 0
            if job.attempts < args.max_attempts and not terminal_failure:
                requeue_job(conn, job.id, error=error_text, delay_seconds=requeue_delay_seconds)
                conn.execute(
                    "UPDATE jobs SET last_exit_code = ?, failure_bucket = ? WHERE id = ?",
                    (code, failure_bucket, job.id),
                )
                conn.commit()
                write_attempt_telemetry(
                    Path(run_dir),
                    job=job,
                    backend=backend,
                    execution_backend=args.execution_backend,
                    requested_model=args.claude_model,
                    web_mode=args.wafer_web_mode,
                    command=executed_cmd,
                    return_code=code,
                    stdout=stdout,
                    stderr=stderr,
                    validation_error=validation_error,
                    failure_bucket=failure_bucket,
                    final_status="requeued",
                    terminal_failure=False,
                )
                print(f"requeued: {run_dir} attempt={job.attempts}/{args.max_attempts}")
            else:
                finish_job(conn, job.id, status="failed", run_id=Path(run_dir).name, run_dir=run_dir, error=error_text)
                conn.execute(
                    "UPDATE jobs SET last_exit_code = ?, failure_bucket = ? WHERE id = ?",
                    (code, failure_bucket, job.id),
                )
                conn.commit()
                write_attempt_telemetry(
                    Path(run_dir),
                    job=job,
                    backend=backend,
                    execution_backend=args.execution_backend,
                    requested_model=args.claude_model,
                    web_mode=args.wafer_web_mode,
                    command=executed_cmd,
                    return_code=code,
                    stdout=stdout,
                    stderr=stderr,
                    validation_error=validation_error,
                    failure_bucket=failure_bucket,
                    final_status="failed",
                    terminal_failure=terminal_failure,
                )
                if terminal_failure:
                    print(f"terminal-failed: {run_dir} bucket={failure_bucket}")
                else:
                    print(f"failed: {run_dir} attempts={job.attempts}/{args.max_attempts}")
            if args.stop_on_error and (job.attempts >= args.max_attempts or terminal_failure):
                return code
        processed += 1
    print(f"processed: {processed}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Queue attendee dossier jobs ranked by CV event engagement.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite queue DB path.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    enqueue = subparsers.add_parser("enqueue", help="Pull ranked people from platform DB or a JSON people file into the queue.")
    enqueue.add_argument("--queue-name", default="signed-up-ranking")
    enqueue.add_argument("--limit", type=int, default=100)
    enqueue.add_argument("--offset", type=int, default=0)
    enqueue.add_argument("--all", action="store_true", help="Enqueue everyone from the ranked list.")
    enqueue.add_argument("--people-file", help="JSON file containing ranked people rows to enqueue instead of the global DB ranking.")
    enqueue.add_argument("--batch-size", type=int, default=250, help="Chunk size for bulk enqueue.")
    enqueue.set_defaults(func=command_enqueue)

    status = subparsers.add_parser("status", help="Show queue status.")
    status.add_argument("--queue-name", default="signed-up-ranking")
    status.add_argument("--limit", type=int, default=10)
    status.set_defaults(func=command_status)

    overview = subparsers.add_parser("overview", help="Show queue counts, running lanes, failure buckets, and recent performance metrics.")
    overview.add_argument("--queue-name", default="signed-up-ranking")
    overview.add_argument("--limit", type=int, default=10, help="How many failure buckets to show.")
    overview.add_argument("--metrics-limit", type=int, default=250, help="How many recent completed runs to sample for performance metrics.")
    overview.set_defaults(func=command_overview)

    lanes = subparsers.add_parser("lanes", help="Show per-lane running counts, recent completions, failures, and average metrics.")
    lanes.add_argument("--queue-name", default="signed-up-ranking")
    lanes.add_argument("--metrics-limit", type=int, default=250, help="How many recent completed runs to sample for lane metrics.")
    lanes.add_argument("--failed-window-hours", type=int, default=2, help="Recent failure window in hours.")
    lanes.set_defaults(func=command_lanes)

    running = subparsers.add_parser("running", help="Show running jobs with live metadata.")
    running.add_argument("--queue-name", default="signed-up-ranking")
    running.add_argument("--limit", type=int, default=25)
    running.set_defaults(func=command_running)

    recent_completed = subparsers.add_parser("recent-completions", help="Show recent completed jobs with duration, usage, and dossier signal counts.")
    recent_completed.add_argument("--queue-name", default="signed-up-ranking")
    recent_completed.add_argument("--limit", type=int, default=10)
    recent_completed.set_defaults(func=command_recent_completions)

    failures = subparsers.add_parser("failures", help="Show recent failed jobs with failure buckets.")
    failures.add_argument("--queue-name", default="signed-up-ranking")
    failures.add_argument("--limit", type=int, default=25)
    failures.set_defaults(func=command_failures)

    inspect = subparsers.add_parser("inspect", help="Inspect one job/run and its telemetry.")
    inspect.add_argument("--queue-name", default="signed-up-ranking")
    inspect.add_argument("--job-id", type=int)
    inspect.add_argument("--run-id")
    inspect.add_argument("--email")
    inspect.set_defaults(func=command_inspect)

    work = subparsers.add_parser("work", help="Process queued jobs one by one.")
    work.add_argument("--queue-name", default="signed-up-ranking")
    work.add_argument("--max-jobs", type=int)
    work.add_argument("--stop-on-error", action="store_true")
    work.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_ROOT))
    work.add_argument("--prompt-file", default=str(DEFAULT_PROMPT))
    work.add_argument("--dossier-script", default=str(DEFAULT_DOSSIER_SCRIPT))
    work.add_argument("--execution-backend", choices=("local", "daytona"), default="local")
    work.add_argument("--daytona-runner", default=str(DEFAULT_DAYTONA_RUNNER))
    work.add_argument("--config", default=str(DEFAULT_CONFIG))
    work.add_argument("--wafer-api-key-file")
    work.add_argument("--exa-api-key-file")
    work.add_argument("--wafer-web-mode", default="exa", choices=("exa", "mixed", "claude"))
    work.add_argument("--wafer-timeout-seconds", type=int, default=180)
    work.add_argument("--fallback-model", help="Claude CLI fallback model when the primary model is overloaded.")
    work.add_argument("--claude-model", help="Claude CLI model override passed through to the dossier runner.")
    work.add_argument("--effort", choices=("low", "medium", "high", "max"), default="low")
    work.add_argument("--claude-output-format", choices=("text", "json", "stream-json"), default="text")
    work.add_argument("--wafer-synthesis-only", action=argparse.BooleanOptionalAction, default=False)
    work.add_argument("--wafer-expansion-pass", action=argparse.BooleanOptionalAction, default=True)
    work.add_argument("--wafer-braindump-pass", action=argparse.BooleanOptionalAction, default=False)
    work.add_argument("--max-attempts", type=int, default=2, help="Total attempts per job before marking failed.")
    work.add_argument("--stale-after-minutes", type=int, default=30, help="Requeue running jobs older than this many minutes.")
    work.set_defaults(func=command_work)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
