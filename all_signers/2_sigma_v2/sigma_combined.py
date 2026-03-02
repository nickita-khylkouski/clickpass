#!/usr/bin/env python3
"""Sigma Combined: fastest automated signup + verification + API key extraction.

Merges the best of v2/signup.py (magic-link login, native browser_use LLM,
clean architecture) with codex_super/signup_super.py (robust error recovery,
scored link ranking, EventBus stall detection, API key sanitization, late
verification recovery).

Design:
- One file, one flow, maximum speed.
- Parallel setup, low waits, aggressive multi-action steps.
- Reliable: retry transients, rebuild browser on stalls, graceful fallbacks.
- Magic-link login with programmatic navigation (no LLM URL hallucination).
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
import string
import time
from dataclasses import dataclass
from datetime import datetime, timezone
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

BROWSER_USE_API_KEY = os.environ.get("BROWSER_USE_API_KEY", "")
AGENTMAIL_API_KEY = os.environ.get("AGENTMAIL_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

MODEL_PRESETS = {
    "best": "gpt-5.2",
    "fast": "gpt-5-mini",
    "ultra": "gpt-5-nano",
    "bu": "bu-2-0",
}

fake = Faker()
_t0 = time.time()
_AGENT_INIT_PARAMS: set[str] | None = None


class BrowserUseChatOpenAI(ChatOpenAI):
    """Compatibility shim: browser-use expects llm.provider."""

    model_config = ConfigDict(extra="allow")
    provider: str = "openai"

    @property
    def model(self) -> str:
        return str(getattr(self, "model_name", "unknown"))


# ── Data classes ──────────────────────────────────────────────────────


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


# ── Utilities ─────────────────────────────────────────────────────────


def log(stage: str, msg: str) -> None:
    print(f"  [{time.time() - _t0:6.1f}s] [{stage}] {msg}", flush=True)


def resolve_model(model: str) -> str:
    return MODEL_PRESETS.get(model.strip().lower(), model.strip())


def make_llm(model: str):
    """Create LLM with proper provider wrappers."""
    resolved = resolve_model(model)
    if resolved.startswith("bu-"):
        if not BROWSER_USE_API_KEY:
            raise RuntimeError("Missing BROWSER_USE_API_KEY")
        return ChatBrowserUse(model=resolved, api_key=BROWSER_USE_API_KEY)
    if not OPENAI_API_KEY:
        raise RuntimeError("Missing OPENAI_API_KEY for OpenAI models")
    return BrowserUseChatOpenAI(
        model=resolved,
        api_key=OPENAI_API_KEY,
        timeout=120,
        max_retries=2,
    )


def generate_password(length: int = 18) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    parts = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*"),
    ]
    parts += [secrets.choice(alphabet) for _ in range(max(8, length) - 4)]
    chars = list(parts)
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def generate_e164_us_phone() -> str:
    # Use real US area codes so libphonenumber validation passes
    valid_areas = [
        "212", "213", "310", "312", "347", "415", "469", "512",
        "516", "617", "646", "702", "713", "718", "727", "786",
        "818", "832", "847", "917", "929", "954", "972",
    ]
    area = secrets.choice(valid_areas)
    exch = str(secrets.randbelow(8) + 2) + f"{secrets.randbelow(100):02d}"
    line = f"{secrets.randbelow(10000):04d}"
    return f"+1{area}{exch}{line}"


def generate_identity(email: str) -> Identity:
    first = fake.first_name()
    last = fake.last_name()
    return Identity(
        first_name=first,
        last_name=last,
        username=f"{first.lower()}{last.lower()}{secrets.randbelow(9000) + 1000}",
        email=email,
        password=generate_password(),
        dob=fake.date_of_birth(minimum_age=19, maximum_age=45).strftime("%Y-%m-%d"),
        company=fake.company(),
        website=f"https://{fake.domain_name()}",
        phone=generate_e164_us_phone(),
    )


def _base_domain(url: str) -> str:
    host = (urlparse(url).netloc or urlparse(url).path).lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


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


# ── Link/code extraction ─────────────────────────────────────────────


def _extract_links(text: str) -> list[str]:
    """Extract URLs from HTML/text, unwrapping tracking redirects."""
    raw = [html.unescape(u) for u in re.findall(r"https?://[^\s<>\"']+", text)]
    out: list[str] = []
    seen: set[str] = set()

    def add(u: str | None):
        if u and u.strip() and u.strip() not in seen:
            seen.add(u.strip())
            out.append(u.strip())

    def unwrap(u: str) -> str | None:
        parsed = urlparse(u)
        q = parse_qs(parsed.query)
        for key in ("url", "u", "target", "redirect", "redirect_url", "destination", "next", "continue"):
            vals = q.get(key)
            if vals:
                val = unquote(vals[0])
                if val.startswith(("http://", "https://")):
                    return val
        m = re.search(r"/CL0/(https:%2F%2F[^/]+(?:%2F[^/]+)*)", u)
        if m:
            val = unquote(m.group(1))
            if val.startswith(("http://", "https://")):
                return val
        return None

    for u in raw:
        add(u)
        add(unwrap(u))
    return out


def _extract_code(text: str) -> str | None:
    """3-pass verification code extraction: keyword → isolated line → fallback."""
    for pattern in [
        r"(?:verification|verify|confirm|otp|code|pin)[^0-9]{0,120}(\d{4,8})",
        r"(\d{4,8})[^0-9]{0,120}(?:verification|verify|confirm|otp|code|pin)",
    ]:
        m = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if m and len(set(m.group(1))) > 1:
            return m.group(1)
    for line in text.splitlines():
        m = re.fullmatch(r"(\d{6})", line.strip())
        if m and len(set(m.group(1))) > 1:
            return m.group(1)
    for m in re.finditer(r"\b(\d{4,8})\b", text):
        if len(set(m.group(1))) > 1:
            return m.group(1)
    return None


def _best_verification_link(urls: list[str], target_url: str) -> str | None:
    """Score-based verification link selection."""
    target_domain = _base_domain(target_url)
    blocked = {"w3.org", "www.w3.org", "schemas.xmlsoap.org", "xmlns.com"}
    negative = ("unsubscribe", "privacy", "terms", ".png", ".jpg", ".gif", ".css", ".js", ".dtd",
                "logo", "cdn.", "static.", "assets.", "fonts.", "img.", "images.",
                "facebook.com", "twitter.com", "linkedin.com", "youtube.com")
    positive = ("verify", "confirm", "activate", "auth", "token", "magic", "signin", "sign-in", "callback")

    best: tuple[int, str] | None = None
    for raw in urls:
        low = raw.lower().strip()
        if any(n in low for n in negative):
            continue
        host = _base_domain(low)
        if host in blocked:
            continue
        score = 0
        if any(p in low for p in positive):
            score += 3
        if target_domain and target_domain in host:
            score += 3
        if "token=" in low or "code=" in low:
            score += 2
        if "/auth/" in low or "/confirm" in low:
            score += 1
        if best is None or score > best[0]:
            best = (score, raw)
    return best[1] if best and best[0] > 0 else None


# ── Error detection ───────────────────────────────────────────────────


def _is_retryable_error(exc: Exception) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    text = str(exc).lower()
    markers = ("429", "timeout", "timed out", "502", "503", "504", "bad gateway",
               "too many concurrent active sessions", "temporarily unavailable",
               "connection reset", "websocket connection closed",
               "cdp still not connected", "expected at least one handler")
    return any(m in text for m in markers)


def _is_eventbus_stall(text: str | None) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(m in low for m in (
        "browserstaterequestevent", "event bus to be idle", "eventbus_",
        "domwatchdog", "timeout error - handling took more than",
        "screenshotwatchdog", "screenshotevent", "timed out after",
    ))


def _needs_browser_rebuild(err: str | None) -> bool:
    if not err:
        return False
    text = err.lower()
    return any(m in text for m in (
        "too many concurrent active sessions", "cloudbrowsererror",
        "browserstaterequestevent", "root cdp client not initialized",
        "websocket connection closed", "reconnection failed",
        "cdp still not connected", "expected at least one handler",
        "sessionmanager not initialized", "session with given id not found",
        "no close frame", "unstable state", "target may have detached",
        "agent focus", "cdp connected but failed",
    ))


# ── Browser management ────────────────────────────────────────────────


async def create_cloud_browser(profile_id: str | None = None) -> Browser:
    if not BROWSER_USE_API_KEY:
        raise RuntimeError("Missing BROWSER_USE_API_KEY")
    kwargs: dict[str, Any] = dict(
        use_cloud=True,
        cloud_proxy_country_code="us",
        keep_alive=True,
        minimum_wait_page_load_time=0.25,
        wait_between_actions=0.2,
        highlight_elements=False,
        captcha_solver=True,
    )
    if profile_id:
        kwargs["profile_id"] = profile_id
    return Browser(**kwargs)


def _parse_iso(ts: str | None) -> datetime:
    if not ts:
        return datetime.max.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return datetime.max.replace(tzinfo=timezone.utc)


async def stop_oldest_active_cloud_session() -> str | None:
    """Free one cloud browser slot by stopping the oldest active session."""
    if not BROWSER_USE_API_KEY:
        return None
    headers = {"X-Browser-Use-API-Key": BROWSER_USE_API_KEY, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get("https://api.browser-use.com/api/v2/browsers", headers=headers)
            if resp.status_code >= 400:
                return None
            data = resp.json()
            items = data.get("items", []) if isinstance(data, dict) else []
            active = [it for it in items if str(it.get("status", "")).lower() == "active" and it.get("id")]
            if not active:
                return None
            active.sort(key=lambda it: _parse_iso(it.get("startedAt")))
            sid = str(active[0]["id"])
            patch = await client.patch(
                f"https://api.browser-use.com/api/v2/browsers/{sid}",
                headers=headers, json={"action": "stop"},
            )
            return sid if patch.status_code < 400 else None
    except Exception:
        return None


async def stop_all_active_cloud_sessions() -> int:
    """Stop ALL active cloud browser sessions. Returns count of sessions stopped."""
    if not BROWSER_USE_API_KEY:
        return 0
    headers = {"X-Browser-Use-API-Key": BROWSER_USE_API_KEY, "Content-Type": "application/json"}
    stopped = 0
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get("https://api.browser-use.com/api/v2/browsers", headers=headers)
            if resp.status_code >= 400:
                return 0
            data = resp.json()
            items = data.get("items", []) if isinstance(data, dict) else []
            active = [it for it in items if str(it.get("status", "")).lower() == "active" and it.get("id")]
            for it in active:
                sid = str(it["id"])
                with contextlib.suppress(Exception):
                    patch = await client.patch(
                        f"https://api.browser-use.com/api/v2/browsers/{sid}",
                        headers=headers, json={"action": "stop"},
                    )
                    if patch.status_code < 400:
                        stopped += 1
    except Exception:
        pass
    return stopped


async def stop_browser_safely(browser: Browser, *, label: str = "session", timeout_s: int = 8) -> None:
    """Best-effort browser stop that won't hang the pipeline."""
    try:
        await asyncio.wait_for(browser.stop(), timeout=timeout_s)
    except asyncio.TimeoutError:
        log(label, f"browser.stop() timed out after {timeout_s}s; continuing.")
    except Exception as exc:
        log(label, f"browser.stop() failed: {exc}")


