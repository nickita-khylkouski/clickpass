"""Tiny local UI for TrialPilot live onboarding flows.

Run:
  python3 -m trialpilot.quick_ui --port 8787
"""

from __future__ import annotations

import argparse
import html
import json
import os
import subprocess
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs


def _load_local_env(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _extract_json_payload(stdout: str) -> dict:
    text = str(stdout or "").strip()
    if not text:
        return {}
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {"value": obj}
    except Exception:
        pass

    # Best effort: parse from the first "{" onward.
    brace = text.find("{")
    if brace >= 0:
        snippet = text[brace:]
        try:
            obj = json.loads(snippet)
            return obj if isinstance(obj, dict) else {"value": obj}
        except Exception:
            return {"raw_output": text}
    return {"raw_output": text}


def _run_live_cli(command_args: list[str], *, timeout_seconds: int = 1200) -> dict:
    cmd = [sys.executable, "-m", "trialpilot.live_cli", *command_args]
    started = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds, check=False)
    elapsed = round(max(0.0, time.monotonic() - started), 2)
    payload = _extract_json_payload(proc.stdout)
    return {
        "command": cmd,
        "exit_code": int(proc.returncode),
        "timing_seconds": elapsed,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "payload": payload,
    }


def _build_open_live_args(*, target_url: str, profile_id: str, proxy_country_code: str) -> list[str]:
    args = [
        "open-live",
        "--target-url",
        target_url,
        "--no-open-live-url",
    ]
    if profile_id:
        args.extend(["--profile-id", profile_id])
    if proxy_country_code:
        args.extend(["--proxy-country-code", proxy_country_code])
    return args


def _build_onboard_args(
    *,
    target_url: str,
    inbox: str,
    profile_id: str,
    proxy_country_code: str,
    super_fast: bool,
) -> list[str]:
    args = [
        "onboard",
        "--target-url",
        target_url,
        "--vision",
        "auto",
        "--flash-mode",
        "--verbose",
        "--include-api-key-step",
    ]
    if super_fast:
        args.append("--super-fast")
    if inbox:
        args.extend(["--inbox", inbox])
    if profile_id:
        args.extend(["--profile-id", profile_id])
    if proxy_country_code:
        args.extend(["--proxy-country-code", proxy_country_code])
    return args


