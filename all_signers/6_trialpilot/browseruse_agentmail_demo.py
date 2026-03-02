"""Browser Use + AgentMail signup demo runner.

This module runs an authorized demo flow:
1) Create (or reuse) an AgentMail inbox
2) Start a Browser Use session
3) Run a signup task that stops at OTP verification
4) Poll AgentMail for a verification code
5) Continue in the same Browser Use session with the OTP

It is intentionally designed for hackathon demos and requires explicit user-owned
credentials and targets.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import string
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen


BROWSER_USE_BASE = "https://api.browser-use.com/api/v2"
AGENTMAIL_BASE = "https://api.agentmail.to/v0"
FINAL_STATUSES = {"finished", "stopped"}
TRACKING_HOST_TOKENS = (
    "filecdn.",
    "sendgrid",
    "pstmrk.it",
    "dm-sg.aliyuncs.com",
    "ct.sendgrid.net",
)
AUTH_HOST_TOKENS = ("stytch.com", "auth0.com", "clerk", "supabase", "firebaseapp.com")
BLOCKED_REASONS = {
    "signup_task_failed",
    "signup_validation_failed",
    "invite_only_closed_signup",
    "signup_access_blocked",
    "verification_timeout",
}


@dataclass
class DemoConfig:
    target_url: str
    password: str
    first_name: str
    last_name: str
    phone: str
    llm: str
    max_steps: int
    timeout_seconds: int
    poll_seconds: int
    otp_wait_seconds: int
    otp_regex: str
    inbox: str | None
    inbox_username: str | None
    inbox_domain: str
    session_id: str | None
    dry_run: bool
    browser_use_api_key: str | None
    agentmail_api_key: str | None
    include_api_key_step: bool
    manual_otp: str | None
    pause_for_billing: bool
    resume_only: bool
    verbose: bool
    profile_id: str | None
    proxy_country_code: str | None
    browser_screen_width: int | None
    browser_screen_height: int | None
    highlight_elements: bool
    flash_mode: bool
    thinking_mode: bool
    vision_mode: bool | str
    system_prompt_extension: str
    op_vault_id: str | None
    secrets: dict[str, str] | None
    session_keep_alive: bool | None
    session_persist_memory: bool | None
    demo_fast: bool = False
    cleanup_active_sessions: bool = False
    max_active_sessions: int = 10
    preload_file: str | None = None
    use_preloaded_on_block: bool = False
    strict_fresh_inbox: bool = False


@dataclass
class VerificationArtifact:
    kind: str  # "otp" | "magic_link"
    value: str
    message_id: str | None = None
    subject: str | None = None
    timestamp: str | None = None


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(cfg: DemoConfig, msg: str) -> None:
    if not cfg.verbose:
        return
    print(f"[trialpilot-demo] {msg}", file=sys.stderr, flush=True)


def _random_password() -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(random.choice(alphabet) for _ in range(18))


def _parse_env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _parse_optional_bool(value: str | bool | None) -> bool | None:
    if isinstance(value, bool):
        return value
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {value}")


def _parse_optional_int(value: int | str | None) -> int | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    return int(raw)


def _parse_vision(value: str | bool | None) -> bool | str:
    if isinstance(value, bool):
        return value
    raw = str(value or "").strip().lower()
    if not raw:
        return True
    if raw == "auto":
        return "auto"
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid vision value: {value}")


def _parse_secrets_json(raw: str | None) -> dict[str, str] | None:
    text = str(raw or "").strip()
    if not text:
        return None
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("secrets_json must be a JSON object")
    cleaned: dict[str, str] = {}
    for k, v in parsed.items():
        key = str(k).strip()
        if not key:
            continue
        cleaned[key] = str(v)
    return cleaned or None


def _json_request(
    *,
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    attempts = 3
    for attempt in range(1, attempts + 1):
        body: bytes | None = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        req = Request(url=url, method=method.upper(), data=body)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        req.add_header("Content-Type", "application/json")
        try:
            with urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8") or "{}"
                return json.loads(raw)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            # Retry a narrow set of transient HTTP statuses.
            if exc.code in {408, 429, 500, 502, 503, 504} and attempt < attempts:
                time.sleep(min(2**attempt, 5))
                continue
            raise RuntimeError(f"HTTP {exc.code} for {url}: {detail}") from exc
        except URLError as exc:
            if attempt < attempts:
                time.sleep(min(2**attempt, 5))
                continue
            raise RuntimeError(f"Network error for {url}: {exc}") from exc
        except OSError as exc:
            if attempt < attempts:
                time.sleep(min(2**attempt, 5))
                continue
            raise RuntimeError(f"Transport error for {url}: {exc}") from exc
    raise RuntimeError(f"Network error for {url}: exhausted retries")


def _browser_headers(api_key: str) -> dict[str, str]:
    return {"X-Browser-Use-API-Key": api_key}


def _agentmail_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _list_agentmail_inboxes(cfg: DemoConfig, *, limit: int = 20) -> list[str]:
    if not cfg.agentmail_api_key:
        return []
    data = _json_request(
        method="GET",
        url=f"{AGENTMAIL_BASE}/inboxes?limit={int(limit)}",
        headers=_agentmail_headers(cfg.agentmail_api_key),
    )
    out: list[str] = []
    for item in data.get("inboxes", []) or []:
        inbox_id = str(item.get("inbox_id", "")).strip()
        if inbox_id:
            out.append(inbox_id)
    return out


def _create_agentmail_inbox(cfg: DemoConfig) -> str:
    if cfg.dry_run:
        username = cfg.inbox_username or "demoagent"
        _log(cfg, f"dry-run inbox selected: {username}@{cfg.inbox_domain}")
        return f"{username}@{cfg.inbox_domain}"

    if not cfg.agentmail_api_key:
        raise RuntimeError("AGENTMAIL_API_KEY missing")

    # When a username is explicitly requested (or strict mode is on), try creating first.
    prefer_create = bool(str(cfg.inbox_username or "").strip()) or bool(cfg.strict_fresh_inbox)
    if not prefer_create:
        # Reuse existing inbox first to avoid quota churn.
        existing = _list_agentmail_inboxes(cfg, limit=20)
        if existing:
            picked = random.choice(existing)
            _log(cfg, f"reusing existing agentmail inbox: {picked}")
            return picked

    payload: dict[str, Any] = {"domain": cfg.inbox_domain}
    if cfg.inbox_username:
        payload["username"] = cfg.inbox_username

    try:
        data = _json_request(
            method="POST",
            url=f"{AGENTMAIL_BASE}/inboxes",
            headers=_agentmail_headers(cfg.agentmail_api_key),
            payload=payload,
        )
    except RuntimeError as exc:
        message = str(exc)
        if "LimitExceededError" in message or "already exists" in message.lower():
            if cfg.strict_fresh_inbox:
                raise RuntimeError(
                    "strict_fresh_inbox requested but AgentMail could not create a new inbox "
                    f"(reason: {message})"
                ) from exc
            existing = _list_agentmail_inboxes(cfg, limit=20)
            if existing:
                picked = random.choice(existing)
                _log(cfg, f"inbox create unavailable; reusing existing inbox: {picked}")
                return picked
        raise
    inbox_id = str(data.get("inbox_id", "")).strip()
    if not inbox_id:
        raise RuntimeError(f"AgentMail inbox creation failed: {data}")
    _log(cfg, f"agentmail inbox ready: {inbox_id}")
    return inbox_id


def _list_inbox_message_ids(cfg: DemoConfig, inbox_id: str, *, limit: int = 25) -> set[str]:
    if cfg.dry_run:
        return set()
    if not cfg.agentmail_api_key:
        raise RuntimeError("AGENTMAIL_API_KEY missing")
    list_data = _json_request(
        method="GET",
        url=f"{AGENTMAIL_BASE}/inboxes/{quote(inbox_id, safe='')}/messages?limit={int(limit)}",
        headers=_agentmail_headers(cfg.agentmail_api_key),
    )
    out: set[str] = set()
    for item in list_data.get("messages", []) or []:
        message_id = str(item.get("message_id", "")).strip()
        if message_id:
            out.add(message_id)
    return out


def _create_browser_session(cfg: DemoConfig) -> str:
    if cfg.dry_run:
        _log(cfg, "dry-run browser session selected: session_dryrun_123")
        return cfg.session_id or "session_dryrun_123"
    if cfg.session_id:
        _log(cfg, f"reusing browser session: {cfg.session_id}")
        return cfg.session_id
    if not cfg.browser_use_api_key:
        raise RuntimeError("BROWSER_USE_API_KEY missing")
    if cfg.cleanup_active_sessions:
        prune = _prune_active_sessions(cfg)
        _log(
            cfg,
            "active session cleanup: "
            f"before={prune.get('active_before')} deleted={prune.get('deleted')} after={prune.get('active_after')}",
        )

    start_url = _target_signup_url(cfg, _host_for_target(cfg.target_url))
    payload: dict[str, Any] = {"startUrl": start_url}
    if cfg.profile_id:
        payload["profileId"] = cfg.profile_id
    if cfg.proxy_country_code:
        payload["proxyCountryCode"] = cfg.proxy_country_code.lower()
    if cfg.browser_screen_width:
        payload["browserScreenWidth"] = cfg.browser_screen_width
    if cfg.browser_screen_height:
        payload["browserScreenHeight"] = cfg.browser_screen_height
    if cfg.session_keep_alive is not None:
        payload["keepAlive"] = cfg.session_keep_alive
    if cfg.session_persist_memory is not None:
        payload["persistMemory"] = cfg.session_persist_memory

    try:
        data = _json_request(
            method="POST",
            url=f"{BROWSER_USE_BASE}/sessions",
            headers=_browser_headers(cfg.browser_use_api_key),
            payload=payload,
        )
    except RuntimeError as exc:
        message = str(exc).lower()
        if "too many concurrent active sessions" in message and cfg.cleanup_active_sessions:
            _log(cfg, "session creation hit active-session limit, pruning again and retrying once")
            _prune_active_sessions(cfg)
            data = _json_request(
                method="POST",
                url=f"{BROWSER_USE_BASE}/sessions",
                headers=_browser_headers(cfg.browser_use_api_key),
                payload=payload,
            )
        else:
            raise
    session_id = str(data.get("id", "")).strip()
    if not session_id:
        raise RuntimeError(f"Browser Use session creation failed: {data}")
    _log(cfg, f"browser session created: {session_id}")
    return session_id


def _create_fresh_browser_session(cfg: DemoConfig) -> str:
    return _create_browser_session(replace(cfg, session_id=None))


def _create_task(
    cfg: DemoConfig,
    *,
    session_id: str,
    task_text: str,
    allowed_domains: list[str],
) -> str:
    if cfg.dry_run:
        suffix = abs(hash(task_text)) % 100000
        _log(cfg, f"dry-run task created: task_dryrun_{suffix}")
        return f"task_dryrun_{suffix}"

    if not cfg.browser_use_api_key:
        raise RuntimeError("BROWSER_USE_API_KEY missing")

    payload: dict[str, Any] = {
        "sessionId": session_id,
        "task": task_text,
        "llm": cfg.llm,
        "maxSteps": cfg.max_steps,
        "allowedDomains": allowed_domains,
        "metadata": {"flow": "signup-demo", "createdAt": _now_utc_iso()},
        "judge": False,
        "vision": cfg.vision_mode,
        "highlightElements": cfg.highlight_elements,
        "flashMode": cfg.flash_mode,
        "thinking": cfg.thinking_mode,
    }
    if cfg.system_prompt_extension:
        payload["systemPromptExtension"] = cfg.system_prompt_extension
    if cfg.op_vault_id:
        payload["opVaultId"] = cfg.op_vault_id
    if cfg.secrets:
        payload["secrets"] = cfg.secrets

    data = _json_request(
        method="POST",
        url=f"{BROWSER_USE_BASE}/tasks",
        headers=_browser_headers(cfg.browser_use_api_key),
        payload=payload,
    )
    task_id = str(data.get("id", "")).strip()
    if not task_id:
        raise RuntimeError(f"Browser Use task creation failed: {data}")
    _log(cfg, f"task created: {task_id}")
    return task_id


def _create_task_with_session_recovery(
    cfg: DemoConfig,
    *,
    session_id: str,
    task_text: str,
    allowed_domains: list[str],
    recovery_task_text: str | None = None,
) -> tuple[str, str, bool]:
    active_session_id = session_id
    recovered = False
    try:
        task_id = _create_task(
            cfg,
            session_id=active_session_id,
            task_text=task_text,
            allowed_domains=allowed_domains,
        )
    except RuntimeError as exc:
        if "browser session is stopped" not in str(exc).lower():
            raise
        recovered = True
        _log(cfg, "session stopped; creating a fresh session for follow-up task")
        active_session_id = _create_fresh_browser_session(cfg)
        task_id = _create_task(
            cfg,
            session_id=active_session_id,
            task_text=recovery_task_text or task_text,
            allowed_domains=allowed_domains,
        )
    return task_id, active_session_id, recovered


def _get_task(cfg: DemoConfig, task_id: str) -> dict[str, Any]:
    if cfg.dry_run:
        return {
            "id": task_id,
            "status": "finished",
            "output": "DRY RUN: simulated Browser Use output.",
            "isSuccess": True,
        }

    if not cfg.browser_use_api_key:
        raise RuntimeError("BROWSER_USE_API_KEY missing")

    return _json_request(
        method="GET",
        url=f"{BROWSER_USE_BASE}/tasks/{quote(task_id, safe='')}",
        headers=_browser_headers(cfg.browser_use_api_key),
    )


def _wait_for_task(cfg: DemoConfig, task_id: str, *, wait_seconds: int) -> dict[str, Any]:
    start = time.time()
    last_status = ""
    while True:
        data = _get_task(cfg, task_id)
        status = str(data.get("status", "")).lower()
        if status and status != last_status:
            _log(cfg, f"task {task_id} status: {status}")
            last_status = status
        if status in FINAL_STATUSES:
            return data
        if time.time() - start > wait_seconds:
            raise TimeoutError(f"Task timed out after {wait_seconds}s: {task_id}")
        time.sleep(cfg.poll_seconds)


def _task_result_has_stopped_session(result: dict[str, Any]) -> bool:
    status = str((result or {}).get("status", "") or "").strip().lower()
    output = str((result or {}).get("output", "") or "").strip().lower()
    error = str((result or {}).get("error", "") or "").strip().lower()
    detail = str((result or {}).get("detail", "") or "").strip().lower()
    joined = " ".join(part for part in (status, output, error, detail) if part)
    return (
        "browser session is stopped" in joined
        or "session is stopped" in joined
        or (status == "stopped" and "session" in joined)
    )


def _run_task_with_wait_and_recovery(
    cfg: DemoConfig,
    *,
    session_id: str,
    task_text: str,
    allowed_domains: list[str],
    recovery_task_text: str | None = None,
    wait_seconds: int | None = None,
) -> tuple[str, str, bool, dict[str, Any], list[str]]:
    runtime_retries = max(0, int(os.getenv("TRIALPILOT_SESSION_RUNTIME_RECOVERY_RETRIES", "2")))
    max_attempts = 1 + runtime_retries
    active_session_id = session_id
    recovered = False
    task_ids: list[str] = []
    last_task_id = ""
    last_result: dict[str, Any] = {}

    for attempt in range(1, max_attempts + 1):
        task_id, active_session_id, create_recovered = _create_task_with_session_recovery(
            cfg,
            session_id=active_session_id,
            task_text=task_text,
            allowed_domains=allowed_domains,
            recovery_task_text=recovery_task_text,
        )
        recovered = recovered or create_recovered
        task_ids.append(task_id)
        last_task_id = task_id
        last_result = _wait_for_task(cfg, task_id, wait_seconds=wait_seconds or cfg.timeout_seconds)
        if not _task_result_has_stopped_session(last_result):
            return task_id, active_session_id, recovered, last_result, task_ids
        if attempt >= max_attempts:
            break
        recovered = True
        _log(cfg, "task ended with stopped-session signal; creating a fresh session and retrying")
        active_session_id = _create_fresh_browser_session(cfg)

    return last_task_id, active_session_id, recovered, last_result, task_ids


def _extract_first_otp(text: str, otp_regex: str) -> str | None:
    # Prefer explicit "Verification Code" / OTP-context matches first.
    contextual_patterns = [
        r"verification\s*code[^0-9]{0,24}([0-9]{6})",
        r"one[-\s]*time\s*code[^0-9]{0,24}([0-9]{6})",
        r"\botp\b[^0-9]{0,24}([0-9]{6})",
        r">\s*([0-9]{6})\s*<",
    ]
    for raw in contextual_patterns:
        match = re.search(raw, text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1)

    # Fallback to caller-supplied regex.
    pattern = re.compile(otp_regex)
    match = pattern.search(text)
    if match:
        return match.group(1) if match.groups() else match.group(0)

    # Final fallback: score all 6-digit candidates by nearby OTP keywords.
    candidates = list(re.finditer(r"\b([0-9]{6})\b", text))
    if not candidates:
        return None
    best_code: str | None = None
    best_score = -1
    for found in candidates:
        code = found.group(1)
        start, end = found.span(1)
        window = text[max(0, start - 60) : min(len(text), end + 60)].lower()
        score = 0
        if "verification" in window:
            score += 3
        if "code" in window:
            score += 2
        if "otp" in window:
            score += 2
        if "expire" in window:
            score += 1
        if score > best_score:
            best_score = score
            best_code = code
    return best_code or candidates[-1].group(1)


def _extract_links(text: str) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    # Prefer explicit href links first, then fallback to raw URL detection.
    patterns = [
        r"href\s*=\s*[\"'](https?://[^\"']+)[\"']",
        r"https?://[^\s\"'<>]+",
    ]
    for pattern in patterns:
        for raw in re.findall(pattern, text, flags=re.IGNORECASE):
            norm = _normalize_link(raw)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            out.append(norm)
    return out


def _normalize_link(raw_link: str) -> str:
    link = html.unescape(str(raw_link or "").strip())
    return link.rstrip(").,;")


def _parse_iso_ts(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _host_matches_target(host: str, target_host: str | None) -> bool:
    if not host or not target_host:
        return False
    host = host.strip().lower()
    target_host = target_host.strip().lower()
    return host == target_host or host.endswith(f".{target_host}") or target_host.endswith(f".{host}")


def _target_keyword(target_host: str | None) -> str:
    host = (target_host or "").strip().lower()
    if not host:
        return ""
    parts = [p for p in host.split(".") if p and p != "www"]
    if len(parts) >= 2:
        return parts[-2]
    return parts[0] if parts else ""


def _compact_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _looks_like_password_reset_message(subject: str, blob: str) -> bool:
    text = f"{subject}\n{blob}".lower()
    return any(
        token in text
        for token in (
            "reset password",
            "password reset",
            "forgot password",
            "recover your account",
            "reset your password",
        )
    )


def _sender_domain(sender: str) -> str:
    text = str(sender or "").strip().lower()
    match = re.search(r"<[^@<>\s]+@([a-z0-9.-]+)>", text)
    if match:
        return match.group(1).strip(".")
    match = re.search(r"[^@<>\s]+@([a-z0-9.-]+)", text)
    if match:
        return match.group(1).strip(".")
    return ""


def _unwrap_embedded_link(link: str) -> str:
    parsed = urlparse(link)
    query = parse_qs(parsed.query)
    for key in ("url", "u", "redirect", "target", "destination", "dest", "to", "next", "continue"):
        for value in query.get(key, []) or []:
            decoded = html.unescape(unquote(str(value or "").strip()))
            lower = decoded.lower()
            if lower.startswith("http://") or lower.startswith("https://"):
                return _normalize_link(decoded)
    return link


def _is_non_verification_link(link: str) -> bool:
    lower_link = link.lower()
    parsed = urlparse(lower_link)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if host in {"www.w3.org", "w3.org"}:
        return True
    if any(token in host for token in TRACKING_HOST_TOKENS):
        return True
    if host in {"img.clerk.com", "images.clerk.dev", "images.clerk.com"}:
        return True
    if path.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
        return True
    if "x-oss-process=image" in lower_link or "image/format" in lower_link:
        return True
    if "unsubscribe" in lower_link:
        return True
    if "/trace/" in path or path.startswith("/wf/open"):
        return True
    return False


def _looks_like_magic_link(link: str, blob: str, *, target_host: str | None = None) -> bool:
    lower_link = link.lower()
    lower_blob = blob.lower()
    parsed = urlparse(lower_link)
    host = (parsed.hostname or "").lower()
    if _is_non_verification_link(link):
        return False
    if "stytch.com/v1/magic_links/redirect" in lower_link:
        return True
    if any(token in host for token in AUTH_HOST_TOKENS):
        return True
    if any(token in lower_link for token in ("magic", "verify", "verification", "confirm", "token=", "stytch_token_type")):
        return True
    # Only allow content-based heuristics for target-domain links.
    if _host_matches_target(host, target_host):
        return any(token in lower_blob for token in ("check your email", "login request", "verify", "verification", "confirm"))
    return False


def _score_magic_link(link: str, blob: str, *, target_host: str | None = None) -> int:
    lower_link = link.lower()
    lower_blob = blob.lower()
    host = (urlparse(lower_link).hostname or "").lower()
    score = 0
    if _host_matches_target(host, target_host):
        score += 6
    if any(token in host for token in AUTH_HOST_TOKENS):
        score += 4
    if any(token in lower_link for token in ("magic", "verify", "verification", "confirm", "token=", "stytch_token_type")):
        score += 4
    if "login request" in lower_blob or "check your email" in lower_blob:
        score += 2
    if "password reset" in lower_blob:
        score -= 6
    if "unsubscribe" in lower_link:
        score -= 6
    return score


def _fetch_verification_from_agentmail(
    cfg: DemoConfig,
    inbox_id: str,
    *,
    target_host: str | None = None,
    not_before: datetime | None = None,
    ignore_message_ids: set[str] | None = None,
) -> VerificationArtifact | None:
    if cfg.dry_run:
        return VerificationArtifact(kind="otp", value="000000")
    if not cfg.agentmail_api_key:
        raise RuntimeError("AGENTMAIL_API_KEY missing")

    message_scan_limit = max(3, int(os.getenv("TRIALPILOT_VERIFICATION_MESSAGE_SCAN_LIMIT", "6")))
    list_data = _json_request(
        method="GET",
        url=f"{AGENTMAIL_BASE}/inboxes/{quote(inbox_id, safe='')}/messages?limit={message_scan_limit}",
        headers=_agentmail_headers(cfg.agentmail_api_key),
    )
    messages = list_data.get("messages", []) or []
    for item in messages:
        message_id = str(item.get("message_id", "")).strip()
        if not message_id:
            continue
        if ignore_message_ids and message_id in ignore_message_ids:
            continue
        detail = _json_request(
            method="GET",
            url=(
                f"{AGENTMAIL_BASE}/inboxes/{quote(inbox_id, safe='')}"
                f"/messages/{quote(message_id, safe='')}"
            ),
            headers=_agentmail_headers(cfg.agentmail_api_key),
        )
        subject = str(detail.get("subject", "") or "")
        sender = str(detail.get("from", "") or "")
        timestamp_raw = str(detail.get("timestamp", "") or "")
        if not_before is not None:
            message_ts = _parse_iso_ts(timestamp_raw)
            if message_ts is not None and message_ts < not_before:
                continue
        preview = str(detail.get("preview", "") or "")
        text_body = str(detail.get("text", "") or "")
        html_body = str(detail.get("html", "") or "")
        text_blob = "\n".join(
            [
                subject,
                sender,
                preview,
                text_body,
            ]
        )
        if _looks_like_password_reset_message(subject, text_blob):
            continue
        blob = "\n".join(
            [
                text_blob,
                html_body,
            ]
        )
        otp = _extract_first_otp(text_blob, cfg.otp_regex)
        subject_low = subject.lower()
        target_kw = _target_keyword(target_host)
        target_compact = _compact_text(target_kw)
        sender_domain = _sender_domain(sender)
        is_target_relevant = (
            (not target_compact)
            or (target_compact in _compact_text(text_blob))
            or _host_matches_target(sender_domain, target_host)
        )
        links = _extract_links(blob)
        chosen_magic: str | None = None
        chosen_score = -999
        for link in links:
            norm_link = _unwrap_embedded_link(_normalize_link(link))
            if not norm_link:
                continue
            if _is_non_verification_link(norm_link):
                continue
            if not _looks_like_magic_link(norm_link, blob, target_host=target_host):
                continue
            score = _score_magic_link(norm_link, blob, target_host=target_host)
            host = (urlparse(norm_link).hostname or "").strip().lower()
            if target_host and _host_matches_target(host, target_host):
                score += 3
            if "stytch.com" in host:
                score += 2
            if score > chosen_score:
                chosen_score = score
                chosen_magic = norm_link
        if chosen_magic and chosen_score >= 2:
            return VerificationArtifact(
                kind="magic_link",
                value=chosen_magic,
                message_id=message_id,
                subject=subject,
                timestamp=timestamp_raw,
            )
        if is_target_relevant and otp and any(
            token in subject_low
            for token in (
                "verification code",
                "otp code",
                "is your verification code",
                "email code",
            )
        ):
            return VerificationArtifact(
                kind="otp",
                value=otp,
                message_id=message_id,
                subject=subject,
                timestamp=timestamp_raw,
            )
        # Use OTP only when message content looks target-relevant to avoid cross-provider stale code pickup.
        if is_target_relevant and otp:
            return VerificationArtifact(
                kind="otp",
                value=otp,
                message_id=message_id,
                subject=subject,
                timestamp=timestamp_raw,
            )
    return None


def _wait_for_verification(
    cfg: DemoConfig,
    inbox_id: str,
    *,
    target_host: str | None = None,
    not_before: datetime | None = None,
    ignore_message_ids: set[str] | None = None,
) -> VerificationArtifact:
    if cfg.manual_otp:
        _log(cfg, "using manual OTP override")
        return VerificationArtifact(kind="otp", value=cfg.manual_otp)
    start = time.time()
    _log(cfg, f"waiting for verification artifact in inbox: {inbox_id}")
    while True:
        try:
            artifact = _fetch_verification_from_agentmail(
                cfg,
                inbox_id,
                target_host=target_host,
                not_before=not_before,
                ignore_message_ids=ignore_message_ids,
            )
        except RuntimeError as exc:
            text = str(exc).lower()
            if "inbox not found" in text or "notfounderror" in text:
                raise TimeoutError(f"Inbox unavailable during verification polling: {inbox_id}") from exc
            raise
        if artifact:
            _log(cfg, f"verification received: {artifact.kind}")
            return artifact
        if time.time() - start > cfg.otp_wait_seconds:
            raise TimeoutError(f"No verification artifact found within {cfg.otp_wait_seconds}s for inbox={inbox_id}")
        time.sleep(cfg.poll_seconds)


def _host_for_target(url: str) -> str:
    host = (urlparse(url).hostname or "").strip().lower()
    if not host:
        raise ValueError(f"Invalid target URL: {url}")
    return host


def _domain_family(host: str) -> set[str]:
    norm = (host or "").strip().lower()
    if not norm:
        return set()
    out: set[str] = set()
    parts = [p for p in norm.split(".") if p]
    for idx in range(len(parts)):
        suffix = ".".join(parts[idx:])
        if suffix.count(".") < 1:
            continue
        out.add(suffix)
        if not suffix.startswith("www."):
            out.add(f"www.{suffix}")
    return out


def _extra_allowed_domains() -> set[str]:
    raw = os.getenv("TRIALPILOT_EXTRA_ALLOWED_DOMAINS", "")
    if not raw.strip():
        return set()
    out: set[str] = set()
    for token in re.split(r"[,\\s]+", raw.strip()):
        norm = token.strip().lower()
        if not norm:
            continue
        out.update(_domain_family(norm))
    return out


def _target_signup_url(cfg: DemoConfig, target_host: str) -> str:
    """Use direct signup routes for known targets to reduce explorer drift."""
    if target_host.endswith("apollo.io"):
        return "https://www.apollo.io/sign-up"
    return cfg.target_url


def _target_allowed_domain_overrides(target_host: str) -> set[str]:
    """Add known auth/provider domains for common targets."""
    out: set[str] = set()
    if target_host.endswith("apollo.io"):
        out.update(_domain_family("app.apollo.io"))
        out.update(_domain_family("tryapollo.io"))
    return out


def _load_preloaded_result(path: str | None) -> dict[str, Any] | None:
    raw = str(path or "").strip()
    if not raw:
        return None
    file_path = Path(raw)
    if not file_path.exists():
        return None
    try:
        data = json.loads(file_path.read_text())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _list_browser_sessions(cfg: DemoConfig, page_size: int = 100, page_number: int = 1) -> list[dict[str, Any]]:
    if cfg.dry_run or not cfg.browser_use_api_key:
        return []
    data = _json_request(
        method="GET",
        url=f"{BROWSER_USE_BASE}/sessions?pageSize={max(1, min(page_size, 100))}&pageNumber={max(1, page_number)}",
        headers=_browser_headers(cfg.browser_use_api_key),
    )
    items = data.get("items")
    if isinstance(items, list):
        return [i for i in items if isinstance(i, dict)]
    if isinstance(data.get("sessions"), list):
        return [i for i in data["sessions"] if isinstance(i, dict)]
    if isinstance(data, list):
        return [i for i in data if isinstance(i, dict)]
    return []


def _delete_browser_session(cfg: DemoConfig, session_id: str) -> None:
    if cfg.dry_run or not cfg.browser_use_api_key:
        return
    _json_request(
        method="DELETE",
        url=f"{BROWSER_USE_BASE}/sessions/{quote(str(session_id).strip(), safe='')}",
        headers=_browser_headers(cfg.browser_use_api_key),
    )


def _prune_active_sessions(cfg: DemoConfig) -> dict[str, Any]:
    """Keep active Browser Use sessions under max_active_sessions to avoid 429."""
    if cfg.dry_run or not cfg.browser_use_api_key:
        return {"active_before": 0, "deleted": 0, "active_after": 0}

    max_active = max(0, int(cfg.max_active_sessions))
    active: list[tuple[str, str]] = []  # (session_id, started_at)
    for page in range(1, 4):
        items = _list_browser_sessions(cfg, page_size=100, page_number=page)
        if not items:
            break
        for item in items:
            status = str(item.get("status", "")).strip().lower()
            session_id = str(item.get("id", "")).strip()
            if status == "active" and session_id:
                started_at = str(item.get("startedAt", "") or "")
                active.append((session_id, started_at))

    # Delete oldest first.
    active_sorted = sorted(active, key=lambda x: x[1] or "")
    to_delete_count = max(0, len(active_sorted) - max_active)
    deleted = 0
    for session_id, _ in active_sorted[:to_delete_count]:
        try:
            _delete_browser_session(cfg, session_id)
            deleted += 1
        except Exception as exc:
            _log(cfg, f"failed to delete active session {session_id}: {type(exc).__name__}: {exc}")

    # Recount quickly on first page.
    active_after = 0
    for item in _list_browser_sessions(cfg, page_size=100, page_number=1):
        if str(item.get("status", "")).strip().lower() == "active":
            active_after += 1

    return {
        "active_before": len(active_sorted),
        "deleted": deleted,
        "active_after": active_after,
        "max_active_sessions": max_active,
    }


def _build_signup_task(cfg: DemoConfig, email: str) -> str:
    target_host = _host_for_target(cfg.target_url)
    start_url = _target_signup_url(cfg, target_host)
    lines = [
        f"Go to {start_url} and create a NEW account.",
        f"Use email: {email}",
        f"Use password: {cfg.password}",
        f"Use first name: {cfg.first_name}",
        f"Use last name: {cfg.last_name}",
    ]
    if str(cfg.phone or "").strip():
        lines.append(f"Use phone number: {cfg.phone}")
    lines.extend(
        [
            "If registration fails, inspect field-level errors and retry once with corrected input formats.",
            "If email verification is required, stop when the page says to check email OR when an OTP field is visible "
            "and report that you are waiting for verification.",
            "Speed policy: do not use search/write_file tools, and do not visit unrelated sites (Google/Wikipedia/docs).",
            "If page stays about:blank, empty, or target-detached for two consecutive attempts, hard reload once and retry the same signup URL once before reporting blocker.",
            "Do not attempt to bypass any security, KYC, or anti-bot checks.",
        ]
    )
    if target_host.endswith("apollo.io"):
        lines.extend(
            [
                "Apollo-specific guidance: prefer https://www.apollo.io/sign-up first.",
                "Do not switch to app.apollo.io login/settings until signup form submit succeeds.",
            ]
        )
    return "\n".join(lines)


def _build_signup_email(base_inbox_email: str, *, target_host: str | None = None) -> str:
    raw = str(base_inbox_email or "").strip().lower()
    if not raw or "@" not in raw:
        return raw
    local, domain = raw.split("@", 1)
    local = local.strip()
    domain = domain.strip()
    if not local or not domain:
        return raw
    # Some providers normalize plus-addressing (local+tag@domain -> local@domain).
    # For those hosts, using +aliases causes false "already registered" collisions.
    target = (target_host or "").strip().lower()
    if "apollo.io" in target:
        return f"{local}@{domain}"
    # Always randomize signup emails to avoid "already exists" retries on reused inboxes.
    alias_suffix = f"tp{int(time.time() * 1000) % 1_000_000_000:09d}{random.randint(100, 999)}"
    if "+" in local:
        local = local.split("+", 1)[0]
    return f"{local}+{alias_suffix}@{domain}"


def _build_continue_task(artifact: VerificationArtifact, include_api_key_step: bool) -> str:
    tail = ""
    if include_api_key_step:
        tail = (
            "\nAfter verification succeeds, navigate to account settings and create an API key. "
            "Return the page URL and key creation result."
        )
    if artifact.kind == "magic_link":
        return (
            f"Open this email verification or magic-login link exactly: {artifact.value}\n"
            "Complete verification/login and finish onboarding to the first logged-in page.\n"
            "Speed policy: avoid exploratory loops, do not use search/write_file, and do not revisit the same page section more than once."
            f"{tail}"
        )
    return (
        f"Continue from the current page. Enter verification code: {artifact.value}. "
        "Submit the form and complete onboarding to the first logged-in page. "
        "Speed policy: avoid exploratory loops, do not use search/write_file, and do not revisit the same page section more than once."
        f"{tail}"
    )


def _build_recovery_continue_task(
    cfg: DemoConfig,
    email: str,
    artifact: VerificationArtifact,
    include_api_key_step: bool,
) -> str:
    if artifact.kind == "magic_link":
        return _build_continue_task(artifact, include_api_key_step)
    tail = ""
    if include_api_key_step:
        tail = (
            "\nAfter verification succeeds, navigate to account settings and create an API key. "
            "Return the page URL and key creation result."
        )
    return (
        f"Go to {cfg.target_url} and log in to the existing account.\n"
        f"Use email: {email}\n"
        f"Use password: {cfg.password}\n"
        f"If prompted for verification code, enter: {artifact.value}\n"
        "Complete onboarding to the first logged-in page."
        f"{tail}"
    )


def _build_post_verification_billing_task(artifact: VerificationArtifact) -> str:
    has_card_secrets = bool(
        os.getenv("TRIALPILOT_CARD_NUMBER", "").strip()
        and os.getenv("TRIALPILOT_CARD_CVC", "").strip()
    )
    billing_tail = (
        "If payment form appears, use provided task secrets CARD_NUMBER, CARD_EXP_MONTH, CARD_EXP_YEAR, "
        "CARD_CVC, CARDHOLDER_NAME, CARD_ZIP and report trial activation result."
        if has_card_secrets
        else "then stop and report that manual billing input is required."
    )
    if artifact.kind == "magic_link":
        return (
            f"Open this email verification or magic-login link exactly: {artifact.value}\n"
            "Complete verification/login. Navigate until you reach the billing/payment step, "
            f"{billing_tail}"
        )
    return (
        f"Continue from the current page. Enter verification code: {artifact.value}. "
        "Submit verification. Navigate until you reach the billing/payment step, "
        f"{billing_tail}"
    )


def _build_request_verification_code_task(cfg: DemoConfig, email: str) -> str:
    return (
        f"Stay on {cfg.target_url} flow and ensure account verification code is issued for {email}.\n"
        "If you are on a verification step, click 'Resend code' / 'Send code' exactly once.\n"
        "If you are on signup form, submit the form again once with the same details.\n"
        "Stop once the UI confirms a verification code was sent, or once an OTP input is visible."
    )


def _build_resend_verification_task(email: str) -> str:
    return (
        "You are on an email verification step. "
        "If a 'Resend code', 'Send again', or equivalent action exists, click it once. "
        f"Ensure verification is being sent to {email}. "
        "Stay on verification page and report whether resend was triggered."
    )


def _verification_needs_retry(output_text: str) -> bool:
    text = (output_text or "").lower()
    if "verification" not in text and "code" not in text:
        return False
    retry_signals = (
        "rejected",
        "invalid code",
        "incorrect code",
        "resend",
        "provide the new verification code",
        "provide the latest verification code",
        "try again",
    )
    return any(token in text for token in retry_signals)


def _signup_is_blocked(output_text: str) -> bool:
    text = (output_text or "").lower()
    block_signals = (
        "anti-bot",
        "captcha",
        "security block",
        "about:blank",
        "blocked",
        "failed to render",
        "access denied",
    )
    positive_progress = (
        "check your email",
        "verification",
        "otp",
        "code sent",
    )
    return any(token in text for token in block_signals) and not any(token in text for token in positive_progress)


def _signup_is_validation_failure(output_text: str) -> bool:
    text = (output_text or "").lower()
    failure_signals = (
        "registration failed",
        "signup failed",
        "failed to register",
        "invalid phone",
        "invalid password",
        "email already exists",
        "already registered",
        "please try again",
    )
    positive_progress = (
        "check your email",
        "verification",
        "otp",
        "code sent",
    )
    return any(token in text for token in failure_signals) and not any(token in text for token in positive_progress)


def _signup_is_invite_only(output_text: str) -> bool:
    text = (output_text or "").lower()
    invite_signals = (
        "invite list",
        "waitlist",
        "no sign-up",
        "no signup",
        "no registration form",
        "no account creation",
        "does not have a public user account creation",
        "static corporate landing page",
        "no publicly available registration form",
        "contact the team",
        "contact sales",
        "schedule a demo",
        "hello@",
    )
    positive_progress = (
        "check your email",
        "verification",
        "otp",
        "code sent",
    )
    return any(token in text for token in invite_signals) and not any(token in text for token in positive_progress)


def _signup_requires_verification(output_text: str) -> bool:
    text = (output_text or "").lower()
    verification_signals = (
        "check your email",
        "verification",
        "otp",
        "code sent",
        "magic link",
        "confirm your email",
        "one-time code",
    )
    if any(token in text for token in verification_signals):
        return True
    success_without_verification_signals = (
        "reached the dashboard",
        "logged in",
        "account created and reached",
        "successfully created a new account",
        "onboarding complete",
        "account created successfully",
    )
    if any(token in text for token in success_without_verification_signals):
        return False
    # Conservative default for unknown site output.
    return True


def _signup_task_failed_without_progress(task_result: dict[str, Any], output_text: str) -> bool:
    status = str((task_result or {}).get("status", "")).strip().lower()
    is_success = (task_result or {}).get("isSuccess")
    text = str(output_text or "").lower()
    if is_success is True:
        return False
    if status not in {"finished", "stopped"}:
        return False
    progress_signals = (
        "check your email",
        "verification",
        "otp",
        "code sent",
        "magic link",
        "dashboard",
        "account created",
        "logged in",
    )
    return not any(token in text for token in progress_signals)


def _summarize_preloaded_result(data: dict[str, Any]) -> dict[str, Any]:
    summary_keys = (
        "ok",
        "mode",
        "target_url",
        "session_id",
        "continue_session_id",
        "signup_task_id",
        "continue_task_id",
        "signup_status",
        "continue_status",
        "verification_kind",
        "timestamp",
    )
    summary: dict[str, Any] = {}
    for key in summary_keys:
        if key in data:
            summary[key] = data.get(key)
    if not summary:
        summary = {"keys": sorted(data.keys())[:30]}
    return summary


def _blocked_result(cfg: DemoConfig, blocked_reason: str, payload: dict[str, Any]) -> dict[str, Any]:
    blocked_payload = dict(payload)
    blocked_payload["ok"] = False
    blocked_payload["blocked_reason"] = blocked_reason
    blocked_payload.setdefault("target_url", cfg.target_url)
    blocked_payload.setdefault("timestamp", _now_utc_iso())

    if not cfg.use_preloaded_on_block or blocked_reason not in BLOCKED_REASONS:
        return blocked_payload

    preloaded = _load_preloaded_result(cfg.preload_file)
    if not preloaded:
        return blocked_payload

    return {
        "ok": True,
        "mode": "preloaded_fallback",
        "target_url": cfg.target_url,
        "blocked_reason": blocked_reason,
        "preload_file": str(cfg.preload_file or ""),
        "live_result": blocked_payload,
        "preloaded_result": _summarize_preloaded_result(preloaded),
        "timestamp": _now_utc_iso(),
    }


def _validate_config(cfg: DemoConfig) -> list[str]:
    missing: list[str] = []
    if cfg.dry_run:
        return missing
    if not (cfg.browser_use_api_key or "").strip():
        missing.append("BROWSER_USE_API_KEY")
    if not cfg.resume_only and not (cfg.agentmail_api_key or "").strip():
        missing.append("AGENTMAIL_API_KEY")
    return missing


def _run_resume_only(cfg: DemoConfig) -> dict[str, Any]:
    if not cfg.session_id:
        raise ValueError("--resume-only requires --session-id")
    target_host = _host_for_target(cfg.target_url)
    allowed_set = _domain_family(target_host)
    allowed_set.update(_extra_allowed_domains())
    allowed_set.update(_target_allowed_domain_overrides(target_host))
    manual_value = (cfg.manual_otp or "").strip()
    if manual_value.lower().startswith("http://") or manual_value.lower().startswith("https://"):
        artifact = VerificationArtifact(kind="magic_link", value=_normalize_link(manual_value))
        magic_host = (urlparse(artifact.value).hostname or "").strip().lower()
        if magic_host:
            allowed_set.update(_domain_family(magic_host))
    else:
        artifact = VerificationArtifact(kind="otp", value=manual_value or "already-verified")
    allowed_domains = sorted(d for d in allowed_set if d)
    continue_task_id, continue_session_id, continue_session_recovered, continue_result, continue_task_ids = _run_task_with_wait_and_recovery(
        cfg,
        session_id=cfg.session_id,
        task_text=_build_continue_task(artifact, cfg.include_api_key_step),
        allowed_domains=allowed_domains,
        recovery_task_text=_build_continue_task(artifact, cfg.include_api_key_step),
        wait_seconds=cfg.timeout_seconds,
    )
    return {
        "ok": True,
        "mode": "resume_only",
        "dry_run": cfg.dry_run,
        "target_url": cfg.target_url,
        "session_id": cfg.session_id,
        "continue_session_id": continue_session_id,
        "continue_session_recovered": continue_session_recovered,
        "continue_task_id": continue_task_id,
        "continue_task_ids": continue_task_ids,
        "continue_status": continue_result.get("status"),
        "continue_output": continue_result.get("output"),
        "timestamp": _now_utc_iso(),
    }


def run_demo(cfg: DemoConfig) -> dict[str, Any]:
    if cfg.resume_only:
        return _run_resume_only(cfg)

    target_host = _host_for_target(cfg.target_url)
    allowed_set = _domain_family(target_host)
    allowed_set.update(_extra_allowed_domains())
    allowed_set.update(_target_allowed_domain_overrides(target_host))
    allowed_domains = sorted(d for d in allowed_set if d)

    inbox_id = cfg.inbox or _create_agentmail_inbox(cfg)
    signup_email = _build_signup_email(inbox_id, target_host=target_host)
    inbox_history_limit = max(3, int(os.getenv("TRIALPILOT_INBOX_HISTORY_LIMIT", "10")))
    ignored_message_ids = _list_inbox_message_ids(cfg, inbox_id, limit=inbox_history_limit)
    if ignored_message_ids:
        _log(cfg, f"ignoring {len(ignored_message_ids)} existing inbox message(s) before signup")
    session_id = _create_browser_session(cfg)
    _log(cfg, f"starting signup task for target: {cfg.target_url}")
    verification_not_before = datetime.now(timezone.utc)

    signup_task_text = _build_signup_task(cfg, signup_email)
    signup_task_id, session_id, signup_session_recovered, signup_result, signup_task_ids = _run_task_with_wait_and_recovery(
        cfg,
        session_id=session_id,
        task_text=signup_task_text,
        allowed_domains=allowed_domains,
        recovery_task_text=signup_task_text,
        wait_seconds=cfg.timeout_seconds,
    )
    signup_output = str(signup_result.get("output", "") or "")
    if _signup_task_failed_without_progress(signup_result, signup_output):
        return _blocked_result(cfg, "signup_task_failed", {
            "inbox_id": inbox_id,
            "signup_email": signup_email,
            "session_id": session_id,
            "signup_session_recovered": signup_session_recovered,
            "signup_task_id": signup_task_id,
            "signup_task_ids": signup_task_ids,
            "signup_status": signup_result.get("status"),
            "signup_output": signup_output,
        })
    if _signup_is_validation_failure(signup_output):
        return _blocked_result(cfg, "signup_validation_failed", {
            "inbox_id": inbox_id,
            "signup_email": signup_email,
            "session_id": session_id,
            "signup_session_recovered": signup_session_recovered,
            "signup_task_id": signup_task_id,
            "signup_task_ids": signup_task_ids,
            "signup_status": signup_result.get("status"),
            "signup_output": signup_output,
        })
    if _signup_is_invite_only(signup_output):
        return _blocked_result(cfg, "invite_only_closed_signup", {
            "inbox_id": inbox_id,
            "signup_email": signup_email,
            "session_id": session_id,
            "signup_session_recovered": signup_session_recovered,
            "signup_task_id": signup_task_id,
            "signup_task_ids": signup_task_ids,
            "signup_status": signup_result.get("status"),
            "signup_output": signup_output,
        })
    if _signup_is_blocked(signup_output):
        return _blocked_result(cfg, "signup_access_blocked", {
            "inbox_id": inbox_id,
            "signup_email": signup_email,
            "session_id": session_id,
            "signup_session_recovered": signup_session_recovered,
            "signup_task_id": signup_task_id,
            "signup_task_ids": signup_task_ids,
            "signup_status": signup_result.get("status"),
            "signup_output": signup_output,
        })

    verification_resend_attempts: list[dict[str, Any]] = []
    verification = None
    timeout_error: TimeoutError | None = None
    if _signup_requires_verification(signup_output):
        max_resend_attempts = max(0, int(os.getenv("TRIALPILOT_VERIFICATION_RESEND_RETRIES", "2")))
        for resend_attempt in range(max_resend_attempts + 1):
            try:
                verification = _wait_for_verification(
                    cfg,
                    inbox_id,
                    target_host=target_host,
                    not_before=verification_not_before,
                    ignore_message_ids=ignored_message_ids,
                )
                if verification.message_id:
                    ignored_message_ids.add(verification.message_id)
                timeout_error = None
                break
            except TimeoutError as exc:
                timeout_error = exc
                if resend_attempt >= max_resend_attempts:
                    break
                resend_task_id, resend_session_id, resend_session_recovered, resend_result, resend_task_ids = _run_task_with_wait_and_recovery(
                    cfg,
                    session_id=session_id,
                    task_text=_build_resend_verification_task(inbox_id),
                    allowed_domains=allowed_domains,
                    recovery_task_text=_build_resend_verification_task(inbox_id),
                    wait_seconds=cfg.timeout_seconds,
                )
                verification_resend_attempts.append(
                    {
                        "attempt": resend_attempt + 1,
                        "resend_task_id": resend_task_id,
                        "resend_task_ids": resend_task_ids,
                        "resend_session_id": resend_session_id,
                        "resend_session_recovered": resend_session_recovered,
                        "resend_status": resend_result.get("status"),
                        "resend_output": resend_result.get("output"),
                    }
                )
                session_id = resend_session_id
                verification_not_before = datetime.now(timezone.utc)
    else:
        _log(cfg, "signup output indicates verified session; skipping inbox verification wait")
        verification = VerificationArtifact(kind="otp", value="already-verified")

    if verification is None:
        return _blocked_result(cfg, "verification_timeout", {
            "inbox_id": inbox_id,
            "signup_email": signup_email,
            "session_id": session_id,
            "signup_session_recovered": signup_session_recovered,
            "signup_task_id": signup_task_id,
            "signup_task_ids": signup_task_ids,
            "signup_status": signup_result.get("status"),
            "signup_output": signup_output,
            "verification_timeout_error": str(timeout_error) if timeout_error else "verification_timeout",
            "verification_resend_attempts": verification_resend_attempts,
        })

    if verification.kind == "magic_link":
        magic_host = (urlparse(verification.value).hostname or "").strip().lower()
        if magic_host:
            allowed_set.update(_domain_family(magic_host))
    allowed_domains = sorted(d for d in allowed_set if d)

    if cfg.pause_for_billing:
        _log(cfg, "pause-for-billing mode enabled; running post-otp task")
        post_otp_task_id, post_otp_session_id, post_otp_session_recovered, post_otp_result, post_otp_task_ids = _run_task_with_wait_and_recovery(
            cfg,
            session_id=session_id,
            task_text=_build_post_verification_billing_task(verification),
            allowed_domains=allowed_domains,
            recovery_task_text=_build_recovery_continue_task(cfg, inbox_id, verification, False),
            wait_seconds=cfg.timeout_seconds,
        )
        return {
            "ok": True,
            "mode": "pause_for_billing",
            "dry_run": cfg.dry_run,
            "target_url": cfg.target_url,
            "inbox_id": inbox_id,
            "signup_email": signup_email,
            "session_id": session_id,
            "post_otp_session_id": post_otp_session_id,
            "post_otp_session_recovered": post_otp_session_recovered,
            "signup_task_id": signup_task_id,
            "signup_task_ids": signup_task_ids,
            "post_otp_task_id": post_otp_task_id,
            "post_otp_task_ids": post_otp_task_ids,
            "verification_kind": verification.kind,
            "verification_value": verification.value,
            "signup_status": signup_result.get("status"),
            "post_otp_status": post_otp_result.get("status"),
            "signup_output": signup_result.get("output"),
            "post_otp_output": post_otp_result.get("output"),
            "verification_resend_attempts": verification_resend_attempts,
            "next_command": (
                "python3 -m trialpilot.browseruse_agentmail_demo "
                f"--target-url {cfg.target_url} --session-id {session_id} --resume-only --include-api-key-step"
            ),
            "timestamp": _now_utc_iso(),
        }

    continue_task_ids: list[str] = []
    continue_result: dict[str, Any] = {}
    continue_task_id = ""
    continue_session_id = session_id
    continue_session_recovered = False
    max_verify_attempts = max(1, int(os.getenv("TRIALPILOT_VERIFICATION_RETRIES", "3")))
    verification_retries = 0

    for attempt in range(1, max_verify_attempts + 1):
        continue_task_text = _build_continue_task(verification, cfg.include_api_key_step)
        _log(cfg, f"running continuation task attempt {attempt}/{max_verify_attempts}")
        continue_task_id, continue_session_id, recovered, continue_result, continue_task_ids_for_attempt = _run_task_with_wait_and_recovery(
            cfg,
            session_id=continue_session_id,
            task_text=continue_task_text,
            allowed_domains=allowed_domains,
            recovery_task_text=_build_recovery_continue_task(cfg, inbox_id, verification, cfg.include_api_key_step),
            wait_seconds=cfg.timeout_seconds,
        )
        continue_session_recovered = continue_session_recovered or recovered
        continue_task_ids.extend(continue_task_ids_for_attempt)
        continue_output = str(continue_result.get("output", "") or "")
        if not _verification_needs_retry(continue_output):
            break
        if attempt >= max_verify_attempts:
            break
        verification_retries += 1
        _log(cfg, "verification was rejected; fetching fresh artifact and retrying")
        verification_not_before = datetime.now(timezone.utc)
        verification = _wait_for_verification(
            cfg,
            inbox_id,
            target_host=target_host,
            not_before=verification_not_before,
            ignore_message_ids=ignored_message_ids,
        )
        if verification.message_id:
            ignored_message_ids.add(verification.message_id)
        if verification.kind == "magic_link":
            magic_host = (urlparse(verification.value).hostname or "").strip().lower()
            if magic_host:
                allowed_set.update(_domain_family(magic_host))
                allowed_domains = sorted(d for d in allowed_set if d)

    return {
        "ok": True,
        "dry_run": cfg.dry_run,
        "target_url": cfg.target_url,
        "inbox_id": inbox_id,
        "signup_email": signup_email,
        "session_id": session_id,
        "continue_session_id": continue_session_id,
        "continue_session_recovered": continue_session_recovered,
        "signup_task_id": signup_task_id,
        "signup_task_ids": signup_task_ids,
        "continue_task_id": continue_task_id,
        "continue_task_ids": continue_task_ids,
        "verification_kind": verification.kind,
        "verification_value": verification.value,
        "verification_retries": verification_retries,
        "verification_resend_attempts": verification_resend_attempts,
        "signup_status": signup_result.get("status"),
        "continue_status": continue_result.get("status"),
        "signup_output": signup_result.get("output"),
        "continue_output": continue_result.get("output"),
        "timestamp": _now_utc_iso(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Browser Use + AgentMail signup demo runner")
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--password", default=_random_password())
    parser.add_argument("--first-name", default="Demo")
    parser.add_argument("--last-name", default="User")
    parser.add_argument("--phone", default="+14155550123")
    parser.add_argument("--llm", default="browser-use-2.0")
    parser.add_argument("--max-steps", type=int, default=80)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--poll-seconds", type=int, default=5)
    parser.add_argument("--otp-wait-seconds", type=int, default=300)
    parser.add_argument("--otp-regex", default=r"\b(\d{6})\b")
    parser.add_argument(
        "--inbox",
        default=None,
        help="Existing AgentMail inbox_id (email address). Defaults to AGENTMAIL_INBOX_ID if set.",
    )
    parser.add_argument("--inbox-username", default=None, help="AgentMail username when creating new inbox")
    parser.add_argument("--inbox-domain", default="agentmail.to")
    parser.add_argument("--session-id", default=None, help="Reuse an existing Browser Use session")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include-api-key-step", action="store_true")
    parser.add_argument(
        "--demo-fast",
        action="store_true",
        help="Fast demo profile: trims retries/timeouts, enables keep-alive, and cleans stale active sessions.",
    )
    parser.add_argument("--manual-otp", default=None, help="Skip inbox polling and use this OTP directly")
    parser.add_argument("--profile-id", default=None, help="Browser Use profile ID for persistent cookies/session.")
    parser.add_argument(
        "--proxy-country-code",
        "--cloud-proxy-country-code",
        dest="proxy_country_code",
        default=None,
        help="Browser Use proxy country code, e.g. us, uk, de.",
    )
    parser.add_argument("--screen-width", type=int, default=None, help="Browser viewport width.")
    parser.add_argument("--screen-height", type=int, default=None, help="Browser viewport height.")
    parser.add_argument("--highlight-elements", action="store_true", help="Enable interactive element highlighting.")
    parser.add_argument("--flash-mode", action="store_true", help="Enable Browser Use flash mode.")
    parser.add_argument("--thinking", action="store_true", help="Enable Browser Use thinking mode.")
    parser.add_argument("--vision", default=None, help="Vision mode: true, false, or auto.")
    parser.add_argument("--system-prompt-extension", default=None, help="Append extra task system prompt guidance.")
    parser.add_argument("--op-vault-id", default=None, help="Optional 1Password vault ID for secret injection.")
    parser.add_argument("--secrets-json", default=None, help='JSON object for task secrets, e.g. {"KEY":"VALUE"}.')
    parser.add_argument("--session-keep-alive", default=None, help="Override session keepAlive (true/false).")
    parser.add_argument(
        "--session-persist-memory",
        default=None,
        help="Override session persistMemory (true/false).",
    )
    parser.add_argument(
        "--pause-for-billing",
        action="store_true",
        help="Stop after OTP/billing page so you can enter card details manually, then resume with --resume-only.",
    )
    parser.add_argument(
        "--resume-only",
        action="store_true",
        help="Skip signup/OTP and continue onboarding in an existing --session-id.",
    )
    parser.add_argument(
        "--cleanup-active-sessions",
        action="store_true",
        help="Delete older active Browser Use sessions before creating a new one to prevent 429 limits.",
    )
    parser.add_argument(
        "--max-active-sessions",
        type=int,
        default=1,
        help="Maximum active Browser Use sessions to keep when cleanup is enabled.",
    )
    parser.add_argument(
        "--preload-file",
        default=None,
        help="JSON result file from a prior successful run (used for optional fallback in demo mode).",
    )
    parser.add_argument(
        "--use-preloaded-on-block",
        action="store_true",
        help="If live flow is blocked, return a transparent preloaded fallback summary when --preload-file is provided.",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.demo_fast:
        args.max_steps = min(int(args.max_steps), 60)
        args.timeout_seconds = min(int(args.timeout_seconds), 420)
        args.poll_seconds = min(int(args.poll_seconds), 3)
        args.otp_wait_seconds = min(int(args.otp_wait_seconds), 240)
        args.include_api_key_step = True
        args.cleanup_active_sessions = True
        if not args.use_preloaded_on_block and str(args.preload_file or "").strip():
            args.use_preloaded_on_block = True

    profile_id = (args.profile_id or os.getenv("BROWSER_USE_PROFILE_ID", "")).strip() or None
    proxy_country_code = (
        args.proxy_country_code
        or os.getenv("BROWSER_USE_CLOUD_PROXY_COUNTRY_CODE", "")
        or os.getenv("BROWSER_USE_PROXY_COUNTRY_CODE", "")
    ).strip() or None
    screen_width = _parse_optional_int(args.screen_width or os.getenv("BROWSER_USE_SCREEN_WIDTH", ""))
    screen_height = _parse_optional_int(args.screen_height or os.getenv("BROWSER_USE_SCREEN_HEIGHT", ""))
    highlight_elements = bool(args.highlight_elements) or _parse_env_bool("BROWSER_USE_HIGHLIGHT_ELEMENTS", default=False)
    flash_mode = bool(args.flash_mode) or _parse_env_bool("BROWSER_USE_FLASH_MODE", default=False)
    thinking_mode = bool(args.thinking) or _parse_env_bool("BROWSER_USE_THINKING", default=False)
    vision_mode = _parse_vision(args.vision if args.vision is not None else os.getenv("BROWSER_USE_VISION", "true"))
    prompt_extension = args.system_prompt_extension
    if prompt_extension is None:
        prompt_extension = os.getenv("BROWSER_USE_SYSTEM_PROMPT_EXTENSION", "")
    op_vault_id = (args.op_vault_id or os.getenv("BROWSER_USE_OP_VAULT_ID", "")).strip() or None
    secrets_raw = args.secrets_json if args.secrets_json is not None else os.getenv("BROWSER_USE_SECRETS_JSON", "")
    secrets = _parse_secrets_json(secrets_raw)
    keep_alive_raw = args.session_keep_alive if args.session_keep_alive is not None else os.getenv("BROWSER_USE_KEEP_ALIVE", "")
    persist_raw = (
        args.session_persist_memory
        if args.session_persist_memory is not None
        else os.getenv("BROWSER_USE_PERSIST_MEMORY", "")
    )
    keep_alive_opt = _parse_optional_bool(keep_alive_raw)
    persist_opt = _parse_optional_bool(persist_raw)

    cfg = DemoConfig(
        target_url=args.target_url,
        password=args.password,
        first_name=args.first_name,
        last_name=args.last_name,
        phone=args.phone,
        llm=args.llm,
        max_steps=args.max_steps,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
        otp_wait_seconds=args.otp_wait_seconds,
        otp_regex=args.otp_regex,
        inbox=args.inbox or (os.getenv("AGENTMAIL_INBOX_ID", "").strip() or None),
        inbox_username=args.inbox_username,
        inbox_domain=args.inbox_domain,
        session_id=args.session_id,
        dry_run=bool(args.dry_run),
        browser_use_api_key=os.getenv("BROWSER_USE_API_KEY", "").strip() or None,
        agentmail_api_key=os.getenv("AGENTMAIL_API_KEY", "").strip() or None,
        include_api_key_step=bool(args.include_api_key_step),
        manual_otp=(args.manual_otp.strip() if isinstance(args.manual_otp, str) else None) or None,
        pause_for_billing=bool(args.pause_for_billing),
        resume_only=bool(args.resume_only),
        verbose=bool(args.verbose),
        profile_id=profile_id,
        proxy_country_code=proxy_country_code,
        browser_screen_width=screen_width,
        browser_screen_height=screen_height,
        highlight_elements=highlight_elements,
        flash_mode=flash_mode or bool(args.demo_fast),
        thinking_mode=thinking_mode,
        vision_mode=vision_mode,
        system_prompt_extension=str(prompt_extension or ""),
        op_vault_id=op_vault_id,
        secrets=secrets,
        session_keep_alive=keep_alive_opt if keep_alive_opt is not None else (True if args.demo_fast else None),
        session_persist_memory=persist_opt if persist_opt is not None else (True if args.demo_fast else None),
        demo_fast=bool(args.demo_fast),
        cleanup_active_sessions=bool(args.cleanup_active_sessions),
        max_active_sessions=max(0, int(args.max_active_sessions)),
        preload_file=(str(args.preload_file).strip() if args.preload_file else None),
        use_preloaded_on_block=bool(args.use_preloaded_on_block),
    )

    missing = _validate_config(cfg)
    if missing:
        payload = {
            "ok": False,
            "error": f"missing_required_env:{','.join(missing)}",
            "dry_run": cfg.dry_run,
            "target_url": cfg.target_url,
            "timestamp": _now_utc_iso(),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1

    try:
        result = run_demo(cfg)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "dry_run": cfg.dry_run,
            "target_url": cfg.target_url,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