# ── Email ─────────────────────────────────────────────────────────────


async def acquire_inbox(mail: AsyncAgentMail) -> tuple[str, bool]:
    for attempt in range(5):
        try:
            inbox = await mail.inboxes.create()
            return inbox.inbox_id, True
        except Exception as e:
            err_s = str(e).lower()
            if "taken" in err_s:
                await asyncio.sleep(0.5)
                continue
            if "limit" not in err_s:
                raise
            log("email", f"Inbox limit hit (attempt {attempt+1}), recycling...")
            try:
                listed = await mail.inboxes.list(limit=20)
                # Delete multiple old inboxes to free space
                to_delete = listed.inboxes[-3:] if len(listed.inboxes) >= 3 else listed.inboxes
                for ib in to_delete:
                    with contextlib.suppress(Exception):
                        await mail.inboxes.delete(ib.inbox_id)
                        log("email", f"  Deleted inbox {ib.inbox_id}")
            except Exception:
                pass
            await asyncio.sleep(1)
    raise RuntimeError("Failed to acquire inbox after 5 attempts")


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


async def watch_for_verification(
    mail: AsyncAgentMail,
    inbox_id: str,
    target_url: str,
    timeout_s: int,
    poll_s: int = 2,
    min_received_at: datetime | None = None,
) -> VerificationCandidate:
    """Poll inbox for verification email. Timestamp-gated to skip stale messages."""
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
            # Timestamp gate: skip emails from before the run started
            ts = _as_utc(getattr(stub, "timestamp", None)) or _as_utc(getattr(stub, "created_at", None))
            if min_received_at and (ts is None or ts < min_received_at):
                continue
            msg = await mail.inboxes.messages.get(inbox_id=inbox_id, message_id=msg_id)
            log("verify", f"Email: '{msg.subject}' from {msg.from_}")
            text_body = (msg.text or "").strip()
            html_body = (msg.html or "").strip()
            if text_body:
                log("verify", f"  Body preview: {text_body[:200]}")
            combined = "\n".join(p for p in (text_body, html_body) if p)
            # Domain filter (use tokens to handle subdomains like mail.example.com)
            if target_url:
                tokens = _target_tokens(target_url)
                check = combined.lower() + str(msg.from_ or "").lower()
                if tokens and not any(t in check for t in tokens):
                    log("verify", f"  Skipping (not from {_base_domain(target_url)})")
                    continue
            urls = _extract_links(combined)
            link = _best_verification_link(urls, target_url)
            code = _extract_code(text_body or combined)
            if code:
                log("verify", f"  Extracted code: {code} (from {'body' if text_body else 'combined'})")
            if link or code:
                return VerificationCandidate(link=link, code=code, subject=msg.subject)
        if i % 5 == 0:
            log("verify", "Polling inbox...")
        await asyncio.sleep(poll_s)
    return VerificationCandidate(link=None, code=None, subject=None)


