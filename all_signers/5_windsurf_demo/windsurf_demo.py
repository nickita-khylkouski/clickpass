#!/usr/bin/env python3
"""Windsurf Demo: signup + verify + upgrade to Pro free trial with credit card.

Designed for hackathon live demo. Uses browser-use cloud browser so
demo organizers can watch via the live URL.

SPEED-OPTIMIZED: vision off, max actions per step cranked up,
prompts force single-step execution, minimal DOM/history.

Usage:
    uv run python windsurf_demo/windsurf_demo.py
    uv run python windsurf_demo/windsurf_demo.py --card-number 4242424242424242 --card-expiry 12/28 --card-cvc 123
    uv run python windsurf_demo/windsurf_demo.py --skip-upgrade   # signup only
    uv run python windsurf_demo/windsurf_demo.py --cancel-after    # upgrade then cancel
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from agentmail import AsyncAgentMail
from browser_use import Agent, Browser, ChatBrowserUse
from dotenv import load_dotenv
from faker import Faker
from pydantic import ConfigDict

# Reuse infrastructure from the main pipeline
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from signup_super import (
    AGENTMAIL_API_KEY,
    BROWSER_USE_API_KEY,
    RunResult,
    VerificationCandidate,
    _as_utc,
    _extract_code,
    _extract_links,
    _best_verification_link,
    _is_retryable_error,
    _needs_browser_rebuild,
    acquire_inbox,
    build_llm,
    create_cloud_browser,
    log,
    stop_oldest_active_cloud_session,
    try_http_verification,
    watch_for_verification,
)

load_dotenv()

fake = Faker()
_t0 = time.time()

# ─── Windsurf-specific constants ───────────────────────────────────────────

WINDSURF_SIGNUP_URL = "https://windsurf.com/editor/signup"
WINDSURF_SIGNIN_URL = "https://windsurf.com/editor/signin"
WINDSURF_PRICING_URL = "https://windsurf.com/pricing"
WINDSURF_ACCOUNT_URL = "https://windsurf.com/account"


async def create_local_browser() -> Browser:
    """Create a LOCAL browser on user's machine (residential IP — Stripe works)."""
    return Browser(
        use_cloud=False,
        keep_alive=True,
        minimum_wait_page_load_time=0.5,
        wait_between_actions=0.3,
        highlight_elements=True,  # Nice for demo viewing
        captcha_solver=True,
        headless=False,  # Visible window for demo
    )


# ─── Agent task runner (SPEED OPTIMIZED) ──────────────────────────────────

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
    import inspect

    def build_agent(task_text: str) -> Agent:
        kwargs: dict[str, Any] = {
            "task": task_text,
            "llm": llm,
            "browser": browser,
            "use_vision": False,          # OFF = skip screenshot capture/processing per step
            "captcha_solver": True,
            "max_actions_per_step": 25,    # Crank up — let agent batch everything in 1 LLM call
            "extend_system_message": (
                "SPEED IS CRITICAL. Execute ALL actions in a SINGLE step. "
                "Fill ALL form fields and click submit in ONE step — never split across steps. "
                "Never wait, never explore, never use write_file/replace_file/read_file/todo. "
                "Go as fast as possible."
            ),
            "use_judge": False,
            "include_attributes": ["id", "name", "type", "placeholder", "value"],
            "max_history_items": 6,        # Minimal history = faster LLM response
            "max_clickable_elements_length": 8000,  # Less DOM = faster LLM response
            "message_compaction": False,
            "loop_detection_enabled": False,
            "llm_timeout": 20,             # Tighter timeout per LLM call
            "step_timeout": 120,
        }
        provider = str(getattr(llm, "provider", "")).lower()
        if provider == "browser-use":
            kwargs["flash_mode"] = True

        allowed = set(inspect.signature(Agent.__init__).parameters.keys())
        filtered = {k: v for k, v in kwargs.items() if k in allowed}
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
                log(label, f"Output: {output[:300]}")
            if last_error:
                log(label, f"Error: {last_error[:200]}")
            if not success and attempt < retries and last_error and _is_retryable_error(Exception(last_error)):
                await asyncio.sleep(2 ** attempt)
                continue
            return RunResult(output=output, success=success, steps=steps, error=last_error)
        except Exception as exc:
            last_exc = exc
            if attempt < retries and _is_retryable_error(exc):
                log(label, f"Retryable error: {exc}")
                await asyncio.sleep(2 ** attempt)
                continue
            log(label, f"Failed: {exc}")
            return RunResult(output=None, success=False, steps=0, error=str(exc))
    raise RuntimeError(f"Agent failed: {last_exc}")


# ─── Phase tasks (SPEED: force single-step execution) ────────────────────

