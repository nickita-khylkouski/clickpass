"""Sigma: fast automated signup + API key extraction.

Uses browser-use open-source with cloud browser + max_actions_per_step=10.
Default LLM: bu-2-0 (Browser Use's optimized model). Alt: gpt-4o via --llm flag.
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


def log(step: str, msg: str):
    print(f"  [{time.time() - _t0:6.1f}s] [{step}] {msg}", flush=True)


def make_llm(model: str):
    """Create an LLM instance based on model name."""
    if model.startswith("bu-") or model == "bu-2-0":
        from browser_use import ChatBrowserUse
        return ChatBrowserUse(model=model, api_key=BROWSER_USE_API_KEY)
    elif model.startswith("gpt-") or model.startswith("o1") or model.startswith("o3"):
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, api_key=os.environ["OPENAI_API_KEY"])
    else:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, api_key=os.environ["OPENAI_API_KEY"])


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
    # Decode HTML entities (&amp; → &, etc.) so URLs work correctly
    return [html.unescape(u) for u in raw]


def extract_verify_code(message) -> str | None:
    """Extract numeric verification code from email."""
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


def find_verification_link(urls: list[str], target_domain: str | None = None) -> str | None:
    prefer = ["verify", "confirm", "activate", "validate", "token", "auth", "callback"]
    skip = [
        "unsubscribe", "privacy", "terms", "png", "jpg", "gif", "logo",
        "w3.org", "schema.org", "xmlns", ".dtd", ".xsd", "cloudflare",
        "cdn.", "static.", "assets.", "fonts.", "img.", "images.",
        "mailto:", "tel:", ".css", ".js", "favicon", "tracking",
        "pixel", "beacon", "analytics", "facebook.com", "twitter.com",
        "linkedin.com", "youtube.com", "instagram.com",
    ]
    # First pass: prefer links matching target_domain with verify keywords
    if target_domain:
        for url in urls:
            low = url.lower()
            if target_domain not in low:
                continue
            if any(k in low for k in skip):
                continue
            if any(k in low for k in prefer):
                return url
    # Second pass: any link with verify keywords
    for url in urls:
        low = url.lower()
        if any(k in low for k in skip):
            continue
        if any(k in low for k in prefer):
            return url
    return None


def is_cloud_session_limit_error(text: str | None) -> bool:
    if not text:
        return False
    low = text.lower()
    return (
        "too many concurrent active sessions" in low
        or "http 429" in low
        or "status code 429" in low
    )


async def browser_goto(browser: Browser, url: str, wait: float = 3) -> bool:
    """Navigate the browser directly (avoids LLM garbling URLs)."""
    try:
        await browser.navigate_to(url)
        await asyncio.sleep(wait)
        return True
    except Exception as e:
        log("browser", f"navigate_to failed: {e}")
        return False


async def browser_js_navigate(browser: Browser, url: str, wait: float = 5) -> bool:
    """Navigate via JS in the current page context (preserves session state)."""
    try:
        page = await browser.get_current_page()
        safe_url = url.replace("\\", "\\\\").replace("'", "\\'")
        await page.evaluate(f"() => {{ window.location.href = '{safe_url}'; }}")
        await asyncio.sleep(wait)
        return True
    except Exception as e:
        log("browser", f"JS navigate failed: {e}")
        # Fallback to navigate_to
        try:
            await browser.navigate_to(url)
            await asyncio.sleep(wait)
            return True
        except Exception as e2:
            log("browser", f"navigate_to fallback also failed: {e2}")
            return False


async def create_cloud_browser() -> Browser:
    """Cloud browser with aggressive speed settings."""
    browser = Browser(
        use_cloud=True,
        cloud_proxy_country_code="us",
        keep_alive=True,
        minimum_wait_page_load_time=0.05,
        wait_between_actions=0.05,
        wait_for_network_idle_page_load_time=0.05,
        highlight_elements=False,
        headless=True,
        captcha_solver=True,
    )
    return browser


async def run_agent(
    browser: Browser, llm, label: str, task: str,
    max_steps: int = 15, timeout: int = 180,
) -> tuple[str | None, bool]:
    """Run browser-use agent. Returns (output, success)."""
    log(label, f"Starting: {task[:80]}...")
    agent = Agent(
        task=task,
        llm=llm,
        browser=browser,
        flash_mode=True,
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
            "Be extremely concise and direct. Get to the goal as quickly as possible. "
            "Use multi-action sequences whenever possible to reduce steps. "
            "Fill all form fields in a single step. Never hesitate or explore unnecessarily. "
            "Navigate directly to URLs when known. Do not explain your actions."
        ),
    )
    try:
        history = await asyncio.wait_for(
            agent.run(max_steps=max_steps), timeout=timeout,
        )
    except (TimeoutError, asyncio.TimeoutError):
        log(label, f"Timed out after {timeout}s")
        return f"ERROR: timed out after {timeout}s", False
    except Exception as e:
        log(label, f"Error: {e}")
        return f"ERROR: {e}", False

    output = history.final_result()
    success = history.is_successful() or False  # agent's self-reported success
    log(label, f"Done: steps={history.number_of_steps()} success={success}")
    if output:
        log(label, f"Output: {output[:200]}")
    errors = [e for e in history.errors() if e]
    if errors:
        log(label, f"Errors: {errors[-1][:100]}")
    return output, success


async def acquire_inbox(mail: AsyncAgentMail) -> tuple[str, bool]:
    try:
        inbox = await mail.inboxes.create()
        return inbox.inbox_id, True
    except Exception as e:
        if "limit" not in str(e).lower():
            raise
        # Delete oldest inbox and create a fresh one
        log("email", "Inbox limit hit, deleting oldest and creating fresh...")
        listed = await mail.inboxes.list(limit=5)
        if listed.inboxes:
            try:
                await mail.inboxes.delete(listed.inboxes[-1].inbox_id)
            except Exception:
                pass
        inbox = await mail.inboxes.create()
        return inbox.inbox_id, True


async def wait_for_verification(
    mail,
    inbox_id,
    timeout_s=60,
    poll_s=3,
    target_domain=None,
    subject_keywords: list[str] | None = None,
    exclude_links: set[str] | None = None,
):
    """Poll for verification email. Returns (link, code).

    target_domain: if set, skip emails whose sender doesn't match this domain.
    subject_keywords: if set, only accept emails whose subject contains one of these substrings.
    """
    seen: set[str] = set()
    got_non_verify_at: float | None = None
    for attempt in range(max(1, timeout_s // poll_s)):
        if got_non_verify_at and (time.time() - got_non_verify_at) > 15:
            log("verify", "Got email(s) but no verify link — moving on.")
            return None, None
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
            sender = str(msg.from_ or "").lower()
            log("verify", f"Email: '{msg.subject}' from {msg.from_}")
            # Skip emails from unrelated domains
            if target_domain and target_domain not in sender:
                log("verify", f"  Skipping (sender doesn't match {target_domain})")
                continue
            subject = str(msg.subject or "").lower()
            if subject_keywords and not any(k in subject for k in subject_keywords):
                log("verify", f"  Skipping (subject doesn't match {subject_keywords})")
                continue
            urls = extract_links(msg)
            link = find_verification_link(urls, target_domain=target_domain) if urls else None
            if link and exclude_links and link in exclude_links:
                log("verify", "  Skipping (link already used)")
                continue
            code = extract_verify_code(msg)
            if link or code:
                return link, code
            log("verify", f"  No link/code matched ({len(urls)} URLs found)")
            if got_non_verify_at is None:
                got_non_verify_at = time.time()
        if attempt % 5 == 0:
            log("verify", "Waiting for verification email...")
        await asyncio.sleep(poll_s)
    return None, None


async def signup(
    url: str,
    model: str = "bu-2-0",
    max_steps: int = 15,
    verify_timeout: int = 60,
    skip_verification: bool = False,
    timeout: int = 300,
):
    from urllib.parse import urlparse
    llm = make_llm(model)
    mail = AsyncAgentMail(api_key=AGENTMAIL_API_KEY, timeout=20)
    target_domain = urlparse(url).netloc.replace("www.", "")

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

    # ── Phase 1: Signup + parallel email polling ──
    # Poll inbox DURING signup so email is ready the moment signup finishes.
    # Poller runs long; we cancel after verify_timeout post-signup.
    email_poller: asyncio.Task | None = None
    if not skip_verification:
        email_poller = asyncio.create_task(
            wait_for_verification(mail, email, timeout_s=300, poll_s=2, target_domain=target_domain)
        )

    signup_task = f"""Create an account on {url} using email (NOT OAuth/Google/GitHub).