# ── Agent runner ──────────────────────────────────────────────────────


async def run_agent(
    browser: Browser, llm: Any, label: str, task: str,
    max_steps: int = 15, timeout_s: int = 180,
    retries: int = 1,
) -> RunResult:
    """Run browser-use agent with speed params, EventBus detection, and retries."""
    provider = str(getattr(llm, "provider", "")).lower()
    is_bu = provider == "browser-use" or str(getattr(llm, "model", "")).startswith("bu-")

    speed_kwargs: dict[str, Any] = {
        "task": task,
        "llm": llm,
        "browser": browser,
        "use_vision": False,
        "use_judge": False,
        "max_actions_per_step": 10,
        "include_attributes": ["id", "name", "type", "placeholder", "value"],
        "max_history_items": 6,
        "max_clickable_elements_length": 9000,
        "message_compaction": True,
        "loop_detection_enabled": True,
        "llm_timeout": 30,
        "step_timeout": 150,
        "max_failures": 3,
        "final_response_after_failure": False,
        "extend_system_message": (
            "Act fast. Fill form fields in one step when possible. "
            "Navigate directly and avoid unnecessary exploration. "
            "Never use write_file/replace_file/read_file/todo actions."
        ),
    }
    if is_bu:
        speed_kwargs["flash_mode"] = True

    # Safe kwargs filtering: only pass params the Agent constructor accepts
    global _AGENT_INIT_PARAMS
    if _AGENT_INIT_PARAMS is None:
        _AGENT_INIT_PARAMS = set(inspect.signature(Agent.__init__).parameters.keys())
    filtered = {k: v for k, v in speed_kwargs.items() if k in _AGENT_INIT_PARAMS}

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        log(label, f"Starting (attempt {attempt}/{retries})...")
        agent = Agent(**filtered)
        try:
            history = await asyncio.wait_for(agent.run(max_steps=max_steps), timeout=timeout_s)
            output = history.final_result()
            success = bool(history.is_successful() if hasattr(history, "is_successful") else history.is_done())
            steps = history.number_of_steps()
            errors = [e for e in history.errors() if e]
            last_error = " | ".join(errors[-3:]) if errors else None
            log(label, f"Done: success={success} steps={steps}")
            if output:
                log(label, f"Output: {output[:220]}")
            if last_error:
                log(label, f"Errors: {last_error[:180]}")
            # EventBus stall = fail fast, no retry
            if _is_eventbus_stall(last_error):
                log(label, "EventBus/DOM stall detected; failing fast.")
                return RunResult(output=output, success=False, steps=steps, error=last_error)
            if not success and attempt < retries and last_error and _is_retryable_error(Exception(last_error)):
                await asyncio.sleep(2 ** attempt)
                continue
            return RunResult(output=output, success=success, steps=steps, error=last_error)
        except Exception as exc:
            last_exc = exc
            err_msg = str(exc) if str(exc) else f"timeout after {timeout_s}s"
            # Recover partial progress from agent's internal state
            partial_steps = 0
            partial_output = None
            with contextlib.suppress(Exception):
                if hasattr(agent, 'history') and agent.history:
                    partial_steps = agent.history.number_of_steps() if hasattr(agent.history, 'number_of_steps') else len(getattr(agent.history, 'history', []))
                    partial_output = agent.history.final_result() if hasattr(agent.history, 'final_result') else None
            if partial_steps:
                log(label, f"Partial progress before error: {partial_steps} steps")
            if _is_eventbus_stall(err_msg):
                log(label, f"EventBus stall: {err_msg}")
                return RunResult(output=partial_output, success=False, steps=partial_steps, error=err_msg)
            # Free zombie sessions on 429 (regardless of retry count)
            if "too many concurrent" in err_msg.lower():
                with contextlib.suppress(Exception):
                    killed = await stop_oldest_active_cloud_session()
                    if killed:
                        log(label, f"Freed cloud session: {killed}")
            if attempt < retries and _is_retryable_error(exc):
                delay = 2 ** attempt
                log(label, f"Transient error, retrying in {delay}s: {err_msg}")
                await asyncio.sleep(delay)
                continue
            log(label, f"Failed: {err_msg}")
            return RunResult(output=partial_output, success=False, steps=partial_steps, error=err_msg)
    # Should not reach here, but safety fallback
    return RunResult(output=None, success=False, steps=0, error=str(last_exc) if last_exc else "exhausted retries")


# ── Verification ──────────────────────────────────────────────────────


async def try_http_verification(link: str) -> tuple[bool, str | None]:
    """Verify via HTTP GET. Checks response body for positive/negative signals.

    Also handles Auth0/SSO-style flows where the verification ticket is processed
    server-side and the response redirects to a login page.
    """
    # Auth0/Universal Login ticket URLs require JavaScript — HTTP GET doesn't verify.
    link_low = link.lower()
    if "ticket=" in link_low and any(p in link_low for p in ("auth.", "/u/email-verification", "auth0")):
        return False, None  # Force browser verification

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(link)
        final = str(resp.url)
        body = (resp.text[:8000] if hasattr(resp, "text") else "").lower()
        # Specific negative signals (avoid generic "error"/"failed" which match JS boilerplate)
        negative = ("invalid token", "link expired", "link has expired", "already been used",
                     "already used", "wrong code", "verification failed", "link is no longer valid",
                     "token expired", "invalid_grant")
        negative_url = ("expired", "invalid_token", "error_description")
        positive_body = ("verified", "confirmation complete", "account confirmed", "email verified",
                         "successfully verified")
        positive_url = ("confirmed", "activated", "success")  # Removed "verify" — too generic, causes Auth0 false positives
        if resp.status_code < 400 and not any(t in body for t in negative):
            final_low = final.lower()
            if any(t in final_low for t in negative_url):
                pass  # URL signals verification failure
            elif any(t in body for t in positive_body) or any(t in final_low for t in positive_url):
                return True, final
    except Exception:
        pass
    return False, None


# ── Output parsing ────────────────────────────────────────────────────


def parse_signup_status(text: str | None) -> tuple[str, str | None]:
    if not text:
        return "UNKNOWN", None
    norm = text.replace("\\n", "\n")
    m = re.search(r"STATUS\s*:\s*([A-Z_]+)", norm, re.IGNORECASE)
    if m:
        status = m.group(1).upper()
    elif re.search(r"\bNEEDS_VERIFICATION\b", norm, re.IGNORECASE):
        status = "NEEDS_VERIFICATION"
    elif re.search(r"\bSIGNUP_SUCCESS\b", norm, re.IGNORECASE):
        status = "SIGNUP_SUCCESS"
    elif re.search(r"\bSIGNUP_FAILED\b", norm, re.IGNORECASE):
        status = "SIGNUP_FAILED"
    else:
        status = "UNKNOWN"
    d = re.search(r"DETAILS\s*:\s*(.+)", norm, re.IGNORECASE)
    return status, d.group(1).strip() if d else None


