#!/usr/bin/env python3
"""Sigma v3: reverse-engineer any signup page, then sign up reliably.

Three execution tiers:
  Tier 1 — DETERMINISTIC: Firecrawl recon + Playwright DOM inspection → JS form fill.
           No LLM. Fast, reliable, never clicks the wrong button.
  Tier 2 — AGENT-ASSISTED: browser-use agent with profile-informed prompts.
           Knows the signup URL, form fields, auth provider, CAPTCHA type.
  Tier 3 — BLIND DISCOVERY: generic agent prompt (current behavior).
           Captures everything for next time (selectors, URLs, flow).

Each run feeds back into a cached SiteProfile so subsequent runs use a higher tier.

Design:
- Local Playwright browser by default (no cloud crash limit, residential IP).
- Cloud browser as fallback (--cloud flag or automatic on local failure).
- Firecrawl pre-recon builds site intelligence before touching a browser.
- HAR capture during signup for deeper site analysis.
- Sensitive data redaction via browser-use sensitive_data map.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import html
import inspect
import json
import os
import pathlib
import re
import secrets
import string
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from agentmail import AsyncAgentMail
from browser_use import Agent, Browser, ChatBrowserUse, Controller
from dotenv import load_dotenv
from faker import Faker
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict

load_dotenv()

# ── browser-use event bus timeouts ──
# Auth0 and heavy SPAs can stall the DOMWatchdog for >30s during redirects.
# Increase key timeouts so the agent survives post-login navigation.
_BU_TIMEOUT_OVERRIDES = {
    "TIMEOUT_BrowserStateRequestEvent": "60",   # default 30 → 60
    "TIMEOUT_NavigationCompleteEvent": "60",     # default 30 → 60
    "TIMEOUT_NavigateToUrlEvent": "45",          # default 30 → 45
}
for _k, _v in _BU_TIMEOUT_OVERRIDES.items():
    os.environ.setdefault(_k, _v)

BROWSER_USE_API_KEY = os.environ.get("BROWSER_USE_API_KEY", "")
AGENTMAIL_API_KEY = os.environ.get("AGENTMAIL_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "")
FIRECRAWL_BASE = "https://api.firecrawl.dev/v1"

MODEL_PRESETS = {
    "best": "gpt-5.2",
    "fast": "gpt-5-mini",
    "ultra": "gpt-5-nano",
    "bu": "bu-2-0",
}

fake = Faker()
_t0 = time.time()
_AGENT_INIT_PARAMS: set[str] | None = None
_GLOBAL_SENSITIVE: dict[str, str] = {}  # Set by signup() — auto-injected into all run_agent calls


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


# ── Structured output models (Pydantic) for browser-use Controller ────
# These replace fragile regex parsing of agent free-text output.
# The agent calls `done(data={...})` with validated structured data.


class SignupOutput(BaseModel):
    """Structured output for signup agent."""
    status: str  # SIGNUP_SUCCESS, NEEDS_VERIFICATION, SIGNUP_FAILED
    details: str = ""
    verification_type: str | None = None  # link, code, magic_link, otp, none


class LoginApiKeyOutput(BaseModel):
    """Structured output for login + API key agent."""
    login: str  # SUCCESS, FAILED
    api_key: str | None = None
    api_key_url: str | None = None
    login_url: str | None = None
    notes: str = ""


class VerifyOutput(BaseModel):
    """Structured output for verification agent."""
    verified: str  # YES, NO
    details: str = ""


def make_controller(output_model: type[BaseModel] | None = None) -> Controller:
    """Create a browser-use Controller with structured output and dangerous actions excluded."""
    ctrl = Controller()
    if output_model:
        ctrl.use_structured_output_action(output_model)
    # Exclude file-system actions the agents should never use
    for action_name in ("write_file", "replace_file", "read_file", "todo"):
        try:
            ctrl.exclude_action(action_name)
        except Exception:
            pass  # action may not exist in this version
    return ctrl


@dataclass
class FormField:
    selector: str
    field_type: str
    name: str | None = None
    label: str | None = None
    placeholder: str | None = None
    required: bool = False
    identity_key: str | None = None  # maps to Identity attr: email, password, first_name, etc.


@dataclass
class SiteProfile:
    domain: str
    signup_url: str | None = None
    login_url: str | None = None
    api_key_url: str | None = None
    auth_provider: str | None = None
    form_fields: list[FormField] = field(default_factory=list)
    submit_selector: str | None = None
    captcha_type: str | None = None
    oauth_options: list[str] = field(default_factory=list)
    verification_method: str | None = None
    requires_credit_card: bool = False
    notes: list[str] = field(default_factory=list)
    confidence: float = 0.0
    success_count: int = 0
    failure_count: int = 0
    last_updated: float = 0.0
    har_path: str | None = None


# ── Profile storage ──────────────────────────────────────────────────


def _profile_dir(custom: str | None = None) -> pathlib.Path:
    d = pathlib.Path(custom) if custom else pathlib.Path.home() / ".sigma" / "profiles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_profile(domain: str, profile_dir: str | None = None) -> SiteProfile | None:
    p = _profile_dir(profile_dir) / f"{domain}.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        fields = [FormField(**f) for f in data.pop("form_fields", [])]
        return SiteProfile(form_fields=fields, **data)
    except Exception as e:
        log("profile", f"Failed to load profile for {domain}: {e}")
        return None


def save_profile(profile: SiteProfile, profile_dir: str | None = None) -> None:
    profile.last_updated = time.time()
    p = _profile_dir(profile_dir) / f"{profile.domain}.json"
    data = asdict(profile)
    p.write_text(json.dumps(data, indent=2))
    log("profile", f"Saved profile → {p}")


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


# Stealth JS injected before each page load to reduce bot detection
_STEALTH_JS = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    delete window.__playwright__binding__;
    delete window.__pwInitScripts;
    window.chrome = window.chrome || { runtime: {} };
    const _origQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (p) => (
        p.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : _origQuery(p)
    );
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5]
    });
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en']
    });
"""

_STEALTH_CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--window-size=1920,1080",
]


async def create_local_browser(
    *, headless: bool = True,
    har_path: str | pathlib.Path | None = None,
) -> Browser:
    """Create a local Playwright browser. More reliable than cloud — no crash limit, residential IP."""
    kwargs: dict[str, Any] = dict(
        use_cloud=False,
        keep_alive=True,
        headless=headless,
        minimum_wait_page_load_time=0.5,
        wait_between_actions=0.3,
        highlight_elements=False,
        captcha_solver=True,
        args=_STEALTH_CHROME_ARGS,
    )
    if har_path:
        kwargs["record_har_path"] = str(har_path)
        kwargs["record_har_content"] = "embed"
        kwargs["record_har_mode"] = "full"
    return Browser(**kwargs)


async def create_cloud_browser(
    profile_id: str | None = None,
    har_path: str | pathlib.Path | None = None,
) -> Browser:
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
    if har_path:
        kwargs["record_har_path"] = str(har_path)
        kwargs["record_har_content"] = "embed"
        kwargs["record_har_mode"] = "full"
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


async def acquire_inbox(mail: AsyncAgentMail, domain: str | None = None) -> tuple[str, bool]:
    """Create inbox for signup. Domain param reserved for future idempotent inbox reuse."""
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


# ── Firecrawl recon ───────────────────────────────────────────────────


_SIGNUP_LINK_PATTERNS = re.compile(
    r"sign\s*up|register|create\s*account|get\s*started|start\s*free|try\s*free|free\s*trial",
    re.IGNORECASE,
)
_SIGNUP_PATH_FALLBACKS = [
    "/signup", "/sign-up", "/register", "/auth/signup", "/auth/register",
    "/create-account", "/get-started", "/join",
]
_AUTH_PROVIDER_PATTERNS = {
    "auth0": re.compile(r"auth0\.com|auth0-js|lock\.min\.js", re.IGNORECASE),
    "clerk": re.compile(r"clerk\.com|clerk\.browser|@clerk/", re.IGNORECASE),
    "cognito": re.compile(r"cognito-idp|amazoncognito|aws-amplify.*auth", re.IGNORECASE),
    "firebase": re.compile(r"firebaseapp\.com|firebase\.auth|firebase/auth", re.IGNORECASE),
    "supabase": re.compile(r"supabase\.co|supabase-js|@supabase/auth", re.IGNORECASE),
}
_CAPTCHA_PATTERNS = {
    "turnstile": re.compile(r"cf-turnstile|challenges\.cloudflare\.com/turnstile", re.IGNORECASE),
    "recaptcha": re.compile(r"g-recaptcha|google\.com/recaptcha", re.IGNORECASE),
    "hcaptcha": re.compile(r"h-captcha|hcaptcha\.com", re.IGNORECASE),
}


