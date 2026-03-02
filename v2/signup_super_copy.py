#!/usr/bin/env python3
"""Sigma Super: high-speed signup + verification + API key fetch.

Design goals:
- Keep everything in one file and one flow.
- Fast by default (parallel setup, low waits, aggressive multi-action steps).
- Reliable (retry transient failures, robust parsing, graceful fallbacks).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import html
import inspect
import json
import os
import re
import secrets
import sqlite3
import string
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from agentmail import AsyncAgentMail
from browser_use import Agent, Browser, ChatBrowserUse
from dotenv import load_dotenv
from faker import Faker
from langchain_openai import ChatOpenAI
from pydantic import ConfigDict

load_dotenv()

BROWSER_USE_API_KEY = os.getenv("BROWSER_USE_API_KEY", "")
AGENTMAIL_API_KEY = os.getenv("AGENTMAIL_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

MODEL_PRESETS = {
    "openai-best": "gpt-5.2",
    "openai-fast": "gpt-5-mini",
    "openai-ultrafast": "gpt-5-nano",
    "browseruse-fast": "bu-2-0",
}

fake = Faker()
_t0 = time.time()
STATE_DIR = Path(__file__).resolve().parent / "state"
SITE_INBOXES_FILE = STATE_DIR / "site_inboxes.json"
RUNS_DB_FILE = STATE_DIR / "signup_runs.db"


@dataclass
class Identity:
    first_name: str
    last_name: str
    username: str
    email: str
    password: str
    dob: str
    company: str
    website: str
    phone: str


@dataclass
class VerificationCandidate:
    link: str | None
    code: str | None
    subject: str | None


@dataclass
class RunResult:
    output: str | None
    success: bool
    steps: int
    error: str | None = None


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class BrowserUseChatOpenAI(ChatOpenAI):
    """Compatibility shim: browser-use expects llm.provider."""

    model_config = ConfigDict(extra="allow")
    provider: str = "openai"

    @property
    def model(self) -> str:
        return str(getattr(self, "model_name", "unknown"))


def log(stage: str, msg: str) -> None:
    elapsed = time.time() - _t0
    print(f"[{elapsed:6.1f}s] [{stage}] {msg}", flush=True)


def resolve_model(model: str) -> str:
    return MODEL_PRESETS.get(model.strip().lower(), model.strip())


def build_llm(model: str):
    resolved = resolve_model(model)
    if resolved.startswith("bu-") or resolved == "bu-2-0":
        if not BROWSER_USE_API_KEY:
            raise RuntimeError("Missing BROWSER_USE_API_KEY")
        return ChatBrowserUse(model=resolved, api_key=BROWSER_USE_API_KEY), resolved

    if not OPENAI_API_KEY:
        raise RuntimeError("Missing OPENAI_API_KEY for OpenAI models")
    return (
        BrowserUseChatOpenAI(
            model=resolved,
            api_key=OPENAI_API_KEY,
            timeout=120,
            max_retries=2,
        ),
        resolved,
    )


def generate_password(length: int = 18) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    parts = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*"),
    ]
    parts.extend(secrets.choice(alphabet) for _ in range(max(8, length) - 4))
    chars = list(parts)
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def generate_e164_us_phone() -> str:
    """Generate a valid US E.164 phone number (+1NXXNXXXXXX)."""
    # NANP: area/exchange codes cannot start with 0 or 1.
    area_first = str(secrets.randbelow(8) + 2)
    area_rest = f"{secrets.randbelow(100):02d}"
    exch_first = str(secrets.randbelow(8) + 2)
    exch_rest = f"{secrets.randbelow(100):02d}"
    line = f"{secrets.randbelow(10000):04d}"
    return f"+1{area_first}{area_rest}{exch_first}{exch_rest}{line}"


def normalize_phone_e164(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D+", "", value)
    if not digits:
        return None
    # US local 10-digit number.
    if len(digits) == 10:
        return f"+1{digits}"
    # US prefixed with country code already.
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    # Generic international (keep as +digits if plausible).
    if len(digits) >= 8:
        return f"+{digits}"
    return None


def generate_identity(email: str) -> Identity:
    first = fake.first_name()
    last = fake.last_name()
    username = f"{first.lower()}{last.lower()}{secrets.randbelow(9000) + 1000}"
    return Identity(
        first_name=first,
        last_name=last,
        username=username,
        email=email,
        password=generate_password(),
        dob=fake.date_of_birth(minimum_age=19, maximum_age=45).strftime("%Y-%m-%d"),
        company=fake.company(),
        website=f"https://{fake.domain_name()}",
        phone=generate_e164_us_phone(),
    )


def _extract_links(text: str) -> list[str]:
    raw = [html.unescape(u) for u in re.findall(r"https?://[^\s<>\"']+", text)]
    out: list[str] = []
    seen: set[str] = set()

    def add(u: str | None) -> None:
        if not u:
            return
        u = u.strip()
        if not u or u in seen:
            return
        seen.add(u)
        out.append(u)

    def unwrap_tracking_link(u: str) -> str | None:
        parsed = urlparse(u)
        q = parse_qs(parsed.query)
        for key in ("url", "u", "target", "redirect", "redirect_url", "destination", "next", "continue"):
            vals = q.get(key)
            if vals:
                val = unquote(vals[0])
                if val.startswith("http://") or val.startswith("https://"):
                    return val
        # Resend wrapper style:
        # /CL0/https:%2F%2Fexample.com%2Fverify/1/....
        m = re.search(r"/CL0/(https:%2F%2F[^/]+(?:%2F[^/]+)*)", u)
        if m:
            val = unquote(m.group(1))
            if val.startswith("http://") or val.startswith("https://"):
                return val
        return None

    for u in raw:
        add(u)
        add(unwrap_tracking_link(u))
    return out


def _extract_code(text: str) -> str | None:
    """
    Extract a verification code from email text.

    We prefer plain-text bodies and line-oriented numeric tokens to avoid
    matching unrelated CSS color numbers from HTML emails.
    """
    # First pass: keyword-guided extraction with generous non-digit gap.
    keyword_patterns = [
        r"(?:verification|verify|confirm|otp|code|pin)[^0-9]{0,120}(\d{4,8})",
        r"(\d{4,8})[^0-9]{0,120}(?:verification|verify|confirm|otp|code|pin)",
    ]
    for pattern in keyword_patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if m:
            code = m.group(1)
            if len(set(code)) > 1:
                return code

    # Second pass: pick isolated 6-digit line tokens (common email OTP format).
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.fullmatch(r"(\d{6})", line)
        if m:
            code = m.group(1)
            if len(set(code)) > 1:
                return code

    # Third pass: fallback to isolated 4-8 digit tokens in text-like content.
    for m in re.finditer(r"\b(\d{4,8})\b", text):
        code = m.group(1)
        if len(set(code)) > 1:
            return code
    return None


def _base_domain(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.netloc or parsed.path).lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def _target_tokens(url: str) -> set[str]:
    base = _base_domain(url)
    if not base:
        return set()
    parts = [p for p in base.split(".") if p]
    tokens = {base}
    tokens.update(parts)
    if len(parts) >= 2:
        tokens.add(parts[-2])
    return {t for t in tokens if len(t) >= 3}


def _apple_ns_from_datetime(dt: datetime) -> int:
    # Apple Messages "date" is nanoseconds since 2001-01-01 UTC.
    return max(0, int((dt.timestamp() - 978307200) * 1_000_000_000))


async def watch_for_sms_code_local(
    *,
    target_url: str,
    timeout_s: int,
    min_received_at: datetime,
    db_path: str,
    phone_number: str | None = None,
    poll_s: int = 3,
) -> str | None:
    db = os.path.expanduser(db_path)
    if not os.path.exists(db):
        log("sms", f"Messages DB not found: {db}")
        return None

    min_apple_ns = _apple_ns_from_datetime(min_received_at)
    target_tokens = _target_tokens(target_url)
    seen_ids: set[int] = set()
    attempts = max(1, timeout_s // poll_s)
    keywords = ("code", "otp", "verification", "verify", "passcode", "security")

    for i in range(attempts):
        candidates: list[tuple[int, int, str]] = []
        try:
            conn = sqlite3.connect(db)
            cur = conn.cursor()
            cur.execute(
                """
                SELECT m.ROWID, m.date, COALESCE(h.id,''), COALESCE(m.service,''), COALESCE(m.text,''), COALESCE(m.subject,'')
                FROM message m
                LEFT JOIN handle h ON m.handle_id = h.ROWID
                WHERE m.is_from_me = 0
                  AND m.text IS NOT NULL
                  AND m.date > ?
                ORDER BY m.date DESC
                LIMIT 120
                """,
                (min_apple_ns,),
            )
            rows = cur.fetchall()
            conn.close()
        except Exception as e:
            log("sms", f"SMS DB read failed: {e}")
            await asyncio.sleep(poll_s)
            continue

        for rowid, msg_date, sender, service, text, subject in rows:
            rid = int(rowid)
            if rid in seen_ids:
                continue
            seen_ids.add(rid)

            sender_l = (sender or "").lower()
            service_u = (service or "").upper()
            body = f"{subject or ''}\n{text or ''}".strip()
            body_l = body.lower()
            code = _extract_code(body)
            if not code:
                continue

            score = 0
            if service_u in {"SMS", "RCS"}:
                score += 2
            if any(k in body_l for k in keywords):
                score += 2
            if any(t in body_l or t in sender_l for t in target_tokens):
                score += 3
            if sender_l.isdigit() and len(sender_l) <= 8:
                score += 1
            if phone_number:
                digits = re.sub(r"\D+", "", phone_number)
                if digits and digits in body_l:
                    score += 1

            if score >= 3:
                candidates.append((score, int(msg_date), code))

        if candidates:
            candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
            return candidates[0][2]

        if i % 2 == 0:
            log("sms", "Polling local Messages DB for OTP...")
        await asyncio.sleep(poll_s)

    return None


def _load_site_inboxes() -> dict[str, str]:
    if not SITE_INBOXES_FILE.exists():
        return {}
    try:
        return json.loads(SITE_INBOXES_FILE.read_text())
    except Exception:
        return {}


def _save_site_inboxes(mapping: dict[str, str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SITE_INBOXES_FILE.write_text(json.dumps(mapping, indent=2, sort_keys=True))


def _set_site_inbox(site: str, inbox_id: str) -> None:
    mapping = _load_site_inboxes()
    mapping[site] = inbox_id
    _save_site_inboxes(mapping)


def _ensure_runs_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signup_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                url TEXT NOT NULL,
                domain TEXT,
                model_requested TEXT,
                model_active TEXT,
                profile_id TEXT,
                inbox_id TEXT,
                email TEXT,
                password TEXT,
                username TEXT,
                signup_status TEXT,
                verified INTEGER,
                verify_channel TEXT,
                login_status TEXT,
                api_key TEXT,
                api_key_url TEXT,
                login_url TEXT,
                notes TEXT,
                subscription TEXT,
                entitlements TEXT,
                limits_text TEXT,
                profile_name TEXT,
                profile_email TEXT,
                connectors TEXT,
                runtime_seconds REAL,
                exit_code INTEGER,
                error TEXT,
                signup_output TEXT,
                login_output TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_signup_runs_recorded_at ON signup_runs(recorded_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_signup_runs_domain ON signup_runs(domain)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_signup_runs_exit_code ON signup_runs(exit_code)"
        )
        conn.commit()
    finally:
        conn.close()


def _truncate_text(value: str | None, max_len: int = 4000) -> str | None:
    if value is None:
        return None
    if len(value) <= max_len:
        return value
    return value[: max_len - 3] + "..."


def _persist_signup_run(db_path: Path, row: dict[str, Any]) -> None:
    _ensure_runs_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO signup_runs (
                run_id, recorded_at, url, domain, model_requested, model_active, profile_id, inbox_id,
                email, password, username, signup_status, verified, verify_channel, login_status,
                api_key, api_key_url, login_url, notes, subscription, entitlements, limits_text,
                profile_name, profile_email, connectors, runtime_seconds, exit_code, error,
                signup_output, login_output
            ) VALUES (
                :run_id, :recorded_at, :url, :domain, :model_requested, :model_active, :profile_id, :inbox_id,
                :email, :password, :username, :signup_status, :verified, :verify_channel, :login_status,
                :api_key, :api_key_url, :login_url, :notes, :subscription, :entitlements, :limits_text,
                :profile_name, :profile_email, :connectors, :runtime_seconds, :exit_code, :error,
                :signup_output, :login_output
            )
            """,
            {
                "run_id": row.get("run_id"),
                "recorded_at": row.get("recorded_at"),
                "url": row.get("url"),
                "domain": row.get("domain"),
                "model_requested": row.get("model_requested"),
                "model_active": row.get("model_active"),
                "profile_id": row.get("profile_id"),
                "inbox_id": row.get("inbox_id"),
                "email": row.get("email"),
                "password": row.get("password"),
                "username": row.get("username"),
                "signup_status": row.get("signup_status"),
                "verified": row.get("verified"),
                "verify_channel": row.get("verify_channel"),
                "login_status": row.get("login_status"),
                "api_key": row.get("api_key"),
                "api_key_url": row.get("api_key_url"),
                "login_url": row.get("login_url"),
                "notes": row.get("notes"),
                "subscription": row.get("subscription"),
                "entitlements": row.get("entitlements"),
                "limits_text": row.get("limits_text"),
                "profile_name": row.get("profile_name"),
                "profile_email": row.get("profile_email"),
                "connectors": row.get("connectors"),
                "runtime_seconds": row.get("runtime_seconds"),
                "exit_code": row.get("exit_code"),
                "error": _truncate_text(row.get("error")),
                "signup_output": _truncate_text(row.get("signup_output")),
                "login_output": _truncate_text(row.get("login_output")),
            },
        )
        conn.commit()
    finally:
        conn.close()