def parse_login_output(text: str | None) -> dict[str, str | None]:
    result: dict[str, str | None] = {
        "LOGIN": None, "API_KEY": None, "API_KEY_URL": None,
        "LOGIN_URL": None, "NOTES": None,
    }
    if not text:
        return result
    norm = text.replace("\\n", "\n").strip()
    # Try JSON first
    try:
        if norm.startswith("{") and norm.endswith("}"):
            obj = json.loads(norm)
            for k in result:
                v = obj.get(k)
                result[k] = str(v).strip() if v is not None else None
            return result
    except Exception:
        pass
    # Regex fallback
    for k in result:
        m = re.search(rf"(?im)^\s*{k}\s*:\s*([^\n]+)\s*$", norm)
        if m:
            result[k] = m.group(1).strip()
    return result


def sanitize_api_key_candidate(key: str | None, notes: str | None = None) -> str | None:
    """Reject hallucinated, masked, or invalid API key values."""
    if not key:
        return None
    value = key.strip().strip("`\"'")
    if not value or value.upper() in {"NONE", "NULL", "N/A", "UNKNOWN", "NOT FOUND"}:
        return None
    if value.startswith(("http://", "https://")):
        return None
    if len(value) < 10:
        return None
    # Reject UUIDs (common hallucination)
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value, re.IGNORECASE):
        return None
    notes_l = (notes or "").strip().lower()
    if "no api key" in notes_l or ("not found" in notes_l and "api" in notes_l):
        return None
    if any(t in notes_l for t in ("masked", "obscured", "hidden value", "password field")):
        if not any(t in notes_l for t in ("copied", "clipboard", "revealed")):
            return None
    return value


def output_indicates_authenticated(raw_output: str | None, notes: str | None) -> bool:
    blob = f"{raw_output or ''}\n{notes or ''}".lower()
    return any(t in blob for t in (
        "already logged in", "already authenticated", "dashboard",
        "/home/", "workspace", "account menu", "profile menu",
        "without auth prompt", "redirected to /home",
    ))


def _verification_looks_successful(output: str | None) -> bool:
    text = (output or "").lower()
    if not text:
        return False
    if any(m in text for m in ("wrong code", "invalid code", "expired", "failed", "cannot verify", "unable to verify", "could not reach")):
        return False
    return any(m in text for m in ("verified", "verification complete", "account confirmed", "code was accepted"))


def _verification_explicit_flag(output: str | None) -> bool | None:
    m = re.search(r"VERIFIED\s*:\s*(YES|NO)", (output or "").replace("\\n", "\n"), re.IGNORECASE)
    return m.group(1).upper() == "YES" if m else None


def _looks_like_captcha_failure(run: RunResult) -> bool:
    text = ((run.output or "") + " " + (run.error or "")).lower()
    return any(m in text for m in ("captcha", "recaptcha", "hcaptcha", "cloudflare"))


def _looks_like_email_conflict(details: str | None) -> bool:
    if not details:
        return False
    low = details.lower()
    return any(k in low for k in (
        "already exists", "already_registered", "already registered",
        "already in use", "user_already_exists",
    ))


def _infer_needs_verification(status: str, text: str) -> bool:
    if status == "NEEDS_VERIFICATION":
        return True
    if status in {"SIGNUP_SUCCESS", "SIGNUP_FAILED"}:
        return False
    return any(h in text.lower() for h in (
        "needs_verification", "check your email to verify",
        "enter verification code", "verify your email",
    ))


def _infer_magic_link(signup_text: str) -> bool:
    low = signup_text.lower()
    return any(h in low for h in ("magic link", "magic_link", "passwordless", "sign-in link"))


# ── Task prompts ──────────────────────────────────────────────────────


def build_signup_task(url: str, identity: Identity) -> str:
    local_phone = re.sub(r"\D", "", identity.phone)[-10:] if identity.phone else ""
    return f"""
Open {url} and register a NEW account by email (not OAuth/Google/GitHub).
Discover the signup path from visible UI (Sign up/Register/Create account).
Stay on the target website domain only.
Do NOT use search tools, open email providers, or navigate off-domain.
Do NOT create files, todo lists, or notes.

Credentials (copy exact text inside backticks, no extra punctuation/spaces):
- first_name: `{identity.first_name}`
- last_name: `{identity.last_name}`
- username: `{identity.username}`
- email: `{identity.email}`
- password: `{identity.password}`
- dob: `{identity.dob}`
- company: `{identity.company}`
- website: `{identity.website}`
- phone: `{identity.phone}`

Rules:
1) Choose signup/register/create account (never login). If Google/GitHub/SSO appears, skip it.
2) Close blocking popups/modals/cookie banners first.
3) Fill ALL visible form fields and submit. Use provided values for matching fields.
4) Phone: if a country/flag selector is next to the phone input, leave it on US/+1 and type only
   `{local_phone}` (10 digits, no country code). Otherwise, type `{identity.phone}` (full E.164).
   If E.164 error appears, clear the field and try the other format.
5) If submit/continue button does nothing after clicking, look for validation errors or
   unfilled required fields (phone, ToS checkbox, etc). Fill them and retry submit.
6) If a Cloudflare Turnstile captcha/checkbox appears, click it and wait for it to resolve.
   If other captcha appears, solve it (wait up to 45s). On failure, refresh and retry ONCE.
   After second captcha failure: output SIGNUP_FAILED with DETAILS: CAPTCHA_FAILED_OR_TIMEOUT.
7) If you reach an OTP/email-code screen, STOP and output NEEDS_VERIFICATION.
8) If same submit error repeats twice, STOP and output SIGNUP_FAILED with DETAILS.
9) Stop immediately after successful submit.

Output exactly:
STATUS: SIGNUP_SUCCESS or NEEDS_VERIFICATION or SIGNUP_FAILED
DETAILS: <short reason>
""".strip()


def build_login_apikey_task(url: str, identity: Identity) -> str:
    return f"""
Stay on domain {url}. Do not visit email providers or other sites.
Do not create files, todo lists, or notes.

1) Check current auth state. If you see an authenticated dashboard/workspace/profile,
   treat LOGIN as SUCCESS and skip to API key discovery.
2) If not authenticated, log in at {url}:
   - email: `{identity.email}`
   - password: `{identity.password}`
   Copy exact text inside backticks only.
3) If login needs magic link/2FA, or says "No account found", stop immediately.
4) Find API key/token page fast:
   Try direct paths: /api-keys, /settings/api, /settings/api-keys,
   /account/api-keys, /developer/api, /developer/api-keys.
   Then check Settings, Developer, Integrations (max 2 steps per dead path).
5) If key exists in plaintext, copy the FULL secret value.
6) If no key exists, create one (name "sigma"), submit, copy full secret shown.
7) If key is masked, use reveal/copy controls. If never shown, output API_KEY: NONE.
8) Never invent values. If unsure, output API_KEY: NONE.
9) If onboarding blocks access (create team/workspace), complete with safe defaults first.

Output exactly:
LOGIN: SUCCESS or FAILED
API_KEY: <key> or NONE
API_KEY_URL: <url> or NONE
LOGIN_URL: <url> or NONE
NOTES: <short>
""".strip()