def build_windsurf_signup_task(email: str, first_name: str, last_name: str, password: str) -> str:
    return f"""Navigate to {WINDSURF_SIGNUP_URL} and complete signup in minimum steps.

IN YOUR FIRST STEP after page loads, do ALL of these actions at once:
1. Type "{first_name}" into the "First name" input
2. Type "{last_name}" into the "Last name" input
3. Type "{email}" into the "Email" input
4. Click the terms of service checkbox
5. Click the "Continue" button

IN YOUR SECOND STEP (password page will show), do ALL at once:
1. Type "{password}" into the "Create password" field
2. Type "{password}" into the "Confirm password" field
3. Press Enter key to submit (do NOT click any buttons)

AFTER THAT: If you see "check your email" or verification code input, output STATUS: NEEDS_VERIFICATION. If CAPTCHA appears, wait for it to be solved then click Continue. Then output STATUS: NEEDS_VERIFICATION.

NEVER click "Other Sign up options". NEVER click Google/GitHub signup.

Output: STATUS: NEEDS_VERIFICATION or SIGNUP_SUCCESS or SIGNUP_FAILED
DETAILS: <what you see>""".strip()


def build_windsurf_verify_task(code: str) -> str:
    digits = list(code)
    return f"""The page has 6 OTP input fields for a verification code.

IN ONE SINGLE STEP, do all of these:
1. Type "{digits[0]}" in the 1st input field
2. Type "{digits[1]}" in the 2nd input field
3. Type "{digits[2]}" in the 3rd input field
4. Type "{digits[3]}" in the 4th input field
5. Type "{digits[4]}" in the 5th input field
6. Type "{digits[5]}" in the 6th input field
7. Press Enter to submit

IMPORTANT: Do NOT click any buttons. After typing the last digit, just press Enter.
NEVER click "Back to Sign up".

Output: VERIFIED: YES or NO
DETAILS: <what happened>""".strip()


def build_windsurf_upgrade_task(card_number: str, card_expiry: str, card_cvc: str, card_name: str) -> str:
    return f"""Navigate to {WINDSURF_PRICING_URL}

STEP 1: Click the "Start Free Trial" button under the Pro plan.
STEP 2: A CAPTCHA modal appears. Wait for it to be solved (auto), then click "Continue".
STEP 3: Wait for Stripe checkout to load. If "Something went wrong", go back to {WINDSURF_PRICING_URL} and retry.
STEP 4: Fill ALL card and billing fields in ONE step:
   - Card number: {card_number}
   - Expiry: {card_expiry}
   - CVC: {card_cvc}
   - Name on card: {card_name}
   - Country: United States
   - Address: 1712 Brewster Avenue
   - City: Redwood City
   - State: California
   - ZIP: 94062
   - Phone: 6502766006
STEP 5: Click Subscribe/Start trial button.

TIPS: Card fields may be in an iframe. Use Tab between fields. Click field first if typing doesn't work.
Fill address BEFORE clicking Start trial — Stripe requires it.

Output: UPGRADE: SUCCESS or FAILED
PLAN: <plan name>
DETAILS: <what happened>""".strip()


def build_windsurf_cancel_task() -> str:
    return f"""Navigate to {WINDSURF_ACCOUNT_URL}. Find and click "Cancel subscription"/"Cancel trial"/"Downgrade". Confirm if prompted.

Output: CANCELLED: YES or NO
DETAILS: <what happened>""".strip()


# ─── Main pipeline ─────────────────────────────────────────────────────────

