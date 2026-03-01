"""Sigma: fast automated signup + API key extraction.

Uses browser-use open-source with cloud browser + max_actions_per_step=15.
Parallel email polling + HTTP verification fast path for speed.
"""

import argparse
import asyncio
import os
import re
import secrets
import string
import time

import httpx
from agentmail import AsyncAgentMail
from browser_use import Agent, Browser
from dotenv import load_dotenv
from faker import Faker

load_dotenv()

BROWSER_USE_API_KEY = os.environ["BROWSER_USE_API_KEY"]
AGENTMAIL_API_KEY = os.environ["AGENTMAIL_API_KEY"]

fake = Faker()
_t0 = time.time()

MODEL_PRESETS = {
    "best": "gpt-5.2",
    "fast": "gpt-5-mini",
    "ultra": "gpt-5-nano",
    "bu": "bu-2-0",
}


def log(step: str, msg: str):
    print(f"  [{time.time() - _t0:6.1f}s] [{step}] {msg}", flush=True)


def resolve_model(model: str) -> str:
    return MODEL_PRESETS.get(model.strip().lower(), model.strip())


def make_llm(model: str):
    """Create an LLM instance based on model name."""
    model = resolve_model(model)
    if model.startswith("bu-"):
        from browser_use import ChatBrowserUse
        return ChatBrowserUse(model=model, api_key=BROWSER_USE_API_KEY)
    else:
        from browser_use.llm.openai.chat import ChatOpenAI
        return ChatOpenAI(model=model, api_key=os.environ["OPENAI_API_KEY"], temperature=0.0)


def generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    parts = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%"),
    ]
    parts += [secrets.choice(alphabet) for _ in range(length - 4)]
    chars = list(parts)
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def extract_links(message) -> list[str]:
    import html
    body = message.html or message.text or ""
    raw = re.findall(r"https?://[^\s<>\"']+", body)
    return [html.unescape(u) for u in raw]