Only visit {url}. Do NOT visit agentmail.to or any other site.

Credentials: name={first} {last} | username={username} | email={email} | password={password} | dob={dob}

DO THIS:
1. Go to {url} and find the signup/register page. Click "Create account", "Sign up", or "Register". If OAuth options appear, choose email signup.
2. Fill ALL form fields in ONE step (name, username, email, password, password confirmation). Check terms checkbox. Invent values for unlisted fields. Submit.
3. If the site uses passwordless auth and says "Check your email" or "magic link sent" (no password field), report MAGIC_LINK immediately and STOP. Do not keep searching.
4. If CAPTCHA cleared the form, re-fill ALL fields and submit again.
5. Report "SIGNUP_SUCCESS", "NEEDS_VERIFICATION", or "MAGIC_LINK" then STOP."""

    signup_output = None
    signup_ok = False
    signup_attempts = 3
    for attempt in range(1, signup_attempts + 1):
        signup_output, signup_ok = await run_agent(
            browser, llm, "signup", signup_task, max_steps, timeout,
        )
        out_low = (signup_output or "").lower()
        if not signup_ok and (
            "needs_verification" in out_low
            or "magic_link" in out_low
            or "magic link" in out_low
            or "check your email" in out_low
            or "passwordless" in out_low
        ):
            signup_ok = True
            # Preserve MAGIC_LINK vs NEEDS_VERIFICATION distinction
            if "magic_link" in out_low or "magic link" in out_low or "passwordless" in out_low:
                signup_output = "MAGIC_LINK"
            elif "needs_verification" not in out_low:
                signup_output = "NEEDS_VERIFICATION"
        if signup_ok:
            break
        if attempt >= signup_attempts:
            break

        if is_cloud_session_limit_error(signup_output):
            delay_s = min(20, 5 * attempt)
            log("signup", f"Cloud browser at capacity (429), waiting {delay_s}s before retry...")
            await asyncio.sleep(delay_s)
        else:
            log("signup", f"Signup attempt {attempt} failed, retrying with fresh browser...")

        try:
            await browser.stop()
        except Exception:
            pass
        browser = await create_cloud_browser()

    if not signup_ok:
        if email_poller:
            email_poller.cancel()
        await browser.stop()
        print(f"\n  SIGNUP FAILED on {url}", flush=True)
        if signup_output:
            print(f"  Output: {signup_output[:300]}", flush=True)
        return False

    # ── Phase 2: Verification (fast path — no agent needed) ──
    needs_verify = signup_output and any(
        w in signup_output.lower()
        for w in ["needs_verification", "magic_link", "verify", "check your", "confirmation"]
    )

    verification_link = None
    verification_code = None
    verified_via_http = False

    # Detect if signup was via magic link / passwordless
    is_magic_link_site = "magic_link" in (signup_output or "").lower()
    if is_magic_link_site:
        log("detect", "Magic link / passwordless site detected — will use browser for verification")

    if needs_verify and not skip_verification and email_poller:
        # Check if email was already found during signup
        if email_poller.done():
            verification_link, verification_code = email_poller.result()
            if verification_link or verification_code:
                log("verify", "Email found during signup (zero extra wait)!")
        if not (verification_link or verification_code):
            # Wait up to 15s for the email — it often arrives right after signup
            log("verify", "Waiting up to 15s for verification email...")
            try:
                verification_link, verification_code = await asyncio.wait_for(
                    email_poller, timeout=15,
                )
                if verification_link or verification_code:
                    log("verify", "Email found!")
            except (asyncio.TimeoutError, TimeoutError):
                log("verify", "No verification email after 15s, proceeding to login.")

        if verification_link and is_magic_link_site:
            # MAGIC LINK SITES: navigate browser to the verify link so Clerk's JS
            # can process the verification code. The page may require WebAuthn/passkey
            # setup — inject a virtual authenticator via CDP to auto-complete it.
            log("verify", f"Magic link site — browser verify: {verification_link[:80]}...")
            try:
                # Inject virtual WebAuthn authenticator via CDP so passkey setup
                # completes automatically in the headless cloud browser.
                try:
                    cdp_session = await browser.get_or_create_cdp_session()
                    sid = cdp_session.session_id
                    cdp = cdp_session.cdp_client
                    await cdp.send.WebAuthn.enable(
                        params={"enableUI": False}, session_id=sid,
                    )
                    result = await cdp.send.WebAuthn.addVirtualAuthenticator(
                        params={
                            "options": {
                                "protocol": "ctap2",
                                "transport": "internal",
                                "hasResidentKey": True,
                                "hasUserVerification": True,
                                "isUserVerified": True,
                            }
                        },
                        session_id=sid,
                    )
                    log("verify", f"Virtual WebAuthn authenticator injected: {result}")
                except Exception as e:
                    log("verify", f"WebAuthn CDP setup failed: {e}")

                await browser.navigate_to(verification_link)
                # Wait for Clerk JS to process verification + WebAuthn flow
                await asyncio.sleep(12)
                try:
                    cur_url = await browser.get_current_page_url()
                    log("verify", f"After verify: {cur_url[:80]}")
                except Exception:
                    pass

                # Auto-complete any profile/onboarding form via JS
                # (agent consistently clicks Cancel instead of Save)
                await asyncio.sleep(3)  # Let dashboard/form render
                try:
                    page = await browser.get_current_page()
                    form_result = await page.evaluate("""() => {
                        const setter = Object.getOwnPropertyDescriptor(
                            HTMLInputElement.prototype, 'value'
                        ).set;
                        function setInput(el, val) {
                            setter.call(el, val);
                            el.dispatchEvent(new Event('input', {bubbles: true}));
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                        }
                        const fn = document.getElementById('firstName')
                            || document.querySelector('input[name="firstName"]');
                        const ln = document.getElementById('lastName')
                            || document.querySelector('input[name="lastName"]');
                        const tos = document.getElementById('tos')
                            || document.querySelector('input[name="tos"]');
                        if (!fn && !ln) return 'no-form';
                        if (fn) setInput(fn, 'Sigma');
                        if (ln) setInput(ln, 'Agent');
                        if (tos && !tos.checked) {
                            tos.click();
                        }
                        const btns = Array.from(document.querySelectorAll('button'));
                        const save = btns.find(b =>
                            /save|continue|submit|next/i.test(b.textContent)
                            && !/cancel/i.test(b.textContent)
                        );
                        if (save) { save.click(); return 'submitted'; }
                        return 'filled-no-save-btn';
                    }""")
                    log("verify", f"Profile form: {form_result}")
                    if form_result == "submitted":
                        await asyncio.sleep(3)  # Wait for form submission
                except Exception as e:
                    log("verify", f"Profile form JS: {e}")

                verified_via_http = True
            except Exception as e:
                log("verify", f"Browser verify error: {e}")
        elif verification_link:
            # REGULAR SITES: HTTP GET is faster (1s vs 30-50s agent)
            log("verify", f"HTTP verify: {verification_link[:80]}...")
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
                    resp = await client.get(verification_link)
                    verified_via_http = resp.status_code < 400
                    log("verify", f"HTTP {resp.status_code} → {'verified!' if verified_via_http else 'failed'}")
            except Exception as e:
                log("verify", f"HTTP verify error: {e}")
    else:
        if email_poller:
            email_poller.cancel()
        if needs_verify and skip_verification:
            log("verify", "Skipping verification.")

    # ── Phase 3: Login + API Key ──
    log("apikey", "Looking for API key...")

    if is_magic_link_site:
        # MAGIC LINK SITES: Phase 2 verified email + completed WebAuthn via CDP.
        # We should already be logged in (on dashboard). Just need to complete
        # any onboarding and find API keys.
        apikey_task = (
            f"Only visit {url}. Do NOT visit agentmail.to or any other site.\n\n"
            f"You are ALREADY LOGGED IN. Do NOT sign in again.\n\n"
            f"1. COMPLETE PROFILE (if shown): If you see a profile form asking for first name, "
            f"last name, or terms of service — fill them in and click 'Save and continue'. "
            f"CRITICAL: Click 'Save and continue', NEVER click 'Cancel'.\n\n"
            f"2. GET API KEY: Navigate to API Keys, Settings, Account, or Developer pages. "
            f"If a key exists, copy it. Otherwise create one named 'sigma'. "
            f"Click/expand to reveal the full key string. Read it from the page text. "
            f"If no API section after checking 2-3 pages, report NONE.\n\n"
            f"OUTPUT (each on new line):\n"
            f"LOGIN_STATUS: success\n"
            f"API_KEY: <the full key string or NONE>\n"
            f"API_KEY_URL: <url of key page or NONE>"
        )
    else:
        # If HTTP verify failed, tell the agent to verify first
        verify_preamble = ""
        if verification_link and not verified_via_http:
            verify_preamble = (
                f"STEP 0 — VERIFY EMAIL FIRST:\n"
                f"  Navigate to: {verification_link}\n"
                f"  Click Confirm/Verify. Skip onboarding.\n\n"
            )
        elif verification_code and not verified_via_http:
            verify_preamble = (
                f"STEP 0 — ENTER VERIFICATION CODE:\n"
                f"  Enter code: {verification_code} and submit.\n\n"
            )

        apikey_task = (
            f"Only visit {url}. Do NOT visit agentmail.to or any other site.\n\n"
            f"{verify_preamble}"
            f"1. LOG IN: Go to {url}, find the login/sign-in page. "
            f"Enter email={email} password={password}. Submit. If already logged in, skip. "
            f"If you see onboarding/welcome screens, SKIP them immediately — navigate directly to the settings page. "
            f"If there is no password field and the site says it sent a magic link, report failed:magic link required immediately (do not loop).\n\n"
        f"2. GET API KEY: Look in Settings/Account for API keys, developer settings, or extensions. "
        f"If a key exists, copy it. Otherwise create one named 'sigma'. "
        f"Click/expand to reveal the full key string. Read it from the page text. "
        f"If no API section after checking 2-3 pages, report NONE.\n\n"
        f"OUTPUT (each on new line):\n"
        f"LOGIN_STATUS: success OR failed:<reason> (e.g. failed:email not confirmed)\n"
        f"API_KEY: <the full key string or NONE>\n"
        f"API_KEY_URL: <url of key page or NONE>"
    )

    apikey_output, login_ok = await run_agent(
        browser, llm, "login+apikey", apikey_task, max_steps + 5, timeout,
    )

    # ── Detect actual login failure from agent output ──
    _fail_words = [
        "not confirmed", "not verified", "confirm your email", "verify your email",
        "invalid login", "invalid credentials", "incorrect password", "login failed",
        "authentication failed", "account not found", "wrong password",
    ]
    out_lower = (apikey_output or "").lower()
    # Check LOGIN_STATUS field first (most reliable)
    login_status_match = re.search(r"login_status:\s*failed", out_lower)
    if login_status_match or any(w in out_lower for w in _fail_words):
        login_ok = False

    # ── Retry: passwordless/magic-link login flow (only for non-magic-link sites) ──
    magic_link_required = (
        not is_magic_link_site  # Magic link sites already handled above
        and not login_ok
        and (
            "magic link" in out_lower
            or "passwordless" in out_lower
            or "check your email" in out_lower
        )
    )
    if magic_link_required:
        # Collect already-used links so we skip the verification email
        used_links: set[str] = set()
        if verification_link:
            used_links.add(verification_link)

        # The login agent likely already entered the email and triggered the magic link send.
        # Poll for the new magic link email (exclude old verification link).
        magic_wait_s = 15
        log("verify", f"Magic-link login detected — polling inbox ({magic_wait_s}s)...")
        magic_link, _ = await wait_for_verification(
            mail,
            email,
            timeout_s=magic_wait_s,
            poll_s=2,
            target_domain=target_domain,
            exclude_links=used_links,
        )
        if magic_link:
            # Use JS navigation to preserve Clerk/auth session context.
            log("verify", f"Opening magic link in browser: {magic_link[:80]}...")
            magic_ok = await browser_js_navigate(browser, magic_link, wait=8)
            if magic_ok:
                # Check where we ended up
                try:
                    cur_url = await browser.get_current_page_url()
                    log("verify", f"After magic link, page at: {cur_url[:80]}")
                    # If still on verify page, wait more for JS redirect
                    if "verify" in (cur_url or "").lower():
                        log("verify", "Still on verify page, waiting 5s more...")
                        await asyncio.sleep(5)
                        cur_url = await browser.get_current_page_url()
                        log("verify", f"Now at: {cur_url[:80]}")
                except Exception:
                    pass
            if magic_ok:
                log("retry", "Retrying API key extraction after magic-link login...")
                api_only_task = (
                    f"Only visit {url}. You are already logged in.\n\n"
                    f"Find API keys/developer settings/extensions in account/settings pages. "
                    f"Copy existing key or create one named 'sigma'. Expand to reveal full key.\n\n"
                    f"OUTPUT (each on new line):\n"
                    f"LOGIN_STATUS: success\n"
                    f"API_KEY: <the full key string or NONE>\n"
                    f"API_KEY_URL: <url of key page or NONE>"
                )
                apikey_output, login_ok = await run_agent(
                    browser, llm, "api-key-only", api_only_task, max_steps + 3, timeout,
                )
            else:
                log("verify", "Magic-link open failed; cannot auto-login.")
        if not magic_link:
            # Fallback: trigger a fresh magic link send and retry
            log("verify", "No magic email yet — triggering fresh send...")
            trigger_task = (
                f"Go to {url} login/sign-in page. Enter email={email} and submit. "
                f"Once you see 'magic link sent' or 'check your email', report MAGIC_LINK_SENT and STOP."
            )
            await run_agent(browser, llm, "trigger-magic", trigger_task, 5, 60)
            magic_link, _ = await wait_for_verification(
                mail, email, timeout_s=20, poll_s=2,
                target_domain=target_domain, exclude_links=used_links,
            )
        if not magic_link:
            log("verify", "No magic-link email found; cannot auto-login.")

    # ── Retry: if login failed due to unconfirmed email ──
    _unconfirmed_words = ["not confirmed", "not verified", "confirm your email", "verify your email"]
    login_failed_unconfirmed = login_status_match and any(w in out_lower for w in _unconfirmed_words)
    if not login_failed_unconfirmed:
        login_failed_unconfirmed = any(w in out_lower for w in _unconfirmed_words)
    if login_failed_unconfirmed:
        # Case 1: Poller still running — wait for it
        if email_poller and not email_poller.done():
            log("verify", f"Login blocked — waiting for verify email ({verify_timeout}s)...")
            try:
                verification_link, verification_code = await asyncio.wait_for(
                    email_poller, timeout=verify_timeout,
                )
            except (asyncio.TimeoutError, TimeoutError):
                verification_link, verification_code = None, None
        # Case 2: Poller done but found nothing useful — poll fresh
        elif not verified_via_http and not verification_link and not verification_code:
            log("verify", f"Login blocked — polling fresh for verify email ({verify_timeout}s)...")
            verification_link, verification_code = await wait_for_verification(
                mail, email, timeout_s=verify_timeout, poll_s=2, target_domain=target_domain,
            )

        # Try HTTP verification if we have a link
        if verification_link and not verified_via_http:
            log("verify", f"HTTP verify: {verification_link[:80]}...")
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
                    resp = await client.get(verification_link)
                    verified_via_http = resp.status_code < 400
                    log("verify", f"HTTP {resp.status_code} → {'verified!' if verified_via_http else 'failed'}")
            except Exception as e:
                log("verify", f"HTTP verify error: {e}")
            if verified_via_http:
                log("retry", "Retrying login after verification...")
                apikey_output, login_ok = await run_agent(
                    browser, llm, "login+apikey", apikey_task, max_steps + 5, timeout,
                )
                # Re-check login status on retry
                if apikey_output and any(w in apikey_output.lower() for w in _fail_words):
                    login_ok = False
        elif verification_code:
            log("verify", f"Got code: {verification_code} — agent will enter it on retry")
            code_task = f"Enter verification code {verification_code} on {url}, submit, then: " + apikey_task
            apikey_output, login_ok = await run_agent(
                browser, llm, "login+apikey", code_task, max_steps + 5, timeout,
            )
            if apikey_output and any(w in apikey_output.lower() for w in _fail_words):
                login_ok = False
        else:
            log("verify", "No verification email arrived — cannot unblock login.")

    # Cancel poller if still running
    if email_poller and not email_poller.done():
        email_poller.cancel()

    # Parse results
    api_key = api_key_url = login_url = login_status = None
    if apikey_output:
        norm = apikey_output.replace("\\n", "\n")
        m = re.search(r"LOGIN_STATUS:\s*(.+)", norm, re.IGNORECASE)
        if m:
            login_status = m.group(1).strip()
            if "failed" in login_status.lower():
                login_ok = False
        m = re.search(r"API_KEY:\s*(.+)", norm)
        if m and "none" not in m.group(1).strip().lower():
            api_key = m.group(1).strip()
        m = re.search(r"API_KEY_URL:\s*(https?://\S+)", norm)
        if m:
            api_key_url = m.group(1).strip()
        m = re.search(r"LOGIN_URL:\s*(https?://\S+)", norm)
        if m:
            login_url = m.group(1).strip()

    await browser.stop()

    # Cleanup
    if created_new:
        try:
            await mail.inboxes.delete(email)
        except Exception:
            pass

    # ── Output ──
    elapsed = time.time() - _t0
    print(f"\n{'='*50}", flush=True)
    print(f"  RESULTS ({elapsed:.1f}s, model={model})", flush=True)
    print(f"{'='*50}", flush=True)
    print(f"  URL:      {url}", flush=True)
    print(f"  Email:    {email}", flush=True)
    print(f"  Password: {password}", flush=True)
    print(f"  Username: {username}", flush=True)
    print(f"  Signup:   {'ok' if signup_ok else 'failed'}", flush=True)
    vmethod = 'http(fast)' if verified_via_http else 'agent' if (verification_link or verification_code) else 'n/a'
    print(f"  Verified: {vmethod}", flush=True)
    login_display = 'ok' if login_ok else f'failed ({login_status})' if login_status else 'failed'
    print(f"  Login:    {login_display}", flush=True)
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
    return True


_t0 = time.time()


def main():
    global _t0
    _t0 = time.time()

    parser = argparse.ArgumentParser(description="Sigma: auto signup + API key")
    parser.add_argument("url", help="Website URL")
    parser.add_argument("--llm", default="bu-2-0",
        help="LLM model: bu-2-0 (default, fastest), gpt-4o, gpt-4o-mini")
    parser.add_argument("--max-steps", type=int, default=15)
    parser.add_argument("--verify-timeout", type=int, default=60)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--skip-verification", action="store_true")
    args = parser.parse_args()

    ok = asyncio.run(signup(
        url=args.url,
        model=args.llm,
        max_steps=args.max_steps,
        verify_timeout=args.verify_timeout,
        skip_verification=args.skip_verification,
        timeout=args.timeout,
    ))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
