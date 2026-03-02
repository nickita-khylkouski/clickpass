"""Browser Use Cloud Agent wrapper for AI-driven signup with HAR capture."""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("ghostapi.signup")


def _root_domain(hostname: str) -> str:
    """Extract root domain from hostname (e.g. www.minimax.io -> minimax.io)."""
    parts = hostname.rsplit(".", 2)
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return hostname


@dataclass
class SignupResult:
    """Outcome of an AI-driven signup attempt."""

    success: bool
    har_path: str
    credentials_used: dict
    error: str | None
    agent_history: list[str]


def _build_signup_task(
    target_url: str,
    email_placeholder: str,
    password_placeholder: str,
) -> str:
    """Build the natural-language task prompt for the signup agent."""
    return (
        f"Navigate to {target_url} and complete a free account signup.\n"
        "\n"
        "Steps:\n"
        "1. FIRST check the page header/navigation bar for 'Sign Up', 'Get Started', "
        "'Start Free Trial', or 'Create Account' buttons. These are usually in the "
        "top-right corner of the page. If you don't see one, use the extract tool "
        "to find signup links on the page.\n"
        "   IMPORTANT: Click Sign Up, NOT Sign In/Login. These are different actions.\n"
        "2. If asked to choose a plan, pick the Free Trial or cheapest option "
        "(even if it requires a credit card — we WANT to enter card info).\n"
        f"3. Fill in the signup form with these values:\n"
        f"   - Email: {email_placeholder}\n"
        f"   - Password: {password_placeholder}\n"
        "   - First name: x_first_name\n"
        "   - Last name: x_last_name\n"
        "   - Phone: x_phone\n"
        "\n"
        "4. CREDIT CARD / PAYMENT FIELDS — this is critical:\n"
        "   If you see payment/billing fields, fill them in:\n"
        "   - Card number: x_card_number\n"
        "   - Expiration month (MM): x_card_exp_month\n"
        "   - Expiration year (YY or YYYY): x_card_exp_year\n"
        "   - If there's a COMBINED expiry field (MM/YY), type: x_card_exp_month/x_card_exp_year\n"
        "   - CVV / CVC / Security code: x_card_cvv\n"
        "   - Billing ZIP / Postal code: x_card_zip\n"
        "   - Cardholder name: x_first_name x_last_name\n"
        "   DO NOT skip payment fields. Fill ALL payment fields you see.\n"
        "   If using Stripe Elements (embedded iframe), click into each field and type the value.\n"
        "\n"
        "5. Check any 'I agree to the Terms' or 'Accept Terms' checkboxes.\n"
        "6. Submit the form by clicking the signup/create account/start trial button.\n"
        "7. If you land on an email verification page, wait 10 seconds.\n"
        "8. If you see an OTP / verification code input, wait 10 seconds.\n"
        "9. If you reach a dashboard, welcome page, or confirmation page, the signup is complete.\n"
    )