async def _firecrawl_scrape(url: str, timeout: int = 15) -> dict | None:
    """Scrape a URL with Firecrawl, returning markdown + rawHtml."""
    if not FIRECRAWL_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {FIRECRAWL_API_KEY}"},
            timeout=timeout,
        ) as client:
            resp = await client.post(
                f"{FIRECRAWL_BASE}/scrape",
                json={"url": url, "formats": ["markdown", "rawHtml"], "onlyMainContent": False, "timeout": 12000},
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    return data.get("data", {})
    except Exception as e:
        log("recon", f"Firecrawl error for {url}: {e}")
    return None


def _find_signup_url(markdown: str, raw_html: str, base_url: str) -> str | None:
    """Find the signup page URL from markdown links and HTML."""
    parsed_base = urlparse(base_url)
    base_host = parsed_base.netloc.lower()

    # Search markdown for signup links (href text)
    for m in re.finditer(r"\[([^\]]*)\]\(([^)]+)\)", markdown):
        link_text, href = m.group(1), m.group(2)
        if _SIGNUP_LINK_PATTERNS.search(link_text) or _SIGNUP_LINK_PATTERNS.search(href):
            if href.startswith("/"):
                return f"{parsed_base.scheme}://{parsed_base.netloc}{href}"
            if href.startswith("http"):
                return href

    # Search HTML for signup links
    for m in re.finditer(r'href=["\']([^"\']+)["\'][^>]*>([^<]*)', raw_html, re.IGNORECASE):
        href, text = m.group(1), m.group(2)
        if _SIGNUP_LINK_PATTERNS.search(text) or _SIGNUP_LINK_PATTERNS.search(href):
            if href.startswith("/"):
                return f"{parsed_base.scheme}://{parsed_base.netloc}{href}"
            if href.startswith("http"):
                return href

    # Fallback: try common signup paths
    return None


def _detect_auth_provider(raw_html: str) -> str | None:
    for name, pattern in _AUTH_PROVIDER_PATTERNS.items():
        if pattern.search(raw_html):
            return name
    return None


async def infer_oauth_provider_from_profile(profile_id: str | None) -> str:
    """Infer best OAuth provider from browser-use profile cookies."""
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
    if has_google:
        return "google"
    if has_github:
        return "github"
    return "auto"


def _looks_like_oauth_fallback_candidate(
    signup_status: str, signup_details: str | None, signup_text: str,
    profile: SiteProfile | None = None,
) -> bool:
    """Should we retry with OAuth? True for captcha fails, disposable email blocks, OAuth-only, crash, etc."""
    if signup_status not in ("SIGNUP_FAILED", "UNKNOWN"):
        return False
    raw = f"{signup_details or ''}\n{signup_text}".lower()
    text = raw.replace("_", " ")  # normalize underscores for matching

    # Profile says OAuth-only (has oauth options but no form fields)
    if profile and profile.oauth_options and not profile.form_fields:
        return True

    markers = (
        "oauth only", "no email signup", "email signup unavailable",
        "captcha failed", "captcha timeout", "cloudflare",
        "registration failed", "failed to register", "failed to sign up",
        "something went wrong", "try again",
        "invalid email", "email is not allowed", "disposable",
        "cannot create account", "no signup form", "form not found",
        "google only", "github only", "sso only",
        "no signup option", "oauth login required",
    )
    if any(m in text for m in markers):
        return True

    # Agent crashed with no meaningful output — worth trying OAuth
    if signup_status == "UNKNOWN" and len(raw.strip()) < 200:
        return True

    return False


def _detect_captcha(raw_html: str) -> str | None:
    for name, pattern in _CAPTCHA_PATTERNS.items():
        if pattern.search(raw_html):
            return name
    return None


def _detect_oauth(raw_html: str, markdown: str) -> list[str]:
    combined = (raw_html + " " + markdown).lower()
    options = []
    if "google" in combined and any(k in combined for k in ("sign in with google", "continue with google", "google oauth", "accounts.google.com")):
        options.append("google")
    if "github" in combined and any(k in combined for k in ("sign in with github", "continue with github", "github.com/login/oauth")):
        options.append("github")
    if any(k in combined for k in ("sign in with sso", "single sign-on", "saml", "enterprise sso")):
        options.append("sso")
    return options


def _extract_form_fields_from_html(raw_html: str) -> list[FormField]:
    """Extract form input fields from raw HTML."""
    fields: list[FormField] = []
    # Find inputs, selects, textareas
    for m in re.finditer(
        r'<(input|select|textarea)\b([^>]*)/?>', raw_html, re.IGNORECASE | re.DOTALL
    ):
        tag = m.group(1).lower()
        attrs_str = m.group(2)

        def attr(name: str) -> str | None:
            am = re.search(rf'{name}=["\']([^"\']*)["\']', attrs_str, re.IGNORECASE)
            return am.group(1) if am else None

        input_type = (attr("type") or ("text" if tag == "input" else tag)).lower()
        # Skip hidden, submit, button, csrf tokens
        if input_type in ("hidden", "submit", "button", "image", "reset"):
            continue
        name = attr("name")
        input_id = attr("id")
        placeholder = attr("placeholder")
        aria_label = attr("aria-label")
        required = "required" in attrs_str.lower()

        # Build selector
        if input_id:
            selector = f"#{input_id}"
        elif name:
            selector = f'{tag}[name="{name}"]'
        else:
            continue  # Can't target this field

        # Try to find associated label
        label_text = None
        if input_id:
            lm = re.search(
                rf'<label[^>]*for=["\']?{re.escape(input_id)}["\']?[^>]*>([^<]+)',
                raw_html, re.IGNORECASE,
            )
            if lm:
                label_text = lm.group(1).strip()

        fields.append(FormField(
            selector=selector,
            field_type=input_type,
            name=name,
            label=label_text or aria_label,
            placeholder=placeholder,
            required=required,
            identity_key=_guess_identity_key(name, input_type, placeholder, label_text or aria_label),
        ))
    return fields


def _guess_identity_key(
    name: str | None, field_type: str, placeholder: str | None, label: str | None,
) -> str | None:
    """Map a form field to an Identity attribute using heuristics."""
    blob = " ".join(s.lower() for s in (name or "", field_type, placeholder or "", label or "") if s)
    if not blob.strip():
        return None

    if field_type == "email" or "email" in blob:
        return "email"
    if field_type == "password" or "password" in blob:
        return "password"
    if any(k in blob for k in ("first_name", "firstname", "first name", "fname", "given")):
        return "first_name"
    if any(k in blob for k in ("last_name", "lastname", "last name", "lname", "surname", "family")):
        return "last_name"
    if any(k in blob for k in ("fullname", "full_name", "full name", "your name")):
        return "first_name"  # We'll concatenate first+last in the fill logic
    if any(k in blob for k in ("user", "username", "login", "handle", "nickname")):
        return "username"
    if field_type == "tel" or any(k in blob for k in ("phone", "mobile", "tel")):
        return "phone"
    if any(k in blob for k in ("company", "org", "organization", "business")):
        return "company"
    if any(k in blob for k in ("website", "url", "homepage")):
        return "website"
    if any(k in blob for k in ("dob", "birth", "birthday")):
        return "dob"
    if field_type == "checkbox":
        if any(k in blob for k in ("terms", "agree", "accept", "tos", "privacy", "consent")):
            return "_tos_checkbox"
        return None
    return None


async def recon_site(url: str) -> SiteProfile:
    """Firecrawl-powered recon: scrape homepage + signup page, build SiteProfile."""
    domain = _base_domain(url)
    log("recon", f"Scanning {domain}...")

    profile = SiteProfile(domain=domain)
    parsed = urlparse(url)

    # 1. Scrape homepage
    home_data = await _firecrawl_scrape(url)
    if not home_data:
        log("recon", "Firecrawl unavailable or failed; returning minimal profile")
        return profile

    md = home_data.get("markdown", "")
    raw = home_data.get("rawHtml", "")
    log("recon", f"Homepage scraped: {len(md)} chars markdown, {len(raw)} chars HTML")

    # 2. Find signup URL
    signup_url = _find_signup_url(md, raw, url)
    if not signup_url:
        # Try common fallback paths
        for path in _SIGNUP_PATH_FALLBACKS:
            test_url = f"{parsed.scheme}://{parsed.netloc}{path}"
            test_data = await _firecrawl_scrape(test_url, timeout=10)
            if test_data and len(test_data.get("rawHtml", "")) > 500:
                signup_url = test_url
                raw = test_data.get("rawHtml", "")
                md = test_data.get("markdown", "")
                log("recon", f"Found signup at fallback path: {path}")
                break
    if signup_url:
        profile.signup_url = signup_url
        log("recon", f"Signup URL: {signup_url}")

    # 3. Scrape signup page (if different from homepage)
    if signup_url and signup_url.rstrip("/") != url.rstrip("/"):
        signup_data = await _firecrawl_scrape(signup_url)
        if signup_data:
            raw = signup_data.get("rawHtml", raw)
            md = signup_data.get("markdown", md)
            log("recon", f"Signup page scraped: {len(raw)} chars HTML")

    # 4. Analyze
    profile.auth_provider = _detect_auth_provider(raw)
    profile.captcha_type = _detect_captcha(raw)
    profile.oauth_options = _detect_oauth(raw, md)
    profile.form_fields = _extract_form_fields_from_html(raw)

    mapped = [f for f in profile.form_fields if f.identity_key]
    log("recon", f"Auth={profile.auth_provider} CAPTCHA={profile.captcha_type} "
                  f"OAuth={profile.oauth_options} Fields={len(profile.form_fields)} "
                  f"({len(mapped)} mapped)")

    return profile


# ── Live DOM inspection + deterministic fill ─────────────────────────

_DOM_INSPECT_JS = """() => {
    const results = [];

    // Recursive finder that traverses Shadow DOM (Login Machine pattern)
    function findInputs(root) {
        const els = root.querySelectorAll('input, select, textarea');
        for (const el of els) {
            const tag = el.tagName.toLowerCase();
            const type = (el.type || (tag === 'input' ? 'text' : tag)).toLowerCase();
            if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
            // Skip invisible (but allow checkboxes which may be custom-styled)
            try {
                const s = window.getComputedStyle(el);
                if (s.display === 'none' || s.visibility === 'hidden') continue;
            } catch(e) {}

            const id = el.id;
            const name = el.name;
            if (!id && !name) continue;

            const selector = id ? '#' + CSS.escape(id) : tag + '[name="' + CSS.escape(name) + '"]';

            let label = null;
            if (id) {
                const lbl = (root === document ? document : root).querySelector('label[for="' + CSS.escape(id) + '"]');
                if (lbl) label = lbl.textContent.trim();
            }
            if (!label) label = el.getAttribute('aria-label') || null;
            if (!label && el.parentElement && el.parentElement.tagName === 'LABEL') {
                label = el.parentElement.textContent.trim();
            }

            results.push({
                selector: selector,
                field_type: type,
                name: name || null,
                label: label,
                placeholder: el.placeholder || null,
                required: el.required || el.getAttribute('aria-required') === 'true'
            });
        }
        // Traverse shadow roots
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) findInputs(el.shadowRoot);
        }
    }
    findInputs(document);

    // Find submit button (also shadow-DOM-aware)
    let submit = null;
    function findSubmit(root) {
        if (submit) return;
        const buttons = root.querySelectorAll('button[type="submit"], input[type="submit"]');
        if (buttons.length > 0) {
            const btn = buttons[0];
            if (btn.id) submit = '#' + CSS.escape(btn.id);
            else if (btn.name) submit = 'button[name="' + CSS.escape(btn.name) + '"]';
            else submit = 'button[type="submit"]';
            return;
        }
        // Try any button that looks like signup/submit
        for (const btn of root.querySelectorAll('button')) {
            const txt = (btn.textContent || '').toLowerCase();
            if (/sign.?up|register|create|submit|get.?started|continue|next/.test(txt)) {
                if (btn.id) submit = '#' + CSS.escape(btn.id);
                break;
            }
        }
        // Traverse shadow roots
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) findSubmit(el.shadowRoot);
        }
    }
    findSubmit(document);

    return { fields: results, submit_selector: submit };
}"""


async def inspect_signup_form(page: Any) -> tuple[list[FormField], str | None]:
    """Run JS in the live page to extract exact form fields + submit button."""
    try:
        result = await page.evaluate(_DOM_INSPECT_JS)
    except Exception as e:
        log("inspect", f"DOM inspection failed: {e}")
        return [], None

    fields = []
    for f in result.get("fields", []):
        fields.append(FormField(
            selector=f["selector"],
            field_type=f["field_type"],
            name=f.get("name"),
            label=f.get("label"),
            placeholder=f.get("placeholder"),
            required=f.get("required", False),
            identity_key=_guess_identity_key(
                f.get("name"), f["field_type"], f.get("placeholder"), f.get("label"),
            ),
        ))

    submit = result.get("submit_selector")
    mapped = [f for f in fields if f.identity_key]
    log("inspect", f"Found {len(fields)} fields ({len(mapped)} mapped), submit={submit}")
    return fields, submit


def _identity_value(identity: Identity, key: str) -> str | None:
    """Get the value from Identity for a given identity_key."""
    if key == "_tos_checkbox":
        return "__check__"
    if key == "first_name":
        return identity.first_name
    if key == "last_name":
        return identity.last_name
    if key == "full_name":
        return f"{identity.first_name} {identity.last_name}"
    return getattr(identity, key, None)


_REACT_FILL_JS = """(selector, value) => {
    const el = document.querySelector(selector);
    if (!el) return { ok: false, reason: 'not_found' };
    el.focus();
    const nativeSet = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value')?.set
        || Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype, 'value')?.set;
    if (nativeSet) nativeSet.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('blur', { bubbles: true }));
    return { ok: true };
}"""

_CHECKBOX_JS = """(selector) => {
    const el = document.querySelector(selector);
    if (!el) return { ok: false, reason: 'not_found' };
    if (!el.checked) el.click();
    return { ok: true };
}"""


async def deterministic_signup(
    page: Any,
    fields: list[FormField],
    identity: Identity,
    submit_selector: str | None,
) -> bool:
    """Fill signup form deterministically via JS. Returns True if form submitted.

    Uses selector validation + fallback (Login Machine pattern):
    1. Validate primary selector exists in DOM
    2. If missing, find alternative via name/type/placeholder/label/aria
    3. Fill via iframe-aware React-compatible setter
    """
    filled = 0
    required_missed = 0

    for f in fields:
        if not f.identity_key:
            if f.required:
                required_missed += 1
            continue

        value = _identity_value(identity, f.identity_key)
        if not value:
            if f.required:
                required_missed += 1
            continue

        # Selector validation + fallback (Login Machine pattern)
        selector = f.selector
        if not await validate_selector(page, selector):
            alt = await find_alternative_selector(page, f)
            if alt:
                selector = alt
            else:
                log("fill", f"Selector not found and no alternative: {f.selector} ({f.identity_key})")
                if f.required:
                    required_missed += 1
                continue

        try:
            if f.identity_key == "_tos_checkbox":
                result = await page.evaluate(_CHECKBOX_JS, selector)
                ok = result and result.get("ok")
            else:
                # iframe-aware fill (Login Machine pattern) — searches main + iframes
                ok = await fill_in_page_or_iframe(page, selector, value)

            if ok:
                filled += 1
            elif f.required:
                required_missed += 1
        except Exception as e:
            log("fill", f"Failed to fill {selector}: {e}")
            if f.required:
                required_missed += 1

    log("fill", f"Filled {filled} fields, {required_missed} required fields missed")

    if required_missed > 0:
        log("fill", "Cannot submit — required fields unmapped")
        return False

    if filled == 0:
        log("fill", "No fields filled — aborting deterministic signup")
        return False

    # Click submit
    if submit_selector:
        try:
            await page.click(submit_selector)
            log("fill", f"Clicked submit: {submit_selector}")
        except Exception:
            # Fallback: try pressing Enter on last field
            try:
                await page.keyboard.press("Enter")
                log("fill", "Submit click failed, pressed Enter instead")
            except Exception as e:
                log("fill", f"Submit failed entirely: {e}")
                return False
    else:
        # No submit selector — try Enter
        try:
            await page.keyboard.press("Enter")
            log("fill", "No submit selector, pressed Enter")
        except Exception as e:
            log("fill", f"Enter key failed: {e}")
            return False

    # Wait for navigation/response
    await asyncio.sleep(3)

    # Check for success signals
    try:
        current_url = page.url
        body_text = await page.evaluate("() => document.body?.innerText?.substring(0, 2000) || ''")

        # Success signals
        success_patterns = [
            "check your email", "verify your email", "confirmation link",
            "we sent", "verification email", "dashboard", "welcome",
            "account created", "successfully registered", "almost done",
            "confirm your", "activate your", "one more step",
        ]
        for pat in success_patterns:
            if pat in body_text.lower():
                log("fill", f"Success signal detected: '{pat}'")
                return True

        # Check for URL change (redirect to dashboard/verify page = success)
        if current_url and submit_selector:
            parsed_current = urlparse(current_url)
            if any(k in parsed_current.path.lower() for k in (
                "dashboard", "verify", "confirm", "welcome", "onboarding",
                "check-email", "success", "complete",
            )):
                log("fill", f"Success: redirected to {parsed_current.path}")
                return True

        # Error signals — form didn't submit properly
        error_patterns = ["invalid", "error", "required", "already exists", "try again"]
        for pat in error_patterns:
            if pat in body_text.lower()[:500]:
                log("fill", f"Error signal detected: '{pat}' — form may have validation errors")
                return False

    except Exception as e:
        log("fill", f"Post-submit check failed: {e}")

    # Ambiguous — assume it worked if we filled fields and clicked submit
    log("fill", "No clear success/error signal — assuming submission worked")
    return True


# ── DOM-first API key scanning ────────────────────────────────────────

_CLIPBOARD_INTERCEPT_JS = """() => {
    // Intercept clipboard.writeText so we can read back copied API keys.
    // Sites like Helicone hide keys behind copy-to-clipboard buttons.
    if (!window.__sigma_clipboard_intercepted) {
        window.__sigma_clipboard_values = [];
        const original = navigator.clipboard.writeText.bind(navigator.clipboard);
        navigator.clipboard.writeText = async function(text) {
            window.__sigma_clipboard_values.push(text);
            return original(text);
        };
        // Also intercept execCommand('copy') for older implementations
        const origExec = document.execCommand.bind(document);
        document.execCommand = function(cmd, ...args) {
            if (cmd === 'copy') {
                const sel = window.getSelection();
                if (sel && sel.toString()) {
                    window.__sigma_clipboard_values.push(sel.toString());
                }
            }
            return origExec(cmd, ...args);
        };
        window.__sigma_clipboard_intercepted = true;
    }
    return true;
}"""

_API_KEY_SCAN_JS = """() => {
    const keyPatterns = [
        /sk[-_](?:live|test)[-_][a-zA-Z0-9]{20,}/,
        /pk[-_](?:live|test)[-_][a-zA-Z0-9]{20,}/,
        /AKIA[0-9A-Z]{16}/,
        /ghp_[a-zA-Z0-9]{36}/,
        /glpat-[a-zA-Z0-9\\-]{20,}/,
        /[a-f0-9]{32}/,
        /[a-f0-9]{40}/,
        /[a-f0-9]{64}/,
    ];
    const candidates = [];

    // Scan visible text in code blocks, pre elements, input values
    const textSources = [
        ...document.querySelectorAll('code, pre, .api-key, [data-testid*="key"], [data-testid*="token"]'),
        ...document.querySelectorAll('input[type="text"], input[type="password"], input[readonly]'),
    ];
    for (const el of textSources) {
        const text = el.value || el.textContent || '';
        if (text.length < 16 || text.length > 256) continue;
        for (const pat of keyPatterns) {
            const m = text.match(pat);
            if (m) candidates.push({ key: m[0], source: el.tagName + (el.className ? '.' + el.className.split(' ')[0] : '') });
        }
    }

    // Scan clipboard-copy buttons (GitHub-style)
    for (const btn of document.querySelectorAll('[data-clipboard-text], [data-copy]')) {
        const text = btn.getAttribute('data-clipboard-text') || btn.getAttribute('data-copy') || '';
        if (text.length >= 16 && text.length <= 256) {
            for (const pat of keyPatterns) {
                const m = text.match(pat);
                if (m) candidates.push({ key: m[0], source: 'clipboard-attr' });
            }
        }
    }

    return candidates;
}"""


async def inject_clipboard_intercept(page: Any) -> None:
    """Inject clipboard interception early so we can read back copied API keys."""
    try:
        await page.evaluate(_CLIPBOARD_INTERCEPT_JS)
        log("clipboard", "Clipboard interception injected")
    except Exception as e:
        log("clipboard", f"Clipboard injection failed (non-fatal): {e}")


async def _inject_clipboard_safe(browser: Any) -> None:
    """Inject clipboard intercept into current page (no-op if page unavailable)."""
    try:
        page = await browser.get_current_page()
        if page:
            await inject_clipboard_intercept(page)
    except Exception:
        pass


async def inject_stealth(browser: Browser) -> None:
    """Inject stealth JS via CDP init script (runs before every page load)."""
    try:
        await browser._cdp_add_init_script(_STEALTH_JS)
        log("stealth", "Stealth JS injected via CDP init script")
    except Exception:
        pass  # Best effort — cloud browser may not support this


async def scan_for_api_key(page: Any) -> str | None:
    """DOM-first API key scanning — try to find keys without using the agent."""
    try:
        # First check intercepted clipboard values (from copy buttons)
        clipboard_values = await page.evaluate(
            "() => (window.__sigma_clipboard_values || []).filter(v => v && v.length >= 16 && v.length <= 256)"
        )
        if isinstance(clipboard_values, list) and clipboard_values:
            # Return the most recent clipboard value (last copied)
            key = clipboard_values[-1]
            log("api-scan", f"Found API key via clipboard intercept: {key[:8]}...{key[-4:]}")
            return key
    except Exception:
        pass

    try:
        candidates = await page.evaluate(_API_KEY_SCAN_JS)
        if not isinstance(candidates, list) or not candidates:
            return None
        # Prefer longer keys (more likely to be real)
        candidates = sorted(candidates, key=lambda c: len(c.get("key", "") if isinstance(c, dict) else ""), reverse=True)
        if not candidates or not isinstance(candidates[0], dict):
            return None
        key = candidates[0].get("key", "")
        if key and len(key) >= 16:
            log("api-scan", f"Found API key via DOM scan: {key[:8]}...{key[-4:]} (source: {candidates[0].get('source', '?')})")
            return key
    except Exception as e:
        log("api-scan", f"DOM scan failed: {e}")
    return None


# ── Post-action page validation ───────────────────────────────────────


async def validate_page_state(page: Any, expected: str) -> dict[str, Any]:
    """Check current page state for success/failure signals after an action.

    Args:
        expected: What we expect to see — "signup_success", "login_success", "dashboard"

    Returns:
        dict with 'success' bool, 'url', 'signals' list, 'body_preview'
    """
    try:
        url = page.url
        body = await page.evaluate("() => document.body?.innerText?.substring(0, 3000) || ''")
        body_low = body.lower()

        signals = []
        success = False

        if expected == "signup_success":
            for s in ("check your email", "verify your email", "account created",
                      "successfully registered", "welcome", "confirmation", "almost done"):
                if s in body_low:
                    signals.append(s)
                    success = True
            if any(k in (url or "").lower() for k in ("dashboard", "verify", "confirm", "welcome")):
                signals.append(f"url:{url}")
                success = True
        elif expected in ("login_success", "dashboard"):
            for s in ("dashboard", "welcome", "workspace", "settings", "api key",
                      "api keys", "overview", "home", "projects"):
                if s in body_low:
                    signals.append(s)
                    success = True
            if any(k in (url or "").lower() for k in ("dashboard", "settings", "home", "workspace")):
                signals.append(f"url:{url}")
                success = True

        # Negative signals
        for neg in ("error", "invalid", "forbidden", "access denied", "not found"):
            if neg in body_low[:500]:
                signals.append(f"NEGATIVE:{neg}")

        return {"success": success, "url": url, "signals": signals, "body_preview": body[:200]}
    except Exception as e:
        return {"success": False, "url": None, "signals": [f"error:{e}"], "body_preview": ""}


# ── Login Machine patterns ────────────────────────────────────────────
# Adapted from github.com/RichardHruby/login-machine

_STRIP_HTML_JS = """() => {
    function extractHTML(node) {
        if (node.nodeType === 3) return node.textContent?.trim() || "";
        if (node.nodeType !== 1) return "";
        const el = node;
        const styles = window.getComputedStyle(el);
        if (styles.display === "none" || styles.visibility === "hidden") return "";
        const exclude = ["SCRIPT", "STYLE", "svg", "IMG", "NOSCRIPT", "LINK"];
        if (exclude.includes(el.tagName)) return "";
        const root = el.shadowRoot || el;
        let html = "<" + el.tagName.toLowerCase();
        for (const attr of el.attributes) {
            if (["id", "class", "type", "name", "placeholder",
                 "role", "aria-label", "href", "for", "value",
                 "maxlength", "inputmode", "autocomplete"].includes(attr.name)) {
                html += " " + attr.name + '="' + attr.value + '"';
            }
        }
        html += ">";
        for (const child of root.childNodes) {
            if (child instanceof HTMLSlotElement) {
                const assigned = child.assignedNodes()[0];
                html += assigned ? extractHTML(assigned) : child.innerHTML;
            } else {
                html += extractHTML(child);
            }
        }
        html += "</" + el.tagName.toLowerCase() + ">";
        return html;
    }
    return extractHTML(document.body);
}"""


async def extract_stripped_html(page: Any, max_chars: int = 80000) -> str:
    """Extract stripped HTML from page (Login Machine pattern).

    Walks the DOM, keeps only locator-relevant attributes. Traverses Shadow DOM.
    Skips invisible elements and noise tags. ~10x token reduction vs raw HTML.
    """
    try:
        html = await page.evaluate(_STRIP_HTML_JS)
        # Also extract iframe content
        if hasattr(page, 'frames'):
            for frame in page.frames:
                try:
                    if hasattr(frame, 'evaluate'):
                        iframe_html = await frame.evaluate(_STRIP_HTML_JS)
                        if iframe_html:
                            html += f"<iframe-content>{iframe_html}</iframe-content>"
                except Exception:
                    pass  # cross-origin frames
        return (html or "")[:max_chars]
    except Exception:
        return ""


_WAIT_FOR_CONTENT_JS = """() => {
    const body = document.body;
    if (!body) return false;
    return (
        body.querySelectorAll('input, button, a[href]').length >= 2 ||
        (body.innerText || '').trim().length > 100
    );
}"""


async def wait_for_page_content(page: Any, timeout_ms: int = 15000) -> None:
    """Wait for SPA to render meaningful content (Login Machine pattern).

    Waits until: 2+ interactive elements (input/button/link) appear,
    OR body has 100+ chars of text. Max wait = timeout_ms.
    """
    try:
        await page.wait_for_function(_WAIT_FOR_CONTENT_JS, timeout=timeout_ms)
    except Exception:
        pass  # timeout is fine — proceed with whatever we have
    await asyncio.sleep(1)


async def fill_in_page_or_iframe(page: Any, selector: str, value: str) -> bool:
    """Fill a field, searching main page then iframes (Login Machine pattern).

    Uses React-compatible native setter + events. Falls back to iframes
    for Auth0/Okta SSO widgets.
    """
    fill_js = """(selector, value) => {
        const el = document.querySelector(selector);
        if (!el) return { ok: false, reason: 'not_found' };
        el.focus();
        const nativeSet = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value')?.set
            || Object.getOwnPropertyDescriptor(
            window.HTMLTextAreaElement.prototype, 'value')?.set;
        if (nativeSet) nativeSet.call(el, value);
        else el.value = value;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new Event('blur', { bubbles: true }));
        return { ok: true };
    }"""
    # Try main frame
    try:
        result = await page.evaluate(fill_js, selector, value)
        if result and result.get("ok"):
            return True
    except Exception:
        pass
    # Try iframes (Auth0/Okta render login forms in iframes)
    try:
        frames = page.frames if hasattr(page, 'frames') else []
        for frame in frames:
            try:
                result = await frame.evaluate(fill_js, selector, value)
                if result and result.get("ok"):
                    log("fill", f"Filled {selector} in iframe")
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


# ── Selector validation + fallback (Login Machine pattern) ───────────


_VALIDATE_SELECTOR_JS = """(selector) => {
    // Check main document
    let el = document.querySelector(selector);
    if (el) return { found: true, frame: 'main' };
    // Check shadow roots
    function findInShadow(root) {
        for (const node of root.querySelectorAll('*')) {
            if (node.shadowRoot) {
                el = node.shadowRoot.querySelector(selector);
                if (el) return true;
                if (findInShadow(node.shadowRoot)) return true;
            }
        }
        return false;
    }
    if (findInShadow(document)) return { found: true, frame: 'shadow' };
    return { found: false, frame: null };
}"""


async def validate_selector(page: Any, selector: str) -> bool:
    """Check if a CSS selector resolves to an element in the page DOM (incl. Shadow DOM)."""
    try:
        result = await page.evaluate(_VALIDATE_SELECTOR_JS, selector)
        return bool(result and result.get("found"))
    except Exception:
        return False


_FIND_ALTERNATIVE_JS = """(hints) => {
    // Given field metadata, try to find the element via multiple strategies
    const name = hints.name;
    const fieldType = hints.field_type;
    const placeholder = hints.placeholder;
    const label = hints.label;
    const identityKey = hints.identity_key;

    function trySelector(sel) {
        try {
            const el = document.querySelector(sel);
            if (el && el.offsetParent !== null) return sel;
        } catch(e) {}
        return null;
    }

    // Strategy 1: by name attribute
    if (name) {
        const s = trySelector('input[name="' + CSS.escape(name) + '"]');
        if (s) return s;
        const s2 = trySelector('textarea[name="' + CSS.escape(name) + '"]');
        if (s2) return s2;
    }

    // Strategy 2: by type + identity key mapping
    const typeMap = {
        'email': 'input[type="email"]',
        'password': 'input[type="password"]',
        'tel': 'input[type="tel"]',
    };
    if (fieldType && typeMap[fieldType]) {
        const s = trySelector(typeMap[fieldType]);
        if (s) return s;
    }

    // Strategy 3: by placeholder text
    if (placeholder) {
        const inputs = document.querySelectorAll('input, textarea');
        for (const el of inputs) {
            if (el.placeholder && el.placeholder.toLowerCase().includes(placeholder.toLowerCase().substring(0, 10))) {
                if (el.id) return '#' + CSS.escape(el.id);
                if (el.name) return el.tagName.toLowerCase() + '[name="' + CSS.escape(el.name) + '"]';
                // Fallback: nth-of-type
                const idx = Array.from(el.parentElement.children).indexOf(el);
                return el.tagName.toLowerCase() + ':nth-child(' + (idx+1) + ')';
            }
        }
    }

    // Strategy 4: by label text
    if (label) {
        const labels = document.querySelectorAll('label');
        for (const lbl of labels) {
            if (lbl.textContent.toLowerCase().includes(label.toLowerCase().substring(0, 10))) {
                const forId = lbl.getAttribute('for');
                if (forId) {
                    const s = trySelector('#' + CSS.escape(forId));
                    if (s) return s;
                }
                // Try input inside label
                const innerInput = lbl.querySelector('input, textarea, select');
                if (innerInput) {
                    if (innerInput.id) return '#' + CSS.escape(innerInput.id);
                    if (innerInput.name) return innerInput.tagName.toLowerCase() + '[name="' + CSS.escape(innerInput.name) + '"]';
                }
            }
        }
    }

    // Strategy 5: by aria-label matching identity key
    if (identityKey) {
        const ariaMap = {
            'email': ['email', 'e-mail'],
            'password': ['password', 'passwd'],
            'first_name': ['first name', 'first', 'given name'],
            'last_name': ['last name', 'last', 'family name', 'surname'],
            'username': ['username', 'user name', 'handle'],
            'phone': ['phone', 'telephone', 'mobile'],
        };
        const terms = ariaMap[identityKey] || [identityKey.replace('_', ' ')];
        const inputs = document.querySelectorAll('input, textarea');
        for (const el of inputs) {
            const ariaLabel = (el.getAttribute('aria-label') || '').toLowerCase();
            const autocomp = (el.getAttribute('autocomplete') || '').toLowerCase();
            for (const term of terms) {
                if (ariaLabel.includes(term) || autocomp.includes(term)) {
                    if (el.id) return '#' + CSS.escape(el.id);
                    if (el.name) return el.tagName.toLowerCase() + '[name="' + CSS.escape(el.name) + '"]';
                }
            }
        }
    }

    return null;
}"""


async def find_alternative_selector(page: Any, field: FormField) -> str | None:
    """Find alternative CSS selector for a field when the primary one fails."""
    try:
        hints = {
            "name": field.name,
            "field_type": field.field_type,
            "placeholder": field.placeholder,
            "label": field.label,
            "identity_key": field.identity_key,
        }
        alt = await page.evaluate(_FIND_ALTERNATIVE_JS, hints)
        if alt:
            log("selector", f"Alternative found: {field.selector} → {alt}")
        return alt
    except Exception:
        return None


# ── Screen type classification (Login Machine pattern) ───────────────

_CLASSIFY_SCREEN_JS = """() => {
    const body = document.body;
    if (!body) return { type: 'loading', confidence: 0.5, detail: 'no body' };

    const text = (body.innerText || '').toLowerCase();
    const textLen = text.trim().length;
    const url = window.location.href.toLowerCase();
    const html = body.innerHTML || '';
    const htmlLower = html.toLowerCase();

    // Count interactive elements
    const inputs = body.querySelectorAll('input:not([type="hidden"])');
    const emailInputs = body.querySelectorAll('input[type="email"], input[name*="email"]');
    const passwordInputs = body.querySelectorAll('input[type="password"]');
    const buttons = body.querySelectorAll('button, input[type="submit"]');

    // Check for OAuth buttons
    const oauthTerms = ['google', 'github', 'sso', 'saml', 'sign in with', 'continue with', 'log in with'];
    let oauthCount = 0;
    for (const btn of buttons) {
        const btnText = (btn.textContent || '').toLowerCase();
        if (oauthTerms.some(t => btnText.includes(t))) oauthCount++;
    }
    // Also check links
    for (const a of body.querySelectorAll('a')) {
        const aText = (a.textContent || '').toLowerCase();
        if (oauthTerms.some(t => aText.includes(t))) oauthCount++;
    }

    // Check for CAPTCHA
    const hasCaptcha = htmlLower.includes('cf-turnstile') ||
                       htmlLower.includes('g-recaptcha') ||
                       htmlLower.includes('h-captcha') ||
                       htmlLower.includes('captcha');

    // Loading detection
    if (textLen < 50 && inputs.length === 0 && buttons.length === 0) {
        return { type: 'loading', confidence: 0.7, detail: 'minimal content' };
    }

    // Already authenticated
    const authSignals = ['dashboard', 'workspace', 'settings', 'api key', 'log out', 'sign out', 'my account'];
    const authUrlSignals = ['dashboard', '/settings', '/workspace', '/home', '/projects'];
    let authScore = 0;
    for (const s of authSignals) { if (text.includes(s)) authScore++; }
    for (const s of authUrlSignals) { if (url.includes(s)) authScore++; }
    if (authScore >= 2 && emailInputs.length === 0 && passwordInputs.length === 0) {
        return { type: 'authenticated', confidence: 0.8, detail: 'dashboard signals', authScore };
    }

    // Captcha blocked
    if (hasCaptcha && inputs.length <= 1 && text.includes('verify')) {
        return { type: 'captcha_blocked', confidence: 0.7, detail: 'captcha gate' };
    }

    // OAuth-only (OAuth buttons present but NO email/password inputs)
    if (oauthCount > 0 && emailInputs.length === 0 && passwordInputs.length === 0) {
        return { type: 'oauth_only', confidence: 0.8, detail: oauthCount + ' oauth buttons', oauthCount };
    }

    // Signup/login form with email + password
    if (emailInputs.length > 0 && passwordInputs.length > 0) {
        return { type: 'credential_form', confidence: 0.9, detail: 'email+password inputs',
                 emailInputs: emailInputs.length, passwordInputs: passwordInputs.length,
                 hasCaptcha: hasCaptcha, oauthCount: oauthCount };
    }

    // Email-only form (magic link or step 1 of multi-step)
    if (emailInputs.length > 0 && passwordInputs.length === 0) {
        return { type: 'email_only_form', confidence: 0.7, detail: 'email input, no password',
                 hasCaptcha: hasCaptcha };
    }

    // Generic form (has inputs but they're not obviously email/password)
    if (inputs.length >= 2) {
        return { type: 'generic_form', confidence: 0.5, detail: inputs.length + ' inputs',
                 hasCaptcha: hasCaptcha, oauthCount: oauthCount };
    }

    // Error page
    const errorTerms = ['access denied', 'forbidden', '404', 'not found', 'error'];
    for (const e of errorTerms) {
        if (text.substring(0, 500).includes(e)) {
            return { type: 'error_page', confidence: 0.6, detail: e };
        }
    }

    // Verification/confirmation page
    const verifyTerms = ['check your email', 'verify your email', 'confirmation link', 'almost done'];
    for (const v of verifyTerms) {
        if (text.includes(v)) {
            return { type: 'verification_pending', confidence: 0.8, detail: v };
        }
    }

    return { type: 'unknown', confidence: 0.3, detail: textLen + ' chars, ' + inputs.length + ' inputs' };
}"""


async def classify_screen(page: Any) -> dict[str, Any]:
    """Classify the current page type (Login Machine pattern).

    Returns dict with 'type' (one of: loading, authenticated, captcha_blocked,
    oauth_only, credential_form, email_only_form, generic_form, error_page,
    verification_pending, unknown), 'confidence', 'detail'.
    """
    try:
        result = await page.evaluate(_CLASSIFY_SCREEN_JS)
        if result:
            log("classify", f"Screen: {result.get('type')} (conf={result.get('confidence', 0):.1f}) — {result.get('detail', '')}")
            return result
    except Exception as e:
        log("classify", f"Classification failed: {e}")
    return {"type": "unknown", "confidence": 0.0, "detail": "classify failed"}


# ── Error history for agent retries (Login Machine pattern) ──────────

def build_error_history_context(errors: list[str]) -> str:
    """Format previous errors into XML tags for agent prompt context.

    Login Machine pattern: feed specific errors back to the LLM so it can
    self-correct rather than repeating the same mistake.
    """
    if not errors:
        return ""
    history = "\n".join(f"  Attempt {i+1}: {e}" for i, e in enumerate(errors))
    return f"""

<error-history>
Previous attempts failed with these errors:
{history}
Do NOT repeat these same actions. Try alternative approaches.
</error-history>"""


# ── Agent runner ──────────────────────────────────────────────────────


async def run_agent(
    browser: Browser, llm: Any, label: str, task: str,
    max_steps: int = 15, timeout_s: int = 180,
    retries: int = 1,
    sensitive_data: dict[str, str] | None = None,
    inject_page_html: bool = False,
    controller: Controller | None = None,
    initial_actions: list[dict[str, dict[str, Any]]] | None = None,
) -> RunResult:
    """Run browser-use agent with speed params, EventBus detection, and retries."""
    # Inject stripped HTML context so the agent sees a clean DOM view
    if inject_page_html:
        try:
            page = await browser.get_current_page()
            stripped = await extract_stripped_html(page, max_chars=20000)
            if stripped and len(stripped) > 80:
                task = (
                    f"<page-html>\n{stripped}\n</page-html>\n\n{task}"
                )
        except Exception:
            pass  # page may not be loaded yet — agent will navigate

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
            "If a <page-html> block is provided in the task, it contains a stripped DOM snapshot "
            "of the current page with only key attributes (id, class, type, name, placeholder, role). "
            "Use it to identify the correct form fields, buttons, and links before acting."
        ),
    }
    # Pass structured output controller if provided
    if controller:
        speed_kwargs["controller"] = controller
    # Pre-navigation actions (no LLM cost) — e.g., navigate to known signup URL
    if initial_actions:
        speed_kwargs["initial_actions"] = initial_actions
    if is_bu:
        speed_kwargs["flash_mode"] = True
    # Auto-inject global sensitive data (credentials)
    merged_sensitive = {**_GLOBAL_SENSITIVE, **(sensitive_data or {})}
    if merged_sensitive:
        speed_kwargs["sensitive_data"] = merged_sensitive

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
    # One-time-use verification tokens — HTTP GET consumes the token but doesn't
    # complete verification in the browser context. Force browser verification.
    link_low = link.lower()
    if "ticket=" in link_low and any(p in link_low for p in ("auth.", "/u/email-verification", "auth0")):
        return False, None
    # Supabase verify tokens are also one-time use and need browser context
    if "supabase" in link_low and "/auth/v1/verify" in link_low:
        return False, None
    # Clerk verify tokens
    if "clerk" in link_low and "verify" in link_low:
        return False, None

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
    norm = text.replace("\\n", "\n").strip()
    # Try structured JSON first (from Controller structured output)
    try:
        if norm.startswith("{") and norm.endswith("}"):
            obj = json.loads(norm)
            status = str(obj.get("status", "UNKNOWN")).upper()
            details = obj.get("details") or None
            return status, details
    except Exception:
        pass
    # Regex fallback for free-text output
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
    # Try JSON first (from Controller structured output or agent JSON)
    try:
        if norm.startswith("{") and norm.endswith("}"):
            obj = json.loads(norm)
            # Handle both UPPER (legacy agent) and lower (Pydantic model) keys
            key_map = {
                "LOGIN": ["LOGIN", "login"],
                "API_KEY": ["API_KEY", "api_key"],
                "API_KEY_URL": ["API_KEY_URL", "api_key_url"],
                "LOGIN_URL": ["LOGIN_URL", "login_url"],
                "NOTES": ["NOTES", "notes"],
            }
            for k, aliases in key_map.items():
                for alias in aliases:
                    v = obj.get(alias)
                    if v is not None:
                        result[k] = str(v).strip()
                        break
            return result
    except Exception:
        pass
    # Split "KEY: val / KEY: val" format into separate lines for cleaner parsing
    # This handles the common single-line agent output format
    norm_split = re.sub(r"\s*/\s*(?=(LOGIN|API_KEY|API_KEY_URL|LOGIN_URL|NOTES)\s*:)", "\n", norm)
    # Regex fallback
    for k in result:
        m = re.search(rf"(?im)^\s*{k}\s*:\s*([^\n]+)\s*$", norm_split)
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
        if not any(t in notes_l for t in ("copied", "clipboard", "revealed", "full value", "retrieve", "created a new")):
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
    # Check structured output verification_type field
    if '"verification_type"' in low and '"magic_link"' in low:
        return True
    return any(h in low for h in ("magic link", "magic_link", "passwordless", "sign-in link"))