def _best_verification_link(urls: list[str], target_url: str) -> str | None:
    target_domain = _base_domain(target_url)
    blocked_hosts = {"w3.org", "www.w3.org", "schemas.xmlsoap.org", "xmlns.com"}
    negative = ("unsubscribe", "privacy", "terms", ".png", ".jpg", ".gif", ".css", ".js", ".dtd")
    positive = ("verify", "confirm", "activate", "auth", "token", "magic", "signin", "sign-in")

    best: tuple[int, str] | None = None
    for raw in urls:
        low = raw.lower().strip()
        if any(n in low for n in negative):
            continue
        host = _base_domain(low)
        if host in blocked_hosts:
            continue

        score = 0
        if any(p in low for p in positive):
            score += 3
        if target_domain and target_domain in host:
            score += 3
        if "token=" in low or "code=" in low or "verify" in low:
            score += 2
        if "/auth/" in low or "/confirm" in low:
            score += 1

        if best is None or score > best[0]:
            best = (score, raw)

    if best and best[0] > 0:
        return best[1]
    return None


async def create_cloud_browser(
    proxy_country_code: str = "us",
    profile_id: str | None = None,
) -> Browser:
    if not BROWSER_USE_API_KEY:
        raise RuntimeError("Missing BROWSER_USE_API_KEY")
    kwargs: dict[str, Any] = dict(
        use_cloud=True,
        cloud_proxy_country_code=proxy_country_code,
        keep_alive=True,
        minimum_wait_page_load_time=0.1,
        wait_between_actions=0.1,
        highlight_elements=False,
        captcha_solver=True,
    )
    if profile_id:
        kwargs["profile_id"] = profile_id
    return Browser(**kwargs)


async def infer_oauth_provider_from_profile(profile_id: str | None) -> str:
    """Infer best OAuth provider preference from Browser Use profile cookies."""
    if not profile_id or not BROWSER_USE_API_KEY:
        return "auto"
    url = f"https://api.browser-use.com/api/v2/profiles/{profile_id}"
    headers = {"X-Browser-Use-API-Key": BROWSER_USE_API_KEY}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code >= 400:
                return "auto"
            payload = resp.json()
    except Exception:
        return "auto"

    domains = [str(d).lower() for d in (payload.get("cookieDomains") or [])]
    has_github = any("github.com" in d for d in domains)
    has_google = any("google.com" in d for d in domains)

    if has_github and has_google:
        return "google"
    if has_github and not has_google:
        return "github"
    if has_google and not has_github:
        return "google"
    return "auto"


async def acquire_inbox(mail: AsyncAgentMail, reuse_inbox: bool) -> tuple[str, bool]:
    if reuse_inbox:
        listed = await mail.inboxes.list(limit=1)
        if listed.inboxes:
            return listed.inboxes[0].inbox_id, False
    try:
        inbox = await mail.inboxes.create()
        return inbox.inbox_id, True
    except Exception as e:
        if "limit" not in str(e).lower():
            raise
        # Recycle oldest inbox first so tests can still get fresh emails.
        listed = await mail.inboxes.list(limit=10)
        if listed.inboxes:
            oldest = listed.inboxes[-1]
            try:
                await mail.inboxes.delete(oldest.inbox_id)
                await asyncio.sleep(0.2)
                inbox = await mail.inboxes.create()
                return inbox.inbox_id, True
            except Exception:
                pass
        listed_fallback = await mail.inboxes.list(limit=1)
        if not listed_fallback.inboxes:
            raise RuntimeError("Inbox create failed and no reusable inbox found")
        return listed_fallback.inboxes[0].inbox_id, False


async def _acquire_inbox_for_site(
    mail: AsyncAgentMail,
    *,
    target_url: str,
    reuse_inbox: bool,
    sticky_inbox: bool,
) -> tuple[str, bool]:
    if sticky_inbox:
        mapping = _load_site_inboxes()
        site = _base_domain(target_url)
        cached = mapping.get(site)
        if cached:
            try:
                await mail.inboxes.messages.list(inbox_id=cached, limit=1)
                log("email", f"Reusing sticky inbox for {site}: {cached}")
                return cached, False
            except Exception:
                mapping.pop(site, None)
                _save_site_inboxes(mapping)
        inbox_id, created = await acquire_inbox(mail, reuse_inbox=reuse_inbox)
        mapping[site] = inbox_id
        _save_site_inboxes(mapping)
        log("email", f"Saved sticky inbox for {site}: {inbox_id}")
        return inbox_id, created
    return await acquire_inbox(mail, reuse_inbox=reuse_inbox)


