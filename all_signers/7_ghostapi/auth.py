from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import time

OTP_RE = re.compile(r"\b(\d{4,8})\b")


def extract_otp(text: str) -> str | None:
    m = OTP_RE.search(text or "")
    return m.group(1) if m else None


def latest_imessage_code(expected_phone: str, lookback_seconds: int = 180) -> str | None:
    """Read most recent OTP-like code from local iMessage DB (macOS only)."""
    db_path = os.path.expanduser("~/Library/Messages/chat.db")
    if not os.path.exists(db_path):
        return None

    conn = sqlite3.connect(db_path)
    try:
        since_apple = int((time.time() - lookback_seconds - 978307200) * 1_000_000_000)
        row = conn.execute(
            """
            SELECT m.text
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.is_from_me = 0
              AND m.date > ?
              AND (h.id LIKE ? OR h.id LIKE ?)
            ORDER BY m.date DESC
            LIMIT 1
            """,
            (since_apple, f"%{expected_phone}%", "%@%"),
        ).fetchone()
        if not row or not row[0]:
            return None
        return extract_otp(str(row[0]))
    finally:
        conn.close()


async def maybe_fill_otp_from_imessage(page, expected_phone: str, timeout_seconds: int = 30) -> bool:
    selectors = [
        'input[name*="otp"]',
        'input[name*="code"]',
        'input[id*="otp"]',
        'input[id*="code"]',
        'input[autocomplete="one-time-code"]',
    ]
    start = time.time()
    while time.time() - start < timeout_seconds:
        otp_input = None
        for selector in selectors:
            candidate = page.locator(selector).first
            if await candidate.count():
                otp_input = candidate
                break
        if otp_input:
            code = latest_imessage_code(expected_phone=expected_phone)
            if code:
                await otp_input.fill(code)
                await page.keyboard.press("Enter")
                return True
        await asyncio.sleep(1.5)
    return False


async def maybe_fill_otp_with_adapters(
    page,
    *,
    expected_phone: str | None,
    expected_email: str | None,
    totp_secret: str | None = None,
    timeout_seconds: int = 30,
) -> bool:
    selectors = [
        'input[name*="otp"]',
        'input[name*="code"]',
        'input[id*="otp"]',
        'input[id*="code"]',
        'input[autocomplete="one-time-code"]',
    ]
    start = time.time()
    while time.time() - start < timeout_seconds:
        otp_input = None
        for selector in selectors:
            candidate = page.locator(selector).first
            if await candidate.count():
                otp_input = candidate
                break
        if otp_input:
            # Import lazily to avoid circular dependency at module import time.
            from .auth_adapters import resolve_otp_with_adapters

            result = await resolve_otp_with_adapters(
                phone=expected_phone,
                email=expected_email,
                totp_secret=totp_secret,
                timeout_seconds=8,
            )
            if result and result.code:
                await otp_input.fill(result.code)
                await page.keyboard.press("Enter")
                return True
        await asyncio.sleep(1.2)
    return False