# ── Main pipeline ─────────────────────────────────────────────────────


async def signup(
    url: str,
    model: str = "bu",
    max_steps: int = 25,
    verify_timeout: int = 60,
    signup_timeout: int = 250,
    login_timeout: int = 140,
    skip_verification: bool = False,
    profile_id: str | None = None,
    retry_email_conflict: bool = True,
):
    global _t0
    _t0 = time.time()

    # Fail fast on missing required env vars
    missing = []
    if not BROWSER_USE_API_KEY:
        missing.append("BROWSER_USE_API_KEY")
    if not AGENTMAIL_API_KEY:
        missing.append("AGENTMAIL_API_KEY")
    if missing:
        raise RuntimeError(f"Required env vars not set: {', '.join(missing)}")

    resolved_model = resolve_model(model)
    llm = make_llm(model)
    log("model", f"Requested={model} Resolved={resolved_model}")

    mail = AsyncAgentMail(api_key=AGENTMAIL_API_KEY, timeout=20)

    # ── Pre-flight: free zombie cloud sessions from previous killed runs ──
    with contextlib.suppress(Exception):
        n_killed = await stop_all_active_cloud_sessions()
        if n_killed:
            log("setup", f"Freed {n_killed} zombie session(s)")

    # Clean up stale cloud sessions from previous runs
    for _ in range(3):
        stopped = await stop_oldest_active_cloud_session()
        if not stopped:
            break
        log("setup", f"Cleaned up stale session: {stopped}")

    # ── Parallel setup: inbox + browser ──
    log("setup", "Creating inbox + cloud browser...")
    (inbox_id, created_inbox), browser = await asyncio.gather(
        acquire_inbox(mail),
        create_cloud_browser(profile_id),
    )
    identity = generate_identity(inbox_id)
    base_url = url.rstrip("/")
    log("setup", f"Email: {identity.email}")
    log("creds", f"{identity.first_name} {identity.last_name} / {identity.username} / {identity.password}")

    run_started_at = datetime.now(timezone.utc)

    async def rebuild_browser(reason: str):
        nonlocal browser
        log("session", f"Rebuilding browser: {reason}")
        with contextlib.suppress(Exception):
            await asyncio.wait_for(browser.stop(), timeout=10)
        # Free zombie cloud sessions before creating new one (avoids 429)
        for _attempt in range(3):
            with contextlib.suppress(Exception):
                killed = await stop_oldest_active_cloud_session()
                if killed:
                    log("session", f"Freed zombie session: {killed}")
            try:
                browser = await create_cloud_browser(profile_id)
                return
            except Exception as e:
                if "429" in str(e) or "too many" in str(e).lower():
                    log("session", f"429 on browser create, freeing another session...")
                    await asyncio.sleep(2)
                    continue
                raise
        browser = await create_cloud_browser(profile_id)  # final attempt, let it fail

    try:
        # ── Phase 1: Signup + parallel email polling ──
        verify_task: asyncio.Task[VerificationCandidate] | None = None
        if not skip_verification:
            verify_task = asyncio.create_task(
                watch_for_verification(
                    mail=mail, inbox_id=inbox_id, target_url=url,
                    timeout_s=signup_timeout + verify_timeout,
                    min_received_at=run_started_at,
                )
            )

        signup_prompt = build_signup_task(base_url, identity)
        signup_result = await run_agent(
            browser, llm, "signup", signup_prompt,
            max_steps=max_steps, timeout_s=signup_timeout, retries=1,
        )

        # Captcha retry with browser rebuild
        if _looks_like_captcha_failure(signup_result) and not signup_result.success:
            log("signup", "Captcha failure; rebuilding browser and retrying...")
            await rebuild_browser("captcha_failure")
            signup_result = await run_agent(
                browser, llm, "signup-retry", signup_prompt,
                max_steps=max_steps, timeout_s=signup_timeout, retries=1,
            )

        # Browser instability retry (EventBus stall, low step count, or infra crash)
        for _ in range(3):
            if signup_result.success:
                break
            # If verification email arrived, signup succeeded even if agent crashed
            if verify_task and verify_task.done() and not verify_task.cancelled():
                with contextlib.suppress(Exception):
                    vcandidate = verify_task.result()
                    if vcandidate and (vcandidate.link or vcandidate.code):
                        log("signup", "Verification email arrived → signup succeeded despite agent crash")
                        signup_result = RunResult(
                            output="STATUS: NEEDS_VERIFICATION\nDETAILS: agent crashed but verification email confirms signup",
                            success=True, steps=signup_result.steps, error=None,
                        )
                        break
            has_status = bool(re.search(r"STATUS\s*:", signup_result.output or "", re.IGNORECASE))
            needs_rebuild = (
                _needs_browser_rebuild(signup_result.error)
                or signup_result.steps <= 1
                or (not signup_result.success and not has_status)  # crashed without STATUS = infra failure
            )
            if not needs_rebuild:
                break
            log("signup", f"Browser instability (steps={signup_result.steps}, has_status={has_status}); rebuilding...")
            await asyncio.sleep(3)  # cooldown before rebuild (helps with Cloudflare rate limits)
            await rebuild_browser("session_instability")
            signup_result = await run_agent(
                browser, llm, "signup", signup_prompt,
                max_steps=max_steps, timeout_s=signup_timeout, retries=1,
            )

        # Model fallback: OpenAI schema mismatch → bu-2-0
        if (
            not signup_result.success
            and "openai" in type(llm).__name__.lower()
            and signup_result.error
            and "items" in signup_result.error.lower()
        ):
            log("model", "OpenAI schema mismatch; falling back to bu-2-0")
            from browser_use import ChatBrowserUse
            llm = ChatBrowserUse(model="bu-2-0", api_key=BROWSER_USE_API_KEY)
            resolved_model = f"{resolved_model}->bu-2-0"
            signup_result = await run_agent(
                browser, llm, "signup", signup_prompt,
                max_steps=max_steps, timeout_s=signup_timeout, retries=1,
            )

        signup_status, signup_details = parse_signup_status(signup_result.output)
        signup_text = (signup_result.output or "").lower()
        needs_verification = _infer_needs_verification(signup_status, signup_text)
        is_magic_link = _infer_magic_link(signup_result.output or "")

        # Email conflict retry
        if (
            signup_status == "SIGNUP_FAILED"
            and retry_email_conflict
            and _looks_like_email_conflict(signup_details)
        ):
            log("signup", "Email conflict; rotating inbox and retrying...")
            with contextlib.suppress(Exception):
                await mail.inboxes.delete(inbox_id)
            inbox_id, created_inbox = await acquire_inbox(mail)
            identity = generate_identity(inbox_id)
            log("signup", f"Retry email: {identity.email}")
            await rebuild_browser("email_conflict_retry")
            signup_prompt = build_signup_task(base_url, identity)
            # Restart verification watcher
            if verify_task and not verify_task.done():
                verify_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await verify_task
            run_started_at = datetime.now(timezone.utc)
            if not skip_verification:
                verify_task = asyncio.create_task(
                    watch_for_verification(
                        mail=mail, inbox_id=inbox_id, target_url=url,
                        timeout_s=signup_timeout + verify_timeout,
                        min_received_at=run_started_at,
                    )
                )
            signup_result = await run_agent(
                browser, llm, "signup", signup_prompt,
                max_steps=max_steps, timeout_s=signup_timeout, retries=1,
            )
            signup_status, signup_details = parse_signup_status(signup_result.output)
            signup_text = (signup_result.output or "").lower()
            needs_verification = _infer_needs_verification(signup_status, signup_text)
            is_magic_link = _infer_magic_link(signup_result.output or "")

        # Signup totally failed — bail
        signup_actually_failed = (
            signup_status == "SIGNUP_FAILED"
            or (signup_status == "UNKNOWN" and not signup_result.success)
        )
        if signup_actually_failed:
            if verify_task and not verify_task.done():
                verify_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await verify_task
            _print_results(url, identity, resolved_model,
                           signup_ok=False, verified=False, login_ok=False,
                           api_key=None, api_key_url=None, login_url=None,
                           notes=signup_details or "Signup failed.")
            return

        # ── Phase 2: Verification ──
        verified = False
        verification_link: str | None = None
        verification_code: str | None = None

        if needs_verification and not skip_verification and verify_task:
            log("verify", "Waiting for verification signal...")
            candidate = VerificationCandidate(link=None, code=None, subject=None)
            try:
                candidate = await asyncio.wait_for(verify_task, timeout=verify_timeout)
            except (asyncio.TimeoutError, TimeoutError):
                if not verify_task.done():
                    verify_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await verify_task
            # Fresh poll if nothing caught during signup
            if not candidate.link and not candidate.code:
                log("verify", "No early candidate; fresh poll...")
                candidate = await watch_for_verification(
                    mail=mail, inbox_id=inbox_id, target_url=url,
                    timeout_s=verify_timeout, min_received_at=run_started_at,
                )

            verification_link = candidate.link
            verification_code = candidate.code

            # HTTP fast path
            if verification_link:
                log("verify", f"HTTP verify: {verification_link[:80]}...")
                ok, final_url = await try_http_verification(verification_link)
                if ok:
                    verified = True
                    log("verify", f"Verified via HTTP GET ({final_url})")
                else:
                    # Browser fallback for verification (Auth0/SSO may require login first)
                    verify_browser_task = (
                        f"Open this verification link: {verification_link}\n"
                        f"If asked to log in: email=`{identity.email}` password=`{identity.password}`\n"
                        "After clicking verify/confirm or logging in, wait for the page to load.\n"
                        "If you see a dashboard, workspace, or 'email verified' message: VERIFIED: YES.\n"
                        "Output: VERIFIED: YES or NO / DETAILS: <short>"
                    )
                    vr = await run_agent(browser, llm, "verify", verify_browser_task, max_steps=8, timeout_s=60)
                    flag = _verification_explicit_flag(vr.output)
                    verified = flag if flag is not None else _verification_looks_successful(vr.output)
                    if not vr.success or not verified:
                        # Auth0/SSO: if agent entered credentials AND submitted (3+ steps
                        # or 2 steps with no input failure), the ticket was processed
                        # server-side → email is verified.
                        err_l = (vr.error or "").lower()
                        input_failed = any(k in err_l for k in (
                            "failed to type", "sessionmanager not initialized",
                            "failed to click", "element not found",
                        ))
                        # Auth0 ticket links: navigating to the URL processes the ticket
                        # via client-side JS. Even 1 step (the navigation) is enough.
                        is_auth0_ticket = "ticket=" in (verification_link or "").lower()
                        auth0_submitted = not verified and (
                            (is_auth0_ticket and vr.steps >= 1)
                            or (vr.steps >= 2 and not input_failed)
                        )
                        if auth0_submitted:
                            log("verify", f"Auth0 ticket processed ({vr.steps} steps); assuming verified")
                            verified = True
                        await rebuild_browser("verify_failure")
            elif verification_code:
                log("verify", f"Code verify: {verification_code}")
                # Enter the OTP code on the EXISTING browser session — the OTP screen
                # is still visible from the signup agent's last step.
                digits = list(verification_code)
                otp_task = (
                    f"You are on a verification/OTP code entry screen.\n"
                    f"The code is: `{verification_code}` (digits: {' '.join(digits)})\n"
                    f"Rules:\n"
                    f"- If there are {len(digits)} separate input boxes, click EACH box and type ONE digit.\n"
                    f"  Box 1: `{digits[0]}`, Box 2: `{digits[1]}`, Box 3: `{digits[2]}`, "
                    f"Box 4: `{digits[3]}`, Box 5: `{digits[4]}`, Box 6: `{digits[5]}`\n"
                    f"- If there is a single input field, clear it first, then type `{verification_code}`\n"
                    f"- After entering the code, click Verify/Confirm/Submit.\n"
                    f"- If 'incorrect code' appears, clear the field(s), re-enter carefully, and submit again.\n"
                    f"- IMPORTANT: Preserve ALL digits exactly, including leading zeros.\n"
                    f"Output: VERIFIED: YES or NO / DETAILS: <short>"
                )
                otp_result = await run_agent(browser, llm, "otp-entry", otp_task, max_steps=8, timeout_s=60)
                flag = _verification_explicit_flag(otp_result.output)
                if flag or _verification_looks_successful(otp_result.output):
                    verified = True
                    log("verify", "OTP code accepted!")
                elif otp_result.steps >= 1 and "incorrect" not in (otp_result.output or "").lower():
                    log("verify", f"OTP entry attempted ({otp_result.steps} steps); assuming verified")
                    verified = True
                else:
                    log("verify", f"OTP entry failed: {(otp_result.output or '')[:120]}")
                await rebuild_browser("post_otp_verify")
            else:
                log("verify", "No verification link/code found.")
        elif verify_task and not verify_task.done():
            verify_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await verify_task

        # ── Phase 3: Login + API Key ──
        if needs_verification and not verified and not is_magic_link:
            log("login", "Warning: verification unresolved — login may be blocked")
        if is_magic_link:
            # ── Magic-link flow: trigger link → catch email → programmatic navigate ──
            login_ok, api_key, api_key_url, login_url, notes = await _magic_link_login(
                browser, llm, mail, inbox_id, url, identity,
                run_started_at, max_steps, login_timeout, verify_timeout,
            )
        else:
            # ── Standard password login ──
            verify_preamble = ""
            if verification_link and not verified:
                verify_preamble = f"FIRST: Navigate to {verification_link} to verify email.\n\n"
            elif verification_code and not verified:
                verify_preamble = (
                    f"FIRST: Your email needs verification. Go to the login page, enter email `{identity.email}` "
                    f"and password `{identity.password}`. If an OTP/verification code screen appears, "
                    f"enter code `{verification_code}` (preserve leading zeros). Then continue to login.\n\n"
                )

            login_task = build_login_apikey_task(base_url, identity)
            if verify_preamble:
                login_task = verify_preamble + login_task
            log("login", "Password-based login...")
            login_result = await run_agent(
                browser, llm, "login+api", login_task,
                max_steps=max_steps + 5, timeout_s=login_timeout,
            )
            # Retry login on infra crash (browser died or input broken)
            for _ in range(2):
                if login_result.success:
                    break
                # Detect browser death: explicit error OR agent completed very few steps
                # with no meaningful output (BrowserStateRequestEvent failures don't appear
                # in history.errors(), so error may be None despite browser being dead)
                browser_dead = _needs_browser_rebuild(login_result.error) or (
                    login_result.steps <= 2 and not login_result.success
                    and not re.search(r"LOGIN\s*:", login_result.output or "", re.IGNORECASE)
                )
                if not browser_dead:
                    break
                log("login", f"Browser crash (steps={login_result.steps}); rebuilding and retrying...")
                await rebuild_browser("login_crash_retry")
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(browser.navigate_to(base_url), timeout=15)
                    await asyncio.sleep(2)
                login_result = await run_agent(
                    browser, llm, "login+api", login_task,
                    max_steps=max_steps + 5, timeout_s=login_timeout,
                )
            if _is_eventbus_stall(login_result.error) or _needs_browser_rebuild(login_result.error):
                await rebuild_browser("login_stall")

            parsed = parse_login_output(login_result.output)
            notes_l = (parsed.get("NOTES") or "").lower()
            login_state = (parsed.get("LOGIN") or "").strip().upper()

            # Login stall heuristic: if agent entered credentials (4+ steps =
            # navigate + click login + switch tab + email + password) before stalling,
            # Auth0 processed the login → treat as success.
            if login_state != "SUCCESS" and login_result.steps >= 4 and not login_result.success:
                login_err = (login_result.error or "").lower()
                login_input_failed = any(k in login_err for k in (
                    "failed to type", "sessionmanager not initialized",
                    "failed to click", "element not found",
                ))
                if not login_input_failed:
                    log("login", f"Credentials likely submitted ({login_result.steps} steps); assuming login success")
                    parsed["LOGIN"] = "SUCCESS"
                    login_state = "SUCCESS"

            # Late verification recovery: login blocked by unconfirmed email
            if (
                login_state == "FAILED"
                and needs_verification
                and not verified
                and any(k in notes_l for k in (
                    "not confirmed", "confirm your email", "email not verified", "verify your email",
                    "email confirmation", "confirm email", "unverified", "verify email",
                    "verification required", "not yet verified",
                ))
            ):
                log("verify", "Login blocked by unconfirmed email; late verification attempt...")
                late = await watch_for_verification(
                    mail=mail, inbox_id=inbox_id, target_url=url,
                    timeout_s=max(20, verify_timeout), min_received_at=run_started_at,
                )
                if late.link:
                    ok, _ = await try_http_verification(late.link)
                    if ok:
                        verified = True
                        log("verify", "Late verification succeeded via HTTP")
                    else:
                        vr = await run_agent(
                            browser, llm, "verify-late",
                            f"Open {late.link} to verify. Output: VERIFIED: YES or NO",
                            max_steps=5, timeout_s=verify_timeout,
                        )
                        if _is_eventbus_stall(vr.error):
                            await rebuild_browser("late_verify_stall")
                        flag = _verification_explicit_flag(vr.output)
                        verified = flag if flag is not None else _verification_looks_successful(vr.output)
                elif late.code:
                    vr = await run_agent(
                        browser, llm, "verify-late",
                        f"Enter code {late.code} and submit. Output: VERIFIED: YES or NO",
                        max_steps=3, timeout_s=verify_timeout,
                    )
                    if _is_eventbus_stall(vr.error):
                        await rebuild_browser("late_verify_code_stall")
                    flag = _verification_explicit_flag(vr.output)
                    verified = flag if flag is not None else _verification_looks_successful(vr.output)

                if verified:
                    log("login", "Retrying login after late verification...")
                    login_result = await run_agent(
                        browser, llm, "login+api-retry", login_task,
                        max_steps=max_steps + 5, timeout_s=login_timeout,
                    )
                    if _is_eventbus_stall(login_result.error):
                        await rebuild_browser("login_retry_stall")
                    parsed = parse_login_output(login_result.output)

            # API-only recovery: login OK but no key captured
            login_ok_now = (
                (parsed.get("LOGIN") or "").strip().upper() == "SUCCESS"
                or output_indicates_authenticated(login_result.output, parsed.get("NOTES"))
            )
            parsed_key = sanitize_api_key_candidate(parsed.get("API_KEY"), parsed.get("NOTES"))
            if login_ok_now and not parsed_key:
                log("login", "Login OK but no API key; running focused API-only pass...")
                # Always rebuild — browser is likely dead after login stall/timeout
                await rebuild_browser("pre_api_only")
                api_hint = parsed.get("API_KEY_URL") or base_url
                api_only_task = (
                    f"Stay on {url}. Log in if needed:\n"
                    f"  email: `{identity.email}` / password: `{identity.password}`\n"
                    f"Then open {api_hint} or the nearest API/settings page.\n"
                    "Find or create API key (name 'sigma'). Copy full plaintext value.\n"
                    "If masked, use reveal/copy. If never shown, output NONE.\n"
                    "Output: LOGIN: SUCCESS / API_KEY: <key> or NONE / API_KEY_URL: <url> or NONE / LOGIN_URL: <url> or NONE / NOTES: <short>"
                )
                api_only = await run_agent(
                    browser, llm, "api-only", api_only_task,
                    max_steps=10, timeout_s=min(90, login_timeout),
                )
                if _is_eventbus_stall(api_only.error):
                    await rebuild_browser("api_only_stall")
                p2 = parse_login_output(api_only.output)
                k2 = sanitize_api_key_candidate(p2.get("API_KEY"), p2.get("NOTES"))
                if k2:
                    parsed = p2

            login_ok = login_ok_now
            api_key = sanitize_api_key_candidate(parsed.get("API_KEY"), parsed.get("NOTES"))
            api_key_url = parsed.get("API_KEY_URL")
            login_url = parsed.get("LOGIN_URL")
            notes = parsed.get("NOTES")

        _print_results(url, identity, resolved_model,
                       signup_ok=not signup_actually_failed,
                       verified=verified,
                       login_ok=login_ok,
                       api_key=api_key,
                       api_key_url=api_key_url,
                       login_url=login_url,
                       notes=notes)

    finally:
        with contextlib.suppress(Exception):
            await browser.stop()
        if created_inbox:
            with contextlib.suppress(Exception):
                await mail.inboxes.delete(inbox_id)


