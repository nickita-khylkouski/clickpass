#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import os
import sqlite3
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = Path("/Users/nickita/cv-rank/outputs/attendee_dossier_queue/queue.sqlite3")
DEFAULT_RUNS_ROOT = Path("/Users/nickita/cv-rank/outputs/attendee_dossier_queue/nyc-unified/runs")
METRICS_CACHE: dict[str, Any] = {"key": None, "value": None}


@dataclass
class WorkerLog:
    worker_type: str
    path: str
    name: str
    updated_at: float
    age_seconds: float
    claimed: str | None
    tail: str


@dataclass
class WorkerProcess:
    worker_type: str
    pid: int
    command: str


def summarize_error(error: str | None) -> str:
    text = (error or "").strip()
    if not text:
        return "No error text"
    compact = " ".join(text.split())
    if "Failed to install Codex in Daytona sandbox" in compact:
        return "Codex bootstrap install failed"
    if "daytona_minimax_remote.py" in compact and "Traceback" in compact:
        return "MiniMax runner traceback"
    if "daytona_wafer_remote.py" in compact and "Traceback" in compact:
        return "Wafer runner traceback"
    if "daytona_cv_rank_remote.py" in compact and "Traceback" in compact:
        return "Codex runner traceback"
    if "apt-get" in compact or "dpkg" in compact or "deb.debian.org" in compact:
        return "APT/bootstrap dependency failure"
    if "Missing dossier directory" in compact:
        return "Missing dossier directory"
    if "Supabase x: HTTP 400" in compact:
        return "Supabase HTTP 400 warning/failure"
    if "Invalid authentication credentials" in compact or "authentication_error" in compact:
        return "Claude authentication failure"
    if "NO_MORE_CREDITS" in compact or "402" in compact:
        return "Exa credits exhausted"
    if "INVALID_API_KEY" in compact or "401" in compact:
        return "Exa invalid API key"
    if "You've hit your limit" in compact:
        return "Codex usage limit shell"
    return compact[:100]


def read_tail(path: Path, limit: int = 4000) -> str:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return ""
    return data[-limit:].decode("utf-8", errors="replace")


def classify_log(path: Path) -> str:
    lowered = path.name.lower()
    if "claude" in lowered:
        return "claude"
    if "minimax" in lowered:
        return "minimax"
    if "wafer" in lowered:
        return "wafer"
    if "codex" in lowered:
        return "codex"
    return "unknown"


def parse_claimed_job(tail: str) -> str | None:
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if line.startswith("processing #"):
            return line
    return None


def gather_worker_logs() -> list[WorkerLog]:
    patterns = [
        "/private/tmp/daytona-claude*/*.log",
        "/private/tmp/daytona-codex*/*.log",
        "/private/tmp/daytona-minimax*/*.log",
        "/private/tmp/daytona-wafer*/*.log",
    ]
    seen: set[str] = set()
    items: list[WorkerLog] = []
    now = time.time()
    for pattern in patterns:
        for path in sorted(Path("/").glob(pattern.lstrip("/"))):
            if str(path) in seen:
                continue
            seen.add(str(path))
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            tail = read_tail(path)
            items.append(
                WorkerLog(
                    worker_type=classify_log(path),
                    path=str(path),
                    name=path.name,
                    updated_at=stat.st_mtime,
                    age_seconds=max(0.0, now - stat.st_mtime),
                    claimed=parse_claimed_job(tail),
                    tail=tail,
                )
            )
    items.sort(key=lambda item: (item.worker_type, item.name))
    return items


def classify_process(command: str) -> str:
    lowered = command.lower()
    if "daytona_plain_remote.py" in lowered or "cv-rank-claude" in lowered:
        return "claude"
    if "daytona_minimax_remote.py" in lowered or "cv-rank-minimax" in lowered:
        return "minimax"
    if "daytona_wafer_remote.py" in lowered or "cv-rank-wafer" in lowered:
        return "wafer"
    if "daytona_cv_rank_remote.py" in lowered or "cv-rank-codex" in lowered:
        return "codex"
    if "minimax" in lowered:
        return "minimax"
    if "claude_wafer_agent.py" in lowered:
        return "wafer"
    if "codex" in lowered or "attendee_dossier_queue.py" in lowered:
        return "codex"
    return "unknown"


