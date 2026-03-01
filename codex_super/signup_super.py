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
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
        phone=fake.phone_number(),
    )


def _extract_links(text: str) -> list[str]:
    raw = re.findall(r"https?://[^\s<>\"']+", text)
    return [html.unescape(u) for u in raw]


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
    host = urlparse(url).netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


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


async def create_cloud_browser(proxy_country_code: str = "us") -> Browser:
    if not BROWSER_USE_API_KEY:
        raise RuntimeError("Missing BROWSER_USE_API_KEY")
    return Browser(
        use_cloud=True,
        cloud_proxy_country_code=proxy_country_code,
        keep_alive=True,
        minimum_wait_page_load_time=0.1,
        wait_between_actions=0.1,
        highlight_elements=False,
        captcha_solver=True,
    )


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
            if min_received_at and ts and ts < min_received_at:
                continue
            msg = await mail.inboxes.messages.get(inbox_id=inbox_id, message_id=msg_id)
            text_body = (msg.text or "").strip()
            html_body = (msg.html or "").strip()
            primary_body = text_body or html_body
            urls = _extract_links(primary_body)
            link = _best_verification_link(urls, target_url)
            code = _extract_code(primary_body)
            if link or code:
                return VerificationCandidate(link=link, code=code, subject=msg.subject)
        if i % 5 == 0:
            log("verify", "Polling inbox...")
        await asyncio.sleep(poll_s)
    return VerificationCandidate(link=None, code=None, subject=None)


def _is_retryable_error(exc: Exception) -> bool:
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
    )
    return any(marker in text for marker in retry_markers)


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
    retries: int = 2,
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
            steps = history.number_of_steps()
            errors = [e for e in history.errors() if e]
            last_error = errors[-1] if errors else None
            log(label, f"Done: success={success} steps={steps}")
            if output:
                log(label, f"Output: {output[:220]}")
            if last_error:
                log(label, f"Error detail: {last_error[:180]}")
            return RunResult(output=output, success=success, steps=steps, error=last_error)
        except Exception as exc:
            last_exc = exc
            if attempt < retries and _is_retryable_error(exc):
                backoff = 2**attempt
                if _is_concurrency_limit_error(exc):
                    killed = await stop_oldest_active_cloud_session()
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
    status = m.group(1).upper() if m else "UNKNOWN"
    d = re.search(r"DETAILS\s*:\s*(.+)", normalized, flags=re.IGNORECASE)
    details = d.group(1).strip() if d else None
    return status, details