def _infer_otp_only(signup_text: str, profile: SiteProfile | None = None) -> bool:
    """Detect OTP-only auth (no password) from signup output.

    Only trusts explicit OTP signals in the signup agent's output.
    Does NOT infer from missing password field in profile — Auth0/Clerk sites
    often hide the password on a second screen that recon doesn't capture.
    """
    low = (signup_text or "").lower()
    # Check structured output verification_type field
    if '"verification_type"' in low and ('"otp"' in low or '"code"' in low):
        return True
    otp_signals = ("otp", "one-time password", "one time password", "send otp",
                   "verification code sent", "code sent to", "enter otp")
    return any(s in low for s in otp_signals)


# ── Task prompts ──────────────────────────────────────────────────────


def build_signup_task(
    url: str, identity: Identity, profile: SiteProfile | None = None,
) -> str:
    local_phone = re.sub(r"\D", "", identity.phone)[-10:] if identity.phone else ""

    # Profile-informed hints
    nav_hint = ""
    if profile and profile.signup_url:
        nav_hint = f"Go directly to {profile.signup_url} (known signup page).\n"
    else:
        nav_hint = (
            f"Open {url} and find the signup page from visible UI "
            "(Sign up/Register/Create account).\n"
        )

    field_hint = ""
    if profile and profile.form_fields:
        mapped = [f for f in profile.form_fields if f.identity_key]
        if mapped:
            names = [f.identity_key for f in mapped]
            field_hint = f"This form is known to have fields: {', '.join(names)}. Fill them all.\n"

    auth_hint = ""
    if profile and profile.auth_provider:
        auth_hint = f"This site uses {profile.auth_provider} authentication.\n"

    captcha_hint = ""
    if profile and profile.captcha_type:
        captcha_hint = f"Expect a {profile.captcha_type} captcha — handle it when it appears.\n"

    return f"""
{nav_hint}Register a NEW account by email (not OAuth/Google/GitHub).
Stay on the target website domain only.
Do NOT use search tools, open email providers, or navigate off-domain.
Do NOT create files, todo lists, or notes.
{field_hint}{auth_hint}{captcha_hint}
Credentials (copy exact text inside backticks, no extra punctuation/spaces):
- first_name: `{identity.first_name}`
- last_name: `{identity.last_name}`
- username: `{identity.username}`
- email: `x_email`
- password: `x_password`
- dob: `{identity.dob}`
- company: `{identity.company}`
- website: `{identity.website}`
- phone: `x_phone`

Rules:
1) Choose signup/register/create account (never login). If Google/GitHub/SSO appears, skip it.
2) Close blocking popups/modals/cookie banners first.
3) Fill ALL visible form fields and submit. Use provided values for matching fields.
4) Phone: if a country/flag selector is next to the phone input, leave it on US/+1 and type only
   `x_local_phone` (10 digits, no country code). Otherwise, type `x_phone` (full E.164).
   If E.164 error appears, clear the field and try the other format.
5) If submit/continue button does nothing after clicking, look for validation errors or
   unfilled required fields (phone, ToS checkbox, etc). Fill them and retry submit.
6) If a Cloudflare Turnstile captcha/checkbox appears, click it and wait for it to resolve.
   If other captcha appears, solve it (wait up to 45s). On failure, refresh and retry ONCE.
   After second captcha failure: output SIGNUP_FAILED with DETAILS: CAPTCHA_FAILED_OR_TIMEOUT.
7) If you reach an OTP/email-code screen, STOP and output NEEDS_VERIFICATION.
8) If same submit error repeats twice, STOP and output SIGNUP_FAILED with DETAILS.
9) Stop immediately after successful submit.

When done, call `done` with structured output:
- status: SIGNUP_SUCCESS or NEEDS_VERIFICATION or SIGNUP_FAILED
- details: short reason
- verification_type: link, code, magic_link, otp, or null
""".strip()