def gather_live_worker_processes() -> list[WorkerProcess]:
    pattern = r"attendee_dossier_queue.py|daytona_.*_remote.py|start_daytona_.*queue|claude_wafer_agent.py"
    cmd = (
        "ps -axo pid=,command= | "
        f"egrep '{pattern}' | "
        "egrep -v 'egrep|serve_attendee_queue_dashboard.py|python3 - <<'"
    )
    out = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True, check=False)
    rows: list[WorkerProcess] = []
    for line in out.stdout.splitlines():
        raw = line.strip()
        if not raw:
            continue
        parts = raw.split(None, 1)
        if len(parts) != 2:
            continue
        pid_text, command = parts
        if not pid_text.isdigit():
            continue
        rows.append(
            WorkerProcess(
                worker_type=classify_process(command),
                pid=int(pid_text),
                command=command,
            )
        )
    rows.sort(key=lambda item: (item.worker_type, item.pid))
    return rows


def connect_queue(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


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
        aggregate = (payload.get("aggregate") or {}).get("usage")
        if isinstance(aggregate, dict):
            return aggregate
    return None


def compute_completed_metrics(
    conn: sqlite3.Connection,
    queue_name: str,
    runs_root: Path,
    *,
    limit: int = 250,
) -> dict[str, Any]:
    rows = conn.execute(
        """
        select priority_rank, backend, requested_model, run_id, started_at, finished_at
        from jobs
        where queue_name = ? and status = 'completed'
        order by finished_at desc
        limit ?
        """,
        (queue_name, limit),
    ).fetchall()
    durations: list[float] = []
    cost_values: list[float] = []
    input_values: list[int] = []
    output_values: list[int] = []
    lane_stats: dict[str, dict[str, float | int]] = {}
    samples = 0
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
                "output_tokens_total": 0,
                "output_tokens_count": 0,
            },
        )
        lane["completed_recent"] += 1
        if duration_seconds is not None:
            lane["duration_seconds_total"] += duration_seconds
            lane["duration_seconds_count"] += 1
        run_id = row["run_id"]
        if not run_id:
            continue
        usage = read_usage_aggregate(runs_root / run_id)
        if not usage:
            continue
        samples += 1
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
        output_tokens = usage.get("output_tokens")
        if isinstance(output_tokens, int):
            output_values.append(output_tokens)
            lane["output_tokens_total"] += output_tokens
            lane["output_tokens_count"] += 1
    failed_recent_by_lane = {
        (row["backend"] or "unknown"): int(row["c"] or 0)
        for row in conn.execute(
            """
            select backend, count(*) c
            from jobs
            where queue_name = ? and status = 'failed' and backend is not null
              and finished_at >= datetime('now', '-2 hours')
            group by backend
            """,
            (queue_name,),
        ).fetchall()
    }
    for lane_name, stats in lane_stats.items():
        stats["failed_recent"] = failed_recent_by_lane.get(lane_name, 0)
        duration_count = int(stats["duration_seconds_count"] or 0)
        cost_count = int(stats["cost_usd_count"] or 0)
        input_count = int(stats["input_tokens_count"] or 0)
        output_count = int(stats["output_tokens_count"] or 0)
        stats["avg_seconds"] = round(float(stats["duration_seconds_total"]) / duration_count, 1) if duration_count else None
        stats["avg_cost_usd"] = round(float(stats["cost_usd_total"]) / cost_count, 4) if cost_count else None
        stats["avg_input_tokens"] = round(int(stats["input_tokens_total"]) / input_count) if input_count else None
        stats["avg_output_tokens"] = round(int(stats["output_tokens_total"]) / output_count) if output_count else None
    return {
        "sample_size": len(rows),
        "usage_sample_size": samples,
        "avg_seconds_per_applicant": round(sum(durations) / len(durations), 1) if durations else None,
        "median_seconds_per_applicant": round(sorted(durations)[len(durations) // 2], 1) if durations else None,
        "avg_cost_usd_per_applicant": round(sum(cost_values) / len(cost_values), 4) if cost_values else None,
        "avg_input_tokens_per_applicant": round(sum(input_values) / len(input_values)) if input_values else None,
        "avg_output_tokens_per_applicant": round(sum(output_values) / len(output_values)) if output_values else None,
        "lane_metrics": lane_stats,
    }


def fetch_queue_state(db_path: Path, queue_name: str) -> dict[str, Any]:
    conn = connect_queue(db_path)
    try:
        counts = {
            row["status"]: row["c"]
            for row in conn.execute(
                "select status, count(*) c from jobs where queue_name = ? group by status order by status",
                (queue_name,),
            ).fetchall()
        }
        running = [
            dict(row)
            for row in conn.execute(
                """
                select id, priority_rank, name, email, status, started_at, finished_at, attempts, backend, requested_model, run_id, run_dir, last_error
                from jobs
                where queue_name = ? and status = 'running'
                order by priority_rank asc
                """,
                (queue_name,),
            ).fetchall()
        ]
        recent_completed = [
            dict(row)
            for row in conn.execute(
                """
                select id, priority_rank, name, email, finished_at, attempts, run_id, run_dir
                from jobs
                where queue_name = ? and status = 'completed'
                order by finished_at desc
                limit 20
                """,
                (queue_name,),
            ).fetchall()
        ]
        failed = [
            dict(row)
            for row in conn.execute(
                """
                select id, priority_rank, name, email, finished_at, attempts, run_id, run_dir, last_error
                from jobs
                where queue_name = ? and status = 'failed'
                order by finished_at desc
                limit 20
                """,
                (queue_name,),
            ).fetchall()
        ]
        failed_bucket_rows = [
            dict(row)
            for row in conn.execute(
                """
                select
                    case
                        when last_error is null or trim(last_error) = '' then 'No error text'
                        else substr(last_error, 1, 400)
                    end as bucket,
                    count(*) as count
                from jobs
                where queue_name = ? and status = 'failed'
                group by bucket
                order by count desc
                limit 20
                """,
                (queue_name,),
            ).fetchall()
        ]
    finally:
        conn.close()
    merged_buckets: dict[str, int] = {}
    for row in failed:
        row["error_bucket"] = summarize_error(row.get("last_error"))
    for row in failed_bucket_rows:
        label = summarize_error(row.get("bucket"))
        merged_buckets[label] = merged_buckets.get(label, 0) + int(row.get("count") or 0)
    return {
        "queue_name": queue_name,
        "counts": counts,
        "running": running,
        "recent_completed": recent_completed,
        "failed": failed,
        "failed_buckets": [
            {"label": label, "count": count}
            for label, count in sorted(merged_buckets.items(), key=lambda item: (-item[1], item[0]))[:12]
        ],
    }


def find_recent_run_activity(runs_root: Path, limit: int = 30) -> list[dict[str, Any]]:
    if not runs_root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for run_dir in sorted((p for p in runs_root.iterdir() if p.is_dir() and p.name.startswith("nyc-unified-")), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            mtime = run_dir.stat().st_mtime
        except FileNotFoundError:
            continue
        files = sorted((p for p in run_dir.rglob("*") if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
        rows.append(
            {
                "run_id": run_dir.name,
                "path": str(run_dir),
                "updated_at": mtime,
                "latest_files": [
                    {
                        "path": str(p),
                        "mtime": p.stat().st_mtime,
                        "size": p.stat().st_size,
                    }
                    for p in files[:8]
                ],
            }
        )
    return rows


def build_state(db_path: Path, queue_name: str, runs_root: Path) -> dict[str, Any]:
    live_processes = [asdict(item) for item in gather_live_worker_processes()]
    all_logs = [asdict(item) for item in gather_worker_logs()]
    workers = [item for item in all_logs if float(item["age_seconds"]) <= 300]
    if not live_processes:
        workers = []
    queue = fetch_queue_state(db_path, queue_name)
    cache_key = (
        str(db_path),
        queue_name,
        db_path.stat().st_mtime if db_path.exists() else 0,
        len(queue.get("recent_completed", [])),
        len(queue.get("failed", [])),
    )
    if METRICS_CACHE.get("key") == cache_key:
        completed_metrics = METRICS_CACHE.get("value") or {}
    else:
        conn = connect_queue(db_path)
        try:
            completed_metrics = compute_completed_metrics(conn, queue_name, runs_root)
        finally:
            conn.close()
        METRICS_CACHE["key"] = cache_key
        METRICS_CACHE["value"] = completed_metrics
    lane_summary: dict[str, dict[str, int]] = {}
    running_by_lane: dict[str, int] = {}
    for job in queue.get("running", []):
        lane = job.get("backend") or "unknown"
        running_by_lane[lane] = running_by_lane.get(lane, 0) + 1
    for proc in live_processes:
        lane = proc["worker_type"]
        stats = lane_summary.setdefault(
            lane,
            {"active": 0, "fresh_logs": 0, "claimed": 0, "completed_recent": 0, "failed_recent": 0},
        )
        stats["active"] += 1
    for worker in workers:
        lane = worker["worker_type"]
        stats = lane_summary.setdefault(
            lane,
            {"active": 0, "fresh_logs": 0, "claimed": 0, "completed_recent": 0, "failed_recent": 0},
        )
        stats["fresh_logs"] += 1
    for lane, count in running_by_lane.items():
        stats = lane_summary.setdefault(
            lane,
            {"active": 0, "fresh_logs": 0, "claimed": 0, "completed_recent": 0, "failed_recent": 0},
        )
        stats["claimed"] = count
    for lane, metrics in (completed_metrics.get("lane_metrics") or {}).items():
        stats = lane_summary.setdefault(
            lane,
            {"active": 0, "fresh_logs": 0, "claimed": 0, "completed_recent": 0, "failed_recent": 0},
        )
        stats["completed_recent"] = int(metrics.get("completed_recent") or 0)
        stats["failed_recent"] = int(metrics.get("failed_recent") or 0)
        for key in ("avg_seconds", "avg_cost_usd", "avg_input_tokens", "avg_output_tokens"):
            if metrics.get(key) is not None:
                stats[key] = metrics.get(key)
    return {
        "generated_at": time.time(),
        "queue": queue,
        "queue_counts": queue.get("counts", {}),
        "running_jobs": queue.get("running", []),
        "recent_completed": queue.get("recent_completed", []),
        "recent_failed": queue.get("failed", []),
        "failed_buckets": queue.get("failed_buckets", []),
        "workers": workers,
        "active_processes": live_processes,
        "lane_summary": lane_summary,
        "completed_metrics": completed_metrics,
        "recent_run_activity": find_recent_run_activity(runs_root),
    }


def render_html() -> str:
    return """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Attendee Queue Ops</title>
  <style>
    :root {
      --bg: #050505;
      --panel: #0d0d0d;
      --panel-2: #111111;
      --text: #f5f5f5;
      --muted: #9a9a9a;
      --good: #ffffff;
      --warn: #d0d0d0;
      --bad: #7f7f7f;
      --border: #262626;
      --link: #f5f5f5;
      --mono: ui-monospace, SFMono-Regular, Menlo, monospace;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font: 13px/1.4 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    header {
      padding: 16px 18px 10px;
      border-bottom: 1px solid var(--border);
    }
    h1, h2, h3 {
      margin: 0 0 8px;
      font-weight: 700;
      letter-spacing: .01em;
    }
    .muted { color: var(--muted); }
    .wrap {
      padding: 12px 18px 18px;
      display: grid;
      gap: 10px;
    }
    .stats {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
      gap: 8px;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px;
    }
    .stat-value {
      font-size: 24px;
      font-weight: 800;
      line-height: 1;
      margin-top: 6px;
    }
    .grid {
      display: grid;
      grid-template-columns: 1.25fr .85fr;
      gap: 10px;
    }
    .grid-logs {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }
    .grid-ops {
      display: grid;
      grid-template-columns: .95fr 1.05fr;
      gap: 10px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }
    th, td {
      text-align: left;
      padding: 6px 8px;
      border-bottom: 1px solid #1a1a1a;
      vertical-align: top;
    }
    th { color: var(--muted); font-weight: 600; }
    code, pre {
      font-family: var(--mono);
    }
    pre {
      margin: 0;
      padding: 8px;
      white-space: pre-wrap;
      word-break: break-word;
      background: #050505;
      border-radius: 8px;
      border: 1px solid #1f1f1f;
      max-height: 180px;
      overflow: auto;
      font-size: 11px;
      line-height: 1.35;
    }
    .pill {
      display: inline-block;
      padding: 1px 7px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 700;
      border: 1px solid var(--border);
      text-transform: uppercase;
      letter-spacing: .04em;
    }
    .pill.codex, .pill.wafer, .pill.minimax, .pill.unknown {
      background: #101010;
      color: #fff;
    }
    .age.good { color: var(--good); }
    .age.warn { color: var(--warn); }
    .age.bad { color: var(--bad); }
    a { color: var(--link); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .section-head {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 8px;
    }
    .stack {
      display: grid;
      gap: 10px;
    }
    .lane-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
    }
    .mini-stat {
      background: var(--panel-2);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 8px;
    }
    .mini-label {
      font-size: 11px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: .04em;
    }
    .mini-value {
      margin-top: 4px;
      font-size: 18px;
      font-weight: 800;
    }
    .kpi-line {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 4px;
      font-size: 11px;
      color: var(--muted);
    }
    .kpi-stack {
      display: grid;
      gap: 3px;
      margin-top: 6px;
      font-size: 11px;
      color: var(--muted);
    }
    .dense-list {
      display: grid;
      gap: 6px;
    }
    .dense-row {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      align-items: start;
      padding: 6px 0;
      border-bottom: 1px solid #171717;
    }
    .dense-row:last-child {
      border-bottom: 0;
      padding-bottom: 0;
    }
    .small { font-size: 11px; }
    .mono { font-family: var(--mono); }
    .run-files {
      display: grid;
      gap: 4px;
      margin-top: 6px;
    }
    .run-file {
      color: var(--muted);
      font-size: 11px;
    }
    @media (max-width: 1100px) {
      .grid { grid-template-columns: 1fr; }
      .grid-ops { grid-template-columns: 1fr; }
      .grid-logs { grid-template-columns: 1fr; }
      .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .lane-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
  </style>
</head>
<body>
  <header>
    <h1>Attendee Queue Ops</h1>
    <div class="muted" id="meta">Loading...</div>
  </header>
  <div class="wrap">
    <section class="stats" id="stats"></section>
    <section class="grid">
      <div class="card">
        <div class="section-head">
          <h2>Running Jobs</h2>
          <span class="muted" id="running-count"></span>
        </div>
        <div id="running-table"></div>
      </div>
      <div class="stack">
        <div class="card">
          <div class="section-head">
            <h2>Lane Health</h2>
            <span class="muted">fresh worker activity</span>
          </div>
          <div class="lane-grid" id="lane-health"></div>
        </div>
        <div class="card">
          <div class="section-head">
            <h2>Failure Buckets</h2>
            <span class="muted">top current causes</span>
          </div>
          <div id="failed-buckets"></div>
        </div>
      </div>
    </section>
    <section class="grid-ops">
      <div class="card">
        <div class="section-head">
          <h2>Worker Tails</h2>
          <span class="muted" id="worker-count"></span>
        </div>
        <div class="grid-logs" id="worker-logs"></div>
      </div>
      <div class="stack">
        <div class="card">
          <div class="section-head">
            <h2>Recent Completed</h2>
            <span class="muted">latest 20</span>
          </div>
          <div id="completed-table"></div>
        </div>
        <div class="card">
          <div class="section-head">
            <h2>Recent Failed</h2>
            <span class="muted">latest 20</span>
          </div>
          <div id="failed-table"></div>
        </div>
        <div class="card">
          <div class="section-head">
            <h2>Recent Run Activity</h2>
            <span class="muted">modified run dirs</span>
          </div>
          <div id="recent-runs"></div>
        </div>
      </div>
    </section>
  </div>
  <script>
    function esc(v) {
      return String(v ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
    }
    function ageClass(seconds) {
      if (seconds < 90) return "good";
      if (seconds < 300) return "warn";
      return "bad";
    }
    function fmtAge(seconds) {
      seconds = Math.max(0, Math.floor(seconds));
      const m = Math.floor(seconds / 60);
      const s = seconds % 60;
      if (m >= 60) return `${Math.floor(m / 60)}h ${m % 60}m`;
      if (m > 0) return `${m}m ${s}s`;
      return `${s}s`;
    }
    function fmtTs(ts) {
      if (!ts) return "";
      return new Date(ts * 1000).toLocaleString();
    }
    function fmtNum(v) {
      if (v === null || v === undefined || v === "") return "—";
      return Number(v).toLocaleString();
    }
    function fmtMoney(v) {
      if (v === null || v === undefined || v === "") return "—";
      return `$${Number(v).toFixed(4)}`;
    }
    function fmtSeconds(v) {
      if (v === null || v === undefined || v === "") return "—";
      const n = Number(v);
      if (!Number.isFinite(n)) return "—";
      if (n < 60) return `${Math.round(n)}s`;
      const m = Math.floor(n / 60);
      const s = Math.round(n % 60);
      return `${m}m ${s}s`;
    }
    function renderTable(rows, cols) {
      if (!rows.length) return '<div class="muted">None</div>';
      const head = cols.map(c => `<th>${esc(c.label)}</th>`).join("");
      const body = rows.map(row => `<tr>${cols.map(c => `<td>${c.render ? c.render(row) : esc(row[c.key])}</td>`).join("")}</tr>`).join("");
      return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
    }
    async function refresh() {
      const res = await fetch('/api/state');
      const data = await res.json();
      const queue = data.queue || {};
      const workers = data.workers || [];
      const activeProcesses = data.active_processes || [];
      const lanes = data.lane_summary || {};
      const metrics = data.completed_metrics || {};
      const counts = data.queue_counts || queue.counts || {};
      const runningJobs = data.running_jobs || queue.running || [];
      const recentCompleted = data.recent_completed || queue.recent_completed || [];
      const recentFailed = data.recent_failed || queue.failed || [];
      const failedBuckets = data.failed_buckets || queue.failed_buckets || [];
      document.getElementById('meta').textContent = `Queue: ${queue.queue_name} • Updated ${new Date(data.generated_at * 1000).toLocaleTimeString()}`;
      const stats = [
        ['Completed', counts.completed || 0],
        ['Running', counts.running || 0],
        ['Pending', counts.pending || 0],
        ['Failed', counts.failed || 0],
        ['Codex', (lanes.codex || {}).active || 0],
        ['Claude', (lanes.claude || {}).active || 0],
        ['MiniMax', (lanes.minimax || {}).active || 0],
        ['Wafer', (lanes.wafer || {}).active || 0],
        ['Active Procs', activeProcesses.length],
        ['Avg Secs/App', fmtSeconds(metrics.avg_seconds_per_applicant)],
        ['Median Secs', fmtSeconds(metrics.median_seconds_per_applicant)],
        ['Avg Cost/App', fmtMoney(metrics.avg_cost_usd_per_applicant)],
        ['Avg Input Tok', fmtNum(metrics.avg_input_tokens_per_applicant)],
        ['Avg Output Tok', fmtNum(metrics.avg_output_tokens_per_applicant)],
      ];
      document.getElementById('stats').innerHTML = stats.map(([label, value]) => `
        <div class="card">
          <div class="muted">${esc(label)}</div>
          <div class="stat-value">${esc(value)}</div>
        </div>`).join('');

      document.getElementById('running-count').textContent = `${runningJobs.length} active`;
      document.getElementById('running-table').innerHTML = renderTable(runningJobs, [
        { label: '#', render: r => esc(r.priority_rank) },
        { label: 'Name', render: r => `${esc(r.name)}<div class="muted">${esc(r.email)}</div>` },
        { label: 'Attempts', render: r => esc(r.attempts) },
        { label: 'Started', render: r => `<span class="small">${esc(r.started_at || '')}</span>` },
        { label: 'Run', render: r => r.run_dir ? `<div class="mono small">${esc(r.run_id)}</div><div class="muted small">${esc(r.run_dir)}</div>` : '' },
      ]);

      const laneOrder = ['codex', 'claude', 'minimax', 'wafer', 'unknown'];
      document.getElementById('lane-health').innerHTML = laneOrder.map(name => {
        const info = lanes[name] || { active: 0, fresh_logs: 0, claimed: 0, completed_recent: 0, failed_recent: 0 };
        return `<div class="mini-stat">
          <div class="mini-label">${esc(name)}</div>
          <div class="mini-value">${esc(info.active || 0)}</div>
          <div class="kpi-line">
            <span>fresh logs ${esc(info.fresh_logs || 0)}</span>
            <span>claimed ${esc(info.claimed || 0)}</span>
          </div>
          <div class="kpi-stack">
            <span>completed ${esc(info.completed_recent || 0)}</span>
            <span>failed ${esc(info.failed_recent || 0)}</span>
            <span>avg secs ${esc(fmtSeconds(info.avg_seconds))}</span>
            <span>avg cost ${esc(fmtMoney(info.avg_cost_usd))}</span>
          </div>
        </div>`;
      }).join('');

      document.getElementById('failed-buckets').innerHTML = failedBuckets.length ? `
        <div class="dense-list">
          ${failedBuckets.map(item => `<div class="dense-row"><div>${esc(item.label)}</div><strong>${esc(item.count)}</strong></div>`).join('')}
        </div>` : '<div class="muted">No failures.</div>';

      document.getElementById('completed-table').innerHTML = renderTable(recentCompleted, [
        { label: '#', render: r => esc(r.priority_rank) },
        { label: 'Name', render: r => `${esc(r.name)}<div class="muted">${esc(r.email)}</div>` },
        { label: 'Finished', render: r => `<span class="small">${esc(r.finished_at || '')}</span>` },
        { label: 'Run', render: r => `<span class="mono small">${esc(r.run_id || '')}</span>` },
      ]);

      document.getElementById('failed-table').innerHTML = renderTable(recentFailed, [
        { label: '#', render: r => esc(r.priority_rank) },
        { label: 'Name', render: r => `${esc(r.name)}<div class="muted">${esc(r.email)}</div>` },
        { label: 'Finished', render: r => `<span class="small">${esc(r.finished_at || '')}</span>` },
        { label: 'Bucket', render: r => `<span class="small">${esc(r.error_bucket || '')}</span>` },
      ]);

      document.getElementById('worker-count').textContent = `${activeProcesses.length} active • ${workers.length} fresh logs`;
      document.getElementById('worker-logs').innerHTML = workers.map(w => `
        <div class="card">
          <div class="section-head">
            <div>
              <span class="pill ${esc(w.worker_type)}">${esc(w.worker_type)}</span>
              <strong>${esc(w.name)}</strong>
            </div>
            <span class="age ${ageClass(w.age_seconds)}">${fmtAge(w.age_seconds)} ago</span>
          </div>
          <div class="muted small">${esc(w.path)}</div>
          <div style="margin:6px 0 8px" class="small">${w.claimed ? esc(w.claimed) : '<span class="muted">No claim line yet</span>'}</div>
          <pre>${esc(w.tail || '')}</pre>
        </div>`).join('');

      const recentRuns = data.recent_run_activity || [];
      document.getElementById('recent-runs').innerHTML = recentRuns.length ? `
        <div class="dense-list">
          ${recentRuns.map(run => `<div class="dense-row">
            <div>
              <div><strong>${esc(run.run_id)}</strong></div>
              <div class="muted small">${esc(run.path)}</div>
              <div class="run-files">
                ${(run.latest_files || []).slice(0, 4).map(f => `<div class="run-file mono">${esc(f.path)} • ${esc(f.size)}b</div>`).join('')}
              </div>
            </div>
            <div class="small muted">${fmtTs(run.updated_at)}</div>
          </div>`).join('')}
        </div>` : '<div class="muted">No recent run activity.</div>';
    }
    refresh();
    setInterval(refresh, 3000);
  </script>
</body>
</html>"""


def make_handler(db_path: Path, queue_name: str, runs_root: Path):
    html_doc = render_html().encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path in {"/", "/index.html"}:
                self._send(HTTPStatus.OK, html_doc, "text/html; charset=utf-8")
                return
            if self.path == "/api/state":
                body = json.dumps(build_state(db_path, queue_name, runs_root)).encode("utf-8")
                self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
                return
            self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain; charset=utf-8")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve attendee queue dashboard.")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--queue-name", default="nyc-unified")
    parser.add_argument("--runs-root", default=str(DEFAULT_RUNS_ROOT))
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    runs_root = Path(args.runs_root).expanduser().resolve()
    handler = make_handler(db_path, args.queue_name, runs_root)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"Serving attendee queue dashboard at http://127.0.0.1:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
