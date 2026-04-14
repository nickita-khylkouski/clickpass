#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import shlex
import subprocess
import sys
import time
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
RUNS_ROOT = ROOT / "data" / "web_audit_runs"
LIVE_ROOT = ROOT / "data" / "web_audit_live"
CLI_SCRIPT = ROOT / "scripts" / "claude_mini.py"


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Web Audit Dashboard</title>
  <style>
    :root {
      --bg: #0b1020;
      --panel: #121936;
      --muted: #9fb0d0;
      --text: #eef4ff;
      --accent: #6ee7ff;
      --accent-2: #8b5cf6;
      --border: rgba(255,255,255,0.12);
      --good: #34d399;
      --warn: #f59e0b;
      --bad: #f87171;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(110,231,255,0.12), transparent 28%),
        radial-gradient(circle at top right, rgba(139,92,246,0.12), transparent 24%),
        linear-gradient(180deg, #09111f 0%, #0d1327 100%);
      min-height: 100vh;
    }
    .layout {
      display: grid;
      grid-template-columns: 360px 1fr;
      gap: 16px;
      padding: 16px;
      min-height: 100vh;
    }
    .panel {
      background: rgba(18,25,54,0.94);
      border: 1px solid var(--border);
      border-radius: 16px;
      box-shadow: 0 18px 40px rgba(0,0,0,0.22);
      overflow: hidden;
    }
    .panel-header {
      padding: 14px 16px;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .title {
      font-size: 18px;
      font-weight: 700;
      letter-spacing: 0.02em;
    }
    .muted { color: var(--muted); }
    .sidebar-body, .content-body {
      padding: 16px;
    }
    form {
      display: grid;
      gap: 10px;
      margin-bottom: 18px;
    }
    label {
      display: grid;
      gap: 6px;
      font-size: 13px;
      color: var(--muted);
    }
    input, select, button, textarea {
      font: inherit;
    }
    input, select {
      border: 1px solid var(--border);
      border-radius: 10px;
      background: rgba(255,255,255,0.04);
      color: var(--text);
      padding: 10px 12px;
      width: 100%;
    }
    button {
      border: 0;
      border-radius: 12px;
      padding: 10px 14px;
      font-weight: 700;
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      color: #06101d;
      cursor: pointer;
    }
    button.secondary {
      background: rgba(255,255,255,0.08);
      color: var(--text);
      border: 1px solid var(--border);
    }
    .run-list {
      display: grid;
      gap: 8px;
      max-height: calc(100vh - 360px);
      overflow: auto;
    }
    .run-card {
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 12px;
      background: rgba(255,255,255,0.04);
      cursor: pointer;
    }
    .run-card.active {
      border-color: var(--accent);
      box-shadow: 0 0 0 1px rgba(110,231,255,0.25) inset;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 12px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.06);
    }
    .pill.live { color: var(--warn); }
    .pill.done { color: var(--good); }
    .pill.dead { color: var(--bad); }
    .grid {
      display: grid;
      grid-template-columns: 280px 1fr;
      gap: 16px;
    }
    .files {
      display: grid;
      gap: 8px;
      max-height: calc(100vh - 170px);
      overflow: auto;
    }
    .file-btn {
      width: 100%;
      text-align: left;
      padding: 10px 12px;
      border-radius: 10px;
      background: rgba(255,255,255,0.04);
      color: var(--text);
      border: 1px solid var(--border);
    }
    .file-btn.active {
      border-color: var(--accent);
      color: var(--accent);
    }
    pre {
      margin: 0;
      padding: 16px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: rgba(0,0,0,0.26);
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      max-height: calc(100vh - 190px);
    }
    .meta-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }
    .meta-box {
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 10px 12px;
      background: rgba(255,255,255,0.04);
    }
    .toolbar {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }
    .small {
      font-size: 12px;
    }
    @media (max-width: 1100px) {
      .layout { grid-template-columns: 1fr; }
      .grid { grid-template-columns: 1fr; }
      .run-list { max-height: 320px; }
      pre, .files { max-height: none; }
    }
  </style>