async def watch_for_verification(
    mail: AsyncAgentMail,
    inbox_id: str,
    target_url: str,
    timeout_s: int,
    poll_s: int = 2,
    min_received_at: datetime | None = None,
) -> VerificationCandidate:
    seen: set[str] = set()
    attempts = max(1, timeout_s // poll_s)
    min_received_at = _as_utc(min_received_at)
    for i in range(attempts):
        try:
            listed = await mail.inboxes.messages.list(inbox_id=inbox_id, limit=10)
        except Exception:
            await asyncio.sleep(poll_s)
            continue
        for stub in listed.messages:
            msg_id = str(stub.message_id)
            if msg_id in seen:
                continue
            seen.add(msg_id)
            ts = _as_utc(getattr(stub, "timestamp", None)) or _as_utc(getattr(stub, "created_at", None))
            if min_received_at:
                if ts is None or ts < min_received_at:
                    continue
            msg = await mail.inboxes.messages.get(inbox_id=inbox_id, message_id=msg_id)
            text_body = (msg.text or "").strip()
            html_body = (msg.html or "").strip()
            combined_body = "\n".join(part for part in (text_body, html_body) if part)
            primary_body = text_body or combined_body
            urls = _extract_links(combined_body)
            link = _best_verification_link(urls, target_url)
            code = _extract_code(primary_body)
            if link or code:
                return VerificationCandidate(link=link, code=code, subject=msg.subject)
        if i % 5 == 0:
            log("verify", "Polling inbox...")
        await asyncio.sleep(poll_s)
    return VerificationCandidate(link=None, code=None, subject=None)


def _is_retryable_error(exc: Exception) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    text = str(exc).lower()
    retry_markers = (
        "429",
        "timeout",
        "timed out",
        "502",
        "503",
        "504",
        "bad gateway",
        "too many concurrent active sessions",
        "temporarily unavailable",
        "connection reset",
        "websocket connection closed",
        "cdp still not connected",
        "expected at least one handler",
    )
    return any(marker in text for marker in retry_markers)


def _is_eventbus_stall_text(text: str | None) -> bool:
    if not text:
        return False
    low = text.lower()
    markers = (
        "browserstaterequestevent",
        "event bus to be idle",
        "eventbus_",
        "domwatchdog",
        "timeout error - handling took more than",
    )
    return any(m in low for m in markers)


def _is_concurrency_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "too many concurrent active sessions" in text or "http 429" in text


def _needs_browser_rebuild(err: str | None) -> bool:
    if not err:
        return False
    text = err.lower()
    markers = (
        "too many concurrent active sessions",
        "cloudbrowsererror",
        "browserstaterequestevent",
        "root cdp client not initialized",
        "websocket connection closed",
        "reconnection failed",
        "cdp still not connected",
        "expected at least one handler",
        "sessionmanager not initialized",
        "session with given id not found",
    )
    return any(m in text for m in markers)


def _parse_iso(ts: str | None) -> datetime:
    if not ts:
        return datetime.max.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return datetime.max.replace(tzinfo=timezone.utc)


async def stop_oldest_active_cloud_session() -> str | None:
    """Stop one oldest active cloud browser session via Browser Use API."""
    if not BROWSER_USE_API_KEY:
        return None
    headers = {
        "X-Browser-Use-API-Key": BROWSER_USE_API_KEY,
        "Content-Type": "application/json",
    }
    list_url = "https://api.browser-use.com/api/v2/browsers"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(list_url, headers=headers)
        if resp.status_code >= 400:
            return None
        data = resp.json()
        items = data.get("items", []) if isinstance(data, dict) else []
        active = [it for it in items if str(it.get("status", "")).lower() == "active" and it.get("id")]
        if not active:
            return None
        active.sort(key=lambda it: _parse_iso(it.get("startedAt")))
        oldest = active[0]
        session_id = str(oldest["id"])
        patch_url = f"https://api.browser-use.com/api/v2/browsers/{session_id}"
        patch = await client.patch(patch_url, headers=headers, json={"action": "stop"})
        if patch.status_code < 400:
            return session_id
    return None


async def run_agent_task(
    *,
    browser: Browser,
    llm: Any,
    label: str,
    task: str,
    max_steps: int,
    timeout_s: int,
    retries: int = 1,
) -> RunResult:
    def build_agent(task_text: str) -> Agent:
        """Build agent with aggressive speed params if supported by installed version."""
        speed_kwargs: dict[str, Any] = {
            "task": task_text,
            "llm": llm,
            "browser": browser,
            "use_vision": False,
            "max_actions_per_step": 15,
            "extend_system_message": (
                "Act fast. Fill form fields in one step when possible. "
                "Navigate directly and avoid unnecessary exploration. "
                "Never use write_file/replace_file/read_file/todo actions."
            ),
            "use_judge": False,
            "include_attributes": ["id", "name", "type", "placeholder", "value"],
            "max_history_items": 6,
            "max_clickable_elements_length": 12000,
            "message_compaction": False,
            "loop_detection_enabled": False,
            "llm_timeout": 30,
            "step_timeout": 150,
            "max_failures": 2,
            "final_response_after_failure": False,
        }
        provider = str(getattr(llm, "provider", "")).lower()
        if provider == "browser-use":
            speed_kwargs["flash_mode"] = True

        allowed = set(inspect.signature(Agent.__init__).parameters.keys())
        filtered = {k: v for k, v in speed_kwargs.items() if k in allowed}
        return Agent(**filtered)

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        log(label, f"Starting (attempt {attempt}/{retries})...")
        agent = build_agent(task)
        try:
            history = await asyncio.wait_for(agent.run(max_steps=max_steps), timeout=timeout_s)
            output = history.final_result()
            success = history.is_successful() if hasattr(history, "is_successful") else history.is_done()
            success = bool(success)
            steps = history.number_of_steps()
            errors = [e for e in history.errors() if e]
            last_error = " | ".join(errors[-3:]) if errors else None
            log(label, f"Done: success={success} steps={steps}")
            if output:
                log(label, f"Output: {output[:220]}")
            if last_error:
                log(label, f"Error detail: {last_error[:180]}")
            if last_error and _is_eventbus_stall_text(last_error):
                log(label, "Detected EventBus/DOM stall; failing fast (no retry loop).")
                return RunResult(output=output, success=False, steps=steps, error=last_error)
            if not success and attempt < retries and last_error and _is_retryable_error(Exception(last_error)):
                backoff = 2**attempt
                log(label, f"Retrying after non-fatal task errors in {backoff}s...")
                await asyncio.sleep(backoff)
                continue
            return RunResult(output=output, success=success, steps=steps, error=last_error)
        except Exception as exc:
            last_exc = exc
            if _is_eventbus_stall_text(str(exc)):
                log(label, f"Detected EventBus/DOM stall; failing fast: {exc}")
                return RunResult(output=None, success=False, steps=0, error=str(exc))
            if attempt < retries and _is_retryable_error(exc):
                backoff = 2**attempt
                if _is_concurrency_limit_error(exc):
                    try:
                        killed = await stop_oldest_active_cloud_session()
                    except Exception as stop_exc:
                        log(label, f"Session cleanup failed: {stop_exc}")
                        killed = None
                    if killed:
                        log(label, f"Freed oldest cloud session: {killed}")
                log(label, f"Transient error, retrying in {backoff}s: {exc}")
                await asyncio.sleep(backoff)
                continue
            log(label, f"Failed: {exc}")
            return RunResult(output=None, success=False, steps=0, error=str(exc))
    raise RuntimeError(f"Agent failed unexpectedly: {last_exc}")


async def try_http_verification(link: str) -> tuple[bool, str | None]:
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(link)
        final = str(resp.url)
        final_low = final.lower()
        body = (resp.text.lower() if hasattr(resp, "text") else "")[:8000]
        negative = (
            "invalid",
            "expired",
            "already used",
            "wrong code",
            "failed",
            "error",
            "not found",
        )
        positive_body = ("verified", "confirmation complete", "account confirmed", "email verified")
        positive_url = ("verify", "confirmed", "activated", "success")
        if (
            resp.status_code < 400
            and not any(token in body for token in negative)
            and not any(token in final_low for token in ("expired", "invalid", "error"))
            and (any(token in body for token in positive_body) or any(token in final_low for token in positive_url))
        ):
            return True, final
    except Exception:
        return False, None
    return False, None


def parse_login_output(text: str | None) -> dict[str, str | None]:
    result = {"LOGIN": None, "API_KEY": None, "API_KEY_URL": None, "LOGIN_URL": None, "NOTES": None}
    if not text:
        return result

    normalized = text.replace("\\n", "\n").strip()
    try:
        if normalized.startswith("{") and normalized.endswith("}"):
            obj = json.loads(normalized)
            for key in result:
                value = obj.get(key)
                result[key] = str(value).strip() if value is not None else None
            return result
    except Exception:
        pass

    for key in result:
        m = re.search(rf"(?im)^\s*{key}\s*:\s*([^\n]+)\s*$", normalized)
        if m:
            result[key] = m.group(1).strip()
    return result


def output_indicates_authenticated_state(raw_output: str | None, notes: str | None) -> bool:
    blob = f"{raw_output or ''}\n{notes or ''}".lower()
    signals = (
        "already logged in",
        "already authenticated",
        "dashboard",
        "/home/",
        "/home/teams",
        "workspace",
        "account menu",
        "profile menu",
        "without auth prompt",
        "redirected to /home",
    )
    return any(token in blob for token in signals)


def sanitize_api_key_candidate(key: str | None, notes: str | None = None) -> str | None:
    if not key:
        return None
    value = key.strip().strip("`").strip("\"").strip("'")
    if not value:
        return None
    if value.upper() == "NONE":
        return None
    lower = value.lower()
    if lower in {"none", "null", "n/a", "unknown", "not found"}:
        return None
    if value.startswith("http://") or value.startswith("https://"):
        return None
    if len(value) < 10:
        return None
    notes_l = (notes or "").lower()
    if "no api key" in notes_l or "no api keys" in notes_l:
        return None
    if "not found" in notes_l and "api" in notes_l:
        return None
    if any(tok in notes_l for tok in ("masked", "obscured", "hidden value", "password field")):
        if "copied" not in notes_l and "clipboard" not in notes_l and "revealed" not in notes_l:
            return None
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value):
        if any(tok in notes_l for tok in ("id only", "identifier", "masked", "obscured", "not secret", "uncertain")):
            return None
    return value


def parse_account_snapshot(text: str | None) -> dict[str, str | None]:
    result = {
        "SUBSCRIPTION": None,
        "ENTITLEMENTS": None,
        "LIMITS": None,
        "PROFILE_NAME": None,
        "PROFILE_EMAIL": None,
        "CONNECTORS": None,
        "NOTES": None,
    }
    if not text:
        return result

    normalized = text.replace("\\n", "\n").strip()
    for key in result:
        m = re.search(rf"(?im)^\s*{key}\s*:\s*([^\n]+)\s*$", normalized)
        if m:
            result[key] = m.group(1).strip()
    return result