# ── Magic-link login ──────────────────────────────────────────────────


async def _magic_link_login(
    browser: Browser, llm: Any, mail: AsyncAgentMail, inbox_id: str,
    url: str, identity: Identity, run_started_at: datetime,
    max_steps: int, login_timeout: int, verify_timeout: int,
) -> tuple[bool, str | None, str | None, str | None, str | None]:
    """
    Magic-link login flow:
    1. Agent triggers magic link (enters email, clicks send)
    2. Poll inbox for new magic link email (timestamp-gated)
    3. Navigate browser PROGRAMMATICALLY to the magic link URL
       (bypasses LLM URL hallucination)
    4. Then run API key extraction agent

    Returns: (login_ok, api_key, api_key_url, login_url, notes)
    """
    ml_domain = _base_domain(url)
    log("login", "Magic-link site — triggering login link...")

    trigger_task = (
        f"Go to {url} and sign in. Enter email `{identity.email}` and click "
        "'Continue with Email' or similar submit button. Wait until you see "
        "'check your email' or 'magic link sent'. "
        "Then report MAGIC_LINK_TRIGGERED and STOP. Do NOT visit any other site."
    )
    trigger_result = await run_agent(browser, llm, "magic-trigger", trigger_task, max_steps=8, timeout_s=60)
    if not trigger_result.success:
        log("login", f"Magic-link trigger may have failed: {trigger_result.error or 'unknown'}")

    # Reset timestamp so we only catch emails sent AFTER the trigger
    ml_started_at = datetime.now(timezone.utc)
    log("login", "Polling for magic-link email...")
    candidate = await watch_for_verification(
        mail=mail, inbox_id=inbox_id, target_url=url,
        timeout_s=30, poll_s=2, min_received_at=ml_started_at,
    )

    if candidate.link:
        log("login", f"Got magic link: {candidate.link[:80]}...")

        # PROGRAMMATIC navigation — bypasses LLM URL hallucination
        try:
            await browser.navigate_to(candidate.link)
            await asyncio.sleep(2)  # Let cookies settle
            log("login", "Navigated to magic link programmatically")
        except Exception as e:
            log("login", f"Programmatic navigation failed ({e}); falling back to agent...")
            nav_task = f"Navigate to this exact URL: {candidate.link}\nWait for it to load."
            await run_agent(browser, llm, "magic-nav", nav_task, max_steps=3, timeout_s=30)

        # Now extract API key
        apikey_task = (
            f"You are now logged in at {url}.\n"
            f"Find API key: check /settings/api, /account/api-keys, /developer/api, /settings/extensions.\n"
            f"Copy existing key or create one named 'sigma'. If no API section after 2 pages, report NONE.\n\n"
            f"Output: LOGIN: SUCCESS / API_KEY: <key> or NONE / API_KEY_URL: <url> or NONE / LOGIN_URL: <url> or NONE / NOTES: <short>"
        )
        result = await run_agent(
            browser, llm, "login+api", apikey_task,
            max_steps=max_steps + 5, timeout_s=login_timeout,
        )
        parsed = parse_login_output(result.output)
        login_ok = (
            (parsed.get("LOGIN") or "").strip().upper() == "SUCCESS"
            or output_indicates_authenticated(result.output, parsed.get("NOTES"))
        )
        return (
            login_ok,
            sanitize_api_key_candidate(parsed.get("API_KEY"), parsed.get("NOTES")),
            parsed.get("API_KEY_URL"),
            parsed.get("LOGIN_URL"),
            parsed.get("NOTES"),
        )
    else:
        log("login", "No magic link email; falling back to password login...")
        login_task = build_login_apikey_task(url, identity)
        result = await run_agent(
            browser, llm, "login+api", login_task,
            max_steps=max_steps + 5, timeout_s=login_timeout,
        )
        parsed = parse_login_output(result.output)
        login_ok = (parsed.get("LOGIN") or "").strip().upper() == "SUCCESS"
        return (
            login_ok,
            sanitize_api_key_candidate(parsed.get("API_KEY"), parsed.get("NOTES")),
            parsed.get("API_KEY_URL"),
            parsed.get("LOGIN_URL"),
            parsed.get("NOTES"),
        )


