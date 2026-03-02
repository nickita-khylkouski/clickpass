"""Multi-agent Browser Use HAR capture + curl probing + Claude analysis runner.

This tool is designed for authorized web security assessments only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
HTTP_METHODS = {"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"}
AGENTMAIL_BASE = "https://api.agentmail.to/v0"
DEFAULT_AGENT_GOALS = [
    "Map core authenticated user flows end-to-end.",
    "Find settings, billing, API key, and security-related pages.",
    "Explore error paths, validation messages, and edge-case forms.",
]
FORWARDED_HEADER_ALLOWLIST = {
    "accept",
    "content-type",
    "user-agent",
    "origin",
    "referer",
    "x-requested-with",
}


@dataclass
class WorkerResult:
    worker_id: int
    goal: str
    status: str
    har_path: str
    final_result: str | None = None
    errors: list[str | None] | None = None
    urls: list[str | None] | None = None
    action_names: list[str] | None = None
    inbox_id: str | None = None
    otp_retrieved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "goal": self.goal,
            "status": self.status,
            "har_path": self.har_path,
            "final_result": self.final_result,
            "errors": self.errors or [],
            "urls": self.urls or [],
            "action_names": self.action_names or [],
            "inbox_id": self.inbox_id,
            "otp_retrieved": self.otp_retrieved,
        }


def _now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _normalize_host(url: str | None) -> str:
    if not url:
        return ""
    host = (urlparse(url).hostname or "").strip().lower()
    return host


def _base_domain(host: str) -> str:
    if host.startswith("www."):
        return host[4:]
    return host


def _build_agent_prompt(
    *,
    target_url: str,
    goal: str,
    use_account: bool,
    use_agentmail: bool,
    include_billing: bool,
) -> str:
    credential_block = ""
    if use_account:
        credential_block = (
            "If account creation/login is present, use these placeholders exactly:\n"
            "- Email: <secret>ACCOUNT_EMAIL</secret>\n"
            "- Password: <secret>ACCOUNT_PASSWORD</secret>\n"
            "- First name: <secret>ACCOUNT_FIRST_NAME</secret>\n"
            "- Last name: <secret>ACCOUNT_LAST_NAME</secret>\n"
        )
    otp_block = ""
    if use_agentmail:
        otp_block = (
            "If email OTP verification appears, stop when OTP input is visible and wait for continuation.\n"
            "Do not invent OTP values.\n"
        )
    billing_block = ""
    if include_billing:
        billing_block = (
            "If billing/payment appears, continue and fill card details automatically using:\n"
            "- Card number: <secret>CARD_NUMBER</secret>\n"
            "- Exp month: <secret>CARD_EXP_MONTH</secret>\n"
            "- Exp year: <secret>CARD_EXP_YEAR</secret>\n"
            "- CVC: <secret>CARD_CVC</secret>\n"
            "- Name on card: <secret>CARD_NAME</secret>\n"
            "- Billing zip/postal: <secret>CARD_ZIP</secret>\n"
            "Only submit if all required card fields are present.\n"
        )

    return (
        f"Target: {target_url}\n"
        f"Goal: {goal}\n\n"
        "Rules:\n"
        "1) Stay on the target domain and direct subdomains.\n"
        "2) Click through meaningful product/application flows.\n"
        "3) If signup/login appears, complete it with provided credentials.\n"
        "4) Do not bypass security controls, KYC, captchas, or payment locks.\n"
        "5) Prefer broad coverage over deep repetition.\n"
        "6) Stop when you have mapped main logged-in and settings areas.\n"
        "7) Use only non-destructive actions.\n\n"
        f"{credential_block}"
        f"{otp_block}"
        f"{billing_block}"
        "Output requirements:\n"
        "- Return a concise summary of pages visited, auth state reached, and blockers.\n"
    )


def _build_continue_prompt(*, otp_available: bool, include_billing: bool) -> str:
    otp_line = ""
    if otp_available:
        otp_line = (
            "Enter verification code <secret>OTP_CODE</secret> if OTP input is present, "
            "then continue onboarding.\n"
        )
    billing_block = ""
    if include_billing:
        billing_block = (
            "If billing/payment appears, fill and submit card details using:\n"
            "- <secret>CARD_NUMBER</secret>\n"
            "- <secret>CARD_EXP_MONTH</secret>\n"
            "- <secret>CARD_EXP_YEAR</secret>\n"
            "- <secret>CARD_CVC</secret>\n"
            "- <secret>CARD_NAME</secret>\n"
            "- <secret>CARD_ZIP</secret>\n"
        )
    return (
        "Continue from the current page in the same session.\n"
        f"{otp_line}"
        f"{billing_block}"
        "Navigate through post-auth pages (dashboard/settings/billing/API) and stop with a concise summary."
    )


def _resolve_worker_value(value: str | None, worker_id: int) -> str | None:
    if value is None:
        return None
    return value.replace("{worker}", str(worker_id))


def _json_request(
    *,
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: bytes | None = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    req = Request(url=url, method=method.upper(), data=body)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            return json.loads(raw)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} for {url}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Network error for {url}: {exc}") from exc


def _agentmail_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _create_agentmail_inbox(*, api_key: str, domain: str, username: str | None = None) -> str:
    payload: dict[str, Any] = {"domain": domain}
    if username:
        payload["username"] = username
    data = _json_request(
        method="POST",
        url=f"{AGENTMAIL_BASE}/inboxes",
        headers=_agentmail_headers(api_key),
        payload=payload,
    )
    inbox_id = str(data.get("inbox_id", "")).strip()
    if not inbox_id:
        raise RuntimeError(f"AgentMail inbox creation failed: {data}")
    return inbox_id


def _extract_first_otp(text: str, otp_regex: str) -> str | None:
    pattern = re.compile(otp_regex)
    match = pattern.search(text)
    if not match:
        return None
    return match.group(1) if match.groups() else match.group(0)


def _fetch_agentmail_otp(*, api_key: str, inbox_id: str, otp_regex: str) -> str | None:
    list_data = _json_request(
        method="GET",
        url=f"{AGENTMAIL_BASE}/inboxes/{quote(inbox_id, safe='')}/messages?limit=10",
        headers=_agentmail_headers(api_key),
    )
    messages = list_data.get("messages", []) or []
    for item in messages:
        message_id = str(item.get("message_id", "")).strip()
        if not message_id:
            continue
        detail = _json_request(
            method="GET",
            url=f"{AGENTMAIL_BASE}/inboxes/{quote(inbox_id, safe='')}/messages/{quote(message_id, safe='')}",
            headers=_agentmail_headers(api_key),
        )
        blob = "\n".join(
            [
                str(detail.get("subject", "") or ""),
                str(detail.get("preview", "") or ""),
                str(detail.get("text", "") or ""),
                str(detail.get("html", "") or ""),
            ]
        )
        otp = _extract_first_otp(blob, otp_regex)
        if otp:
            return otp
    return None


def _wait_for_agentmail_otp(
    *,
    api_key: str,
    inbox_id: str,
    otp_regex: str,
    wait_seconds: int,
    poll_seconds: int,
) -> str:
    start = time.time()
    while True:
        otp = _fetch_agentmail_otp(api_key=api_key, inbox_id=inbox_id, otp_regex=otp_regex)
        if otp:
            return otp
        if time.time() - start > wait_seconds:
            raise TimeoutError(f"No OTP found within {wait_seconds}s for inbox={inbox_id}")
        time.sleep(max(1, poll_seconds))


async def _run_browseruse_worker(
    *,
    worker_id: int,
    goal: str,
    target_url: str,
    host: str,
    out_dir: Path,
    model: str,
    max_steps: int,
    headless: bool,
    api_key: str,
    account_email: str | None,
    account_password: str | None,
    account_first_name: str | None,
    account_last_name: str | None,
    use_agentmail: bool,
    agentmail_api_key: str | None,
    agentmail_inbox: str | None,
    agentmail_domain: str,
    agentmail_username_prefix: str,
    otp_wait_seconds: int,
    otp_poll_seconds: int,
    otp_regex: str,
    post_otp_steps: int,
    auto_billing: bool,
    card_number: str | None,
    card_exp_month: str | None,
    card_exp_year: str | None,
    card_cvc: str | None,
    card_name: str | None,
    card_zip: str | None,
    har_content: str,
    local_user_data_dir: str | None,
) -> WorkerResult:
    har_path = out_dir / f"worker_{worker_id}.har"
    if local_user_data_dir:
        profile_dir = Path(local_user_data_dir).expanduser().resolve()
    else:
        profile_dir = out_dir / "profiles" / f"worker_{worker_id}"
    profile_dir.mkdir(parents=True, exist_ok=True)
    status = "error"
    final_result: str | None = None
    errors: list[str | None] = []
    urls: list[str | None] = []
    action_names: list[str] = []
    browser: Any = None
    inbox_id: str | None = None
    otp_retrieved = False
    sensitive_data: dict[str, str] = {}

    try:
        from browser_use import Agent, Browser, BrowserProfile, ChatBrowserUse

        resolved_email = _resolve_worker_value(account_email, worker_id)
        resolved_password = _resolve_worker_value(account_password, worker_id) or f"StrongPass!{worker_id}23"
        resolved_first_name = _resolve_worker_value(account_first_name, worker_id) or f"Demo{worker_id}"
        resolved_last_name = _resolve_worker_value(account_last_name, worker_id) or "User"

        if use_agentmail:
            if not agentmail_api_key:
                raise RuntimeError("AGENTMAIL_API_KEY missing while --use-agentmail is enabled")
            inbox_id = _resolve_worker_value(agentmail_inbox, worker_id)
            if not inbox_id:
                inbox_username = f"{agentmail_username_prefix}{worker_id}-{int(time.time())}"
                inbox_id = _create_agentmail_inbox(
                    api_key=agentmail_api_key,
                    domain=agentmail_domain,
                    username=inbox_username,
                )
            resolved_email = inbox_id

        if not resolved_email:
            resolved_email = f"worker{worker_id}-{int(time.time())}@example.com"

        sensitive_data["ACCOUNT_EMAIL"] = resolved_email
        sensitive_data["ACCOUNT_PASSWORD"] = resolved_password
        sensitive_data["ACCOUNT_FIRST_NAME"] = resolved_first_name
        sensitive_data["ACCOUNT_LAST_NAME"] = resolved_last_name

        if auto_billing:
            billing_fields = {
                "CARD_NUMBER": card_number or "",
                "CARD_EXP_MONTH": card_exp_month or "",
                "CARD_EXP_YEAR": card_exp_year or "",
                "CARD_CVC": card_cvc or "",
                "CARD_NAME": card_name or "",
                "CARD_ZIP": card_zip or "",
            }
            missing = [k for k, v in billing_fields.items() if not str(v).strip()]
            if missing:
                raise RuntimeError(f"--auto-billing requires all card fields; missing: {', '.join(missing)}")
            sensitive_data.update({k: str(v).strip() for k, v in billing_fields.items()})

        browser_profile = BrowserProfile(
            headless=headless,
            user_data_dir=profile_dir,
            record_har_path=har_path,
            record_har_mode="full",
            record_har_content=har_content,
            # Keep scope on target + subdomains (e.g. app.<domain>, api.<domain>)
            allowed_domains=[host, _base_domain(host), f"www.{_base_domain(host)}", f"*.{_base_domain(host)}"],
            keep_alive=True,
        )
        browser = Browser(browser_profile=browser_profile)
        llm = ChatBrowserUse(model=model, api_key=api_key)

        initial_prompt = _build_agent_prompt(
            target_url=target_url,
            goal=goal,
            use_account=True,
            use_agentmail=use_agentmail,
            include_billing=False,
        )

        stage1 = Agent(
            task=initial_prompt,
            llm=llm,
            browser=browser,
            sensitive_data=sensitive_data,
            use_judge=False,
            use_thinking=True,
            include_recent_events=True,
        )
        stage1_history = await stage1.run(max_steps=max_steps)

        final_result = stage1_history.final_result()
        errors.extend(stage1_history.errors())
        urls.extend(stage1_history.urls())
        action_names.extend(stage1_history.action_names())

        should_continue = auto_billing or use_agentmail
        if should_continue:
            if use_agentmail and inbox_id:
                otp_code = _wait_for_agentmail_otp(
                    api_key=agentmail_api_key or "",
                    inbox_id=inbox_id,
                    otp_regex=otp_regex,
                    wait_seconds=otp_wait_seconds,
                    poll_seconds=otp_poll_seconds,
                )
                sensitive_data["OTP_CODE"] = otp_code
                otp_retrieved = True

            continue_prompt = _build_continue_prompt(
                otp_available=bool(sensitive_data.get("OTP_CODE")),
                include_billing=auto_billing,
            )
            stage2 = Agent(
                task=continue_prompt,
                llm=llm,
                browser=browser,
                sensitive_data=sensitive_data,
                use_judge=False,
                use_thinking=True,
                include_recent_events=True,
                directly_open_url=False,
            )
            stage2_history = await stage2.run(max_steps=post_otp_steps)
            stage2_final = stage2_history.final_result()
            if stage2_final:
                final_result = stage2_final
            errors.extend(stage2_history.errors())
            urls.extend(stage2_history.urls())
            action_names.extend(stage2_history.action_names())

        status = "ok"
    except Exception as exc:
        errors = [f"{type(exc).__name__}: {exc}"]
        status = "error"
    finally:
        try:
            await browser.kill()
        except Exception:
            pass

    return WorkerResult(
        worker_id=worker_id,
        goal=goal,
        status=status,
        har_path=str(har_path),
        final_result=final_result,
        errors=errors,
        urls=urls,
        action_names=action_names,
        inbox_id=inbox_id,
        otp_retrieved=otp_retrieved,
    )


def _read_har_entries(har_path: Path) -> list[dict[str, Any]]:
    payload = json.loads(har_path.read_text(encoding="utf-8"))
    return payload.get("log", {}).get("entries", []) or []


def _build_inventory(har_files: list[Path], target_host: str) -> list[dict[str, Any]]:
    inventory: dict[tuple[str, str], dict[str, Any]] = {}

    for har_file in har_files:
        try:
            entries = _read_har_entries(har_file)
        except Exception:
            continue

        for entry in entries:
            req = entry.get("request", {}) or {}
            resp = entry.get("response", {}) or {}
            raw_url = str(req.get("url", "")).strip()
            method = str(req.get("method", "GET")).upper()
            if not raw_url or method not in HTTP_METHODS:
                continue

            parsed = urlparse(raw_url)
            host = (parsed.hostname or "").lower()
            if target_host and host != target_host and not host.endswith(f".{target_host}"):
                continue

            key = (method, raw_url)
            headers = {
                str(h.get("name", "")).lower(): str(h.get("value", ""))
                for h in (req.get("headers") or [])
                if isinstance(h, dict) and h.get("name")
            }
            status = resp.get("status")
            status_int = int(status) if isinstance(status, int) else None
            mime_type = str(((resp.get("content") or {}).get("mimeType")) or "")
            post_data = req.get("postData")
            body_text = None
            if isinstance(post_data, dict):
                body_text = post_data.get("text")

            record = inventory.get(key)
            if record is None:
                inventory[key] = {
                    "method": method,
                    "url": raw_url,
                    "path": parsed.path or "/",
                    "host": host,
                    "query": parsed.query,
                    "headers": headers,
                    "status": status_int,
                    "mime_type": mime_type,
                    "body_text": body_text,
                    "seen_in_har": [har_file.name],
                    "count": 1,
                }
            else:
                record["count"] = int(record.get("count", 1)) + 1
                if har_file.name not in record["seen_in_har"]:
                    record["seen_in_har"].append(har_file.name)
                if record.get("status") is None and status_int is not None:
                    record["status"] = status_int
                if not record.get("mime_type") and mime_type:
                    record["mime_type"] = mime_type
                if not record.get("body_text") and body_text:
                    record["body_text"] = body_text

    return sorted(inventory.values(), key=lambda x: (x["method"], x["url"]))


def _build_curl_command(
    endpoint: dict[str, Any],
    *,
    auth_header: str | None,
    cookie_header: str | None,
    timeout_seconds: int,
) -> list[str]:
    cmd = [
        "curl",
        "-sS",
        "-i",
        "--max-time",
        str(timeout_seconds),
        "-X",
        str(endpoint["method"]),
        str(endpoint["url"]),
    ]

    headers = endpoint.get("headers") or {}
    if isinstance(headers, dict):
        for name, value in headers.items():
            lname = str(name).lower()
            if lname in FORWARDED_HEADER_ALLOWLIST and value:
                cmd.extend(["-H", f"{lname}: {value}"])

    if auth_header:
        cmd.extend(["-H", auth_header])
    if cookie_header:
        cmd.extend(["-H", cookie_header])

    body_text = endpoint.get("body_text")
    method = str(endpoint.get("method", "")).upper()
    if method not in SAFE_METHODS and isinstance(body_text, str) and body_text:
        cmd.extend(["--data-raw", body_text])

    return cmd


def _run_curl_probes(
    *,
    inventory: list[dict[str, Any]],
    out_dir: Path,
    limit: int,
    allow_state_changing: bool,
    auth_header: str | None,
    cookie_header: str | None,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    probes: list[dict[str, Any]] = []
    probe_dir = out_dir / "curl"
    probe_dir.mkdir(parents=True, exist_ok=True)

    candidates = []
    for item in inventory:
        method = str(item.get("method", "")).upper()
        if not allow_state_changing and method not in SAFE_METHODS:
            continue
        candidates.append(item)

    for idx, endpoint in enumerate(candidates[:limit], start=1):
        cmd = _build_curl_command(
            endpoint,
            auth_header=auth_header,
            cookie_header=cookie_header,
            timeout_seconds=timeout_seconds,
        )
        proc = subprocess.run(cmd, capture_output=True, text=False, check=False)
        response_text = proc.stdout.decode("utf-8", errors="replace")
        stderr_text = proc.stderr.decode("utf-8", errors="replace")
        output_path = probe_dir / f"probe_{idx:03d}.txt"
        output_path.write_text(response_text, encoding="utf-8")

        first_line = ""
        for line in response_text.splitlines():
            if line.startswith("HTTP/"):
                first_line = line.strip()
                break

        probes.append(
            {
                "index": idx,
                "method": endpoint["method"],
                "url": endpoint["url"],
                "command": shlex.join(cmd),
                "http_status_line": first_line,
                "exit_code": proc.returncode,
                "stderr": stderr_text.strip(),
                "output_file": str(output_path),
            }
        )

    return probes


def _run_claude_analysis(
    *,
    payload: dict[str, Any],
    out_dir: Path,
    claude_cmd: str,
    timeout_seconds: int,
    endpoint_limit: int,
    curl_limit: int,
) -> dict[str, Any]:
    prompt = (
        "You are reviewing authorized web assessment artifacts. "
        "Analyze HAR-derived endpoint inventory and optional curl probe outputs. "
        "Focus on likely auth/session/access-control/data-exposure issues and risky misconfigurations. "
        "Do not provide exploit instructions. "
        "Return markdown with sections: Critical, High, Medium, Low, Unknowns, Recommended Next Checks."
    )
    cmd = [*shlex.split(claude_cmd), "-p", prompt]
    compact_inventory = []
    for item in (payload.get("inventory") or [])[:endpoint_limit]:
        compact_inventory.append(
            {
                "method": item.get("method"),
                "url": item.get("url"),
                "path": item.get("path"),
                "status": item.get("status"),
                "mime_type": item.get("mime_type"),
                "count": item.get("count"),
            }
        )
    compact_curl = []
    for probe in (payload.get("curl_results") or [])[:curl_limit]:
        compact_curl.append(
            {
                "method": probe.get("method"),
                "url": probe.get("url"),
                "http_status_line": probe.get("http_status_line"),
                "exit_code": probe.get("exit_code"),
                "stderr": probe.get("stderr"),
            }
        )
    compact_payload = {
        "run_timestamp_utc": payload.get("run_timestamp_utc"),
        "target_url": payload.get("target_url"),
        "inventory_count": payload.get("inventory_count"),
        "curl_probe_count": len(compact_curl),
        "worker_results": payload.get("worker_results"),
        "inventory": compact_inventory,
        "curl_results": compact_curl,
    }

    try:
        proc = subprocess.run(
            cmd,
            input=json.dumps(compact_payload, indent=2),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "command": shlex.join(cmd),
            "exit_code": None,
            "analysis_file": None,
            "stderr": f"Claude command timed out after {timeout_seconds}s: {exc}",
        }
    analysis_path = out_dir / "claude_analysis.md"
    if proc.stdout:
        analysis_path.write_text(proc.stdout, encoding="utf-8")

    return {
        "command": shlex.join(cmd),
        "exit_code": proc.returncode,
        "analysis_file": str(analysis_path) if analysis_path.exists() else None,
        "stderr": proc.stderr.strip(),
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run multi-agent Browser Use HAR capture and optional curl+Claude analysis."
    )
    parser.add_argument("--target-url", default=None, help="Target base URL (required unless --simulate-har is used)")
    parser.add_argument("--goal", action="append", default=[], help="Agent goal (can be repeated)")
    parser.add_argument("--workers", type=int, default=2, help="Number of Browser Use agents to run")
    parser.add_argument("--max-steps", type=int, default=80)
    parser.add_argument("--browseruse-model", default="bu-latest")
    parser.add_argument("--headed", action="store_true", help="Run browser with UI instead of headless")
    parser.add_argument("--out-dir", default="trialpilot_out/assist")
    parser.add_argument("--simulate-har", action="append", default=[], help="Path to existing HAR file")
    parser.add_argument("--skip-browseruse", action="store_true", help="Skip Browser Use run and use --simulate-har only")
    parser.add_argument("--run-curl", action="store_true", help="Run curl probes from captured inventory")
    parser.add_argument("--curl-limit", type=int, default=20)
    parser.add_argument("--curl-timeout-seconds", type=int, default=20)
    parser.add_argument(
        "--allow-state-changing-curl",
        action="store_true",
        help="Allow POST/PUT/PATCH/DELETE curl probes (default probes only safe methods).",
    )
    parser.add_argument("--auth-header", default=None, help='Optional auth header, e.g. "Authorization: Bearer <token>"')
    parser.add_argument("--cookie-header", default=None, help='Optional cookie header, e.g. "Cookie: session=..."')
    parser.add_argument("--skip-claude", action="store_true")
    parser.add_argument("--claude-cmd", default="claude", help='Claude CLI command, e.g. "claude"')
    parser.add_argument("--claude-timeout-seconds", type=int, default=180)
    parser.add_argument("--claude-endpoint-limit", type=int, default=150)
    parser.add_argument("--claude-curl-limit", type=int, default=100)
    parser.add_argument(
        "--har-only",
        action="store_true",
        help="Capture HAR + inventory only (disables curl and Claude analysis).",
    )
    parser.add_argument("--monitor", action="store_true", help="Print live progress snapshots while workers run")
    parser.add_argument("--monitor-interval-seconds", type=int, default=5)
    parser.add_argument("--confirm-authorized", action="store_true", help="Required confirmation flag")
    parser.add_argument("--account-email", default=None)
    parser.add_argument("--account-password", default=None)
    parser.add_argument("--account-first-name", default=None)
    parser.add_argument("--account-last-name", default=None)
    parser.add_argument("--use-agentmail", action="store_true", help="Create worker inboxes and continue with OTP from AgentMail.")
    parser.add_argument("--agentmail-api-key", default=None, help="Optional override for AGENTMAIL_API_KEY.")
    parser.add_argument(
        "--agentmail-inbox",
        default=None,
        help="Existing AgentMail inbox email/id to reuse (avoids creating new inboxes).",
    )
    parser.add_argument("--agentmail-domain", default="agentmail.to")
    parser.add_argument("--agentmail-username-prefix", default="buworker")
    parser.add_argument("--otp-wait-seconds", type=int, default=300)
    parser.add_argument("--otp-poll-seconds", type=int, default=5)
    parser.add_argument("--otp-regex", default=r"\b(\d{4,8})\b")
    parser.add_argument("--post-otp-steps", type=int, default=80)
    parser.add_argument("--auto-billing", action="store_true", help="Attempt to autofill and submit billing/payment forms.")
    parser.add_argument("--card-number", default=None)
    parser.add_argument("--card-exp-month", default=None)
    parser.add_argument("--card-exp-year", default=None)
    parser.add_argument("--card-cvc", default=None)
    parser.add_argument("--card-name", default=None)
    parser.add_argument("--card-zip", default=None)
    parser.add_argument(
        "--local-user-data-dir",
        default=None,
        help="Use an existing local browser user-data directory (cookies/profile). Requires --workers 1.",
    )
    parser.add_argument(
        "--har-content",
        default="omit",
        choices=["embed", "attach", "omit"],
        help="HAR content mode. Use omit/attach to reduce file size; embed stores full bodies.",
    )
    return parser.parse_args(argv)


async def _run_browseruse_batch(
    *,
    target_url: str,
    goals: list[str],
    workers: int,
    max_steps: int,
    model: str,
    headed: bool,
    out_dir: Path,
    api_key: str,
    account_email: str | None,
    account_password: str | None,
    account_first_name: str | None,
    account_last_name: str | None,
    use_agentmail: bool,
    agentmail_api_key: str | None,
    agentmail_inbox: str | None,
    agentmail_domain: str,
    agentmail_username_prefix: str,
    otp_wait_seconds: int,
    otp_poll_seconds: int,
    otp_regex: str,
    post_otp_steps: int,
    auto_billing: bool,
    card_number: str | None,
    card_exp_month: str | None,
    card_exp_year: str | None,
    card_cvc: str | None,
    card_name: str | None,
    card_zip: str | None,
    har_content: str,
    local_user_data_dir: str | None,
    monitor: bool,
    monitor_interval_seconds: int,
) -> list[WorkerResult]:
    host = _normalize_host(target_url)
    if not host:
        raise ValueError(f"Invalid --target-url: {target_url}")

    if not goals:
        goals = DEFAULT_AGENT_GOALS[:]

    selected_goals: list[str] = []
    for i in range(workers):
        base_goal = goals[i] if i < len(goals) else goals[i % len(goals)]
        selected_goals.append(f"{base_goal} (worker {i + 1}/{workers})")

    task_map: dict[int, asyncio.Task[WorkerResult]] = {}
    for i in range(workers):
        task_map[i + 1] = asyncio.create_task(
            _run_browseruse_worker(
            worker_id=i + 1,
            goal=selected_goals[i],
            target_url=target_url,
            host=host,
            out_dir=out_dir,
            model=model,
            max_steps=max_steps,
            headless=not headed,
            api_key=api_key,
            account_email=account_email,
            account_password=account_password,
            account_first_name=account_first_name,
            account_last_name=account_last_name,
            use_agentmail=use_agentmail,
            agentmail_api_key=agentmail_api_key,
            agentmail_inbox=agentmail_inbox,
            agentmail_domain=agentmail_domain,
            agentmail_username_prefix=agentmail_username_prefix,
            otp_wait_seconds=otp_wait_seconds,
            otp_poll_seconds=otp_poll_seconds,
            otp_regex=otp_regex,
            post_otp_steps=post_otp_steps,
            auto_billing=auto_billing,
            card_number=card_number,
            card_exp_month=card_exp_month,
            card_exp_year=card_exp_year,
            card_cvc=card_cvc,
            card_name=card_name,
            card_zip=card_zip,
            har_content=har_content,
            local_user_data_dir=local_user_data_dir,
        )
        )

    if monitor:
        while True:
            pending = [wid for wid, task in task_map.items() if not task.done()]
            snapshot = {
                "event": "monitor",
                "pending_workers": pending,
                "workers": [],
            }
            for wid, task in task_map.items():
                har_path = out_dir / f"worker_{wid}.har"
                snapshot["workers"].append(
                    {
                        "worker_id": wid,
                        "status": "done" if task.done() else "running",
                        "har_exists": har_path.exists(),
                        "har_bytes": har_path.stat().st_size if har_path.exists() else 0,
                    }
                )
            print(json.dumps(snapshot), flush=True)
            if not pending:
                break
            await asyncio.sleep(max(1, monitor_interval_seconds))

    return [await task for _, task in sorted(task_map.items(), key=lambda x: x[0])]


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.confirm_authorized:
        print(json.dumps({"ok": False, "error": "--confirm-authorized is required"}))
        return 2

    if not args.skip_browseruse and not args.target_url and not args.simulate_har:
        print(json.dumps({"ok": False, "error": "--target-url required when Browser Use is enabled"}))
        return 2

    if args.har_only:
        args.run_curl = False
        args.skip_claude = True

    run_dir = Path(args.out_dir) / _now_tag()
    run_dir.mkdir(parents=True, exist_ok=True)

    worker_results: list[WorkerResult] = []
    if not args.skip_browseruse:
        api_key = (os.getenv("BROWSER_USE_API_KEY") or "").strip()
        if not api_key:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "BROWSER_USE_API_KEY is missing. Set it or pass --skip-browseruse with --simulate-har.",
                    }
                )
            )
            return 2

        agentmail_api_key = (args.agentmail_api_key or os.getenv("AGENTMAIL_API_KEY") or "").strip() or None
        if args.use_agentmail and not agentmail_api_key:
            print(json.dumps({"ok": False, "error": "--use-agentmail requires AGENTMAIL_API_KEY or --agentmail-api-key"}))
            return 2
        if args.auto_billing:
            required_card = [
                ("--card-number", args.card_number),
                ("--card-exp-month", args.card_exp_month),
                ("--card-exp-year", args.card_exp_year),
                ("--card-cvc", args.card_cvc),
                ("--card-name", args.card_name),
                ("--card-zip", args.card_zip),
            ]
            missing = [flag for flag, value in required_card if not str(value or "").strip()]
            if missing:
                print(json.dumps({"ok": False, "error": f"--auto-billing missing required fields: {', '.join(missing)}"}))
                return 2
        if args.local_user_data_dir and int(args.workers) > 1:
            print(json.dumps({"ok": False, "error": "--local-user-data-dir requires --workers 1"}))
            return 2

        worker_results = asyncio.run(
            _run_browseruse_batch(
                target_url=args.target_url,
                goals=list(args.goal),
                workers=max(1, int(args.workers)),
                max_steps=max(1, int(args.max_steps)),
                model=args.browseruse_model,
                headed=bool(args.headed),
                out_dir=run_dir,
                api_key=api_key,
                account_email=args.account_email,
                account_password=args.account_password,
                account_first_name=args.account_first_name,
                account_last_name=args.account_last_name,
                use_agentmail=bool(args.use_agentmail),
                agentmail_api_key=agentmail_api_key,
                agentmail_inbox=(str(args.agentmail_inbox).strip() if args.agentmail_inbox else None),
                agentmail_domain=str(args.agentmail_domain).strip(),
                agentmail_username_prefix=str(args.agentmail_username_prefix).strip(),
                otp_wait_seconds=max(10, int(args.otp_wait_seconds)),
                otp_poll_seconds=max(1, int(args.otp_poll_seconds)),
                otp_regex=str(args.otp_regex),
                post_otp_steps=max(1, int(args.post_otp_steps)),
                auto_billing=bool(args.auto_billing),
                card_number=(str(args.card_number).strip() if args.card_number else None),
                card_exp_month=(str(args.card_exp_month).strip() if args.card_exp_month else None),
                card_exp_year=(str(args.card_exp_year).strip() if args.card_exp_year else None),
                card_cvc=(str(args.card_cvc).strip() if args.card_cvc else None),
                card_name=(str(args.card_name).strip() if args.card_name else None),
                card_zip=(str(args.card_zip).strip() if args.card_zip else None),
                har_content=str(args.har_content),
                local_user_data_dir=(str(args.local_user_data_dir).strip() if args.local_user_data_dir else None),
                monitor=bool(args.monitor),
                monitor_interval_seconds=max(1, int(args.monitor_interval_seconds)),
            )
        )

    har_files: list[Path] = []
    for result in worker_results:
        path = Path(result.har_path)
        if path.exists():
            har_files.append(path)
    for provided in args.simulate_har:
        path = Path(provided).expanduser().resolve()
        if path.exists():
            har_files.append(path)

    if not har_files:
        diagnostics = {
            "run_timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "target_url": args.target_url,
            "error": "No HAR files available to analyze",
            "worker_results": [r.to_dict() for r in worker_results],
        }
        diagnostics_path = run_dir / "no_har_diagnostics.json"
        diagnostics_path.write_text(json.dumps(diagnostics, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "No HAR files available to analyze",
                    "run_dir": str(run_dir),
                    "worker_results": [{"worker_id": r.worker_id, "status": r.status} for r in worker_results],
                    "diagnostics_file": str(diagnostics_path),
                },
                indent=2,
            )
        )
        return 1

    target_host = _normalize_host(args.target_url) if args.target_url else ""
    inventory = _build_inventory(har_files, target_host)
    inventory_path = run_dir / "inventory.json"
    inventory_path.write_text(json.dumps(inventory, indent=2), encoding="utf-8")

    curl_results: list[dict[str, Any]] = []
    if args.run_curl:
        curl_results = _run_curl_probes(
            inventory=inventory,
            out_dir=run_dir,
            limit=max(1, int(args.curl_limit)),
            allow_state_changing=bool(args.allow_state_changing_curl),
            auth_header=args.auth_header,
            cookie_header=args.cookie_header,
            timeout_seconds=max(1, int(args.curl_timeout_seconds)),
        )

    payload = {
        "run_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "target_url": args.target_url,
        "har_files": [str(p) for p in har_files],
        "worker_results": [r.to_dict() for r in worker_results],
        "inventory_count": len(inventory),
        "inventory": inventory,
        "curl_results": curl_results,
    }
    payload_path = run_dir / "analysis_payload.json"
    payload_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    claude_summary: dict[str, Any] | None = None
    if not args.skip_claude:
        claude_summary = _run_claude_analysis(
            payload=payload,
            out_dir=run_dir,
            claude_cmd=args.claude_cmd,
            timeout_seconds=max(10, int(args.claude_timeout_seconds)),
            endpoint_limit=max(1, int(args.claude_endpoint_limit)),
            curl_limit=max(1, int(args.claude_curl_limit)),
        )

    result = {
        "ok": True,
        "run_dir": str(run_dir),
        "har_files": [str(p) for p in har_files],
        "inventory_file": str(inventory_path),
        "payload_file": str(payload_path),
        "inventory_count": len(inventory),
        "curl_probe_count": len(curl_results),
        "worker_status": [{"worker_id": r.worker_id, "status": r.status} for r in worker_results],
        "claude": claude_summary,
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