def build_oauth_signup_task(
    url: str, identity: Identity, oauth_provider: str = "google",
    profile: SiteProfile | None = None,
) -> str:
    """Build a signup task that uses OAuth (Google/GitHub) instead of email."""
    nav_hint = ""
    if profile and profile.signup_url:
        nav_hint = f"Go directly to {profile.signup_url} (known signup page).\n"
    else:
        nav_hint = f"Open {url} and find the signup page.\n"

    provider_priority = (
        "Google first, then GitHub/SSO"
        if oauth_provider == "google"
        else "GitHub first, then Google/SSO"
        if oauth_provider == "github"
        else "Google first, then GitHub/SSO"
    )

    return f"""
{nav_hint}Sign up using OAuth ({provider_priority}).
Use the existing account-picker session — do NOT enter any email or password manually.
If the first OAuth provider asks for manual login/password/2FA, go back and try another OAuth option.

Rules:
1) Click "Sign in with Google" / "Continue with Google" (or GitHub).
2) Use the EXISTING Google/GitHub account in the browser session (account picker).
   NEVER type any email or password into Google/GitHub.
3) If OAuth returns to the site and asks for profile info:
   - first_name: `{identity.first_name}`
   - last_name: `{identity.last_name}`
   - username: `{identity.username}`
   - company: `{identity.company}`
4) If no OAuth option exists: output SIGNUP_FAILED with DETAILS: OAUTH_OPTION_NOT_FOUND.
5) If OAuth says 'No account found' or not eligible: output SIGNUP_FAILED with DETAILS: OAUTH_ACCOUNT_NOT_ELIGIBLE.
6) If all OAuth providers require manual login: output SIGNUP_FAILED with DETAILS: OAUTH_LOGIN_REQUIRED.
7) After successful signup/redirect to dashboard: output SIGNUP_SUCCESS.

When done, call `done` with structured output:
- status: SIGNUP_SUCCESS or NEEDS_VERIFICATION or SIGNUP_FAILED
- details: short reason
- verification_type: link, code, magic_link, otp, or null
""".strip()


