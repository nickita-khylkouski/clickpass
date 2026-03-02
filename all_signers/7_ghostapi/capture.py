from __future__ import annotations

from pathlib import Path

from .auth import maybe_fill_otp_with_adapters


class CaptureError(RuntimeError):
    pass


async def capture_har_with_playwright(
    target_url: str,
    out_har: str,
    *,
    capture_seconds: int = 25,
    headed: bool = False,
    email: str | None = None,
    password: str | None = None,
    phone: str | None = None,
    totp_secret: str | None = None,
) -> str:
    """Capture HAR traffic from deterministic browsing steps."""
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # pragma: no cover - environment dependent
        raise CaptureError(
            "Playwright is required. Install with 'pip install playwright' and run 'playwright install chromium'."
        ) from exc

    Path(out_har).parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headed)
        context = await browser.new_context(
            record_har_path=out_har,
            record_har_mode="full",
            record_har_content="embed",
        )
        page = await context.new_page()

        await page.goto(target_url, wait_until="domcontentloaded", timeout=45_000)

        if email and password:
            await _try_login(
                page,
                email=email,
                password=password,
                phone=phone,
                totp_secret=totp_secret,
            )

        await _basic_explore(page)

        if capture_seconds > 0:
            await page.wait_for_timeout(capture_seconds * 1000)

        await context.close()
        await browser.close()

    return out_har


# Backward-compatible wrapper.
async def capture_har(
    url: str,
    har_path: str,
    *,
    task: str | None = None,
    email: str | None = None,
    password: str | None = None,
    timeout_seconds: int = 45,
    headless: bool = True,
) -> str:
    _ = (task, timeout_seconds)
    return await capture_har_with_playwright(
        target_url=url,
        out_har=har_path,
        headed=not headless,
        email=email,
        password=password,
    )


async def _try_login(
    page,
    *,
    email: str,
    password: str,
    phone: str | None,
    totp_secret: str | None,
) -> None:
    email_selectors = [
        'input[type="email"]',
        'input[name="email"]',
        'input[name="username"]',
        'input[id*="email"]',
    ]
    password_selectors = [
        'input[type="password"]',
        'input[name="password"]',
        'input[id*="password"]',
    ]
    submit_selectors = [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Log in")',
        'button:has-text("Sign in")',
    ]

    for selector in email_selectors:
        element = page.locator(selector).first
        if await element.count():
            await element.fill(email)
            break

    for selector in password_selectors:
        element = page.locator(selector).first
        if await element.count():
            await element.fill(password)
            break

    for selector in submit_selectors:
        element = page.locator(selector).first
        if await element.count():
            await element.click(timeout=3000)
            break

    await maybe_fill_otp_with_adapters(
        page,
        expected_phone=phone,
        expected_email=email,
        totp_secret=totp_secret,
        timeout_seconds=30,
    )

    await page.wait_for_timeout(1200)


async def _basic_explore(page) -> None:
    await page.mouse.wheel(0, 1000)
    await page.wait_for_timeout(600)
    await page.mouse.wheel(0, 1000)
    await page.wait_for_timeout(600)

    links = page.locator("a[href]")
    count = min(await links.count(), 4)
    for idx in range(count):
        link = links.nth(idx)
        href = await link.get_attribute("href")
        if not href or href.startswith("mailto:") or href.startswith("tel:"):
            continue
        try:
            await link.click(timeout=2000)
            await page.wait_for_timeout(900)
            await page.go_back(timeout=5000)
            await page.wait_for_timeout(600)
        except Exception:
            continue