async def _create_and_run_agent(
    *,
    task: str,
    sensitive_data: dict,
    domain: str,
    har_path: str,
    headed: bool,
    max_steps: int,
) -> tuple[bool, list[str]]:
    """Create and run the Browser Use agent via Cloud API.

    Uses BROWSER_USE_API_KEY for cloud execution (CAPTCHA solving, anti-fingerprint,
    residential proxies all included). Falls back to local if no cloud key.

    Returns (success, history_list).
    """
    from browser_use import Agent, BrowserProfile

    browseruse_key = os.environ.get("BROWSER_USE_API_KEY", "")
    use_cloud = bool(browseruse_key)

    # LLM: prefer Anthropic, fallback OpenAI
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")

    if anthropic_key:
        from browser_use.llm.anthropic.chat import ChatAnthropic
        llm = ChatAnthropic(model="claude-sonnet-4-20250514", api_key=anthropic_key)
        log.info("LLM: Claude Sonnet (Anthropic)")
    elif openai_key:
        from browser_use.llm.openai.chat import ChatOpenAI
        llm = ChatOpenAI(model="gpt-4o", api_key=openai_key)
        log.info("LLM: GPT-4o (OpenAI)")
    else:
        raise RuntimeError("Set ANTHROPIC_API_KEY or OPENAI_API_KEY")

    root = _root_domain(domain)
    log.info("Domain: %s (root: %s)", domain, root)

    if use_cloud:
        log.info("Mode: Browser Use Cloud (CAPTCHA solving, anti-fingerprint, proxies)")
        browser_profile = BrowserProfile(
            use_cloud=True,
            record_har_path=har_path,
            record_har_content="embed",
            allowed_domains=[root, f"*.{root}"],
        )
    else:
        log.info("Mode: Local browser (headless=%s)", not headed)
        browser_profile = BrowserProfile(
            headless=not headed,
            record_har_path=har_path,
            record_har_content="embed",
            allowed_domains=[root, f"*.{root}"],
        )

    log.info("Starting agent (max_steps=%d)...", max_steps)
    t0 = time.time()

    agent = Agent(
        task=task,
        llm=llm,
        browser_profile=browser_profile,
        sensitive_data=sensitive_data,
        max_steps=max_steps,
    )

    result = await agent.run()
    elapsed = time.time() - t0
    log.info("Agent finished in %.1fs", elapsed)

    history: list[str] = []
    if hasattr(result, "history") and result.history:
        history = [str(entry) for entry in result.history]
    elif hasattr(result, "actions") and result.actions:
        history = [str(action) for action in result.actions]

    log.info("Agent history: %d entries", len(history))
    return True, history


async def run_signup_agent(
    target_url: str,
    *,
    har_path: str,
    email: str,
    password: str,
    first_name: str,
    last_name: str,
    phone: str,
    card_number: str,
    card_exp_month: str,
    card_exp_year: str,
    card_cvv: str,
    card_zip: str,
    headed: bool = True,
    max_steps: int = 50,
) -> SignupResult:
    """Run an AI agent to complete a signup flow and capture HAR traffic."""
    log.info("=== Signup Agent Start ===")
    log.info("Target: %s", target_url)
    log.info("Email: %s", email)
    log.info("Has password: %s", bool(password))
    log.info("Has card: %s", bool(card_number))
    log.info("Name: %s %s", first_name, last_name)

    credentials = {
        "email": email,
        "password": password,
        "first_name": first_name,
        "last_name": last_name,
        "phone": phone,
        "card_number": card_number,
        "card_exp_month": card_exp_month,
        "card_exp_year": card_exp_year,
        "card_cvv": card_cvv,
        "card_zip": card_zip,
    }

    Path(har_path).parent.mkdir(parents=True, exist_ok=True)

    try:
        import browser_use  # noqa: F401
    except (ImportError, ModuleNotFoundError):
        log.error("browser_use not installed")
        return SignupResult(
            success=False,
            har_path=har_path,
            credentials_used=credentials,
            error="browser_use is not installed. Install with: pip install browser-use",
            agent_history=[],
        )

    domain = urlsplit(target_url).hostname or ""

    sensitive_data = {
        "x_email": email,
        "x_password": password,
        "x_first_name": first_name,
        "x_last_name": last_name,
        "x_phone": phone,
        "x_card_number": card_number,
        "x_card_exp_month": card_exp_month,
        "x_card_exp_year": card_exp_year,
        "x_card_cvv": card_cvv,
        "x_card_zip": card_zip,
    }

    task = _build_signup_task(target_url, "x_email", "x_password")
    log.info("Task prompt built (%d chars)", len(task))

    try:
        success, history = await _create_and_run_agent(
            task=task,
            sensitive_data=sensitive_data,
            domain=domain,
            har_path=har_path,
            headed=headed,
            max_steps=max_steps,
        )
        log.info("=== Signup Agent Complete (success=%s) ===", success)
        return SignupResult(
            success=success,
            har_path=har_path,
            credentials_used=credentials,
            error=None,
            agent_history=history,
        )
    except Exception as exc:
        log.error("=== Signup Agent FAILED: %s ===", exc)
        return SignupResult(
            success=False,
            har_path=har_path,
            credentials_used=credentials,
            error=str(exc),
            agent_history=[],
        )