def build_login_apikey_task(
    url: str, identity: Identity, profile: SiteProfile | None = None,
) -> str:
    login_target = (profile.login_url if profile and profile.login_url else url)
    api_key_hint = ""
    if profile and profile.api_key_url:
        api_key_hint = f"   Known API key page: {profile.api_key_url}\n"

    return f"""
Stay on domain {url}. Do not visit email providers or other sites.
Do not create files, todo lists, or notes.

1) Check current auth state. If you see an authenticated dashboard/workspace/profile,
   treat LOGIN as SUCCESS and skip to API key discovery.
2) If not authenticated, log in at {login_target}:
   - email: `x_email`
   - password: `x_password`
   Copy exact text inside backticks only.
3) If login needs magic link/2FA, or says "No account found", stop immediately.
4) Find API key/token page fast:
{api_key_hint}   Try direct paths: /api-keys, /settings/api, /settings/api-keys,
   /account/api-keys, /developer/api, /developer/api-keys.
   Then check Settings, Developer, Integrations (max 2 steps per dead path).
5) If key exists in plaintext, copy the FULL secret value.
6) If no key exists, create one (name "sigma"), submit, copy full secret shown.
7) If key is masked, click "Copy" or clipboard icon buttons to copy it, then output the key.
   If there's a "Reveal" button, click that first. If never shown, output API_KEY: NONE.
8) Never invent values. If unsure, output API_KEY: NONE.
9) If onboarding blocks access (create team/workspace), complete with safe defaults first.

When done, call `done` with structured output:
- login: SUCCESS or FAILED
- api_key: the full API key string, or null if not found
- api_key_url: URL of the API key page, or null
- login_url: URL of the login page, or null
- notes: short description of what happened
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
    # v3 options
    use_cloud: bool = True,
    no_recon: bool = False,
    refresh_profile: bool = False,
    headed: bool = False,
    custom_profile_dir: str | None = None,
    oauth_profile_id: str | None = None,
    no_oauth_fallback: bool = False,
):
    global _t0
    _t0 = time.time()

    # Fail fast on missing required env vars
    missing = []
    if use_cloud and not BROWSER_USE_API_KEY:
        missing.append("BROWSER_USE_API_KEY")
    if not AGENTMAIL_API_KEY:
        missing.append("AGENTMAIL_API_KEY")
    if missing:
        raise RuntimeError(f"Required env vars not set: {', '.join(missing)}")

    resolved_model = resolve_model(model)
    llm = make_llm(model)
    log("model", f"Requested={model} Resolved={resolved_model}")

    mail = AsyncAgentMail(api_key=AGENTMAIL_API_KEY, timeout=20)
    domain = _base_domain(url)
    base_url = url.rstrip("/")

    # ── Recon: load or build site profile ──
    site_profile: SiteProfile | None = None
    if not no_recon:
        site_profile = load_profile(domain, custom_profile_dir)
        if site_profile and not refresh_profile:
            log("profile", f"Loaded cached profile (confidence={site_profile.confidence:.2f}, "
                           f"successes={site_profile.success_count}, failures={site_profile.failure_count})")
        else:
            log("recon", "Running Firecrawl recon...")
            site_profile = await recon_site(url)
            save_profile(site_profile, custom_profile_dir)

    # ── HAR capture path ──
    har_path = _profile_dir(custom_profile_dir) / f"{domain}.har"
    use_local = not use_cloud

    # ── Pre-flight: free zombie cloud sessions ──
    if not use_local:
        with contextlib.suppress(Exception):
            n_killed = await stop_all_active_cloud_sessions()
            if n_killed:
                log("setup", f"Freed {n_killed} zombie session(s)")
        for _ in range(3):
            stopped = await stop_oldest_active_cloud_session()
            if not stopped:
                break
            log("setup", f"Cleaned up stale session: {stopped}")

    # ── Create browser (cloud by default, local with --local or no API key) ──
    if use_local:
        log("setup", "Creating local Playwright browser...")
        browser = await create_local_browser(headless=not headed, har_path=har_path)
    else:
        log("setup", "Creating browser-use cloud browser...")
        browser = await create_cloud_browser(profile_id, har_path=har_path)
    # Inject stealth JS (anti-bot detection) — best effort, non-blocking
    await inject_stealth(browser)

    # ── Create inbox + identity ──
    log("setup", "Creating inbox...")
    inbox_id, created_inbox = await acquire_inbox(mail, domain=domain)
    identity = generate_identity(inbox_id)
    log("setup", f"Email: {identity.email}")
    log("creds", f"{identity.first_name} {identity.last_name} / {identity.username} / {identity.password}")

    run_started_at = datetime.now(timezone.utc)

    # Sensitive data map — keeps credentials out of agent logs
    global _GLOBAL_SENSITIVE
    local_phone = identity.phone[2:] if identity.phone.startswith("+1") else identity.phone
    _GLOBAL_SENSITIVE = {
        "x_email": identity.email,
        "x_password": identity.password,
        "x_phone": identity.phone,
        "x_local_phone": local_phone,
    }

    active_browser_profile = profile_id  # tracks which browser-use profile is in use

    # ── Structured output controllers (replaces regex parsing) ──
    signup_ctrl = make_controller(SignupOutput)
    login_ctrl = make_controller(LoginApiKeyOutput)

    async def rebuild_browser(reason: str, use_profile: str | None = None):
        nonlocal browser, active_browser_profile
        pid = use_profile or active_browser_profile
        log("session", f"Rebuilding browser: {reason}")
        with contextlib.suppress(Exception):
            await asyncio.wait_for(browser.stop(), timeout=10)
        if use_local:
            browser = await create_local_browser(headless=not headed)
        else:
            for _attempt in range(3):
                with contextlib.suppress(Exception):
                    killed = await stop_oldest_active_cloud_session()
                    if killed:
                        log("session", f"Freed zombie session: {killed}")
                try:
                    browser = await create_cloud_browser(pid)
                    active_browser_profile = pid
                    return
                except Exception as e:
                    if "429" in str(e) or "too many" in str(e).lower():
                        log("session", f"429 on browser create, freeing another session...")
                        await asyncio.sleep(2)
                        continue
                    raise
            browser = await create_cloud_browser(pid)
            active_browser_profile = pid
        await inject_stealth(browser)

    try:
        # ── Phase 1: Signup ──
        # Start email polling in background
        verify_task: asyncio.Task[VerificationCandidate] | None = None
        if not skip_verification:
            verify_task = asyncio.create_task(
                watch_for_verification(
                    mail=mail, inbox_id=inbox_id, target_url=url,
                    timeout_s=signup_timeout + verify_timeout,
                    min_received_at=run_started_at,
                )
            )

        # ── Tier 1: Deterministic form fill ──
        deterministic_succeeded = False
        screen_type: str | None = None  # Login Machine screen classification
        if site_profile and site_profile.signup_url and use_local:
            # Tier 1 requires direct Playwright page access — only works with local browser.
            # Cloud browsers initialize lazily and don't expose a page until first Agent run.
            log("signup", f"Tier 1: Navigating to {site_profile.signup_url}...")
            try:
                page = await browser.get_current_page()
                if page is None:
                    raise RuntimeError("Browser not initialized yet (cloud browser requires Agent first)")
                await page.goto(site_profile.signup_url, wait_until="domcontentloaded", timeout=30000)

                # Wait for SPA to render (Login Machine pattern)
                await wait_for_page_content(page, timeout_ms=10000)

                # Screen classification (Login Machine pattern)
                screen_info = await classify_screen(page)
                screen_type = screen_info.get("type")

                # Short-circuit for non-form screens
                if screen_type == "authenticated":
                    log("signup", "Tier 1: Already authenticated! Skipping signup.")
                    deterministic_succeeded = True
                elif screen_type == "oauth_only":
                    log("signup", "Tier 1: OAuth-only page detected — skipping deterministic fill")
                    # Let Tier 2/3 or OAuth fallback handle it
                elif screen_type == "captcha_blocked":
                    log("signup", "Tier 1: Captcha gate — skipping deterministic fill")
                elif screen_type == "error_page":
                    log("signup", f"Tier 1: Error page — {screen_info.get('detail', '')}")
                elif screen_type in ("credential_form", "email_only_form", "generic_form", "unknown"):
                    # Live DOM inspection (more accurate than Firecrawl HTML parsing)
                    live_fields, live_submit = await inspect_signup_form(page)
                    if live_fields:
                        site_profile.form_fields = live_fields
                        site_profile.submit_selector = live_submit

                    mapped = [f for f in site_profile.form_fields if f.identity_key]
                    has_email = any(f.identity_key == "email" for f in mapped)
                    has_password = any(f.identity_key == "password" for f in mapped)

                    if has_email and has_password and len(mapped) >= 2:
                        log("signup", f"Tier 1: Attempting deterministic fill ({len(mapped)} mapped fields)...")
                        deterministic_succeeded = await deterministic_signup(
                            page, site_profile.form_fields, identity, site_profile.submit_selector,
                        )
                        if deterministic_succeeded:
                            log("signup", "Tier 1 SUCCESS — form filled and submitted without LLM")
                    elif has_email and not has_password and screen_type == "email_only_form":
                        # Multi-step form: fill email, submit, then fill password on next page
                        log("signup", "Tier 1: Email-only form (multi-step) — attempting email first...")
                        email_fields = [f for f in site_profile.form_fields if f.identity_key == "email"]
                        if email_fields:
                            step1_ok = await deterministic_signup(
                                page, email_fields, identity, site_profile.submit_selector,
                            )
                            if step1_ok:
                                await wait_for_page_content(page, timeout_ms=8000)
                                # Re-inspect for step 2
                                step2_fields, step2_submit = await inspect_signup_form(page)
                                if step2_fields:
                                    step2_mapped = [f for f in step2_fields if f.identity_key]
                                    if any(f.identity_key == "password" for f in step2_mapped):
                                        log("signup", f"Tier 1: Step 2 found {len(step2_mapped)} fields — filling...")
                                        deterministic_succeeded = await deterministic_signup(
                                            page, step2_fields, identity, step2_submit,
                                        )
                                        if deterministic_succeeded:
                                            log("signup", "Tier 1 SUCCESS — multi-step form completed without LLM")
                    else:
                        log("signup", f"Tier 1: Insufficient fields (email={has_email}, password={has_password}, mapped={len(mapped)})")
            except Exception as e:
                log("signup", f"Tier 1 failed: {e}")

        # ── Tier 2/3: Agent-based signup (if Tier 1 didn't work) ──
        signup_result: RunResult | None = None
        agent_error_history: list[str] = []  # Login Machine error history
        if not deterministic_succeeded:
            tier = "2" if site_profile else "3"

            # Screen-informed agent hint (Login Machine pattern)
            screen_hint = ""
            if screen_type == "oauth_only":
                screen_hint = "\nNOTE: This page appears to only have OAuth login options (no email/password form). "
                screen_hint += "Look carefully for a hidden email signup option, tab, or link. "
                screen_hint += "If none exists, output SIGNUP_FAILED with DETAILS: NO_EMAIL_SIGNUP_OPTION.\n"
            elif screen_type == "email_only_form":
                screen_hint = "\nNOTE: This appears to be a multi-step form (email first, then password). "
                screen_hint += "Fill email and submit, then fill remaining fields on the next page.\n"
            elif screen_type == "captcha_blocked":
                screen_hint = "\nNOTE: A captcha gate was detected. Handle the captcha first before filling the form.\n"

            log("signup", f"Tier {tier}: Running browser-use agent...{' (screen: ' + screen_type + ')' if screen_type else ''}")
            signup_prompt = build_signup_task(base_url, identity, profile=site_profile)
            if screen_hint:
                signup_prompt += screen_hint
            # Pre-navigate to known signup URL (saves LLM tokens)
            signup_initial: list[dict[str, dict[str, Any]]] | None = None
            if site_profile and site_profile.signup_url:
                signup_initial = [{"navigate": {"url": site_profile.signup_url}}]
            signup_result = await run_agent(
                browser, llm, "signup", signup_prompt,
                max_steps=max_steps, timeout_s=signup_timeout, retries=1,
                inject_page_html=True, controller=signup_ctrl,
                initial_actions=signup_initial,
            )

        # Agent retry logic (skip if deterministic succeeded)
        if signup_result:
            # Captcha retry with browser rebuild
            if _looks_like_captcha_failure(signup_result) and not signup_result.success:
                log("signup", "Captcha failure; rebuilding browser and retrying...")
                agent_error_history.append(f"Captcha failure: {signup_result.error or signup_result.output or 'unknown'}")
                await rebuild_browser("captcha_failure")
                retry_prompt = build_signup_task(base_url, identity, profile=site_profile)
                retry_prompt += build_error_history_context(agent_error_history)
                signup_result = await run_agent(
                    browser, llm, "signup-retry", retry_prompt,
                    max_steps=max_steps, timeout_s=signup_timeout, retries=1,
                    inject_page_html=True, controller=signup_ctrl,
                )

            # Browser instability retry (with error history feedback)
            for retry_i in range(3):
                if signup_result.success:
                    break
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
                _out = signup_result.output or ""
                has_status = bool(
                    re.search(r"STATUS\s*:", _out, re.IGNORECASE)
                    or (_out.strip().startswith("{") and '"status"' in _out.lower())
                )
                needs_rebuild = (
                    _needs_browser_rebuild(signup_result.error)
                    or signup_result.steps <= 1
                    or (not signup_result.success and not has_status)
                )
                if not needs_rebuild:
                    break
                # Accumulate error history (Login Machine pattern)
                err_detail = signup_result.error or signup_result.output or "browser crash"
                agent_error_history.append(f"Attempt {retry_i+1}: {err_detail[:200]}")
                log("signup", f"Browser instability (steps={signup_result.steps}, has_status={has_status}); rebuilding...")
                await asyncio.sleep(3)
                await rebuild_browser("session_instability")
                retry_prompt = build_signup_task(base_url, identity, profile=site_profile)
                retry_prompt += build_error_history_context(agent_error_history)
                signup_result = await run_agent(
                    browser, llm, "signup", retry_prompt,
                    max_steps=max_steps, timeout_s=signup_timeout, retries=1,
                    inject_page_html=True, controller=signup_ctrl,
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
                    inject_page_html=True, controller=signup_ctrl,
                )

        # Parse signup outcome
        if deterministic_succeeded:
            signup_status = "NEEDS_VERIFICATION"  # deterministic can't know for sure
            signup_details = "Deterministic form fill succeeded"
            signup_text = ""
            needs_verification = True  # assume verification needed, pipeline handles if not
            is_magic_link = False
            is_otp_only = False
        else:
            assert signup_result is not None
            signup_status, signup_details = parse_signup_status(signup_result.output)
            signup_text = (signup_result.output or "").lower()
            needs_verification = _infer_needs_verification(signup_status, signup_text)
            is_magic_link = _infer_magic_link(signup_result.output or "")
            is_otp_only = _infer_otp_only(signup_result.output or "", site_profile)

        # Email conflict retry
        if (
            signup_status == "SIGNUP_FAILED"
            and retry_email_conflict
            and _looks_like_email_conflict(signup_details)
        ):
            log("signup", "Email conflict; rotating inbox and retrying...")
            with contextlib.suppress(Exception):
                await mail.inboxes.delete(inbox_id)
            inbox_id, created_inbox = await acquire_inbox(mail, domain=domain)
            identity = generate_identity(inbox_id)
            log("signup", f"Retry email: {identity.email}")
            await rebuild_browser("email_conflict_retry")
            signup_prompt = build_signup_task(base_url, identity, profile=site_profile)
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
                inject_page_html=True, controller=signup_ctrl,
            )
            signup_status, signup_details = parse_signup_status(signup_result.output)
            signup_text = (signup_result.output or "").lower()
            needs_verification = _infer_needs_verification(signup_status, signup_text)
            is_magic_link = _infer_magic_link(signup_result.output or "")
            is_otp_only = _infer_otp_only(signup_result.output or "", site_profile)

        # Signup totally failed — try OAuth fallback before bailing
        signup_actually_failed = (
            signup_status == "SIGNUP_FAILED"
            or (signup_status == "UNKNOWN" and signup_result is not None and not signup_result.success)
        )

        # ── OAuth fallback: retry with Google/GitHub profile if email signup failed ──
        # Screen classification provides early signal — oauth_only screen means no email form exists
        oauth_attempted = False
        is_oauth_candidate = _looks_like_oauth_fallback_candidate(
            signup_status, signup_details, signup_text, site_profile
        ) or screen_type == "oauth_only"
        if (
            signup_actually_failed
            and oauth_profile_id
            and not no_oauth_fallback
            and is_oauth_candidate
        ):
            log("oauth", "Email signup failed — trying OAuth fallback...")
            # Infer which provider the profile supports
            oauth_provider = await infer_oauth_provider_from_profile(oauth_profile_id)
            log("oauth", f"Provider: {oauth_provider} (profile={oauth_profile_id[:12]}...)")

            # Stop old browser and create new one with OAuth profile
            with contextlib.suppress(Exception):
                await asyncio.wait_for(browser.stop(), timeout=10)
            browser = await create_cloud_browser(oauth_profile_id)
            active_browser_profile = oauth_profile_id
            oauth_attempted = True

            # Pre-check: navigate to site and see if OAuth profile is already logged in
            already_authed = False
            try:
                await asyncio.wait_for(browser.navigate_to(base_url), timeout=20)
                await asyncio.sleep(3)
                cur_url = (await browser.get_current_page_url() or "").lower()
                page = await browser.get_current_page()
                body = ""
                if page:
                    body = await page.evaluate("() => document.body?.innerText?.substring(0, 2000) || ''")
                body_low = (body or "").lower()
                auth_signals = ("dashboard", "onboarding", "welcome", "/app", "/home",
                                "/account", "/settings", "/projects", "workspace")
                if any(s in cur_url for s in auth_signals) or any(s in body_low for s in ("log out", "sign out", "my account", "api key")):
                    already_authed = True
                    log("oauth", f"Profile already logged in at {cur_url[:80]}")
            except Exception as e:
                log("oauth", f"Auth pre-check failed: {e}")

            if already_authed:
                log("oauth", "Skipping OAuth signup — already authenticated")
                signup_actually_failed = False
                signup_status = "SIGNUP_SUCCESS"
                signup_details = "OAuth profile already authenticated"
                needs_verification = False
                is_magic_link = False
                is_otp_only = False
            else:
                oauth_task = build_oauth_signup_task(
                    base_url, identity, oauth_provider=oauth_provider, profile=site_profile,
                )
                oauth_result = await run_agent(
                    browser, llm, "signup-oauth", oauth_task,
                    max_steps=max_steps + 2, timeout_s=signup_timeout, retries=1,
                    inject_page_html=True, controller=signup_ctrl,
                )
                oauth_status, oauth_details = parse_signup_status(oauth_result.output)
                oauth_text = (oauth_result.output or "").lower()

            if not already_authed:
                if oauth_status in ("SIGNUP_SUCCESS", "NEEDS_VERIFICATION"):
                    log("oauth", f"OAuth signup succeeded! Status={oauth_status}")
                    signup_actually_failed = False
                    signup_status = oauth_status
                    signup_details = oauth_details
                    signup_text = oauth_text
                    needs_verification = _infer_needs_verification(signup_status, signup_text)
                    is_magic_link = False
                    is_otp_only = False
                    if signup_status == "SIGNUP_SUCCESS":
                        needs_verification = False
                else:
                    log("oauth", f"OAuth fallback also failed: {oauth_details}")

        if signup_actually_failed:
            if verify_task and not verify_task.done():
                verify_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await verify_task
            _print_results(url, identity, resolved_model,
                           signup_ok=False, verified=False, login_ok=False,
                           api_key=None, api_key_url=None, login_url=None,
                           notes=signup_details or "Signup failed.")
            if site_profile:
                site_profile.failure_count += 1
                site_profile.confidence = max(0.05, round(site_profile.confidence - 0.12, 3))
                save_profile(site_profile, custom_profile_dir)
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

                # Supabase direct-navigation verify: faster than agent, avoids lost-agent issue
                link_low = verification_link.lower()
                if not verified and "supabase" in link_low and "/auth/v1/verify" in link_low:
                    log("verify", "Supabase direct-navigate verify...")
                    try:
                        await asyncio.wait_for(browser.navigate_to(verification_link), timeout=20)
                        await asyncio.sleep(5)  # let Supabase redirect chain complete
                        cur_url = await browser.get_current_page_url() or ""
                        cur_low = cur_url.lower()
                        log("verify", f"Supabase redirect landed: {cur_url[:120]}")
                        if "access_denied" in cur_low or "error" in cur_low.split("#")[0].split("?")[-1:][0]:
                            log("verify", "Supabase returned access_denied; token may be invalid")
                        elif any(p in cur_low for p in ("onboarding", "dashboard", "welcome", "/app", "/home")):
                            verified = True
                            log("verify", "Supabase verify SUCCESS — redirected to app")
                        else:
                            # Check page content
                            page = await browser.get_current_page()
                            if page:
                                body = await page.evaluate("() => document.body?.innerText?.substring(0, 2000) || ''")
                                body_low = (body or "").lower()
                                if any(s in body_low for s in ("verified", "confirmed", "welcome", "get started")):
                                    verified = True
                                    log("verify", "Supabase verify SUCCESS — positive page content")
                    except Exception as e:
                        log("verify", f"Supabase direct verify error: {e}")
                    if not verified:
                        await rebuild_browser("supabase_verify_failed")

                if not verified:
                    # Browser agent fallback for verification (Auth0/SSO may require login first)
                    verify_browser_task = (
                        f"Open this verification link: {verification_link}\n"
                        f"If asked to log in: email=`x_email` password=`x_password`\n"
                        "After clicking verify/confirm or logging in, wait for the page to load.\n"
                        "If you see a dashboard, workspace, or 'email verified' message: VERIFIED: YES.\n"
                        "Output: VERIFIED: YES or NO / DETAILS: <short>"
                    )
                    vr = await run_agent(browser, llm, "verify", verify_browser_task, max_steps=8, timeout_s=60)
                    flag = _verification_explicit_flag(vr.output)
                    verified = flag if flag is not None else _verification_looks_successful(vr.output)
                    if not vr.success or not verified:
                        # Only use heuristic if agent gave NO explicit answer
                        # (crash, timeout, ambiguous output). If agent clearly said NO, trust it.
                        explicit_no = flag is False  # _verification_explicit_flag returned False
                        if not explicit_no:
                            err_l = (vr.error or "").lower()
                            input_failed = any(k in err_l for k in (
                                "failed to type", "sessionmanager not initialized",
                                "failed to click", "element not found",
                            ))
                            # Auth0 ticket links: navigating to the URL processes the ticket
                            # via client-side JS. Even 1 step (the navigation) is enough.
                            is_auth0_ticket = "ticket=" in (verification_link or "").lower()
                            # For non-Auth0 links: only apply step heuristic if agent COMPLETED
                            # (didn't timeout). Timeout means agent was still searching and
                            # never reached a verified state. Auth0 tickets are exempt because
                            # navigation IS the verification (browser often crashes after redirect).
                            verify_timed_out = any(k in (vr.error or "").lower() for k in (
                                "timeout", "eventbus", "timed out",
                            ))
                            auth0_submitted = not verified and (
                                (is_auth0_ticket and vr.steps >= 1)
                                or (not verify_timed_out and vr.steps >= 3 and not input_failed)
                            )
                            if auth0_submitted:
                                log("verify", f"Auth0 ticket processed ({vr.steps} steps); assuming verified")
                                verified = True
                        await rebuild_browser("verify_failure")
            elif verification_code:
                log("verify", f"Code verify: {verification_code}")
                # Enter the OTP code on the EXISTING browser session — the OTP screen
                # is still visible from the signup agent's last step.

                # Fast path: try JS-based OTP fill (handles Shadow DOM inputs)
                otp_js_filled = False
                try:
                    page = await browser.get_current_page()
                    if page:
                        otp_js_filled = await page.evaluate("""(code) => {
                            // Recursive Shadow DOM traversal to find OTP inputs
                            function findInputs(root) {
                                let inputs = [];
                                const els = root.querySelectorAll('input');
                                for (const el of els) {
                                    const s = window.getComputedStyle(el);
                                    if (s.display === 'none' || s.visibility === 'hidden') continue;
                                    inputs.push(el);
                                }
                                // Traverse shadow roots
                                for (const el of root.querySelectorAll('*')) {
                                    if (el.shadowRoot) {
                                        inputs = inputs.concat(findInputs(el.shadowRoot));
                                    }
                                }
                                return inputs;
                            }

                            const allInputs = findInputs(document);
                            // Filter to likely OTP inputs: numeric, short maxlength, or code-related
                            const otpInputs = allInputs.filter(el => {
                                const ml = parseInt(el.maxLength) || 999;
                                const im = el.inputMode || '';
                                const ac = el.autocomplete || '';
                                const t = el.type || 'text';
                                const ph = (el.placeholder || '').toLowerCase();
                                return (
                                    ml <= 8 || im === 'numeric' || t === 'number' ||
                                    ac === 'one-time-code' || ph.includes('code') ||
                                    ph.includes('otp') || ph.includes('digit')
                                );
                            });

                            const digits = code.split('');
                            const nativeSet = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value').set;

                            if (otpInputs.length === digits.length) {
                                // Multiple single-digit inputs
                                for (let i = 0; i < digits.length; i++) {
                                    nativeSet.call(otpInputs[i], digits[i]);
                                    otpInputs[i].dispatchEvent(new Event('input', {bubbles: true}));
                                    otpInputs[i].dispatchEvent(new Event('change', {bubbles: true}));
                                }
                                return true;
                            } else if (otpInputs.length === 1) {
                                // Single OTP input
                                nativeSet.call(otpInputs[0], code);
                                otpInputs[0].dispatchEvent(new Event('input', {bubbles: true}));
                                otpInputs[0].dispatchEvent(new Event('change', {bubbles: true}));
                                return true;
                            }
                            return false;
                        }""", verification_code)
                        if otp_js_filled:
                            log("verify", "OTP filled via JS (Shadow DOM aware)")
                            await asyncio.sleep(2)
                            # Try to find and click submit button
                            await page.evaluate("""() => {
                                function findButtons(root) {
                                    let btns = [];
                                    for (const el of root.querySelectorAll('button, input[type=submit]')) {
                                        const t = (el.textContent || el.value || '').toLowerCase();
                                        if (/verify|confirm|submit|continue/.test(t)) btns.push(el);
                                    }
                                    for (const el of root.querySelectorAll('*')) {
                                        if (el.shadowRoot) btns = btns.concat(findButtons(el.shadowRoot));
                                    }
                                    return btns;
                                }
                                const btn = findButtons(document)[0];
                                if (btn) btn.click();
                            }""")
                            await asyncio.sleep(3)
                except Exception as e:
                    log("verify", f"JS OTP fill failed: {e}")

                if otp_js_filled:
                    # Check if we landed on a success page
                    try:
                        cur_url = await browser.get_current_page_url() or ""
                        page = await browser.get_current_page()
                        body = ""
                        if page:
                            body = await page.evaluate("() => document.body?.innerText?.substring(0, 2000) || ''")
                        cur_low = cur_url.lower()
                        body_low = (body or "").lower()
                        if any(p in cur_low for p in ("dashboard", "onboarding", "welcome", "/app", "/home")) or \
                           any(s in body_low for s in ("verified", "welcome", "get started", "dashboard")):
                            verified = True
                            log("verify", "OTP JS fill verified — redirected to app")
                    except Exception:
                        pass

                if not verified:
                    # Fall back to agent-based OTP entry
                    digits = list(verification_code)
                    otp_task = (
                        f"You are on a verification/OTP code entry screen.\n"
                        f"The code is: `{verification_code}` (digits: {' '.join(digits)})\n"
                        f"Rules:\n"
                        f"- If there are {len(digits)} separate input boxes, click EACH box and type ONE digit.\n"
                        f"  Box 1: `{digits[0]}`, Box 2: `{digits[1]}`, Box 3: `{digits[2]}`, "
                        f"Box 4: `{digits[3]}`, Box 5: `{digits[4]}`, Box 6: `{digits[5]}`\n"
                        f"- If there is a single input field, clear it first, then type `{verification_code}`\n"
                        f"- If you CANNOT find any input fields, try running JavaScript:\n"
                        f"  document.querySelectorAll('input').forEach(el => console.log(el.type, el.maxLength))\n"
                        f"  Then check shadow DOM roots for hidden inputs.\n"
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
        await _inject_clipboard_safe(browser)  # intercept copy-to-clipboard API keys
        if needs_verification and not verified and not is_magic_link and not is_otp_only:
            log("login", "Warning: verification unresolved — login may be blocked")
        if oauth_attempted and not signup_actually_failed:
            # ── OAuth path: already authenticated, just extract API key ──
            log("login", "OAuth session active — extracting API key directly...")
            api_key_hint = base_url
            if site_profile and site_profile.api_key_url:
                api_key_hint = site_profile.api_key_url
            apikey_task = (
                f"You are logged in at {url} via OAuth.\n"
                f"Find API key: check /settings/api, /account/api-keys, /developer/api, /settings/extensions.\n"
                f"Or navigate to {api_key_hint}.\n"
                f"Copy existing key or create one named 'sigma'. If no API section after 2 pages, report NONE.\n\n"
                f"Output: LOGIN: SUCCESS / API_KEY: <key> or NONE / API_KEY_URL: <url> or NONE / LOGIN_URL: <url> or NONE / NOTES: <short>"
            )
            login_result = await run_agent(
                browser, llm, "login+api", apikey_task,
                max_steps=max_steps + 5, timeout_s=login_timeout,
                inject_page_html=True, controller=login_ctrl,
            )
            parsed = parse_login_output(login_result.output)
            login_ok = True  # OAuth means we're already logged in
            api_key = sanitize_api_key_candidate(parsed.get("API_KEY"), parsed.get("NOTES"))
            if not api_key:
                try:
                    page = await browser.get_current_page()
                    api_key = await scan_for_api_key(page)
                except Exception:
                    pass
            api_key_url = parsed.get("API_KEY_URL")
            login_url = parsed.get("LOGIN_URL")
            notes = parsed.get("NOTES")
        elif is_magic_link:
            # ── Magic-link flow: trigger link → catch email → programmatic navigate ──
            login_ok, api_key, api_key_url, login_url, notes = await _magic_link_login(
                browser, llm, mail, inbox_id, url, identity,
                run_started_at, max_steps, login_timeout, verify_timeout,
                login_ctrl=login_ctrl,
            )
        elif is_otp_only:
            # ── OTP-only flow: trigger OTP → poll inbox → enter code ──
            login_ok, api_key, api_key_url, login_url, notes = await _otp_login(
                browser, llm, mail, inbox_id, url, identity,
                run_started_at, max_steps, login_timeout, verify_timeout,
                profile=site_profile, login_ctrl=login_ctrl,
            )
        else:
            # ── Standard password login ──
            verify_preamble = ""
            if verification_link and not verified:
                verify_preamble = f"FIRST: Navigate to {verification_link} to verify email.\n\n"
            elif verification_code and not verified:
                verify_preamble = (
                    f"FIRST: Your email needs verification. Go to the login page, enter email `x_email` "
                    f"and password `x_password`. If an OTP/verification code screen appears, "
                    f"enter code `{verification_code}` (preserve leading zeros). Then continue to login.\n\n"
                )

            login_task = build_login_apikey_task(base_url, identity, profile=site_profile)
            if verify_preamble:
                login_task = verify_preamble + login_task
            log("login", "Password-based login...")
            login_result = await run_agent(
                browser, llm, "login+api", login_task,
                max_steps=max_steps + 5, timeout_s=login_timeout,
                inject_page_html=True, controller=login_ctrl,
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
                await _inject_clipboard_safe(browser)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(browser.navigate_to(base_url), timeout=15)
                    await asyncio.sleep(2)
                login_result = await run_agent(
                    browser, llm, "login+api", login_task,
                    max_steps=max_steps + 5, timeout_s=login_timeout,
                    inject_page_html=True, controller=login_ctrl,
                )
            if _is_eventbus_stall(login_result.error) or _needs_browser_rebuild(login_result.error):
                await rebuild_browser("login_stall")
                await _inject_clipboard_safe(browser)

            parsed = parse_login_output(login_result.output)
            notes_l = (parsed.get("NOTES") or "").lower()
            login_state = (parsed.get("LOGIN") or "").strip().upper()

            # Login stall heuristic: ONLY if agent crashed/timed out WITHOUT giving
            # a clear LOGIN status. If agent explicitly said FAILED, trust it.
            agent_gave_explicit_status = login_state in ("SUCCESS", "FAILED")
            if (
                not agent_gave_explicit_status
                and login_result.steps >= 4
                and not login_result.success
            ):
                login_err = (login_result.error or "").lower()
                login_input_failed = any(k in login_err for k in (
                    "failed to type", "sessionmanager not initialized",
                    "failed to click", "element not found",
                ))
                if not login_input_failed:
                    log("login", f"Agent crashed after {login_result.steps} steps with no explicit status; assuming login success")
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
                        inject_page_html=True, controller=login_ctrl,
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

            # DOM-first API key scan — faster and more reliable than agent
            if login_ok_now and not parsed_key:
                try:
                    page = await browser.get_current_page()
                    dom_key = await scan_for_api_key(page)
                    if dom_key:
                        parsed_key = dom_key
                        parsed["API_KEY"] = dom_key
                        log("login", f"API key found via DOM scan!")
                except Exception:
                    pass  # Fall through to agent-based scan

            if login_ok_now and not parsed_key:
                log("login", "Login OK but no API key; running focused API-only pass...")
                # Always rebuild — browser is likely dead after login stall/timeout
                await rebuild_browser("pre_api_only")
                await _inject_clipboard_safe(browser)
                api_hint = parsed.get("API_KEY_URL") or base_url
                api_only_task = (
                    f"Stay on {url}. Log in if needed:\n"
                    f"  email: `x_email` / password: `x_password`\n"
                    f"Then open {api_hint} or the nearest API/settings page.\n"
                    "Find or create API key (name 'sigma'). Copy full plaintext value.\n"
                    "If masked, use reveal/copy. If never shown, output NONE.\n"
                    "Output: LOGIN: SUCCESS / API_KEY: <key> or NONE / API_KEY_URL: <url> or NONE / LOGIN_URL: <url> or NONE / NOTES: <short>"
                )
                api_only = await run_agent(
                    browser, llm, "api-only", api_only_task,
                    max_steps=10, timeout_s=min(90, login_timeout),
                    inject_page_html=True, controller=login_ctrl,
                )
                if _is_eventbus_stall(api_only.error):
                    await rebuild_browser("api_only_stall")
                p2 = parse_login_output(api_only.output)
                k2 = sanitize_api_key_candidate(p2.get("API_KEY"), p2.get("NOTES"))
                if not k2:
                    # Agent may have clicked "copy" — check clipboard
                    try:
                        page = await browser.get_current_page()
                        if page:
                            k2 = await scan_for_api_key(page)
                    except Exception:
                        pass
                if k2:
                    parsed = p2
                    parsed["API_KEY"] = k2

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

        # ── Update site profile with results ──
        if site_profile:
            if not signup_actually_failed:
                site_profile.success_count += 1
                site_profile.confidence = min(0.99, round(site_profile.confidence + 0.03, 3))
            else:
                site_profile.failure_count += 1
                site_profile.confidence = max(0.05, round(site_profile.confidence - 0.12, 3))
            # Save discovered URLs
            if login_ok and login_url:
                site_profile.login_url = login_url
            if api_key_url:
                site_profile.api_key_url = api_key_url
            if verified and verification_link:
                site_profile.verification_method = "email_link"
            elif verified and verification_code:
                site_profile.verification_method = "email_code"
            if is_magic_link:
                site_profile.verification_method = "magic_link"
            site_profile.har_path = str(har_path)
            save_profile(site_profile, custom_profile_dir)

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
    login_ctrl: Controller | None = None,
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
        f"Go to {url} and sign in. Enter email `x_email` and click "
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
            inject_page_html=True, controller=login_ctrl,
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
            inject_page_html=True, controller=login_ctrl,
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


async def _otp_login(
    browser: Browser, llm: Any, mail: AsyncAgentMail, inbox_id: str,
    url: str, identity: Identity, run_started_at: datetime,
    max_steps: int, login_timeout: int, verify_timeout: int,
    profile: SiteProfile | None = None,
    login_ctrl: Controller | None = None,
) -> tuple[bool, str | None, str | None, str | None, str | None]:
    """
    OTP-based login flow (no password):
    1. Agent enters email and triggers OTP send
    2. Poll inbox for OTP code
    3. Agent enters OTP code to complete login
    4. Then run API key extraction agent

    Returns: (login_ok, api_key, api_key_url, login_url, notes)
    """
    log("login", "OTP-only site — checking auth state first...")

    login_url_hint = url
    if profile and profile.login_url:
        login_url_hint = profile.login_url

    # Pre-check: if browser is already on a dashboard (e.g., from verification),
    # skip OTP trigger and go straight to API key extraction.
    try:
        cur_url = await browser.get_current_page_url() or ""
        cur_low = cur_url.lower()
        already_authed = any(p in cur_low for p in (
            "dashboard", "onboarding", "/app", "/home", "/settings", "/projects",
            "/workspace", "/console", "/overview",
        ))
        if already_authed:
            log("login", f"Already authenticated ({cur_url[:80]}); skipping OTP trigger")
            apikey_task = (
                f"You are now logged in at {url}.\n"
                f"Find API key: check /settings/api, /account/api-keys, /developer/api, "
                f"/settings/api-keys, /settings/extensions.\n"
                f"If there's an onboarding screen, complete it with defaults first.\n"
                f"Copy existing key or create one named 'sigma'. "
                f"If key is masked, click 'Copy' or 'Reveal' button.\n"
                f"If no API section after 3 pages, report NONE.\n\n"
                f"Output: LOGIN: SUCCESS / API_KEY: <key> or NONE / API_KEY_URL: <url> or NONE / "
                f"LOGIN_URL: <url> or NONE / NOTES: <short>"
            )
            result = await run_agent(
                browser, llm, "login+api", apikey_task,
                max_steps=max_steps + 5, timeout_s=login_timeout,
                inject_page_html=True, controller=login_ctrl,
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
    except Exception:
        pass  # Browser might be dead; continue with normal OTP flow

    log("login", "OTP-only site — triggering OTP code...")

    # Step 1: Agent enters email and triggers OTP
    trigger_task = (
        f"Go to {login_url_hint} and sign in.\n"
        f"Enter email `x_email` and click 'Send OTP', 'Continue', 'Sign In', or similar button.\n"
        f"If there is ONLY Google/GitHub/SSO login and no email option, output OTP_FAILED.\n"
        f"Wait until you see 'Enter OTP', 'check your email', or 'code sent'. "
        f"Then output OTP_TRIGGERED and STOP. Do NOT visit any other site.\n"
        f"Do NOT enter any password — this site uses OTP codes only.\n"
        f"Output: OTP_TRIGGERED or OTP_FAILED / DETAILS: <short>"
    )
    trigger_result = await run_agent(browser, llm, "otp-trigger", trigger_task, max_steps=8, timeout_s=60)

    trigger_out = (trigger_result.output or "").lower()
    if "otp_failed" in trigger_out:
        # Site actually has password login, not OTP-only. Fall back to password flow.
        log("login", "OTP trigger failed — falling back to standard password login...")
        from browser_use import Browser  # noqa: already imported at top
        login_task = build_login_apikey_task(url, identity, profile=profile)
        result = await run_agent(
            browser, llm, "login+api", login_task,
            max_steps=max_steps + 5, timeout_s=login_timeout,
            inject_page_html=True, controller=login_ctrl,
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

    # Step 2: Poll inbox for OTP code
    otp_started_at = datetime.now(timezone.utc)
    log("login", "Polling for OTP code email...")
    candidate = await watch_for_verification(
        mail=mail, inbox_id=inbox_id, target_url=url,
        timeout_s=45, poll_s=2, min_received_at=otp_started_at,
    )

    if not candidate.code:
        # Try broader time window (OTP might have been sent before our timestamp)
        log("login", "No OTP code found; trying broader time window...")
        candidate = await watch_for_verification(
            mail=mail, inbox_id=inbox_id, target_url=url,
            timeout_s=20, poll_s=2, min_received_at=run_started_at,
        )

    if candidate.code:
        log("login", f"Got OTP code: {candidate.code}")

        # Step 3: Agent enters OTP code
        digits = list(candidate.code)
        otp_entry_task = (
            f"You are on an OTP/verification code entry screen.\n"
            f"The code is: `{candidate.code}`\n"
            f"Rules:\n"
            f"- If there are {len(digits)} separate input boxes, click EACH box and type ONE digit.\n"
            f"- If there is a single input field, clear it first, then type `{candidate.code}`\n"
            f"- After entering the code, click Verify/Confirm/Submit/Continue.\n"
            f"- If 'incorrect code' or 'expired' appears, output OTP_ENTRY_FAILED.\n"
            f"- IMPORTANT: Preserve ALL digits exactly, including leading zeros.\n"
            f"- Wait for the page to load after submission.\n"
            f"Output: OTP_SUCCESS or OTP_ENTRY_FAILED / DETAILS: <short>"
        )
        otp_result = await run_agent(browser, llm, "otp-entry", otp_entry_task, max_steps=8, timeout_s=60)

        otp_out = (otp_result.output or "").lower()
        if "otp_entry_failed" in otp_out or "incorrect" in otp_out or "expired" in otp_out:
            log("login", f"OTP entry failed: {(otp_result.output or '')[:120]}")
            return (False, None, None, None, "OTP code rejected")

        log("login", "OTP accepted — now extracting API key...")

        # Step 4: Extract API key
        apikey_task = (
            f"You are now logged in at {url}.\n"
            f"Find API key: check /settings/api, /account/api-keys, /developer/api, "
            f"/settings/api-keys, /settings/extensions.\n"
            f"Copy existing key or create one named 'sigma'. "
            f"If key is masked, click 'Copy' or 'Reveal' button.\n"
            f"If no API section after 2 pages, report NONE.\n\n"
            f"Output: LOGIN: SUCCESS / API_KEY: <key> or NONE / API_KEY_URL: <url> or NONE / "
            f"LOGIN_URL: <url> or NONE / NOTES: <short>"
        )
        result = await run_agent(
            browser, llm, "login+api", apikey_task,
            max_steps=max_steps + 5, timeout_s=login_timeout,
            inject_page_html=True, controller=login_ctrl,
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
        log("login", "No OTP code email received; site may block disposable emails")
        return (False, None, None, None, "OTP email not received (disposable email blocked?)")


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

    p = argparse.ArgumentParser(description="Sigma v3: reverse-engineer signup + auto API key")
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

    # v3 options
    p.add_argument("--local", action="store_true", help="Use local Playwright instead of browser-use cloud")
    p.add_argument("--no-recon", action="store_true", help="Skip Firecrawl recon (blind mode)")
    p.add_argument("--refresh-profile", action="store_true", help="Force re-crawl even if cached profile exists")
    p.add_argument("--headed", action="store_true", help="Show browser window (local mode only)")
    p.add_argument("--profile-dir", default=None, help="Custom profile storage dir (default ~/.sigma/profiles)")
    p.add_argument("--oauth-profile", default=None,
                   help="Browser Use profile ID with Google/GitHub OAuth cookies for fallback")
    p.add_argument("--no-oauth-fallback", action="store_true",
                   help="Disable OAuth fallback even if --oauth-profile is set")

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
        use_cloud=not args.local,
        no_recon=args.no_recon,
        refresh_profile=args.refresh_profile,
        headed=args.headed,
        custom_profile_dir=args.profile_dir,
        oauth_profile_id=args.oauth_profile,
        no_oauth_fallback=args.no_oauth_fallback,
    ))


if __name__ == "__main__":
    main()