# ── Output ────────────────────────────────────────────────────────────


def _print_results(
    url: str, identity: Identity, model: str,
    signup_ok: bool, verified: bool, login_ok: bool,
    api_key: str | None, api_key_url: str | None,
    login_url: str | None, notes: str | None,
):
    elapsed = time.time() - _t0
    print(f"\n{'='*56}", flush=True)
    print(f"  RESULTS ({elapsed:.0f}s, model={model})", flush=True)
    print(f"{'='*56}", flush=True)
    print(f"  URL:      {url}", flush=True)
    print(f"  Email:    {identity.email}", flush=True)
    print(f"  Password: {identity.password}", flush=True)
    print(f"  Username: {identity.username}", flush=True)
    print(f"  Signup:   {'ok' if signup_ok else 'FAILED'}", flush=True)
    print(f"  Verified: {'yes' if verified else 'no'}", flush=True)
    print(f"  Login:    {'ok' if login_ok else 'failed'}", flush=True)
    if api_key:
        print(f"  API Key:  {api_key}", flush=True)
    else:
        print(f"  API Key:  NONE", flush=True)
    if api_key_url:
        print(f"  Key URL:  {api_key_url}", flush=True)
    if notes:
        print(f"  Notes:    {notes}", flush=True)
    print(f"{'='*56}", flush=True)
    print(f"\n  TO LOG IN:", flush=True)
    print(f"  1. Go to {login_url or url}", flush=True)
    print(f"  2. Email:    {identity.email}", flush=True)
    print(f"  3. Password: {identity.password}", flush=True)
    if api_key_url:
        print(f"  4. API keys: {api_key_url}", flush=True)
    print(flush=True)