def parse_signup_status(text: str | None) -> tuple[str, str | None]:
    if not text:
        return "UNKNOWN", None
    normalized = text.replace("\\n", "\n")
    m = re.search(r"STATUS\s*:\s*([A-Z_]+)", normalized, flags=re.IGNORECASE)
    if m:
        status = m.group(1).upper()
    else:
        if re.search(r"\bNEEDS_VERIFICATION\b", normalized, flags=re.IGNORECASE):
            status = "NEEDS_VERIFICATION"
        elif re.search(r"\bSIGNUP_SUCCESS\b", normalized, flags=re.IGNORECASE):
            status = "SIGNUP_SUCCESS"
        elif re.search(r"\bSIGNUP_FAILED\b", normalized, flags=re.IGNORECASE):
            status = "SIGNUP_FAILED"
        else:
            status = "UNKNOWN"
    d = re.search(r"DETAILS\s*:\s*(.+)", normalized, flags=re.IGNORECASE)
    details = d.group(1).strip() if d else None
    return status, details


def parse_signup_verify_channel(text: str | None) -> str | None:
    if not text:
        return None
    normalized = text.replace("\\n", "\n")
    m = re.search(
        r"VERIFY(?:_CHANNEL|_CH)\s*:\s*(EMAIL|SMS|NONE|UNKNOWN)",
        normalized,
        flags=re.IGNORECASE,
    )
    if m:
        return m.group(1).upper()
    return None


def choose_verification_channel(
    *,
    mode: str,
    agent_channel: str | None,
    signup_text: str,
    sms_local_enabled: bool,
    requires_verification: bool,
) -> str:
    """Pick verification source: EMAIL, SMS, or NONE."""
    mode_u = (mode or "auto").strip().upper()
    if mode_u in {"EMAIL", "SMS"}:
        chosen = mode_u
    else:
        chosen = "UNKNOWN"
        if agent_channel in {"EMAIL", "SMS", "NONE"}:
            chosen = agent_channel
        else:
            low = signup_text.lower()
            sms_hints = ("sms", "text message", "phone verification", "enter phone", "mobile number", "otp")
            email_hints = ("verify your email", "check your email", "verification email", "confirmation email")
            if any(h in low for h in sms_hints):
                chosen = "SMS"
            elif any(h in low for h in email_hints):
                chosen = "EMAIL"
            else:
                chosen = "EMAIL"

    if chosen == "NONE" and requires_verification:
        chosen = "EMAIL"
    if chosen == "SMS" and not sms_local_enabled:
        return "EMAIL"
    return chosen


def build_signup_task(
    base_url: str,
    identity: Identity,
    allow_oauth: bool,
    oauth_provider: str = "auto",
) -> str:
    local_phone = re.sub(r"\D", "", identity.phone)[-10:] if identity.phone else ""
    top_line = (
        f"Open {base_url} and register a NEW account (email preferred, OAuth allowed when email signup is unavailable)."
        if allow_oauth
        else f"Open {base_url} and register a NEW account by email (not OAuth)."
    )
    oauth_priority = (
        "Google first, then GitHub/SSO"
        if oauth_provider == "google"
        else "GitHub first, then Google/SSO"
        if oauth_provider == "github"
        else "the provider that already has an active session/account picker; otherwise Google then GitHub/SSO"
    )
    oauth_login_rule = (
        "   Use account-picker existing session only.\n"
        "   If the first provider asks for manual login/password/2FA, go back and try the other visible OAuth provider once.\n"
        "   If all visible providers require manual login/password/2FA, output SIGNUP_FAILED with DETAILS: OAUTH_LOGIN_REQUIRED.\n"
    )

    auth_rule = (
        f"2) OAuth fallback mode: prefer OAuth immediately ({oauth_priority}).\n"
        "   Do NOT retry email signup first in this mode.\n"
        "   If no OAuth option exists on the signup/login path, output SIGNUP_FAILED with DETAILS: OAUTH_OPTION_NOT_FOUND.\n"
        "   If OAuth returns messages like 'No account found' or account not eligible, stop and output\n"
        "   SIGNUP_FAILED with DETAILS: OAUTH_ACCOUNT_NOT_ELIGIBLE.\n"
        "   IMPORTANT: never type the generated site email into Google/GitHub OAuth.\n"
        f"{oauth_login_rule}"
    ) if allow_oauth else "2) If Google/GitHub/SSO appears, skip it and use email signup.\n"
    return f"""
{top_line}
Discover the signup path yourself from visible UI (Sign up/Register/Create account).
Stay on the target website domain only.
Do NOT use search tools, do NOT open any email provider, and do NOT navigate off-domain.
Do NOT create files, todo lists, or notes.

Credentials to use exactly:
- first_name: `{identity.first_name}`
- last_name: `{identity.last_name}`
- username: `{identity.username}`
- email: `{identity.email}`
- password: `{identity.password}`
- dob: `{identity.dob}`
- company: `{identity.company}`
- website: `{identity.website}`
- phone: `{identity.phone}`
Credential rule: when typing values, copy exact text inside backticks only. Never add punctuation, spaces, or quotes.

Rules:
1) Choose signup/register/create account (never login).
{auth_rule}3) Close blocking popups/modals/cookie banners first, then continue.
4) Fill required fields and submit.
5) Use provided identity values only when matching fields are present.
6) Phone rule (strict):
   - First submit attempt: do NOT fill phone unless field is explicitly marked required.
   - If submit fails with explicit phone-required or invalid-phone message, then fill phone and retry once.
7) If phone is required, first try local digits only: `{local_phone}`.
   Only if rejected for missing country code, retry once with E.164: `{identity.phone}`.
8) If a provided field has no matching input, skip it without searching extra pages.
9) If captcha appears (including Cloudflare Turnstile), click/solve it and wait up to 45s for completion.
10) If the first captcha attempt fails or times out, hard-refresh and retry captcha ONCE (wait up to 45s again). Only after a second failure output SIGNUP_FAILED with DETAILS: CAPTCHA_FAILED_OR_TIMEOUT.
11) If you reach an OTP/email-code screen, STOP immediately and output NEEDS_VERIFICATION.
12) If the same submit error repeats twice (for example identical "Registration failed"), STOP and output SIGNUP_FAILED with concise DETAILS.
13) Stop immediately after successful submit.
14) Output exactly:
STATUS: SIGNUP_SUCCESS or NEEDS_VERIFICATION or SIGNUP_FAILED
VERIFY_CH: EMAIL or SMS or NONE or UNKNOWN
DETAILS: <short reason>
""".strip()


def _run_looks_like_captcha_failure(run: RunResult) -> bool:
    text = ((run.output or "") + " " + (run.error or "")).lower()
    markers = ("captcha", "recaptcha", "hcaptcha", "cloudflare")
    return any(m in text for m in markers)


def _verification_looks_successful(output: str | None) -> bool:
    text = (output or "").lower()
    if not text:
        return False
    fail_markers = ("wrong code", "invalid code", "expired", "failed", "cannot verify", "unable to verify", "could not reach")
    if any(m in text for m in fail_markers):
        return False
    ok_markers = ("verified", "verification complete", "account confirmed", "code was accepted")
    return any(m in text for m in ok_markers)


def _verification_explicit_flag(output: str | None) -> bool | None:
    text = (output or "").replace("\\n", "\n")
    m = re.search(r"VERIFIED\s*:\s*(YES|NO)", text, flags=re.IGNORECASE)
    if not m:
        return None
    return m.group(1).upper() == "YES"


def _sms_triggered_flag(output: str | None) -> bool | None:
    text = (output or "").replace("\\n", "\n")
    m = re.search(r"SMS_TRIGGERED\s*:\s*(YES|NO)", text, flags=re.IGNORECASE)
    if not m:
        return None
    return m.group(1).upper() == "YES"


def _infer_needs_verification(signup_status: str, signup_text: str) -> bool:
    if signup_status == "NEEDS_VERIFICATION":
        return True
    if signup_status in {"SIGNUP_SUCCESS", "SIGNUP_FAILED"}:
        return False
    hints = (
        "needs_verification",
        "check your email to verify",
        "enter verification code",
        "verification code sent",
        "verify your email",
    )
    return any(h in signup_text for h in hints)


def _looks_like_email_signup_unavailable(signup_status: str, signup_details: str | None, signup_text: str) -> bool:
    if signup_status != "SIGNUP_FAILED":
        return False
    text = f"{signup_details or ''}\n{signup_text}".lower()
    markers = (
        "oauth",
        "sso",
        "google",
        "github",
        "only provides",
        "no email-based signup",
        "email signup not available",
        "no_email_signup_option_available",
        "no email signup option",
        "oath_login_required",
        "oauth_login_required",
    )
    return any(m in text for m in markers)


def _looks_like_oauth_fallback_candidate(signup_status: str, signup_details: str | None, signup_text: str) -> bool:
    if signup_status != "SIGNUP_FAILED":
        return False
    text = f"{signup_details or ''}\n{signup_text}".lower()
    if _looks_like_email_signup_unavailable(signup_status, signup_details, signup_text):
        return True
    markers = (
        "captcha_failed_or_timeout",
        "captcha failed",
        "cloudflare",
        "registration failed",
        "failed to register",
        "failed to sign up",
        "something went wrong",
        "try again",
        "invalid phone",
        "phone",
        "invalid email",
        "email is not allowed",
        "disposable",
        "cannot create account",
    )
    return any(m in text for m in markers)