async def run_windsurf_demo(args: argparse.Namespace) -> int:
    global _t0
    _t0 = time.time()

    if not AGENTMAIL_API_KEY:
        raise RuntimeError("Missing AGENTMAIL_API_KEY in .env")
    if not args.local and not BROWSER_USE_API_KEY:
        raise RuntimeError("Missing BROWSER_USE_API_KEY in .env")

    llm, resolved_model = build_llm(args.llm)
    log("model", f"{args.llm} -> {resolved_model}")

    mail = AsyncAgentMail(api_key=AGENTMAIL_API_KEY, timeout=20)

    # ── Setup: inbox + browser in parallel ──
    use_local = args.local
    if use_local:
        log("setup", "Creating inbox + LOCAL browser (residential IP)...")
        inbox_coro = acquire_inbox(mail, reuse_inbox=False)
        (inbox_id, created) = await inbox_coro
        browser = await create_local_browser()
    else:
        log("setup", "Creating inbox + cloud browser in parallel...")
        inbox_coro = acquire_inbox(mail, reuse_inbox=False)
        browser_coro = create_cloud_browser(args.proxy_country)
        (inbox_id, created), browser = await asyncio.gather(inbox_coro, browser_coro)
    log("setup", f"Inbox: {inbox_id}")

    first_name = "Nickita"
    last_name = "Khylkouski"
    password = "Ws_Demo_2026!xK9"
    log("creds", f"Name: {first_name} {last_name}")
    log("creds", f"Email: {inbox_id}")
    log("creds", f"Password: {password}")

    run_started_at = datetime.now(timezone.utc)

    # ── Phase 1: Signup ──
    print("\n" + "=" * 60, flush=True)
    print("  PHASE 1: SIGNUP", flush=True)
    print("=" * 60, flush=True)

    # Start email watcher in background
    verify_watcher = asyncio.create_task(
        watch_for_verification(
            mail=mail,
            inbox_id=inbox_id,
            target_url="https://windsurf.com",
            timeout_s=args.signup_timeout + args.verify_timeout,
            min_received_at=run_started_at,
        )
    )

    signup_task = build_windsurf_signup_task(inbox_id, first_name, last_name, password)
    signup = await run_agent_task(
        browser=browser,
        llm=llm,
        label="signup",
        task=signup_task,
        max_steps=6,
        timeout_s=args.signup_timeout,
    )

    if not signup.success:
        log("signup", "Browser issue, rebuilding...")
        with contextlib.suppress(Exception):
            await browser.stop()
        browser = await create_cloud_browser(args.proxy_country)
        signup = await run_agent_task(
            browser=browser,
            llm=llm,
            label="signup",
            task=signup_task,
            max_steps=6,
            timeout_s=args.signup_timeout,
            retries=1,
        )

    signup_text = (signup.output or "").upper()
    if "SIGNUP_FAILED" in signup_text and signup.success:
        log("signup", "Signup explicitly failed.")
        await _cleanup(browser, mail, inbox_id, created, verify_watcher)
        return 1

    log("signup", "Signup submitted. Moving to verification.")

    # ── Phase 2: Email Verification ──
    print("\n" + "=" * 60, flush=True)
    print("  PHASE 2: EMAIL VERIFICATION", flush=True)
    print("=" * 60, flush=True)

    verified = False
    try:
        candidate = await asyncio.wait_for(verify_watcher, timeout=args.verify_timeout)
    except asyncio.TimeoutError:
        candidate = VerificationCandidate(link=None, code=None, subject=None)
        if not verify_watcher.done():
            verify_watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await verify_watcher

    if not candidate.link and not candidate.code:
        log("verify", "No candidate yet, doing fresh poll...")
        candidate = await watch_for_verification(
            mail=mail,
            inbox_id=inbox_id,
            target_url="https://windsurf.com",
            timeout_s=args.verify_timeout,
            min_received_at=run_started_at,
        )

    if candidate.code:
        log("verify", f"Got verification code: {candidate.code}")
        verify_result = await run_agent_task(
            browser=browser,
            llm=llm,
            label="verify",
            task=build_windsurf_verify_task(candidate.code),
            max_steps=3,
            timeout_s=60,
        )
        verified = "YES" in (verify_result.output or "").upper()
    elif candidate.link:
        log("verify", f"Got verification link, trying HTTP...")
        ok, final_url = await try_http_verification(candidate.link)
        if ok:
            verified = True
            log("verify", f"Verified via HTTP: {final_url}")
        else:
            verify_result = await run_agent_task(
                browser=browser,
                llm=llm,
                label="verify",
                task=f"Open this link and complete verification: {candidate.link}\nOutput: VERIFIED: YES or NO",
                max_steps=3,
                timeout_s=60,
            )
            verified = "YES" in (verify_result.output or "").upper()
    else:
        log("verify", "No verification email found.")

    log("verify", f"Verified: {verified}")

    # ── Phase 3: Upgrade to Pro Free Trial ──
    if args.skip_upgrade:
        log("upgrade", "Skipped (--skip-upgrade)")
    else:
        print("\n" + "=" * 60, flush=True)
        print("  PHASE 3: UPGRADE TO PRO (FREE TRIAL)", flush=True)
        print("=" * 60, flush=True)

        if not args.card_number:
            log("upgrade", "No card provided. Skipping upgrade.")
        else:
            # If we used cloud browser for signup, switch to local for Stripe
            # (Stripe blocks datacenter IPs)
            upgrade_browser = browser
            if not use_local:
                log("upgrade", "Switching to LOCAL browser for Stripe (residential IP)...")
                upgrade_browser = await create_local_browser()
                # Log into the account on the local browser first
                login_task = f"""Navigate to {WINDSURF_SIGNIN_URL}
Sign in with:
- Email: {inbox_id}
- Password: {password}
Press Enter or click Sign In after filling both fields.
If CAPTCHA appears, wait for it then click Continue.
When you see a dashboard or plan page, STOP.
Output: LOGGED_IN: YES or NO"""
                login_result = await run_agent_task(
                    browser=upgrade_browser,
                    llm=llm,
                    label="login",
                    task=login_task,
                    max_steps=6,
                    timeout_s=90,
                )
                if "YES" not in (login_result.output or "").upper():
                    log("upgrade", "Failed to log into local browser. Skipping upgrade.")
                    with contextlib.suppress(Exception):
                        await upgrade_browser.stop()
                    upgrade_browser = None

            if upgrade_browser:
                upgrade_result = await run_agent_task(
                    browser=upgrade_browser,
                    llm=llm,
                    label="upgrade",
                    task=build_windsurf_upgrade_task(
                        args.card_number,
                        args.card_expiry,
                        args.card_cvc,
                        os.getenv("WINDSURF_CARD_NAME", f"{first_name} {last_name}"),
                    ),
                    max_steps=20,
                    timeout_s=args.upgrade_timeout,
                )
                upgrade_text = (upgrade_result.output or "").upper()
                upgrade_ok = "SUCCESS" in upgrade_text
                log("upgrade", f"Upgrade: {'SUCCESS' if upgrade_ok else 'FAILED'}")

                if args.cancel_after and upgrade_ok:
                    print("\n" + "=" * 60, flush=True)
                    print("  PHASE 4: CANCEL TRIAL", flush=True)
                    print("=" * 60, flush=True)

                    cancel_result = await run_agent_task(
                        browser=upgrade_browser,
                        llm=llm,
                        label="cancel",
                        task=build_windsurf_cancel_task(),
                        max_steps=8,
                        timeout_s=120,
                    )
                    log("cancel", f"Result: {(cancel_result.output or '')[:200]}")

                # Clean up local upgrade browser if different from main
                if upgrade_browser is not browser:
                    with contextlib.suppress(Exception):
                        await upgrade_browser.stop()

    # ── Final Report ──
    elapsed = time.time() - _t0
    print("\n" + "=" * 60, flush=True)
    print(f"  WINDSURF DEMO RESULTS ({elapsed:.1f}s)", flush=True)
    print("=" * 60, flush=True)
    print(f"EMAIL:       {inbox_id}", flush=True)
    print(f"PASSWORD:    {password}", flush=True)
    print(f"NAME:        {first_name} {last_name}", flush=True)
    print(f"SIGNUP:      {'OK' if signup.success else 'FAILED'}", flush=True)
    print(f"VERIFIED:    {'YES' if verified else 'NO'}", flush=True)
    print(f"UPGRADE:     {'Done' if not args.skip_upgrade and args.card_number else 'SKIPPED'}", flush=True)
    print("=" * 60, flush=True)

    if args.keep_alive:
        log("demo", "Browser kept alive for viewing. Press Ctrl+C to exit.")
        try:
            await asyncio.sleep(args.keep_alive)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    await _cleanup(browser, mail, inbox_id, created, None)
    return 0