def build_signup_task(base_url: str, identity: Identity) -> str:
    return f"""
Open {base_url} and register a NEW account by email (not OAuth).
Discover the signup path yourself from visible UI (Sign up/Register/Create account).
Stay on the target website domain only.
Do NOT use search tools, do NOT open any email provider, and do NOT navigate off-domain.
Do NOT create files, todo lists, or notes.

Credentials to use exactly:
- first_name: {identity.first_name}
- last_name: {identity.last_name}
- username: {identity.username}
- email: {identity.email}
- password: {identity.password}
- dob: {identity.dob}
- company: {identity.company}
- website: {identity.website}
- phone: {identity.phone}

Rules:
1) Choose signup/register/create account (never login).
2) If Google/GitHub/SSO appears, skip it and use email signup.
3) Close blocking popups/modals/cookie banners first, then continue.
4) Fill required fields and submit.
5) Use provided identity values only when matching fields are present.
6) If a provided field has no matching input, skip it without searching extra pages.
7) If captcha appears, solve it.
8) If captcha solving fails or times out once, STOP immediately and output SIGNUP_FAILED.
9) If you reach an OTP/email-code screen, STOP immediately and output NEEDS_VERIFICATION.
10) Stop immediately after successful submit.
11) Output exactly:
STATUS: SIGNUP_SUCCESS or NEEDS_VERIFICATION or SIGNUP_FAILED
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


async def run_signup_pipeline(args: argparse.Namespace) -> int:
    if not AGENTMAIL_API_KEY:
        raise RuntimeError("Missing AGENTMAIL_API_KEY")

    llm, resolved_model = build_llm(args.llm)
    active_model = resolved_model
    log("model", f"{args.llm} -> {resolved_model}")

    mail = AsyncAgentMail(api_key=AGENTMAIL_API_KEY, timeout=20)
    log("setup", "Creating inbox + cloud browser in parallel...")
    (inbox_id, created_inbox), browser = await asyncio.gather(
        _acquire_inbox_for_site(
            mail,
            target_url=args.url,
            reuse_inbox=args.reuse_inbox,
            sticky_inbox=args.sticky_inbox,
        ),
        create_cloud_browser(args.proxy_country),
    )
    identity = generate_identity(inbox_id)
    base_url = args.url.rstrip("/")
    log("setup", f"Inbox={inbox_id}")
    log("creds", f"User={identity.username} Password={identity.password}")

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

    signup_task = build_signup_task(base_url, identity)

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
        signup = RunResult(
            output=signup.output or "STATUS: SIGNUP_FAILED\nDETAILS: CAPTCHA_FAILED_OR_TIMEOUT",
            success=True,
            steps=signup.steps,
            error=signup.error,
        )
    # Handle flaky cloud browser disconnect/focus failures by hard rebuilding browser.
    for _ in range(2):
        if signup.success:
            break
        if args.fail_fast_captcha and _run_looks_like_captcha_failure(signup):
            break
        needs_rebuild = _needs_browser_rebuild(signup.error) or signup.steps == 0
        if not needs_rebuild:
            break
        log("signup", "Rebuilding browser after session instability and retrying.")
        try:
            await browser.stop()
        except Exception:
            pass
        browser = await create_cloud_browser(args.proxy_country)
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
    if not signup.success:
        await browser.stop()
        return 1

    signup_status, signup_details = parse_signup_status(signup.output)
    signup_text = (signup.output or "").lower()
    needs_verification = signup_status == "NEEDS_VERIFICATION" or any(
        token in signup_text for token in ("needs_verification", "verify", "confirmation", "check your email")
    )
    signup_failed_explicit = signup_status == "SIGNUP_FAILED"

    if (
        signup_failed_explicit
        and signup_details
        and args.retry_on_email_conflict
        and any(k in signup_details.lower() for k in ("already exists", "already registered", "already in use"))
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
        browser = await create_cloud_browser(args.proxy_country)
        signup_task = build_signup_task(base_url, identity)
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
        needs_verification = signup_status == "NEEDS_VERIFICATION" or any(
            token in signup_text for token in ("needs_verification", "verify", "confirmation", "check your email")
        )
        signup_failed_explicit = signup_status == "SIGNUP_FAILED"

    if signup_failed_explicit:
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
        print("LOGIN:       SKIPPED", flush=True)
        print("API_KEY:     NONE", flush=True)
        print("API_KEY_URL: NONE", flush=True)
        print("LOGIN_URL:   NONE", flush=True)
        print(f"NOTES:       {signup_details or 'Signup failed.'}", flush=True)
        print("=" * 56, flush=True)
        return 1

    verified = False
    verification_link: str | None = None
    verification_code: str | None = None

    if verify_task and needs_verification:
        log("verify", "Waiting for verification email result...")
        try:
            candidate = await asyncio.wait_for(verify_task, timeout=args.verify_timeout)
        except asyncio.TimeoutError:
            candidate = VerificationCandidate(link=None, code=None, subject=None)
            if not verify_task.done():
                verify_task.cancel()
        # If the background watcher timed out before signup completed, do one fresh bounded poll now.
        if not candidate.link and not candidate.code:
            log("verify", "No candidate from early watcher; doing a fresh verification poll...")
            candidate = await watch_for_verification(
                mail=mail,
                inbox_id=inbox_id,
                target_url=args.url,
                timeout_s=args.verify_timeout,
                min_received_at=run_started_at,
            )
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
                    f"If asked to log in, use email={identity.email} password={identity.password}.\n"
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
            flag = _verification_explicit_flag(verify_result.output)
            if flag is not None:
                verified = flag
            else:
                verified = _verification_looks_successful(verify_result.output)
        else:
            log("verify", "No link/code found within timeout.")

    if verify_task and not needs_verification and not verify_task.done():
        verify_task.cancel()

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

1) Log in with:
   Start at {base_url}, find the correct login path from UI.
- email: {identity.email}
- password: {identity.password}
2) If login needs magic link/2FA, OR page says "No account found"/"Account does not exist", stop immediately.
3) Search for API key/access token in at most 3 areas:
   Settings, Developer, Integrations
4) If found, copy full key.
5) If not found after 3 areas, stop and return NONE.
6) Output exactly:
LOGIN: SUCCESS or FAILED
API_KEY: <key> or NONE
API_KEY_URL: <url> or NONE
LOGIN_URL: <url> or NONE
NOTES: <short>
""".strip()

        login = await run_agent_task(
            browser=browser,
            llm=llm,
            label="login+api",
            task=api_task,
            max_steps=args.max_steps + 4,
            timeout_s=args.login_timeout,
        )

        parsed = parse_login_output(login.output)

        login_ok = (parsed.get("LOGIN") or "").strip().upper() == "SUCCESS"
        if login_ok and args.capture_summary:
            snapshot_task = f"""
Stay on domain {args.url}. Do not log out.
Extract account snapshot data from the currently logged-in UI.
Prefer visible dashboard/profile/settings text.
Do not create files, todos, or notes.

Output exactly:
SUBSCRIPTION: <plan/tier/status> or NONE
ENTITLEMENTS: <general included benefits/features> or NONE
LIMITS: <general limits/quotas shown> or NONE
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
                max_steps=4,
                timeout_s=60,
            )
            snapshot = parse_account_snapshot(snapshot_run.output)
    api_key = parsed["API_KEY"]
    if api_key and api_key.upper() == "NONE":
        api_key = None

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
    print(f"LOGIN:       {parsed['LOGIN'] or ('SUCCESS' if login.success else 'FAILED')}", flush=True)
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
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sigma Super signup pipeline")
    p.add_argument("url", help="Target site URL")
    p.add_argument(
        "--llm",
        default="openai-fast",
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
    p.add_argument("--skip-verification", action="store_true")
    p.add_argument(
        "--fail-fast-captcha",
        dest="fail_fast_captcha",
        action="store_true",
        default=True,
        help="Stop immediately after first captcha failure/time-out (default on).",
    )
    p.add_argument(
        "--no-fail-fast-captcha",
        dest="fail_fast_captcha",
        action="store_false",
        help="Allow retries/rebuild even after captcha failure.",
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
        action="store_true",
        help="Attempt login/API even when email verification is unresolved.",
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
    return p.parse_args()


def main() -> None:
    global _t0
    _t0 = time.time()
    args = parse_args()
    code = asyncio.run(run_signup_pipeline(args))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