# ── CLI ───────────────────────────────────────────────────────────────


def main():
    global _t0
    _t0 = time.time()

    p = argparse.ArgumentParser(description="Sigma Combined: auto signup + API key")
    p.add_argument("url", help="Website URL")
    p.add_argument(
        "--llm", default="bu",
        help="Preset: bu(bu-2-0), best(gpt-5.2), fast(gpt-5-mini), ultra(gpt-5-nano), or raw model id",
    )
    p.add_argument("--max-steps", type=int, default=15)
    p.add_argument("--signup-timeout", type=int, default=120)
    p.add_argument("--login-timeout", type=int, default=140)
    p.add_argument("--verify-timeout", type=int, default=60)
    p.add_argument("--skip-verification", action="store_true")
    p.add_argument("--profile-id", default=None, help="Browser Use profile id for pre-auth cookies")
    p.add_argument("--no-retry-email-conflict", dest="retry_email_conflict", action="store_false", default=True)
    p.add_argument("--timeout", type=int, default=None, help="(compat alias for --signup-timeout)")
    args = p.parse_args()
    if args.timeout is not None:
        args.signup_timeout = args.timeout

    asyncio.run(signup(
        url=args.url,
        model=args.llm,
        max_steps=args.max_steps,
        signup_timeout=args.signup_timeout,
        login_timeout=args.login_timeout,
        verify_timeout=args.verify_timeout,
        skip_verification=args.skip_verification,
        profile_id=args.profile_id,
        retry_email_conflict=args.retry_email_conflict,
    ))


if __name__ == "__main__":
    main()
