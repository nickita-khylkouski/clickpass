#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from daytona_agent_leases import list_launches, pool_snapshot
from daytona_agents_ctl import (
    DEFAULT_LEASE_DB,
    classification_status_from_meta,
    prompt_batch_status_from_meta,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve a generic Daytona agents dashboard.")
    parser.add_argument("--port", type=int, default=8793)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_LEASE_DB)
    parser.add_argument("--launch-limit", type=int, default=40)
    parser.add_argument("--recent-file-limit", type=int, default=30)
    parser.add_argument("--active-only", action="store_true")
    return parser.parse_args()


def parse_iso8601(text: str) -> datetime | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def seconds_ago(iso_text: str) -> float | None:
    dt = parse_iso8601(iso_text)
    if dt is None:
        return None
    return max(0.0, (datetime.now(tz=UTC) - dt.astimezone(UTC)).total_seconds())


def fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return ""
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def launch_status(record: dict[str, object]) -> dict[str, object]:
    launch_kind = str(record.get("launch_kind") or "")
    if launch_kind == "prompt-batch":
        return prompt_batch_status_from_meta(record)
    if launch_kind == "cv-classification":
        return classification_status_from_meta(record)
    return {
        "launch_id": str(record["launch_id"]),
        "task_profile": launch_kind,
        "lease_status": str(record.get("status") or ""),
        "status_note": "unsupported_launch_kind",
    }