def extract_verify_code(message) -> str | None:
    body = message.html or message.text or ""
    for pattern in [
        r'(?:code|pin|otp|verification)[^0-9]{0,30}(\d{4,8})',
        r'(\d{4,8})[^0-9]{0,30}(?:code|pin|otp|verification)',
        r'>\s*(\d{6})\s*<',
    ]:
        m = re.search(pattern, body, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def find_verification_link(urls: list[str]) -> str | None:
    prefer = ["verify", "confirm", "activate", "validate", "token", "auth", "callback"]
    skip = [
        "unsubscribe", "privacy", "terms", "png", "jpg", "gif", "logo",
        "w3.org", "schema.org", "xmlns", ".dtd", ".xsd", "cloudflare",
        "cdn.", "static.", "assets.", "fonts.", "img.", "images.",
        "mailto:", "tel:", ".css", ".js", "favicon", "tracking",
        "pixel", "beacon", "analytics", "facebook.com", "twitter.com",
        "linkedin.com", "youtube.com", "instagram.com",
    ]
    for url in urls:
        low = url.lower()
        if any(k in low for k in skip):
            continue
        if any(k in low for k in prefer):
            return url
    return None


async def create_cloud_browser() -> Browser:
    return Browser(
        use_cloud=True,
        cloud_proxy_country_code="us",
        # Must be True so the browser survives across signup → login agent phases.
        # We explicitly stop in the finally block to avoid zombie sessions.
        keep_alive=True,
        minimum_wait_page_load_time=0.1,
        wait_between_actions=0.1,
        wait_for_network_idle_page_load_time=0.1,
        highlight_elements=False,
        headless=True,
        captcha_solver=True,
    )


def is_retryable_cloud_error(message: str | None) -> bool:
    if not message:
        return False
    low = message.lower()
    needles = [
        "http 429",
        "too many concurrent active sessions",
        "failed to create cloud browser",
        "failed to re-create cdp session",
        "root cdp client not initialized",
    ]
    return any(n in low for n in needles)


async def run_agent(
    browser: Browser, llm, label: str, task: str,
    max_steps: int = 15, timeout: int = 180,
    is_bu_model: bool = False,
) -> tuple[str | None, bool]:
    """Run browser-use agent. Returns (output, success)."""
    log(label, f"Starting: {task[:80]}...")
    kwargs = dict(
        task=task,
        llm=llm,
        browser=browser,
        use_vision=False,
        use_judge=False,
        max_actions_per_step=15,
        include_attributes=["id", "name", "type", "placeholder", "value"],
        max_history_items=6,
        max_clickable_elements_length=12000,
        message_compaction=False,
        loop_detection_enabled=False,
        llm_timeout=30,
        step_timeout=150,
        extend_system_message=(
            "Act fast. Fill all form fields in one step. "
            "Navigate directly to URLs when known. Be concise."
        ),
    )
    if is_bu_model:
        kwargs["flash_mode"] = True
    agent = Agent(**kwargs)
    try:
        history = await asyncio.wait_for(
            agent.run(max_steps=max_steps), timeout=timeout,
        )
    except (TimeoutError, asyncio.TimeoutError):
        log(label, f"Timed out after {timeout}s")
        return None, False
    except Exception as e:
        err = str(e)
        log(label, f"Error: {err}")
        return err, False

    output = history.final_result()
    success = history.is_done()
    log(label, f"Done: steps={history.number_of_steps()} success={success}")
    if output:
        log(label, f"Output: {output[:200]}")
    errors = [e for e in history.errors() if e]
    if errors:
        log(label, f"Errors: {errors[-1][:100]}")
    return output, success


async def run_agent_with_backoff(
    browser: Browser,
    llm,
    label: str,
    task: str,
    max_steps: int,
    timeout: int,
    is_bu_model: bool,
    max_attempts: int = 4,
) -> tuple[str | None, bool, Browser]:
    delays = [12, 24, 36]
    current_browser = browser
    output: str | None = None
    success = False
    for attempt in range(1, max_attempts + 1):
        output, success = await run_agent(
            current_browser, llm, label, task, max_steps, timeout, is_bu_model,
        )
        if success or not is_retryable_cloud_error(output):
            return output, success, current_browser
        if attempt < max_attempts:
            delay = delays[min(attempt - 1, len(delays) - 1)]
            log(label, f"Cloud session busy, retrying in {delay}s (attempt {attempt + 1}/{max_attempts})...")
            try:
                await current_browser.stop()
            except Exception:
                pass
            await asyncio.sleep(delay)
            current_browser = await create_cloud_browser()
    return output, success, current_browser


async def acquire_inbox(mail: AsyncAgentMail) -> tuple[str, bool]:
    try:
        inbox = await mail.inboxes.create()
        return inbox.inbox_id, True
    except Exception as e:
        if "limit" not in str(e).lower():
            raise
        # Delete oldest inbox and create fresh (avoids stale emails)
        log("email", "Inbox limit hit, recycling oldest...")
        listed = await mail.inboxes.list(limit=10)
        if listed.inboxes:
            oldest = listed.inboxes[-1]
            try:
                await mail.inboxes.delete(oldest.inbox_id)
                log("email", f"Deleted {oldest.inbox_id}")
            except Exception:
                pass
        inbox = await mail.inboxes.create()
        return inbox.inbox_id, True


async def wait_for_verification(mail, inbox_id, target_domain: str = "", timeout_s=60, poll_s=2, seen: set[str] | None = None):
    """Poll for verification email. Returns (link, code, seen_ids).
    target_domain filters out emails from unrelated sites.
    Pass seen set to skip already-processed messages across calls.
    """
    if seen is None:
        seen = set()
    for attempt in range(max(1, timeout_s // poll_s)):
        try:
            listed = await mail.inboxes.messages.list(inbox_id=inbox_id, limit=10)
        except Exception:
            await asyncio.sleep(poll_s)
            continue
        for stub in listed.messages:
            mid = str(stub.message_id)
            if mid in seen:
                continue
            seen.add(mid)
            msg = await mail.inboxes.messages.get(inbox_id=inbox_id, message_id=mid)
            log("verify", f"Email: '{msg.subject}' from {msg.from_}")
            # Skip emails from unrelated domains
            if target_domain:
                body = (msg.html or msg.text or "") + str(msg.from_ or "")
                if target_domain not in body.lower():
                    log("verify", f"  Skipping (not from {target_domain})")
                    continue
            urls = extract_links(msg)
            link = find_verification_link(urls) if urls else None
            code = extract_verify_code(msg)
            if link or code:
                return link, code, seen
        if attempt % 5 == 0:
            log("verify", "Waiting for verification email...")
        await asyncio.sleep(poll_s)
    return None, None, seen


async def signup(
    url: str,
    model: str = "bu",
    max_steps: int = 15,
    verify_timeout: int = 60,
    skip_verification: bool = False,
    timeout: int = 300,
):
    resolved_model = resolve_model(model)
    is_bu = resolved_model.startswith("bu-")
    llm = make_llm(model)
    log("model", f"Requested={model} Resolved={resolved_model}")
    mail = AsyncAgentMail(api_key=AGENTMAIL_API_KEY, timeout=20)

    # Parallel setup: inbox + browser
    log("setup", "Creating inbox + cloud browser...")
    (email, created_new), browser = await asyncio.gather(
        acquire_inbox(mail),
        create_cloud_browser(),
    )
    log("setup", f"Email: {email}")

    # Generate realistic identity
    first = fake.first_name()
    last = fake.last_name()
    username = f"{first.lower()}{last.lower()}{secrets.randbelow(1000):03d}"
    password = generate_password()
    dob = fake.date_of_birth(minimum_age=18, maximum_age=45).strftime("%Y-%m-%d")
    log("creds", f"{first} {last} / {username} / {password}")

    try:
        browser = await _run_signup_flow(
            url, browser, llm, mail, email, is_bu, resolved_model,
            first, last, username, password, dob,
            max_steps, verify_timeout, skip_verification, timeout,
        )
    finally:
        # ALWAYS stop browser + clean inbox
        try:
            await browser.stop()
        except Exception:
            pass
        if created_new:
            try:
                await mail.inboxes.delete(email)
            except Exception:
                pass


async def _run_signup_flow(
    url, browser, llm, mail, email, is_bu, resolved_model,
    first, last, username, password, dob,
    max_steps, verify_timeout, skip_verification, timeout,
) -> Browser:
    # ── Phase 1: Signup + parallel email polling ──
    email_poller: asyncio.Task | None = None
    if not skip_verification:
        from urllib.parse import urlparse
        target_domain = urlparse(url).netloc.replace("www.", "").replace("app.", "")
        email_poller = asyncio.create_task(
            wait_for_verification(mail, email, target_domain=target_domain, timeout_s=300, poll_s=2)
        )

    signup_task = f"""Sign up on {url} with email signup (NOT OAuth/Google/GitHub).

Credentials:
  Name: {first} {last}
  Username: {username}
  Email: {email}
  Password: {password}
  Password confirmation: {password}
  Date of birth: {dob}

INSTRUCTIONS:
1. Go to {url} and find the signup/register page.
2. Fill ALL form fields in a SINGLE step. Check any terms checkbox.
3. Submit the form.
4. If CAPTCHA cleared fields, re-fill and submit again.
5. Report "SIGNUP_SUCCESS" or "NEEDS_VERIFICATION" and STOP."""

    signup_output, signup_ok, browser = await run_agent_with_backoff(
        browser, llm, "signup", signup_task, max_steps, timeout, is_bu,
    )

    if not signup_ok:
        if email_poller:
            email_poller.cancel()
        print(f"\n  SIGNUP FAILED on {url}", flush=True)
        if signup_output:
            print(f"  Output: {signup_output[:300]}", flush=True)
        return browser

    # ── Phase 2: Verification (fast path via HTTP) ──
    needs_verify = signup_output and any(
        w in signup_output.lower()
        for w in ["needs_verification", "verify", "check your", "confirmation"]
    )

    verification_link = None
    verification_code = None
    verified_via_http = False
    seen_msg_ids: set[str] = set()

    if needs_verify and not skip_verification and email_poller:
        if email_poller.done():
            verification_link, verification_code, seen_msg_ids = email_poller.result()
            if verification_link or verification_code:
                log("verify", "Email found during signup (zero extra wait)!")
        if not (verification_link or verification_code) and not email_poller.done():
            log("verify", f"Waiting for verification email ({verify_timeout}s)...")
            try:
                verification_link, verification_code, seen_msg_ids = await asyncio.wait_for(
                    email_poller, timeout=verify_timeout,
                )
            except (asyncio.TimeoutError, TimeoutError):
                email_poller.cancel()
                log("verify", "No verification email found.")
        elif not (verification_link or verification_code):
            log("verify", "No verification email found.")

        # FAST PATH: verify via HTTP GET (~1s) instead of browser agent (~30-50s)
        if verification_link:
            log("verify", f"HTTP verify: {verification_link[:80]}...")
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
                    resp = await client.get(verification_link)
                    verified_via_http = resp.status_code < 400
                    log("verify", f"HTTP {resp.status_code} -> {'verified!' if verified_via_http else 'failed'}")
            except Exception as e:
                log("verify", f"HTTP verify error: {e}")
    else:
        if email_poller:
            email_poller.cancel()
        if needs_verify and skip_verification:
            log("verify", "Skipping verification.")

    # ── Phase 3: Login + API Key ──
    is_magic_link = signup_output and "magic link" in signup_output.lower()

    verify_preamble = ""
    if verification_link and not verified_via_http:
        verify_preamble = (
            f"FIRST: Navigate to {verification_link} to verify email.\n\n"
        )
    elif verification_code and not verified_via_http:
        verify_preamble = (
            f"FIRST: Enter verification code {verification_code} and submit.\n\n"
        )

    if is_magic_link:
        # ── Magic-link login: trigger link, catch email, navigate in browser ──
        log("login", "Magic-link site detected — triggering login link...")
        from urllib.parse import urlparse
        ml_domain = urlparse(url).netloc.replace("www.", "").replace("app.", "")

        # 3a: agent triggers the magic link
        trigger_task = (
            f"Go to {url} and sign in. Enter email {email} and click 'Continue with Email' "
            f"or similar submit button. Wait until you see 'check your email' or 'magic link sent'. "
            f"Then report MAGIC_LINK_TRIGGERED and STOP. Do NOT visit any other site."
        )
        await run_agent(browser, llm, "magic-trigger", trigger_task, 8, 60, is_bu)

        # 3b: poll inbox for the login magic link (skip already-seen emails from Phase 2)
        log("login", "Polling for magic-link email...")
        magic_link, _, _ = await wait_for_verification(
            mail, email, target_domain=ml_domain, timeout_s=30, poll_s=2, seen=seen_msg_ids,
        )

        if magic_link:
            # 3c: feed magic link to agent's browser (sets session cookies)
            log("login", f"Got magic link: {magic_link[:80]}...")
            apikey_task = (
                f"FIRST: Navigate to this exact URL to log in: {magic_link}\n"
                f"Wait for the page to load and confirm you are logged in.\n\n"
                f"Then FIND API KEY: Check {url}/settings/api, {url}/account/api-keys, "
                f"{url}/settings/extensions, {url}/dashboard, {url}/settings. "
                f"Copy existing key or create one named 'sigma'. "
                f"If no API section after 2 pages, report NONE.\n\n"
                f"OUTPUT: LOGIN: SUCCESS|FAILED / API_KEY: <key>|NONE / API_KEY_URL: <url>|NONE / LOGIN_URL: <url>"
            )
        else:
            log("login", "No magic link email found, trying password fallback...")
            apikey_task = (
                f"Only visit {url}. Do NOT visit agentmail.to or any other site.\n\n"
                f"1. LOG IN at {url}. Email={email} Password={password}. If already logged in, skip.\n\n"
                f"2. FIND API KEY: Check {url}/settings/api, {url}/account/api-keys, {url}/settings/extensions. "
                f"Copy existing key or create one named 'sigma'. If no API section after 2 pages, report NONE.\n\n"
                f"OUTPUT: LOGIN: SUCCESS|FAILED / API_KEY: <key>|NONE / API_KEY_URL: <url>|NONE / LOGIN_URL: <url>"
            )
    else:
        # ── Standard password login ──
        log("login", "Password-based login...")
        apikey_task = (
            f"Only visit {url}. Do NOT visit agentmail.to or any other site.\n\n"
            f"{verify_preamble}"
            f"1. LOG IN at {url}. Email={email} Password={password}. If already logged in, skip.\n\n"
            f"2. FIND API KEY: Check {url}/settings/api, {url}/account/api-keys, {url}/settings/extensions. "
            f"Copy existing key or create one named 'sigma'. If no API section after 2 pages, report NONE.\n\n"
            f"OUTPUT: LOGIN: SUCCESS|FAILED / API_KEY: <key>|NONE / API_KEY_URL: <url>|NONE / LOGIN_URL: <url>"
        )

    apikey_output, login_ok, browser = await run_agent_with_backoff(
        browser, llm, "login+apikey", apikey_task, max_steps + 5, timeout, is_bu,
    )

    # Parse results
    api_key = api_key_url = login_url = None
    if apikey_output:
        norm = apikey_output.replace("\\n", "\n")
        m = re.search(r"API_KEY:\s*(\S+)", norm)
        if m and "none" not in m.group(1).strip().lower():
            api_key = m.group(1).strip()
        m = re.search(r"API_KEY_URL:\s*(https?://\S+)", norm)
        if m:
            api_key_url = m.group(1).strip()
        m = re.search(r"LOGIN_URL:\s*(https?://\S+)", norm)
        if m:
            login_url = m.group(1).strip()

    # ── Output ──
    elapsed = time.time() - _t0
    print(f"\n{'='*50}", flush=True)
    print(f"  RESULTS ({elapsed:.0f}s, model={resolved_model})", flush=True)
    print(f"{'='*50}", flush=True)
    print(f"  URL:      {url}", flush=True)
    print(f"  Email:    {email}", flush=True)
    print(f"  Password: {password}", flush=True)
    print(f"  Username: {username}", flush=True)
    print(f"  Signup:   {'ok' if signup_ok else 'failed'}", flush=True)
    vmethod = 'http' if verified_via_http else 'agent' if (verification_link or verification_code) else 'n/a'
    print(f"  Verified: {vmethod}", flush=True)
    print(f"  Login:    {'ok' if login_ok else 'failed'}", flush=True)
    if api_key:
        print(f"  API Key:  {api_key}", flush=True)
    if api_key_url:
        print(f"  Key URL:  {api_key_url}", flush=True)
    print(f"{'='*50}", flush=True)
    print(f"\n  TO LOG IN:", flush=True)
    print(f"  1. Go to {login_url or url}", flush=True)
    print(f"  2. Email:    {email}", flush=True)
    print(f"  3. Password: {password}", flush=True)
    if api_key_url:
        print(f"  4. API keys: {api_key_url}", flush=True)
    print(flush=True)
    return browser


_t0 = time.time()


def main():
    global _t0
    _t0 = time.time()

    parser = argparse.ArgumentParser(description="Sigma: auto signup + API key")
    parser.add_argument("url", help="Website URL")
    parser.add_argument(
        "--llm", default="bu",
        help="Preset: bu(bu-2-0, fastest), best(gpt-5.2), fast(gpt-5-mini), ultra(gpt-5-nano), or raw model id",
    )
    parser.add_argument("--max-steps", type=int, default=15)
    parser.add_argument("--verify-timeout", type=int, default=60)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--skip-verification", action="store_true")
    args = parser.parse_args()

    asyncio.run(signup(
        url=args.url,
        model=args.llm,
        max_steps=args.max_steps,
        verify_timeout=args.verify_timeout,
        skip_verification=args.skip_verification,
        timeout=args.timeout,
    ))


if __name__ == "__main__":
    main()