def _render_page(
    *,
    target_url: str,
    inbox: str,
    profile_id: str,
    proxy_country_code: str,
    result: dict | None = None,
    error: str = "",
    super_fast: bool = True,
) -> str:
    result = result or {}
    payload = result.get("payload", {}) if isinstance(result, dict) else {}
    onboard_result = (payload.get("result") or {}) if isinstance(payload, dict) else {}
    summary = (onboard_result.get("summary") or {}) if isinstance(onboard_result, dict) else {}
    login = (onboard_result.get("login") or {}) if isinstance(onboard_result, dict) else {}
    api_key = onboard_result.get("api_key")
    open_live_url = payload.get("live_url")
    blocked_live_url = onboard_result.get("blocked_live_url")
    live_url = open_live_url or blocked_live_url or onboard_result.get("session_live_url")
    status_text = "idle"
    if result:
        if result.get("exit_code", 1) == 0:
            status_text = "success"
        else:
            status_text = "failed"
    escaped_json = html.escape(json.dumps(payload, indent=2, sort_keys=True))
    escaped_stderr = html.escape(str(result.get("stderr", "") if result else ""))
    escaped_error = html.escape(error)
    card_email = html.escape(str(summary.get("account_email") or login.get("email") or ""))
    card_password = html.escape(str(summary.get("account_password") or login.get("password") or ""))
    card_api_key = html.escape(str(api_key or ""))
    card_timing = html.escape(str(summary.get("timing_seconds") or result.get("timing_seconds") or ""))
    card_live_url = html.escape(str(live_url or ""))
    checked = "checked" if super_fast else ""

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>TrialPilot Quick UI</title>
  <style>
    :root {{
      --bg1: #0b132b;
      --bg2: #1c2541;
      --accent: #ffb703;
      --accent2: #8ecae6;
      --ok: #80ed99;
      --bad: #ff6b6b;
      --ink: #f8f9fa;
      --muted: #b8c0d6;
      --panel: rgba(255, 255, 255, 0.08);
      --panel2: rgba(255, 255, 255, 0.12);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background:
        radial-gradient(1200px 600px at -5% -10%, #243b6b 0%, transparent 55%),
        radial-gradient(900px 500px at 110% 5%, #6f1d1b 0%, transparent 55%),
        linear-gradient(135deg, var(--bg1), var(--bg2));
      font-family: "Avenir Next", "Gill Sans", "Trebuchet MS", sans-serif;
      min-height: 100vh;
    }}
    .wrap {{
      max-width: 1120px;
      margin: 0 auto;
      padding: 24px;
    }}
    h1 {{
      margin: 0 0 16px;
      font-weight: 700;
      letter-spacing: 0.02em;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid rgba(255,255,255,0.2);
      border-radius: 16px;
      padding: 16px;
      backdrop-filter: blur(6px);
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin-top: 14px;
    }}
    .card {{
      background: var(--panel2);
      border-radius: 12px;
      padding: 12px;
      border: 1px solid rgba(255,255,255,0.14);
      min-height: 92px;
    }}
    .label {{
      color: var(--accent2);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      margin-bottom: 6px;
    }}
    .value {{
      font-family: "Menlo", "Consolas", monospace;
      font-size: 13px;
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }}
    label {{
      display: block;
      margin: 2px 0 6px;
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    input[type="text"] {{
      width: 100%;
      background: rgba(0,0,0,0.25);
      color: var(--ink);
      border: 1px solid rgba(255,255,255,0.28);
      border-radius: 10px;
      padding: 10px 12px;
      outline: none;
    }}
    input[type="checkbox"] {{ transform: translateY(1px); }}
    .row {{
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 10px;
      margin-bottom: 10px;
    }}
    .buttons {{
      display: flex;
      gap: 8px;
      margin-top: 8px;
      flex-wrap: wrap;
    }}
    button {{
      border: 0;
      border-radius: 999px;
      padding: 10px 14px;
      font-weight: 700;
      cursor: pointer;
      letter-spacing: 0.03em;
    }}
    .primary {{ background: var(--accent); color: #111; }}
    .secondary {{ background: var(--accent2); color: #0a0a0a; }}
    .status {{
      margin-top: 12px;
      font-weight: 700;
      color: {"var(--ok)" if status_text == "success" else ("var(--bad)" if status_text == "failed" else "var(--muted)")};
    }}
    pre {{
      margin-top: 14px;
      background: rgba(0,0,0,0.36);
      border: 1px solid rgba(255,255,255,0.16);
      border-radius: 12px;
      padding: 12px;
      font-size: 12px;
      overflow: auto;
      max-height: 360px;
    }}
    .note {{
      margin-top: 10px;
      color: var(--muted);
      font-size: 12px;
    }}
    @media (max-width: 900px) {{
      .grid, .cards {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>TrialPilot Quick UI</h1>
    <div class="panel">
      <form method="post" action="/run">
        <div class="grid">
          <div>
            <label for="target_url">Target URL</label>
            <input id="target_url" name="target_url" type="text" required value="{html.escape(target_url)}" />
          </div>
          <div>
            <label for="inbox">Inbox (AgentMail)</label>
            <input id="inbox" name="inbox" type="text" value="{html.escape(inbox)}" />
          </div>
          <div>
            <label for="profile_id">Profile ID (Browser Use)</label>
            <input id="profile_id" name="profile_id" type="text" value="{html.escape(profile_id)}" />
          </div>
          <div>
            <label for="proxy_country_code">Proxy Country</label>
            <input id="proxy_country_code" name="proxy_country_code" type="text" value="{html.escape(proxy_country_code)}" />
          </div>
        </div>
        <div class="row">
          <input id="super_fast" name="super_fast" type="checkbox" value="1" {checked}/>
          <label for="super_fast" style="margin:0; text-transform:none; letter-spacing:0;">Super fast mode</label>
        </div>
        <div class="buttons">
          <button class="secondary" type="submit" name="action" value="open_live">Open Live Session</button>
          <button class="primary" type="submit" name="action" value="onboard">Run Onboard</button>
        </div>
      </form>

      <div class="status">Status: {html.escape(status_text)}</div>
      <div class="cards">
        <div class="card"><div class="label">Live Link</div><div class="value">{card_live_url}</div></div>
        <div class="card"><div class="label">Timing (s)</div><div class="value">{card_timing}</div></div>
        <div class="card"><div class="label">Email</div><div class="value">{card_email}</div></div>
        <div class="card"><div class="label">Password</div><div class="value">{card_password}</div></div>
        <div class="card"><div class="label">API Key</div><div class="value">{card_api_key}</div></div>
      </div>
      {"<div class='note'>Error: " + escaped_error + "</div>" if escaped_error else ""}
      {f"<pre>{escaped_stderr}</pre>" if escaped_stderr else ""}
      <pre>{escaped_json}</pre>
      <div class="note">Authorized automation only. If a human check appears, use the live link and resume.</div>
    </div>
  </div>
</body>
</html>
"""


class _QuickUIHandler(BaseHTTPRequestHandler):
    server_version = "TrialPilotQuickUI/1.0"

    def _defaults(self) -> tuple[str, str, str, str]:
        target_url = "https://skillsafe.ai/"
        inbox = os.getenv("AGENTMAIL_INBOX_ID", "").strip()
        profile_id = os.getenv("BROWSER_USE_PROFILE_ID", "").strip()
        proxy = (
            os.getenv("BROWSER_USE_CLOUD_PROXY_COUNTRY_CODE", "")
            or os.getenv("BROWSER_USE_PROXY_COUNTRY_CODE", "")
        ).strip()
        return target_url, inbox, profile_id, proxy

    def _send_html(self, body: str, status: int = HTTPStatus.OK) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/":
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
            return
        target_url, inbox, profile_id, proxy = self._defaults()
        self._send_html(
            _render_page(
                target_url=target_url,
                inbox=inbox,
                profile_id=profile_id,
                proxy_country_code=proxy,
            )
        )

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/run":
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            body_raw = self.rfile.read(max(0, content_length)).decode("utf-8", errors="ignore")
            form = parse_qs(body_raw, keep_blank_values=True)
            target_url = str((form.get("target_url", [""])[0] or "")).strip()
            inbox = str((form.get("inbox", [""])[0] or "")).strip()
            profile_id = str((form.get("profile_id", [""])[0] or "")).strip()
            proxy = str((form.get("proxy_country_code", [""])[0] or "")).strip()
            action = str((form.get("action", ["onboard"])[0] or "onboard")).strip()
            super_fast = bool(form.get("super_fast"))

            if not target_url:
                raise ValueError("target_url is required")

            if action == "open_live":
                run = _run_live_cli(
                    _build_open_live_args(
                        target_url=target_url,
                        profile_id=profile_id,
                        proxy_country_code=proxy,
                    ),
                    timeout_seconds=240,
                )
            else:
                run = _run_live_cli(
                    _build_onboard_args(
                        target_url=target_url,
                        inbox=inbox,
                        profile_id=profile_id,
                        proxy_country_code=proxy,
                        super_fast=super_fast,
                    ),
                    timeout_seconds=1500,
                )

            page = _render_page(
                target_url=target_url,
                inbox=inbox,
                profile_id=profile_id,
                proxy_country_code=proxy,
                result=run,
                super_fast=super_fast,
            )
            self._send_html(page)
        except Exception as exc:
            target_url, inbox, profile_id, proxy = self._defaults()
            page = _render_page(
                target_url=target_url,
                inbox=inbox,
                profile_id=profile_id,
                proxy_country_code=proxy,
                error=f"{type(exc).__name__}: {exc}",
            )
            self._send_html(page, status=HTTPStatus.BAD_REQUEST)


def main(argv: list[str] | None = None) -> int:
    _load_local_env()
    parser = argparse.ArgumentParser(description="TrialPilot quick local UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer((args.host, int(args.port)), _QuickUIHandler)
    print(f"TrialPilot Quick UI running on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