async def try_trigger_sms_verification(
    *,
    browser: Browser,
    llm: Any,
    target_url: str,
    phone_number: str,
    timeout_s: int,
) -> bool:
    domain = _base_domain(target_url) or target_url
    task = f"""
Stay on the CURRENT PAGE in the current verification flow.
Do NOT navigate/open any URL. Do NOT go back to homepage.
Only use visible controls on domain {domain}.
You are on a verification flow. Try to switch verification to PHONE/SMS.
If you see options like "use phone", "SMS", "text me", "resend code", pick SMS.
Enter this phone number exactly where asked: {phone_number}
Submit and trigger sending a text verification code.
Stop immediately after seeing any confirmation like "code sent" or "text sent".
If no phone/SMS option is visible after quick checks, stop immediately.

Output exactly:
SMS_TRIGGERED: YES or NO
DETAILS: <short>
""".strip()
    res = await run_agent_task(
        browser=browser,
        llm=llm,
        label="trigger-sms",
        task=task,
        max_steps=3,
        timeout_s=min(timeout_s, 35),
        retries=1,
    )
    flag = _sms_triggered_flag(res.output)
    if flag is not None:
        return flag
    low = (res.output or "").lower()
    return any(k in low for k in ("code sent", "sms sent", "text sent", "verification code sent"))