async def _cleanup(
    browser: Browser,
    mail: AsyncAgentMail,
    inbox_id: str,
    created: bool,
    watcher: asyncio.Task | None,
) -> None:
    if watcher and not watcher.done():
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await watcher
    with contextlib.suppress(Exception):
        await browser.stop()
    if created:
        with contextlib.suppress(Exception):
            await mail.inboxes.delete(inbox_id)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Windsurf Demo: signup + upgrade to Pro trial")
    p.add_argument("--llm", default="browseruse-fast", help="Model preset (default: browseruse-fast)")
    p.add_argument("--signup-timeout", type=int, default=120)
    p.add_argument("--verify-timeout", type=int, default=45)
    p.add_argument("--upgrade-timeout", type=int, default=180)
    p.add_argument("--proxy-country", default="us")

    p.add_argument("--card-number", default=os.getenv("WINDSURF_CARD_NUMBER", ""), help="Credit card number")
    p.add_argument("--card-expiry", default=os.getenv("WINDSURF_CARD_EXPIRY", "12/28"), help="Card expiry MM/YY")
    p.add_argument("--card-cvc", default=os.getenv("WINDSURF_CARD_CVC", "123"), help="Card CVC")

    p.add_argument("--local", action="store_true", help="Use LOCAL browser (residential IP, Stripe works)")
    p.add_argument("--skip-upgrade", action="store_true", help="Only do signup+verify")
    p.add_argument("--cancel-after", action="store_true", help="Cancel trial after upgrading")
    p.add_argument("--keep-alive", type=int, default=0, help="Seconds to keep browser alive")

    return p.parse_args()


def main() -> None:
    args = parse_args()
    code = asyncio.run(run_windsurf_demo(args))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