def recent_result_files(launch_statuses: list[dict[str, object]], *, limit: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in launch_statuses:
        result_root = str(item.get("result_root") or "").strip()
        if not result_root:
            continue
        root = Path(result_root)
        if not root.exists():
            continue
        for path in root.glob("*"):
            if not path.is_file() or path.suffix == ".json":
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            rows.append(
                {
                    "launch_id": str(item.get("launch_id") or ""),
                    "path": str(path),
                    "name": path.name,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                }
            )
    rows.sort(key=lambda row: float(row["mtime"]), reverse=True)
    return rows[: max(0, int(limit))]


def summarize_launch_record(record: dict[str, object]) -> dict[str, object]:
    status = launch_status(record)
    health_counts = dict(status.get("telemetry", {}).get("health_counts", {}) or {})
    raw_failed_items = status.get("failed_items")
    failed_items = list(raw_failed_items) if isinstance(raw_failed_items, list) else []
    queue_counts = dict(status.get("queue_counts") or {})
    lane_summaries = list(status.get("lane_summaries") or [])
    total_completed = int(status.get("completed_items") or queue_counts.get("completed") or 0)
    if isinstance(raw_failed_items, list):
        total_failed = len(raw_failed_items)
    else:
        total_failed = int(raw_failed_items or queue_counts.get("failed") or 0)
    meta = dict(record.get("metadata") or {})
    lease_status = str(status.get("lease_status") or record.get("status") or "")
    terminal = bool(status.get("terminal") or False)
    if lease_status in {"finished", "finished_with_failures", "stopped", "reaped", "failed", "released"}:
        terminal = True
    return {
        "launch_id": str(record["launch_id"]),
        "task_profile": str(record.get("launch_kind") or ""),
        "pool_name": str(record.get("pool_name") or ""),
        "lease_status": lease_status,
        "requested_sandboxes": int(record.get("requested_sandboxes") or 0),
        "created_at": str(record.get("created_at") or ""),
        "updated_at": str(record.get("updated_at") or ""),
        "last_heartbeat_at": str(record.get("last_heartbeat_at") or ""),
        "released_at": str(record.get("released_at") or ""),
        "age": fmt_age(seconds_ago(str(record.get("updated_at") or ""))),
        "alive_controllers": int(status.get("alive_controllers") or 0),
        "queue_counts": queue_counts,
        "completed_items": total_completed,
        "failed_items_count": total_failed,
        "failed_items": failed_items[:8],
        "health_counts": health_counts,
        "lane_summaries": lane_summaries,
        "launch_dir": str(status.get("launch_dir") or meta.get("launch_dir") or ""),
        "result_root": str(status.get("result_root") or ""),
        "eta": str(status.get("eta") or ""),
        "terminal": terminal,
        "controllers": list(status.get("controllers") or []),
        "status_note": str(status.get("status_note") or ""),
    }


def find_launch_summary(launch_rows: list[dict[str, object]], launch_id: str) -> dict[str, object] | None:
    for row in launch_rows:
        if str(row.get("launch_id") or "") == launch_id:
            return row
    return None


def safe_text_preview(path: Path, *, limit: int = 8000) -> str:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"<unreadable: {exc}>"
    return raw[: max(0, int(limit))]


def launch_related_files(launch_row: dict[str, object], *, limit: int = 80) -> list[dict[str, object]]:
    roots: list[Path] = []
    for key in ("launch_dir", "result_root"):
        value = str(launch_row.get(key) or "").strip()
        if value:
            root = Path(value)
            if root.exists():
                roots.append(root)
    for controller in list(launch_row.get("controllers") or []):
        value = str(dict(controller).get("log") or "").strip()
        if value:
            path = Path(value)
            if path.exists():
                roots.append(path.parent)
    seen: set[str] = set()
    rows: list[dict[str, object]] = []
    for root in roots:
        candidates = [root] if root.is_file() else list(root.rglob("*"))
        for path in candidates:
            if not path.is_file():
                continue
            text = str(path.resolve())
            if text in seen:
                continue
            seen.add(text)
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            rows.append(
                {
                    "path": text,
                    "name": path.name,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                }
            )
    rows.sort(key=lambda row: float(row["mtime"]), reverse=True)
    return rows[: max(0, int(limit))]


def file_preview_payload(launch_rows: list[dict[str, object]], *, launch_id: str, path_text: str, limit: int = 8000) -> dict[str, object]:
    launch = find_launch_summary(launch_rows, launch_id)
    if launch is None:
        raise FileNotFoundError(f"unknown launch_id: {launch_id}")
    allowed: set[str] = {str(item["path"]) for item in launch_related_files(launch, limit=500)}
    requested = Path(path_text).expanduser().resolve()
    if str(requested) not in allowed:
        raise PermissionError(f"path is not in launch scope: {requested}")
    stat = requested.stat()
    return {
        "launch_id": launch_id,
        "path": str(requested),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "preview": safe_text_preview(requested, limit=limit),
    }


def build_summary(db_path: Path, *, launch_limit: int, recent_file_limit: int, active_only: bool) -> dict[str, object]:
    launches = list_launches(db_path, only_active=bool(active_only))
    launch_rows = [summarize_launch_record(record) for record in launches[: max(1, int(launch_limit))]]
    active_count = sum(1 for row in launch_rows if not row["terminal"] and not row["released_at"])
    recent_outputs = recent_result_files(launch_rows, limit=recent_file_limit)
    return {
        "generated_at_epoch": time.time(),
        "db_path": str(db_path),
        "pools": pool_snapshot(db_path),
        "launches": launch_rows,
        "active_launches": active_count,
        "recent_outputs": recent_outputs,
    }


def render_html() -> str:
    return """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Daytona Agents Dashboard</title>
  <style>
    :root {
      --bg: #050505;
      --panel: #0d0d0d;
      --panel-2: #121212;
      --text: #f5f5f5;
      --muted: #a0a0a0;
      --border: #262626;
      --good: #58d68d;
      --warn: #f5b041;
      --bad: #ec7063;
      --mono: ui-monospace, SFMono-Regular, Menlo, monospace;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); font: 13px/1.45 ui-sans-serif, system-ui, sans-serif; }
    header { padding: 16px 18px 10px; border-bottom: 1px solid var(--border); }
    h1, h2, h3 { margin: 0; }
    .muted { color: var(--muted); }
    .wrap { padding: 14px 18px 18px; display: grid; gap: 10px; }
    .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; }
    .card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 10px; }
    .value { font-size: 24px; font-weight: 800; margin-top: 6px; line-height: 1; }
    .grid { display: grid; grid-template-columns: 1.15fr .85fr; gap: 10px; }
    .two-up { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
    th { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
    .mono { font-family: var(--mono); word-break: break-word; }
    .pill { display: inline-block; border: 1px solid var(--border); border-radius: 999px; padding: 2px 8px; margin: 0 6px 6px 0; font-size: 11px; }
    .good { color: var(--good); }
    .warn { color: var(--warn); }
    .bad { color: var(--bad); }
    .launch { display: grid; gap: 8px; }
    .launch + .launch { margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border); }
    .row { display: flex; flex-wrap: wrap; gap: 6px 10px; }
    .tiny { font-size: 11px; color: var(--muted); }
    pre { margin: 0; white-space: pre-wrap; word-break: break-word; font: 11px/1.45 var(--mono); }
    button { background: var(--panel-2); color: var(--text); border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px; cursor: pointer; }
    button:hover { border-color: #4a4a4a; }
    a { color: #8ec5ff; text-decoration: none; }
    @media (max-width: 1100px) { .grid, .two-up { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <header>
    <h1>Daytona Agents Dashboard</h1>
    <div class="muted">Generic view over shared pool capacity, launches, queue health, controllers, and recent outputs.</div>
  </header>
  <div class="wrap">
    <section class="stats" id="stats"></section>
    <section class="grid">
      <div class="card">
        <h2>Launches</h2>
        <div id="launches" class="launch"></div>
      </div>
      <div style="display:grid; gap:10px;">
        <div class="card">
          <h2>Pools</h2>
          <div id="pools"></div>
        </div>
        <div class="card">
          <h2>Recent Outputs</h2>
          <div id="recent"></div>
        </div>
      </div>
    </section>
    <section class="two-up">
      <div class="card">
        <h2>Launch Detail</h2>
        <div id="launch-detail" class="muted">Select a launch.</div>
      </div>
      <div class="card">
        <h2>File Preview</h2>
        <div id="file-preview" class="muted">Select a file from a launch.</div>
      </div>
    </section>
  </div>
  <script>
    let latestSummary = null;

    async function refresh() {
      const res = await fetch('/api/summary');
      const data = await res.json();
      latestSummary = data;
      renderStats(data);
      renderPools(data.pools || []);
      renderLaunches(data.launches || []);
      renderRecent(data.recent_outputs || []);
    }

    function esc(text) {
      return String(text ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
    }

    function renderStats(data) {
      const pools = data.pools || [];
      const launches = data.launches || [];
      const reserved = pools.reduce((n, row) => n + Number(row.reserved || 0), 0);
      const available = pools.reduce((n, row) => n + Number(row.available || 0), 0);
      const active = Number(data.active_launches || 0);
      const completed = launches.reduce((n, row) => n + Number(row.completed_items || 0), 0);
      const failed = launches.reduce((n, row) => n + Number(row.failed_items_count || 0), 0);
      document.getElementById('stats').innerHTML = [
        card('Active Launches', active),
        card('Reserved Sandboxes', reserved),
        card('Available Sandboxes', available),
        card('Completed Items', completed),
        card('Failed Items', failed),
      ].join('');
    }

    function card(label, value) {
      return `<div class="card"><div class="muted">${esc(label)}</div><div class="value">${esc(value)}</div></div>`;
    }

    function renderPools(pools) {
      document.getElementById('pools').innerHTML = pools.map(row => `
        <div class="launch">
          <div><strong>${esc(row.pool_name)}</strong></div>
          <div class="row tiny">
            <span>limit=${esc(row.sandbox_limit)}</span>
            <span>reserved=${esc(row.reserved)}</span>
            <span>available=${esc(row.available)}</span>
          </div>
        </div>
      `).join('') || '<div class="muted">No pools.</div>';
    }

    function statusClass(row) {
      if (row.failed_items_count > 0) return 'bad';
      if (!row.terminal) return 'warn';
      return 'good';
    }

    function renderLaunches(rows) {
      document.getElementById('launches').innerHTML = rows.map(row => {
        const hc = row.health_counts || {};
        const health = Object.entries(hc).filter(([,v]) => Number(v) > 0).map(([k,v]) => `<span class="pill bad">${esc(k)}=${esc(v)}</span>`).join('');
        const failed = (row.failed_items || []).map(item => `<div class="tiny mono">${esc(item.task_id)} ${esc(item.last_error || '')}</div>`).join('');
        const laneSummary = (row.lane_summaries || []).map(item => `<span class="pill">${esc(item.lane)} finished=${esc(item.finished)} running=${esc(item.running)} pending=${esc(item.pending)}</span>`).join('');
        const queue = row.queue_counts || {};
        return `
          <div class="launch">
            <div class="row">
              <strong>${esc(row.launch_id)}</strong>
              <span class="pill ${statusClass(row)}">${esc(row.task_profile)} / ${esc(row.lease_status)}</span>
              <span class="pill">sandboxes=${esc(row.requested_sandboxes)}</span>
              <span class="pill">controllers_alive=${esc(row.alive_controllers)}</span>
              <span class="pill">updated=${esc(row.age)}</span>
            </div>
            <div class="row tiny">
              <span>completed=${esc(row.completed_items)}</span>
              <span>failed=${esc(row.failed_items_count)}</span>
              ${Object.entries(queue).map(([k,v]) => `<span>${esc(k)}=${esc(v)}</span>`).join('')}
            </div>
            <div><button onclick="loadLaunchDetail('${esc(row.launch_id)}')">Inspect launch</button></div>
            ${row.eta ? `<div class="tiny">eta=${esc(row.eta)}</div>` : ''}
            ${laneSummary ? `<div>${laneSummary}</div>` : ''}
            ${health ? `<div>${health}</div>` : ''}
            ${failed ? `<div><div class="tiny bad">Failed items</div>${failed}</div>` : ''}
            <div class="tiny mono">${esc(row.launch_dir || '')}</div>
          </div>
        `;
      }).join('') || '<div class="muted">No launches.</div>';
    }

    function renderRecent(rows) {
      document.getElementById('recent').innerHTML = rows.map(row => `
        <div class="launch">
          <div><strong>${esc(row.launch_id)}</strong></div>
          <div class="tiny mono">${esc(row.name)}</div>
          <div class="row tiny">
            <span>size=${esc(row.size)}</span>
            <span>${esc(new Date(Number(row.mtime) * 1000).toLocaleString())}</span>
          </div>
          <div class="tiny mono">${esc(row.path)}</div>
        </div>
      `).join('') || '<div class="muted">No recent outputs.</div>';
    }

    async function loadLaunchDetail(launchId) {
      const res = await fetch('/api/launch?launch_id=' + encodeURIComponent(launchId));
      const data = await res.json();
      const files = data.files || [];
      const fileButtons = files.map(file => `
        <div class="row tiny">
          <button onclick="loadFilePreview('${esc(data.launch.launch_id)}', '${esc(file.path)}')">${esc(file.name)}</button>
          <span>${esc(file.size)}</span>
        </div>
      `).join('');
      document.getElementById('launch-detail').innerHTML = `
        <div class="launch">
          <div class="row">
            <strong>${esc(data.launch.launch_id)}</strong>
            <span class="pill">${esc(data.launch.task_profile)}</span>
            <span class="pill">${esc(data.launch.lease_status)}</span>
          </div>
          <div class="tiny mono">${esc(data.launch.launch_dir || '')}</div>
          <div class="tiny mono">${esc(data.launch.result_root || '')}</div>
          <div class="row tiny">
            <span>completed=${esc(data.launch.completed_items)}</span>
            <span>failed=${esc(data.launch.failed_items_count)}</span>
            <span>controllers=${esc(data.launch.alive_controllers)}</span>
          </div>
          <div class="tiny">Files</div>
          ${fileButtons || '<div class="muted">No files.</div>'}
        </div>
      `;
    }

    async function loadFilePreview(launchId, path) {
      const res = await fetch('/api/file?launch_id=' + encodeURIComponent(launchId) + '&path=' + encodeURIComponent(path));
      const data = await res.json();
      document.getElementById('file-preview').innerHTML = `
        <div class="launch">
          <div><strong>${esc(data.path)}</strong></div>
          <div class="row tiny">
            <span>size=${esc(data.size)}</span>
            <span>${esc(new Date(Number(data.mtime) * 1000).toLocaleString())}</span>
          </div>
          <pre>${esc(data.preview || '')}</pre>
        </div>
      `;
    }

    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "daytona-agents-dashboard/0.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    @property
    def app(self) -> "DashboardServer":
        return self.server  # type: ignore[return-value]

    def _send_json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=True, indent=2).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, text: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = text.encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(render_html())
            return
        if parsed.path == "/api/summary":
            query = parse_qs(parsed.query)
            launch_limit = int(query.get("launch_limit", [str(self.app.launch_limit)])[0])
            recent_limit = int(query.get("recent_file_limit", [str(self.app.recent_file_limit)])[0])
            payload = build_summary(
                self.app.db_path,
                launch_limit=launch_limit,
                recent_file_limit=recent_limit,
                active_only=self.app.active_only,
            )
            self._send_json(payload)
            return
        if parsed.path == "/api/launch":
            query = parse_qs(parsed.query)
            launch_id = str(query.get("launch_id", [""])[0]).strip()
            if not launch_id:
                self._send_json({"error": "missing_launch_id"}, status=HTTPStatus.BAD_REQUEST)
                return
            summary = build_summary(
                self.app.db_path,
                launch_limit=max(self.app.launch_limit, 200),
                recent_file_limit=self.app.recent_file_limit,
                active_only=False,
            )
            launch = find_launch_summary(list(summary.get("launches") or []), launch_id)
            if launch is None:
                self._send_json({"error": "unknown_launch_id"}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json({"launch": launch, "files": launch_related_files(launch)})
            return
        if parsed.path == "/api/file":
            query = parse_qs(parsed.query)
            launch_id = str(query.get("launch_id", [""])[0]).strip()
            path_text = str(query.get("path", [""])[0]).strip()
            if not launch_id or not path_text:
                self._send_json({"error": "missing_launch_id_or_path"}, status=HTTPStatus.BAD_REQUEST)
                return
            summary = build_summary(
                self.app.db_path,
                launch_limit=max(self.app.launch_limit, 200),
                recent_file_limit=self.app.recent_file_limit,
                active_only=False,
            )
            try:
                payload = file_preview_payload(list(summary.get("launches") or []), launch_id=launch_id, path_text=path_text)
            except FileNotFoundError:
                self._send_json({"error": "unknown_launch_id"}, status=HTTPStatus.NOT_FOUND)
                return
            except PermissionError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
                return
            self._send_json(payload)
            return
        self._send_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)


class DashboardServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler],
        *,
        db_path: Path,
        launch_limit: int,
        recent_file_limit: int,
        active_only: bool,
    ) -> None:
        super().__init__(server_address, handler_cls)
        self.db_path = db_path
        self.launch_limit = launch_limit
        self.recent_file_limit = recent_file_limit
        self.active_only = active_only


def main() -> int:
    args = parse_args()
    db_path = args.db_path.expanduser().resolve()
    server = DashboardServer(
        ("127.0.0.1", int(args.port)),
        Handler,
        db_path=db_path,
        launch_limit=int(args.launch_limit),
        recent_file_limit=int(args.recent_file_limit),
        active_only=bool(args.active_only),
    )
    print(f"Serving Daytona agents dashboard on http://127.0.0.1:{args.port}")
    print(f"Lease DB: {db_path}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