async def run_signup_pipeline(args: argparse.Namespace) -> int:
    if not AGENTMAIL_API_KEY:
        raise RuntimeError("Missing AGENTMAIL_API_KEY")

    llm, resolved_model = build_llm(args.llm)
    active_model = resolved_model
    run_id = f"run-{int(time.time())}-{secrets.token_hex(4)}"
    runs_db_path = Path(args.results_db).expanduser()
    log("model", f"{args.llm} -> {resolved_model}")
    oauth_provider = args.oauth_provider
    if args.allow_oauth and args.profile_id and oauth_provider == "auto":
        inferred = await infer_oauth_provider_from_profile(args.profile_id)
        oauth_provider = inferred or "auto"
        log("oauth", f"provider preference: {oauth_provider} (auto-inferred)")
    email_phase_profile_id = None if args.allow_oauth else args.profile_id
    current_profile_id = email_phase_profile_id

    mail = AsyncAgentMail(api_key=AGENTMAIL_API_KEY, timeout=20)
    log("setup", "Creating inbox + cloud browser in parallel...")
    (inbox_id, created_inbox), browser = await asyncio.gather(
        _acquire_inbox_for_site(
            mail,
            target_url=args.url,
            reuse_inbox=args.reuse_inbox,
            sticky_inbox=args.sticky_inbox,
        ),
        create_cloud_browser(args.proxy_country, current_profile_id),
    )
    identity = generate_identity(inbox_id)
    preferred_phone = normalize_phone_e164(args.sms_phone)
    if preferred_phone:
        identity.phone = preferred_phone
    base_url = args.url.rstrip("/")
    log("setup", f"Inbox={inbox_id}")
    log("creds", f"User={identity.username} Password={identity.password}")

    async def rebuild_browser_session(reason: str) -> None:
        nonlocal browser
        log("session", f"Rebuilding browser after {reason}...")
        with contextlib.suppress(Exception):
            await browser.stop()
        browser = await create_cloud_browser(args.proxy_country, current_profile_id)

    verify_task: asyncio.Task[VerificationCandidate] | None = None
    run_started_at = datetime.now(timezone.utc)
    if not args.skip_verification:
        verify_task = asyncio.create_task(
            watch_for_verification(
                mail=mail,
                inbox_id=inbox_id,
                target_url=args.url,
                timeout_s=args.signup_timeout + args.verify_timeout,
                min_received_at=run_started_at,
            )
        )

    signup_task = build_signup_task(
        base_url,
        identity,
        allow_oauth=False,
        oauth_provider=oauth_provider,
    )

    signup = await run_agent_task(
        browser=browser,
        llm=llm,
        label="signup",
        task=signup_task,
        max_steps=args.max_steps,
        timeout_s=args.signup_timeout,
        retries=2,
    )
    if args.fail_fast_captcha and _run_looks_like_captcha_failure(signup):
        log("signup", "Fail-fast: captcha failure detected, skipping retries/rebuild.")
        out = signup.output or ""
        if "status:" not in out.lower():
            out = "STATUS: SIGNUP_FAILED\nDETAILS: CAPTCHA_FAILED_OR_TIMEOUT"
        signup = RunResult(
            output=out,
            success=True,
            steps=signup.steps,
            error=signup.error,
        )
    elif _run_looks_like_captcha_failure(signup):
        if args.allow_oauth and args.profile_id:
            log("signup", "Captcha failure detected; skipping extra email retries and pivoting to OAuth fallback.")
        else:
            log("signup", "Captcha failure detected; rebuilding browser and retrying signup once.")
            try:
                await browser.stop()
            except Exception:
                pass
            browser = await create_cloud_browser(args.proxy_country, email_phase_profile_id)
            signup = await run_agent_task(
                browser=browser,
                llm=llm,
                label="signup-captcha-retry",
                task=signup_task,
                max_steps=args.max_steps,
                timeout_s=args.signup_timeout,
                retries=1,
            )
    # Handle flaky cloud browser disconnect/focus failures by hard rebuilding browser.
    for _ in range(2):
        if signup.success:
            break
        if args.fail_fast_captcha and _run_looks_like_captcha_failure(signup):
            break
        needs_rebuild = _needs_browser_rebuild(signup.error) or signup.steps <= 1
        if not needs_rebuild:
            break
        log("signup", "Rebuilding browser after session instability and retrying.")
        try:
            await browser.stop()
        except Exception:
            pass
        browser = await create_cloud_browser(args.proxy_country, email_phase_profile_id)
        signup = await run_agent_task(
            browser=browser,
            llm=llm,
            label="signup",
            task=signup_task,
            max_steps=args.max_steps,
            timeout_s=args.signup_timeout,
            retries=1,
        )
    if (
        not signup.success
        and "openai" in type(llm).__name__.lower()
        and signup.error
        and "items" in signup.error.lower()
    ):
        log("model", "OpenAI schema mismatch detected, falling back to bu-2-0 for reliability.")
        llm = ChatBrowserUse(model="bu-2-0", api_key=BROWSER_USE_API_KEY)
        active_model = f"{resolved_model} -> bu-2-0(fallback)"
        signup = await run_agent_task(
            browser=browser,
            llm=llm,
            label="signup",
            task=signup_task,
            max_steps=args.max_steps,
            timeout_s=args.signup_timeout,
            retries=1,
        )
    early_signup_status, _ = parse_signup_status(signup.output)
    if not signup.success and early_signup_status == "UNKNOWN":
        if verify_task and not verify_task.done():
            verify_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await verify_task
        with contextlib.suppress(Exception):
            await browser.stop()
        if created_inbox and not args.keep_inbox and not args.sticky_inbox:
            with contextlib.suppress(Exception):
                await mail.inboxes.delete(inbox_id)
        elapsed = time.time() - _t0
        with contextlib.suppress(Exception):
            _persist_signup_run(
                runs_db_path,
                {
                    "run_id": run_id,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "url": args.url,
                    "domain": _base_domain(args.url),
                    "model_requested": args.llm,
                    "model_active": active_model,
                    "profile_id": args.profile_id,
                    "inbox_id": inbox_id,
                    "email": identity.email,
                    "password": identity.password,
                    "username": identity.username,
                    "signup_status": "FAILED_UNKNOWN",
                    "verified": 0,
                    "verify_channel": "UNKNOWN",
                    "login_status": "SKIPPED",
                    "api_key": None,
                    "api_key_url": None,
                    "login_url": None,
                    "notes": "Signup failed without structured status.",
                    "subscription": None,
                    "entitlements": None,
                    "limits_text": None,
                    "profile_name": None,
                    "profile_email": None,
                    "connectors": None,
                    "runtime_seconds": elapsed,
                    "exit_code": 1,
                    "error": signup.error,
                    "signup_output": signup.output,
                    "login_output": None,
                },
            )
        return 1

    signup_status, signup_details = parse_signup_status(signup.output)
    signup_text = (signup.output or "").lower()
    needs_verification = _infer_needs_verification(signup_status, signup_text)
    agent_verify_channel = parse_signup_verify_channel(signup.output)
    verify_channel = choose_verification_channel(
        mode=args.verify_channel,
        agent_channel=agent_verify_channel,
        signup_text=signup_text,
        sms_local_enabled=args.sms_local,
        requires_verification=needs_verification,
    )
    signup_failed_explicit = signup_status == "SIGNUP_FAILED"

    if (
        signup_failed_explicit
        and signup_details
        and args.retry_on_email_conflict
        and any(
            k in signup_details.lower()
            for k in ("already exists", "already_registered", "already registered", "already in use", "user_already_exists")
        )
    ):
        log("signup", "Email conflict detected; rotating inbox/email and retrying once.")
        try:
            await browser.stop()
        except Exception:
            pass
        try:
            await mail.inboxes.delete(inbox_id)
        except Exception:
            pass
        inbox_id, created_inbox = await acquire_inbox(mail, reuse_inbox=False)
        identity = generate_identity(inbox_id)
        if args.sticky_inbox:
            _set_site_inbox(_base_domain(args.url), inbox_id)
        log("signup", f"Retry email: {identity.email}")
        browser = await create_cloud_browser(args.proxy_country, email_phase_profile_id)
        signup_task = build_signup_task(
            base_url,
            identity,
            allow_oauth=False,
            oauth_provider=oauth_provider,
        )
        signup = await run_agent_task(
            browser=browser,
            llm=llm,
            label="signup",
            task=signup_task,
            max_steps=args.max_steps,
            timeout_s=args.signup_timeout,
            retries=1,
        )
        signup_status, signup_details = parse_signup_status(signup.output)
        signup_text = (signup.output or "").lower()
        needs_verification = _infer_needs_verification(signup_status, signup_text)
        agent_verify_channel = parse_signup_verify_channel(signup.output)
        verify_channel = choose_verification_channel(
            mode=args.verify_channel,
            agent_channel=agent_verify_channel,
            signup_text=signup_text,
            sms_local_enabled=args.sms_local,
            requires_verification=needs_verification,
        )
        signup_failed_explicit = signup_status == "SIGNUP_FAILED"

    if signup_failed_explicit and args.allow_oauth:
        if not args.profile_id:
            log("signup", "OAuth fallback requested but no --profile-id provided; keeping email-only result.")
        elif _looks_like_oauth_fallback_candidate(signup_status, signup_details, signup_text):
            log("signup", "Email signup failed; running OAuth fallback...")
            if email_phase_profile_id != args.profile_id:
                with contextlib.suppress(Exception):
                    await browser.stop()
                current_profile_id = args.profile_id
                browser = await create_cloud_browser(args.proxy_country, current_profile_id)
            oauth_task = build_signup_task(
                base_url,
                identity,
                allow_oauth=True,
                oauth_provider=oauth_provider,
            )
            signup = await run_agent_task(
                browser=browser,
                llm=llm,
                label="signup-oauth",
                task=oauth_task,
                max_steps=args.max_steps + 2,
                timeout_s=args.signup_timeout,
                retries=1,
            )
            signup_status, signup_details = parse_signup_status(signup.output)
            signup_text = (signup.output or "").lower()
            needs_verification = _infer_needs_verification(signup_status, signup_text)
            agent_verify_channel = parse_signup_verify_channel(signup.output)
            verify_channel = choose_verification_channel(
                mode=args.verify_channel,
                agent_channel=agent_verify_channel,
                signup_text=signup_text,
                sms_local_enabled=args.sms_local,
                requires_verification=needs_verification,
            )
            signup_failed_explicit = signup_status == "SIGNUP_FAILED"

    if signup_failed_explicit:
        if verify_task and not verify_task.done():
            verify_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await verify_task
        with contextlib.suppress(Exception):
            await browser.stop()
        if created_inbox and not args.keep_inbox and not args.sticky_inbox:
            try:
                await mail.inboxes.delete(inbox_id)
            except Exception:
                pass
        elapsed = time.time() - _t0
        print("\n" + "=" * 56, flush=True)
        print(f"RESULTS ({elapsed:.1f}s) model={active_model}", flush=True)
        print("=" * 56, flush=True)
        print(f"URL:         {args.url}", flush=True)
        print(f"EMAIL:       {identity.email}", flush=True)
        print(f"PASSWORD:    {identity.password}", flush=True)
        print(f"USERNAME:    {identity.username}", flush=True)
        print("SIGNUP:      FAILED", flush=True)
        print("VERIFIED:    NO/SKIPPED", flush=True)
        print(f"VERIFY_CH:   {verify_channel if needs_verification else 'NONE'}", flush=True)
        print("LOGIN:       SKIPPED", flush=True)
        print("API_KEY:     NONE", flush=True)
        print("API_KEY_URL: NONE", flush=True)
        print("LOGIN_URL:   NONE", flush=True)
        print(f"NOTES:       {signup_details or 'Signup failed.'}", flush=True)
        print("=" * 56, flush=True)
        with contextlib.suppress(Exception):
            _persist_signup_run(
                runs_db_path,
                {
                    "run_id": run_id,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "url": args.url,
                    "domain": _base_domain(args.url),
                    "model_requested": args.llm,
                    "model_active": active_model,
                    "profile_id": args.profile_id,
                    "inbox_id": inbox_id,
                    "email": identity.email,
                    "password": identity.password,
                    "username": identity.username,
                    "signup_status": signup_status or "SIGNUP_FAILED",
                    "verified": 0,
                    "verify_channel": verify_channel if needs_verification else "NONE",
                    "login_status": "SKIPPED",
                    "api_key": None,
                    "api_key_url": None,
                    "login_url": None,
                    "notes": signup_details or "Signup failed.",
                    "subscription": None,
                    "entitlements": None,
                    "limits_text": None,
                    "profile_name": None,
                    "profile_email": None,
                    "connectors": None,
                    "runtime_seconds": elapsed,
                    "exit_code": 1,
                    "error": signup.error,
                    "signup_output": signup.output,
                    "login_output": None,
                },
            )
        return 1

    verified = False
    verification_link: str | None = None
    verification_code: str | None = None

    if verify_task and needs_verification:
        log(
            "verify",
            (
                "Selected channel="
                f"{verify_channel} (mode={args.verify_channel}, agent={agent_verify_channel or 'UNKNOWN'})"
            ),
        )
        candidate = VerificationCandidate(link=None, code=None, subject=None)
        log("verify", "Waiting for verification signal...")
        if verify_channel in {"EMAIL", "NONE"}:
            try:
                candidate = await asyncio.wait_for(verify_task, timeout=args.verify_timeout)
            except asyncio.TimeoutError:
                candidate = VerificationCandidate(link=None, code=None, subject=None)
                if not verify_task.done():
                    verify_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await verify_task
            if not candidate.link and not candidate.code:
                log("verify", "No candidate from early watcher; doing a fresh verification poll...")
                candidate = await watch_for_verification(
                    mail=mail,
                    inbox_id=inbox_id,
                    target_url=args.url,
                    timeout_s=args.verify_timeout,
                    min_received_at=run_started_at,
                )
            if not candidate.link and not candidate.code and args.sms_local:
                log("verify", "No email candidate; checking local SMS inbox...")
                sms_code = await watch_for_sms_code_local(
                    target_url=args.url,
                    timeout_s=min(args.verify_timeout, args.sms_timeout),
                    min_received_at=run_started_at,
                    db_path=args.sms_db_path,
                    phone_number=args.sms_phone,
                )
                if sms_code:
                    candidate = VerificationCandidate(link=None, code=sms_code, subject="local_sms")
                    verify_channel = "SMS"
                    log("verify", "SMS code candidate found from local Messages DB.")
        else:
            if args.sms_local:
                log("verify", "SMS-first mode: checking local Messages DB...")
                sms_code = await watch_for_sms_code_local(
                    target_url=args.url,
                    timeout_s=min(args.verify_timeout, args.sms_timeout),
                    min_received_at=run_started_at,
                    db_path=args.sms_db_path,
                    phone_number=args.sms_phone,
                )
                if sms_code:
                    candidate = VerificationCandidate(link=None, code=sms_code, subject="local_sms")
                    log("verify", "SMS code candidate found from local Messages DB.")
            if not candidate.link and not candidate.code:
                log("verify", "No SMS code candidate; falling back to email poll...")
                try:
                    candidate = await asyncio.wait_for(verify_task, timeout=args.verify_timeout)
                except asyncio.TimeoutError:
                    candidate = VerificationCandidate(link=None, code=None, subject=None)
                    if not verify_task.done():
                        verify_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await verify_task
                if not candidate.link and not candidate.code:
                    candidate = await watch_for_verification(
                        mail=mail,
                        inbox_id=inbox_id,
                        target_url=args.url,
                        timeout_s=args.verify_timeout,
                        min_received_at=run_started_at,
                    )
                if candidate.link or candidate.code:
                    verify_channel = "EMAIL"

        if (
            not candidate.link
            and not candidate.code
            and args.sms_local
            and args.sms_phone
            and (verify_channel == "SMS" or args.verify_channel == "sms")
        ):
            log("verify", f"No verification candidate yet; attempting on-site SMS trigger to {args.sms_phone}...")
            sms_triggered = await try_trigger_sms_verification(
                browser=browser,
                llm=llm,
                target_url=args.url,
                phone_number=args.sms_phone,
                timeout_s=args.verify_timeout,
            )
            if sms_triggered:
                sms_code = await watch_for_sms_code_local(
                    target_url=args.url,
                    timeout_s=args.sms_timeout,
                    min_received_at=run_started_at,
                    db_path=args.sms_db_path,
                    phone_number=args.sms_phone,
                )
                if sms_code:
                    candidate = VerificationCandidate(link=None, code=sms_code, subject="local_sms_triggered")
                    verify_channel = "SMS"
                    log("verify", "SMS verification code received after trigger.")

        verification_link = candidate.link
        verification_code = candidate.code

        if verification_link:
            ok, final_url = await try_http_verification(verification_link)
            if ok:
                verified = True
                log("verify", f"Verified by direct GET ({final_url})")
            else:
                verify_with_browser = (
                    f"Open this verification link and complete verification: {verification_link}\n"
                    "If asked to log in, use EXACT credentials below (copy text inside backticks only):\n"
                    f"- email: `{identity.email}`\n"
                    f"- password: `{identity.password}`\n"
                    "Never add punctuation or whitespace to credential values.\n"
                    "Stop immediately after reaching a confirmed/verified state.\n"
                    "Do not continue onboarding/profile setup.\n"
                    "Output exactly:\n"
                    "VERIFIED: YES or NO\n"
                    "DETAILS: <short>"
                )
                verify_result = await run_agent_task(
                    browser=browser,
                    llm=llm,
                    label="verify",
                    task=verify_with_browser,
                    max_steps=6,
                    timeout_s=args.verify_timeout,
                )
                if _is_eventbus_stall_text(verify_result.error):
                    await rebuild_browser_session("verify_eventbus_stall")
                flag = _verification_explicit_flag(verify_result.output)
                if flag is not None:
                    verified = flag
                else:
                    verified = _verification_looks_successful(verify_result.output)
        elif verification_code:
            verify_with_browser = (
                f"Enter this 6-digit verification code and submit it: {verification_code}\n"
                "If the code is accepted (or page advances past OTP), stop immediately.\n"
                "If page shows wrong/invalid/expired code, stop immediately.\n"
                "Do not continue onboarding/profile/company steps.\n"
                "Output exactly:\n"
                "VERIFIED: YES or NO\n"
                "DETAILS: <short>"
            )
            verify_result = await run_agent_task(
                browser=browser,
                llm=llm,
                label="verify",
                task=verify_with_browser,
                max_steps=3,
                timeout_s=args.verify_timeout,
            )
            if _is_eventbus_stall_text(verify_result.error):
                await rebuild_browser_session("verify_code_eventbus_stall")
            flag = _verification_explicit_flag(verify_result.output)
            if flag is not None:
                verified = flag
            else:
                verified = _verification_looks_successful(verify_result.output)
        else:
            log("verify", "No link/code found within timeout.")

    if verify_task and not needs_verification and not verify_task.done():
        verify_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await verify_task

    parsed: dict[str, str | None]
    login: RunResult
    snapshot: dict[str, str | None] = {
        "SUBSCRIPTION": None,
        "ENTITLEMENTS": None,
        "LIMITS": None,
        "PROFILE_NAME": None,
        "PROFILE_EMAIL": None,
        "CONNECTORS": None,
        "NOTES": None,
    }
    verification_unresolved = needs_verification and not verified
    if needs_verification and args.skip_verification:
        parsed = {
            "LOGIN": "SKIPPED",
            "API_KEY": "NONE",
            "API_KEY_URL": "NONE",
            "LOGIN_URL": "NONE",
            "NOTES": "Skipped login/API because account requires email verification.",
        }
        login = RunResult(output=None, success=False, steps=0, error="verification_skipped")
    elif verification_unresolved and not args.force_login_on_unverified:
        parsed = {
            "LOGIN": "SKIPPED",
            "API_KEY": "NONE",
            "API_KEY_URL": "NONE",
            "LOGIN_URL": "NONE",
            "NOTES": "Verification unresolved; login/API skipped to avoid wasted time.",
        }
        login = RunResult(output=None, success=False, steps=0, error="verification_unresolved")
    else:
        api_task = f"""
Stay on domain {args.url}. Do not visit email providers.
Do not create files, todo lists, or notes.

1) First determine current auth state.
   If you already see an authenticated app/home/dashboard/workspace page
   (user avatar/profile menu/sidebar/account area), treat LOGIN as SUCCESS
   and skip directly to API key discovery.
2) If not already authenticated, log in with:
   Start at {base_url}, find the correct login path from UI.
- email: `{identity.email}`
- password: `{identity.password}`
Credential rule: copy exact text inside backticks only. Never append punctuation/spaces/quotes.
3) If login needs magic link/2FA, OR page says "No account found"/"Account does not exist", stop immediately.
4) Find API key/access token page fast:
   First try direct paths (same domain only): /api-keys, /settings/api, /settings/api-keys,
   /account/api-keys, /developer/api, /developer/api-keys.
   If those fail, check at most 3 UI areas: Settings, Developer, Integrations.
   Do not spend more than 2 steps on one dead path before trying the next.
5) If existing key is visible in plaintext, copy the FULL secret value.
6) If no existing key is visible, click Create/Generate/New API key/token.
   Use name "sigma" (or any accepted default), submit, and copy the FULL secret shown.
7) If key is masked/obscured, use reveal/copy controls.
   If full plaintext secret is never shown or copied, output API_KEY: NONE.
8) Never use DOM/evaluate tricks to read hidden password fields as "key".
   Ignore Client ID/Client Secret unless they are explicitly labeled as the API key token.
   If UI says "No API keys yet", output API_KEY: NONE unless you actually create one.
9) If required onboarding blocks access (for example create team/workspace/organization),
   complete the minimal onboarding with safe defaults, then continue API key discovery.
10) After creation attempt, verify whether key creation actually succeeded
   (key row appears, success message, or full token shown once).
11) Never invent values. If unsure, output API_KEY: NONE.
12) Output exactly:
LOGIN: SUCCESS or FAILED
API_KEY: <key> or NONE
API_KEY_URL: <url> or NONE
LOGIN_URL: <url> or NONE
NOTES: <short, include method=plaintext|copied|revealed|none>
""".strip()

        login = await run_agent_task(
            browser=browser,
            llm=llm,
            label="login+api",
            task=api_task,
            max_steps=args.max_steps + 4,
            timeout_s=args.login_timeout,
        )
        if _is_eventbus_stall_text(login.error):
            await rebuild_browser_session("login_eventbus_stall")

        parsed = parse_login_output(login.output)
        notes_l = (parsed.get("NOTES") or "").lower()
        login_state = (parsed.get("LOGIN") or "").strip().upper()
        blocked_unconfirmed = (
            login_state == "FAILED"
            and any(k in notes_l for k in ("not confirmed", "confirm your email", "email not verified", "verify your email"))
        )
        if blocked_unconfirmed and needs_verification and not verified:
            log("verify", "Login blocked by unconfirmed email; polling inbox again and retrying verification...")
            late_candidate = await watch_for_verification(
                mail=mail,
                inbox_id=inbox_id,
                target_url=args.url,
                timeout_s=max(20, args.verify_timeout),
                min_received_at=run_started_at,
            )
            if late_candidate.link:
                ok, final_url = await try_http_verification(late_candidate.link)
                if ok:
                    verified = True
                    verify_channel = "EMAIL"
                    log("verify", f"Late verification succeeded via direct GET ({final_url})")
                else:
                    verify_with_browser = (
                        f"Open this verification link and complete verification: {late_candidate.link}\n"
                        "If asked to log in, use EXACT credentials below (copy text inside backticks only):\n"
                        f"- email: `{identity.email}`\n"
                        f"- password: `{identity.password}`\n"
                        "Never add punctuation or whitespace to credential values.\n"
                        "Stop immediately after reaching a confirmed/verified state.\n"
                        "Output exactly:\n"
                        "VERIFIED: YES or NO\n"
                        "DETAILS: <short>"
                    )
                    verify_result = await run_agent_task(
                        browser=browser,
                        llm=llm,
                        label="verify-retry",
                        task=verify_with_browser,
                        max_steps=5,
                        timeout_s=args.verify_timeout,
                    )
                    if _is_eventbus_stall_text(verify_result.error):
                        await rebuild_browser_session("verify_retry_eventbus_stall")
                    flag = _verification_explicit_flag(verify_result.output)
                    verified = flag if flag is not None else _verification_looks_successful(verify_result.output)
            elif late_candidate.code:
                verify_with_browser = (
                    f"Enter this verification code and submit it: {late_candidate.code}\n"
                    "If accepted, output VERIFIED: YES.\n"
                    "If wrong/expired, output VERIFIED: NO."
                )
                verify_result = await run_agent_task(
                    browser=browser,
                    llm=llm,
                    label="verify-retry",
                    task=verify_with_browser,
                    max_steps=3,
                    timeout_s=args.verify_timeout,
                )
                if _is_eventbus_stall_text(verify_result.error):
                    await rebuild_browser_session("verify_retry_code_eventbus_stall")
                flag = _verification_explicit_flag(verify_result.output)
                verified = flag if flag is not None else _verification_looks_successful(verify_result.output)

            if verified:
                log("login+api", "Verification recovered; retrying login/API once...")
                login = await run_agent_task(
                    browser=browser,
                    llm=llm,
                    label="login+api-retry",
                    task=api_task,
                    max_steps=args.max_steps + 4,
                    timeout_s=args.login_timeout,
                    retries=1,
                )
                if _is_eventbus_stall_text(login.error):
                    await rebuild_browser_session("login_retry_eventbus_stall")
                parsed = parse_login_output(login.output)

        # Login succeeded (or auth state strongly implied) but key not captured:
        # run a focused API-key-only recovery pass.
        login_state_now = (parsed.get("LOGIN") or "").strip().upper()
        auth_state_implied = output_indicates_authenticated_state(login.output, parsed.get("NOTES"))
        parsed_key = sanitize_api_key_candidate(parsed.get("API_KEY"), parsed.get("NOTES"))
        if (login_state_now == "SUCCESS" or auth_state_implied) and not parsed_key:
            if auth_state_implied and login_state_now != "SUCCESS":
                log("login+api", "Detected authenticated state despite LOGIN=FAILED; forcing API-only recovery.")
            api_url_hint = parsed.get("API_KEY_URL") or base_url
            api_only_task = f"""
Stay on domain {args.url}. You are already logged in.
Open {api_url_hint} (or nearest API/settings page) and ONLY handle API key extraction.
Do not visit email providers. Do not log out. Do not create files/todos.

1) Go to API key/token settings.
2) If key already exists and full plaintext value is visible, copy it.
3) If no key exists, create one (name "sigma" or default), then copy full plaintext key shown.
4) If required onboarding blocks access (create team/workspace/organization), complete it with safe defaults.
5) If key is masked, use reveal/copy controls. If full secret is never shown, output NONE.
6) Never invent key text or use hidden password-field DOM values as key.
Output exactly:
LOGIN: SUCCESS or FAILED
API_KEY: <key> or NONE
API_KEY_URL: <url> or NONE
LOGIN_URL: <url> or NONE
NOTES: <short, include method=plaintext|copied|revealed|none>
""".strip()
            api_only = await run_agent_task(
                browser=browser,
                llm=llm,
                label="api-only",
                task=api_only_task,
                max_steps=6,
                timeout_s=min(90, args.login_timeout),
                retries=1,
            )
            if _is_eventbus_stall_text(api_only.error):
                await rebuild_browser_session("api_only_eventbus_stall")
            parsed_api_only = parse_login_output(api_only.output)
            api_only_key = sanitize_api_key_candidate(parsed_api_only.get("API_KEY"), parsed_api_only.get("NOTES"))
            if api_only_key:
                parsed = parsed_api_only
            elif auth_state_implied:
                merged_notes = (parsed.get("NOTES") or "").strip()
                suffix = "Auth state inferred from dashboard redirect; API key still not found."
                parsed["LOGIN"] = "SUCCESS"
                parsed["NOTES"] = f"{merged_notes} | {suffix}".strip(" |")

        login_ok = (parsed.get("LOGIN") or "").strip().upper() == "SUCCESS" or output_indicates_authenticated_state(
            login.output, parsed.get("NOTES")
        )
        if login_ok and args.capture_summary:
            snapshot_task = f"""
Stay on domain {args.url}. Do not log out.
Extract account snapshot data from the currently logged-in UI.
Prioritize account/billing/usage pages over generic dashboard text.
Do not create files, todos, or notes.

Do this:
1) Open account/plan/billing/usage pages from UI navigation.
2) If buttons/links like "Manage Plan", "View Balance", "Usage", "Billing", "Plan" exist, click them.
3) Capture exact visible numeric balances/quotas/counters/currency values (for example remaining credits, monthly suggestion counts, usage windows).
4) If multiple limits are shown, combine them in LIMITS as semicolon-separated facts.
5) Prefer concrete numbers over generic wording.

Output exactly:
SUBSCRIPTION: <plan/tier/status> or NONE
ENTITLEMENTS: <included features/credits with numbers when visible> or NONE
LIMITS: <exact numeric limits/remaining balances shown> or NONE
PROFILE_NAME: <name> or NONE
PROFILE_EMAIL: <email> or NONE
CONNECTORS: <comma-separated connector names> or NONE
NOTES: <short>
""".strip()
            snapshot_run = await run_agent_task(
                browser=browser,
                llm=llm,
                label="snapshot",
                task=snapshot_task,
                max_steps=6,
                timeout_s=60,
            )
            snapshot = parse_account_snapshot(snapshot_run.output)
    api_key = sanitize_api_key_candidate(parsed.get("API_KEY"), parsed.get("NOTES"))

    await browser.stop()
    if created_inbox and not args.keep_inbox and not args.sticky_inbox:
        try:
            await mail.inboxes.delete(inbox_id)
        except Exception:
            pass

    elapsed = time.time() - _t0
    print("\n" + "=" * 56, flush=True)
    print(f"RESULTS ({elapsed:.1f}s) model={active_model}", flush=True)
    print("=" * 56, flush=True)
    print(f"URL:         {args.url}", flush=True)
    print(f"EMAIL:       {identity.email}", flush=True)
    print(f"PASSWORD:    {identity.password}", flush=True)
    print(f"USERNAME:    {identity.username}", flush=True)
    print(f"SIGNUP:      {'OK' if signup.success else 'FAILED'}", flush=True)
    print(f"VERIFIED:    {'YES' if verified else 'NO/SKIPPED'}", flush=True)
    print(f"VERIFY_CH:   {verify_channel if needs_verification else 'NONE'}", flush=True)
    login_field = (parsed["LOGIN"] or "").strip().upper()
    display_login = login_field if login_field in {"SUCCESS", "FAILED", "SKIPPED"} else "FAILED"
    print(f"LOGIN:       {display_login}", flush=True)
    print(f"API_KEY:     {api_key or 'NONE'}", flush=True)
    print(f"API_KEY_URL: {parsed['API_KEY_URL'] or 'NONE'}", flush=True)
    print(f"LOGIN_URL:   {parsed['LOGIN_URL'] or 'NONE'}", flush=True)
    print(f"NOTES:       {parsed['NOTES'] or '-'}", flush=True)
    if args.capture_summary:
        print(f"SUBSCRIPTN:  {snapshot['SUBSCRIPTION'] or 'NONE'}", flush=True)
        print(f"ENTITLEMNT:  {snapshot['ENTITLEMENTS'] or 'NONE'}", flush=True)
        print(f"LIMITS:      {snapshot['LIMITS'] or 'NONE'}", flush=True)
        print(f"PROFILE:     {snapshot['PROFILE_NAME'] or 'NONE'} <{snapshot['PROFILE_EMAIL'] or 'NONE'}>", flush=True)
        print(f"CONNECTORS:  {snapshot['CONNECTORS'] or 'NONE'}", flush=True)
        print(f"SNAP_NOTES:  {snapshot['NOTES'] or '-'}", flush=True)
    print("=" * 56, flush=True)
    with contextlib.suppress(Exception):
        _persist_signup_run(
            runs_db_path,
            {
                "run_id": run_id,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "url": args.url,
                "domain": _base_domain(args.url),
                "model_requested": args.llm,
                "model_active": active_model,
                "profile_id": args.profile_id,
                "inbox_id": inbox_id,
                "email": identity.email,
                "password": identity.password,
                "username": identity.username,
                "signup_status": signup_status or ("SIGNUP_SUCCESS" if signup.success else "SIGNUP_FAILED"),
                "verified": 1 if verified else 0,
                "verify_channel": verify_channel if needs_verification else "NONE",
                "login_status": display_login,
                "api_key": api_key,
                "api_key_url": parsed["API_KEY_URL"],
                "login_url": parsed["LOGIN_URL"],
                "notes": parsed["NOTES"],
                "subscription": snapshot["SUBSCRIPTION"] if args.capture_summary else None,
                "entitlements": snapshot["ENTITLEMENTS"] if args.capture_summary else None,
                "limits_text": snapshot["LIMITS"] if args.capture_summary else None,
                "profile_name": snapshot["PROFILE_NAME"] if args.capture_summary else None,
                "profile_email": snapshot["PROFILE_EMAIL"] if args.capture_summary else None,
                "connectors": snapshot["CONNECTORS"] if args.capture_summary else None,
                "runtime_seconds": elapsed,
                "exit_code": 0,
                "error": login.error if login.error else signup.error,
                "signup_output": signup.output,
                "login_output": login.output,
            },
        )
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sigma Super signup pipeline")
    p.add_argument("url", help="Target site URL")
    p.add_argument(
        "--profile-id",
        default=None,
        help="Browser Use profile id with pre-authenticated cookies/session (e.g., Google logged in).",
    )
    p.add_argument(
        "--allow-oauth",
        action="store_true",
        help="Two-stage auth: try email signup first, then OAuth fallback if email path fails.",
    )
    p.add_argument(
        "--oauth-provider",
        default="auto",
        choices=["auto", "google", "github"],
        help="OAuth fallback preference when --allow-oauth is enabled.",
    )
    p.add_argument(
        "--llm",
        default="browseruse-fast",
        help=(
            "Preset/model: "
            "openai-best(gpt-5.2), openai-fast(gpt-5-mini), "
            "openai-ultrafast(gpt-5-nano), browseruse-fast(bu-2-0), "
            "or raw model id"
        ),
    )
    p.add_argument("--max-steps", type=int, default=10)
    p.add_argument("--signup-timeout", type=int, default=180)
    p.add_argument("--login-timeout", type=int, default=140)
    p.add_argument("--verify-timeout", type=int, default=25)
    p.add_argument(
        "--verify-channel",
        default="auto",
        choices=["auto", "email", "sms"],
        help="Verification routing mode: auto (agent-guided), email-first, or sms-first.",
    )
    p.add_argument(
        "--sms-local",
        action="store_true",
        help="Enable local macOS Messages DB OTP fallback when email verification is unavailable.",
    )
    p.add_argument(
        "--sms-phone",
        default=None,
        help="Your phone number (optional signal booster for local SMS OTP matching).",
    )
    p.add_argument(
        "--sms-timeout",
        type=int,
        default=35,
        help="Max seconds to poll local Messages DB for SMS OTP (default: 35).",
    )
    p.add_argument(
        "--sms-db-path",
        default="~/Library/Messages/chat.db",
        help="Path to local macOS Messages SQLite database.",
    )
    p.add_argument("--skip-verification", action="store_true")
    p.add_argument(
        "--fail-fast-captcha",
        dest="fail_fast_captcha",
        action="store_true",
        default=False,
        help="Stop immediately after first captcha failure/time-out.",
    )
    p.add_argument(
        "--no-fail-fast-captcha",
        dest="fail_fast_captcha",
        action="store_false",
        help="Allow retries/rebuild even after captcha failure (default).",
    )
    p.add_argument(
        "--retry-on-email-conflict",
        dest="retry_on_email_conflict",
        action="store_true",
        default=True,
        help="If signup says email already exists, rotate to a new inbox/email and retry once (default on).",
    )
    p.add_argument(
        "--no-retry-on-email-conflict",
        dest="retry_on_email_conflict",
        action="store_false",
        help="Disable auto-retry when email already exists.",
    )
    p.add_argument(
        "--force-login-on-unverified",
        dest="force_login_on_unverified",
        action="store_true",
        default=False,
        help="Attempt login/API even when verification is unresolved (default off).",
    )
    p.add_argument(
        "--no-force-login-on-unverified",
        dest="force_login_on_unverified",
        action="store_false",
        help="Skip login/API when verification is unresolved.",
    )
    p.add_argument("--reuse-inbox", action="store_true")
    p.add_argument(
        "--sticky-inbox",
        dest="sticky_inbox",
        action="store_true",
        default=True,
        help="Reuse one inbox per website/domain (default on).",
    )
    p.add_argument(
        "--no-sticky-inbox",
        dest="sticky_inbox",
        action="store_false",
        help="Disable per-website sticky inbox behavior.",
    )
    p.add_argument("--keep-inbox", action="store_true")
    p.add_argument(
        "--capture-summary",
        dest="capture_summary",
        action="store_true",
        default=True,
        help="After successful login, capture account stats/profile/connectors (default on).",
    )
    p.add_argument(
        "--no-capture-summary",
        dest="capture_summary",
        action="store_false",
        help="Disable post-login account snapshot extraction.",
    )
    p.add_argument("--proxy-country", default="us")
    p.add_argument(
        "--results-db",
        default=str(RUNS_DB_FILE),
        help="SQLite path for storing run outputs/credentials (default: codex_super/state/signup_runs.db).",
    )
    return p.parse_args()


def main() -> None:
    global _t0
    _t0 = time.time()
    args = parse_args()
    code = asyncio.run(run_signup_pipeline(args))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