</head>
<body>
  <div class="layout">
    <section class="panel">
      <div class="panel-header">
        <div>
          <div class="title">Web Audit Control</div>
          <div class="muted small">Launch and monitor MiniMax web-audit runs</div>
        </div>
        <button class="secondary" id="refreshBtn">Refresh</button>
      </div>
      <div class="sidebar-body">
        <form id="launchForm">
          <label>Target URL
            <input name="target_url" placeholder="https://app.lessie.ai/mylist" required />
          </label>
          <label>Run ID
            <input name="run_id" placeholder="optional" />
          </label>
          <label>Timeout Seconds
            <input name="timeout_seconds" type="number" value="0" min="0" step="60" />
          </label>
          <label>Effort
            <select name="effort">
              <option value="low" selected>low</option>
              <option value="medium">medium</option>
              <option value="high">high</option>
              <option value="max">max</option>
            </select>
          </label>
          <label>Sandbox Policy
            <select name="sandbox_policy">
              <option value="reuse" selected>reuse</option>
              <option value="fresh">fresh</option>
              <option value="pool">pool</option>
            </select>
          </label>
          <label>Sandbox Name
            <input name="sandbox_name" placeholder="optional reuse target" />
          </label>
          <button type="submit">Launch Audit</button>
        </form>

        <div class="meta-box" style="margin-bottom:18px;">
          <div class="muted small">Visibility</div>
          <div class="small">This UI shows commands, prompts, launcher logs, artifact files, and model outputs. It does not expose private hidden reasoning.</div>
        </div>

        <div class="toolbar">
          <span class="pill live" id="activeCount">0 active</span>
          <span class="pill done" id="doneCount">0 completed</span>
        </div>
        <div class="run-list" id="runList"></div>
      </div>
    </section>

    <section class="panel">
      <div class="panel-header">
        <div>
          <div class="title" id="detailTitle">Run Detail</div>
          <div class="muted small" id="detailSubtitle">Select a run</div>
        </div>
        <div class="muted small" id="detailStatus"></div>
      </div>
      <div class="content-body">
        <div id="emptyState" class="muted">No run selected.</div>
        <div id="detailView" style="display:none;">
          <div class="meta-grid" id="metaGrid"></div>
          <div class="grid">
            <div>
              <div class="toolbar">
                <button class="secondary small" id="autoRefreshBtn">Auto Refresh: On</button>
              </div>
              <div class="files" id="fileList"></div>
            </div>
            <div>
              <pre id="fileContent"></pre>
            </div>
          </div>
        </div>
      </div>
    </section>
  </div>
  <script>
    const runListEl = document.getElementById('runList');
    const fileListEl = document.getElementById('fileList');
    const fileContentEl = document.getElementById('fileContent');
    const metaGridEl = document.getElementById('metaGrid');
    const emptyStateEl = document.getElementById('emptyState');
    const detailViewEl = document.getElementById('detailView');
    const detailTitleEl = document.getElementById('detailTitle');
    const detailSubtitleEl = document.getElementById('detailSubtitle');
    const detailStatusEl = document.getElementById('detailStatus');
    const activeCountEl = document.getElementById('activeCount');
    const doneCountEl = document.getElementById('doneCount');
    const autoRefreshBtn = document.getElementById('autoRefreshBtn');
    const refreshBtn = document.getElementById('refreshBtn');
    const launchForm = document.getElementById('launchForm');

    let state = { runs: [], selectedRunId: null, selectedSource: null, selectedFile: null, autoRefresh: true };

    function fmtTime(ts) {
      if (!ts) return '';
      try { return new Date(ts * 1000).toLocaleString(); } catch { return ''; }
    }

    async function fetchJson(url, options) {
      const resp = await fetch(url, options);
      if (!resp.ok) throw new Error(await resp.text());
      return await resp.json();
    }

    function renderRuns() {
      runListEl.innerHTML = '';
      activeCountEl.textContent = `${state.runs.filter(r => r.source === 'live').length} active`;
      doneCountEl.textContent = `${state.runs.filter(r => r.source === 'completed').length} completed`;
      for (const run of state.runs) {
        const div = document.createElement('div');
        div.className = 'run-card' + (run.run_id === state.selectedRunId ? ' active' : '');
        div.innerHTML = `
          <div style="display:flex;justify-content:space-between;gap:8px;align-items:center;">
            <strong>${run.run_id}</strong>
            <span class="pill ${run.source === 'live' ? (run.process_alive ? 'live' : 'dead') : 'done'}">${run.source === 'live' ? (run.process_alive ? 'live' : 'stopped') : 'completed'}</span>
          </div>
          <div class="muted small" style="margin-top:6px;">${run.target_url || ''}</div>
          <div class="muted small" style="margin-top:6px;">${run.source === 'live' ? 'launcher' : 'artifact'} · ${fmtTime(run.updated_at_epoch)}</div>
        `;
        div.onclick = () => selectRun(run.run_id, run.source);
        runListEl.appendChild(div);
      }
    }

    function renderMeta(detail) {
      const entries = [
        ['Run ID', detail.run_id],
        ['Source', detail.source],
        ['Target', detail.target_url || detail.meta?.target_url || ''],
        ['Updated', fmtTime(detail.updated_at_epoch)],
        ['PID', detail.pid || ''],
        ['Alive', detail.process_alive == null ? '' : String(detail.process_alive)],
        ['Run Path', detail.path || ''],
        ['Completed Path', detail.completed_run_path || '']
      ].filter(([, value]) => value !== '');
      metaGridEl.innerHTML = entries.map(([k, v]) => `
        <div class="meta-box">
          <div class="muted small">${k}</div>
          <div>${v}</div>
        </div>
      `).join('');
    }

    function renderFiles(detail) {
      fileListEl.innerHTML = '';
      const files = detail.files || [];
      if (!files.length) {
        fileListEl.innerHTML = '<div class="muted">No files yet.</div>';
        fileContentEl.textContent = '';
        return;
      }
      if (!state.selectedFile || !files.includes(state.selectedFile)) {
        state.selectedFile = files[0];
      }
      for (const file of files) {
        const btn = document.createElement('button');
        btn.className = 'file-btn' + (file === state.selectedFile ? ' active' : '');
        btn.textContent = file;
        btn.onclick = async () => {
          state.selectedFile = file;
          renderFiles(detail);
          await loadFile(detail.run_id, detail.source, file);
        };
        fileListEl.appendChild(btn);
      }
    }

    async function loadFile(runId, source, file) {
      const payload = await fetchJson(`/api/file?run_id=${encodeURIComponent(runId)}&source=${encodeURIComponent(source)}&path=${encodeURIComponent(file)}`);
      fileContentEl.textContent = payload.text || '';
    }

    async function selectRun(runId, source) {
      state.selectedRunId = runId;
      state.selectedSource = source;
      renderRuns();
      const detail = await fetchJson(`/api/run?run_id=${encodeURIComponent(runId)}&source=${encodeURIComponent(source)}`);
      detailTitleEl.textContent = detail.run_id;
      detailSubtitleEl.textContent = detail.target_url || detail.meta?.target_url || '';
      detailStatusEl.textContent = detail.source === 'live'
        ? (detail.process_alive ? 'live launcher' : 'launcher stopped')
        : 'completed artifact bundle';
      renderMeta(detail);
      renderFiles(detail);
      emptyStateEl.style.display = 'none';
      detailViewEl.style.display = '';
      if (state.selectedFile) {
        await loadFile(detail.run_id, detail.source, state.selectedFile);
      }
    }

    async function refreshRuns(preserveSelection = true) {
      const payload = await fetchJson('/api/runs');
      state.runs = [...payload.active, ...payload.completed];
      renderRuns();
      if (preserveSelection && state.selectedRunId && state.selectedSource) {
        const stillExists = state.runs.find(r => r.run_id === state.selectedRunId && r.source === state.selectedSource);
        if (stillExists) {
          await selectRun(state.selectedRunId, state.selectedSource);
        }
      } else if (state.runs.length) {
        await selectRun(state.runs[0].run_id, state.runs[0].source);
      }
    }

    refreshBtn.onclick = async () => refreshRuns(false);
    autoRefreshBtn.onclick = () => {
      state.autoRefresh = !state.autoRefresh;
      autoRefreshBtn.textContent = `Auto Refresh: ${state.autoRefresh ? 'On' : 'Off'}`;
    };

    launchForm.onsubmit = async (event) => {
      event.preventDefault();
      const form = new FormData(launchForm);
      const body = Object.fromEntries(form.entries());
      body.timeout_seconds = Number(body.timeout_seconds ?? 0);
      try {
        await fetchJson('/api/launch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        await refreshRuns(false);
      } catch (err) {
        alert(String(err));
      }
    };

    setInterval(() => {
      if (state.autoRefresh) {
        refreshRuns(true).catch(err => console.error(err));
      }
    }, 4000);

    refreshRuns(false).catch(err => console.error(err));
  </script>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve a local dashboard for web-audit runs.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    return parser.parse_args()


def now_epoch() -> float:
    return time.time()


def read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def discover_cli_audit_runs() -> list[dict]:
    live: list[dict] = []
    try:
        result = subprocess.run(
            ["ps", "-ax", "-o", "pid=,command="],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return live
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            pid_text, command = line.split(None, 1)
        except ValueError:
            continue
        if "audit-web" not in command or "claude_mini.py" not in command:
            continue
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        try:
            parts = shlex.split(command)
        except ValueError:
            continue
        target_url = ""
        run_id = ""
        for idx, token in enumerate(parts):
            if token == "audit-web" and idx + 1 < len(parts):
                target_url = parts[idx + 1]
            if token == "--run-id" and idx + 1 < len(parts):
                run_id = parts[idx + 1]
        if not run_id:
            continue
        live.append(
            {
                "run_id": run_id,
                "target_url": target_url,
                "pid": pid,
                "command": parts,
            }
        )
    return live


def list_completed_runs() -> list[dict]:
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    runs: list[dict] = []
    for path in sorted(RUNS_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not path.is_dir():
            continue
        meta = read_json(path / "meta.json") or {}
        runs.append(
            {
                "source": "completed",
                "run_id": path.name,
                "target_url": meta.get("target_url", ""),
                "path": str(path),
                "updated_at_epoch": path.stat().st_mtime,
            }
        )
    return runs


def list_live_runs() -> list[dict]:
    LIVE_ROOT.mkdir(parents=True, exist_ok=True)
    runs: list[dict] = []
    seen_run_ids: set[str] = set()
    for path in sorted(LIVE_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not path.is_dir():
            continue
        state = read_json(path / "state.json") or {}
        pid = state.get("pid")
        alive = process_alive(pid if isinstance(pid, int) else None)
        if not alive:
            continue
        run_id = state.get("run_id", path.name)
        seen_run_ids.add(str(run_id))
        runs.append(
            {
                "source": "live",
                "run_id": run_id,
                "target_url": state.get("target_url", ""),
                "path": str(path),
                "pid": pid,
                "process_alive": alive,
                "updated_at_epoch": max(path.stat().st_mtime, (path / "launch.log").stat().st_mtime if (path / "launch.log").exists() else 0),
                "completed_run_path": state.get("completed_run_path", ""),
            }
        )
    for discovered in discover_cli_audit_runs():
        run_id = discovered["run_id"]
        if run_id in seen_run_ids:
            continue
        path = LIVE_ROOT / run_id
        path.mkdir(parents=True, exist_ok=True)
        state = {
            "run_id": run_id,
            "target_url": discovered["target_url"],
            "pid": discovered["pid"],
            "started_at_epoch": now_epoch(),
            "updated_at_epoch": now_epoch(),
            "command": discovered["command"],
            "notes": "Auto-registered from a direct audit-web CLI process.",
        }
        (path / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if not (path / "launch.log").exists():
            (path / "launch.log").write_text(
                "[web-audit-dashboard] Auto-registered direct CLI audit run.\n",
                encoding="utf-8",
            )
        runs.append(
            {
                "source": "live",
                "run_id": run_id,
                "target_url": discovered["target_url"],
                "path": str(path),
                "pid": discovered["pid"],
                "process_alive": True,
                "updated_at_epoch": path.stat().st_mtime,
                "completed_run_path": "",
            }
        )
    runs.sort(key=lambda item: item.get("updated_at_epoch", 0), reverse=True)
    return runs


def allowed_root(source: str) -> Path:
    if source == "live":
        return LIVE_ROOT.resolve()
    return RUNS_ROOT.resolve()


def safe_child(root: Path, *parts: str) -> Path:
    path = (root / Path(*parts)).resolve()
    if root != path and root not in path.parents:
        raise ValueError("Path escapes root")
    return path


def run_detail(run_id: str, source: str) -> dict:
    root = allowed_root(source)
    path = safe_child(root, run_id)
    if not path.exists() or not path.is_dir():
        raise FileNotFoundError(run_id)
    payload: dict = {
        "source": source,
        "run_id": run_id,
        "path": str(path),
        "updated_at_epoch": path.stat().st_mtime,
    }
    if source == "live":
        state = read_json(path / "state.json") or {}
        pid = state.get("pid")
        payload.update(state)
        payload["process_alive"] = process_alive(pid if isinstance(pid, int) else None)
        files = []
        for candidate in sorted(path.glob("**/*")):
            if candidate.is_file():
                files.append(str(candidate.relative_to(path)))
        files.sort(
            key=lambda name: (
                0 if name == "messages.log" else 1 if name == "launch.log" else 2,
                name,
            )
        )
        payload["files"] = files
        return payload
    meta = read_json(path / "meta.json") or {}
    payload["meta"] = meta
    payload["target_url"] = meta.get("target_url", "")
    files = []
    for candidate in sorted(path.glob("**/*")):
        if candidate.is_file():
            files.append(str(candidate.relative_to(path)))
    payload["files"] = files
    return payload


def file_text(run_id: str, source: str, rel_path: str) -> str:
    root = safe_child(allowed_root(source), run_id)
    path = safe_child(root, rel_path)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(rel_path)
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return f"[binary image] {path.name}"
    data = path.read_text(encoding="utf-8", errors="replace")
    if len(data) > 250_000:
        data = data[-250_000:]
    return data


def build_launch_command(payload: dict, run_id: str) -> list[str]:
    cmd = [
        sys.executable,
        str(CLI_SCRIPT),
        "audit-web",
        str(payload["target_url"]).strip(),
        "--run-id",
        run_id,
        "--timeout-seconds",
        str(int(payload.get("timeout_seconds") or 0)),
        "--effort",
        str(payload.get("effort") or "low"),
        "--sandbox-policy",
        str(payload.get("sandbox_policy") or "reuse"),
    ]
    sandbox_name = str(payload.get("sandbox_name") or "").strip()
    if sandbox_name:
        cmd.extend(["--sandbox-name", sandbox_name])
    return cmd


def launch_run(payload: dict) -> dict:
    target_url = str(payload.get("target_url") or "").strip()
    if not target_url:
        raise ValueError("target_url is required")
    run_id = str(payload.get("run_id") or "").strip() or f"web-audit-{datetime.now(tz=UTC).strftime('%Y%m%dT%H%M%SZ')}"
    live_dir = LIVE_ROOT / run_id
    live_dir.mkdir(parents=True, exist_ok=True)
    cmd = build_launch_command(payload, run_id)
    log_path = live_dir / "launch.log"
    with log_path.open("ab") as log_file:
        proc = subprocess.Popen(
            cmd,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            start_new_session=True,
            close_fds=True,
        )
    state = {
        "run_id": run_id,
        "target_url": target_url,
        "pid": proc.pid,
        "started_at_epoch": now_epoch(),
        "updated_at_epoch": now_epoch(),
        "command": cmd,
    }
    (live_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return state


class Handler(BaseHTTPRequestHandler):
    server_version = "web-audit-dashboard/0.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        sys.stderr.write(f"[web-audit-dashboard] {self.address_string()} {format % args}\n")

    def _send_json(self, payload: dict | list, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, text: str, status: int = 200, content_type: str = "text/html; charset=utf-8") -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b"{}"
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/web", "/dashboard"}:
            self._send_text(HTML)
            return
        if parsed.path == "/api/runs":
            self._send_json({"active": list_live_runs(), "completed": list_completed_runs()})
            return
        if parsed.path == "/api/run":
            qs = parse_qs(parsed.query)
            run_id = (qs.get("run_id") or [""])[0]
            source = (qs.get("source") or ["completed"])[0]
            try:
                self._send_json(run_detail(run_id, source))
            except FileNotFoundError:
                self._send_json({"error": "run not found"}, status=404)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=400)
            return
        if parsed.path == "/api/file":
            qs = parse_qs(parsed.query)
            run_id = (qs.get("run_id") or [""])[0]
            source = (qs.get("source") or ["completed"])[0]
            rel_path = (qs.get("path") or [""])[0]
            try:
                self._send_json({"text": file_text(run_id, source, rel_path)})
            except FileNotFoundError:
                self._send_json({"error": "file not found"}, status=404)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=400)
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/launch":
            try:
                payload = self._read_json_body()
                state = launch_run(payload)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=400)
                return
            self._send_json(state, status=201)
            return
        self._send_json({"error": "not found"}, status=404)


def main() -> int:
    args = parse_args()
    LIVE_ROOT.mkdir(parents=True, exist_ok=True)
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"http://{args.host}:{args.port}", flush=True)

    def _shutdown(*_args: object) -> None:
        server.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    server.serve_forever(poll_interval=0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
