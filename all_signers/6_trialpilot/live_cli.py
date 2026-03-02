"""Streamlined live CLI for Browser Use + AgentMail onboarding runs."""

from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import time
import webbrowser
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from .browseruse_agentmail_demo import (
    AGENTMAIL_BASE,
    DemoConfig,
    _create_browser_session,
    _domain_family,
    _extract_first_otp,
    _host_for_target,
    _parse_env_bool,
    _parse_optional_bool,
    _parse_optional_int,
    _parse_secrets_json,
    _parse_vision,
    _run_task_with_wait_and_recovery,
    run_demo,
)
from .browseruse_agentmail_demo import _json_request as _http_json

BROWSER_USE_BASE = "https://api.browser-use.com/api/v2"

SITE_PRESETS: dict[str, dict[str, str]] = {
    "skillsafe": {
        "target_url": "https://skillsafe.ai/",
        "api_key_hint": "Not required for this run. Focus on creating an account and reaching logged-in state.",
        "billing_hint": "Do not open checkout. Report current plan/trial state from account pages only.",
        "include_api_key_step": "false",
    },
    "windsurf": {
        "target_url": "https://windsurf.com/account/register",
        "api_key_hint": "Not applicable for this run. Focus on account creation and subscription/trial state.",
        "billing_hint": (
            "Read-only only: capture plan, visible credits, and usage/balance values. "
            "Do not start trials and do not enter payment details."
        ),
        "include_api_key_step": "false",
    },
    "minimax": {
        "target_url": "https://platform.minimax.io/login",
        "api_key_hint": "Create an API key in user settings and report key + URL.",
        "billing_hint": "Read-only only: capture plan and current credit/balance state; no payment actions.",
    },
    "cohere": {
        "target_url": "https://dashboard.cohere.com/welcome/register",
        "api_key_hint": "Create or reveal trial API key and report key + URL.",
        "billing_hint": "Read-only only: capture plan and credit balance; no checkout or subscription actions.",
    },
    "assemblyai": {
        "target_url": "https://www.assemblyai.com/dashboard/signup",
        "api_key_hint": "Navigate to API keys/settings and create key; report key + URL.",
        "billing_hint": "Read-only only: capture credits/usage and current plan; no payment actions.",
    },
    "daytona": {
        "target_url": "https://app.daytona.io/",
        "api_key_hint": "Create an API key in account settings and report key + URL.",
        "billing_hint": "Read-only only: capture plan and current balance/credits.",
    },
    "dedalus": {
        "target_url": "https://app.dedaluslabs.ai/",
        "api_key_hint": "Create an API key/token from developer settings and report key + URL.",
        "billing_hint": "Read-only only: capture plan and credit/balance state; do not open checkout.",
    },
    "apollo": {
        "target_url": "https://app.apollo.io/#/signup",
        "api_key_hint": "If API/developer key settings are available, create or reveal key and report key + URL; otherwise report key_not_available.",
        "billing_hint": "Read-only only: capture plan/trial and visible credits or usage counters.",
    },
    "moss": {
        "target_url": "https://portal.usemoss.dev/auth/sign-up",
        "api_key_hint": "Navigate to API keys and return the full key value + URL.",
        "billing_hint": "Read-only only: capture plan and visible credits/balance; do not open checkout.",
    },
}


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _load_local_env(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} missing")
    return value


def _resolve_browser_runtime_fields(args: argparse.Namespace | None = None) -> dict[str, Any]:
    def _arg(name: str, default: Any = None) -> Any:
        if args is None:
            return default
        return getattr(args, name, default)

    profile_id = (_arg("profile_id") or os.getenv("BROWSER_USE_PROFILE_ID", "")).strip() or None
    proxy_country_code = (
        _arg("proxy_country_code")
        or os.getenv("BROWSER_USE_CLOUD_PROXY_COUNTRY_CODE", "")
        or os.getenv("BROWSER_USE_PROXY_COUNTRY_CODE", "")
    ).strip() or None
    screen_width = _parse_optional_int(_arg("screen_width") or os.getenv("BROWSER_USE_SCREEN_WIDTH", ""))
    screen_height = _parse_optional_int(_arg("screen_height") or os.getenv("BROWSER_USE_SCREEN_HEIGHT", ""))
    highlight_elements = bool(_arg("highlight_elements", False)) or _parse_env_bool(
        "BROWSER_USE_HIGHLIGHT_ELEMENTS",
        default=False,
    )
    flash_mode = bool(_arg("flash_mode", False)) or _parse_env_bool("BROWSER_USE_FLASH_MODE", default=False)
    thinking_mode = bool(_arg("thinking", False)) or _parse_env_bool("BROWSER_USE_THINKING", default=False)
    vision_raw = _arg("vision")
    if vision_raw is None:
        vision_raw = os.getenv("BROWSER_USE_VISION", "true")
    vision_mode = _parse_vision(vision_raw)
    prompt_extension = _arg("system_prompt_extension")
    if prompt_extension is None:
        prompt_extension = os.getenv("BROWSER_USE_SYSTEM_PROMPT_EXTENSION", "")
    op_vault_id = (_arg("op_vault_id") or os.getenv("BROWSER_USE_OP_VAULT_ID", "")).strip() or None
    secrets_raw = _arg("secrets_json")
    if secrets_raw is None:
        secrets_raw = os.getenv("BROWSER_USE_SECRETS_JSON", "")
    secrets = _parse_secrets_json(secrets_raw) or {}
    payment_autofill_enabled = _parse_env_bool("TRIALPILOT_ENABLE_PAYMENT_AUTOFILL", default=False)
    if payment_autofill_enabled:
        card_secrets_env = {
            "CARD_NUMBER": os.getenv("TRIALPILOT_CARD_NUMBER", "").strip(),
            "CARD_EXP_MONTH": os.getenv("TRIALPILOT_CARD_EXP_MONTH", "").strip(),
            "CARD_EXP_YEAR": os.getenv("TRIALPILOT_CARD_EXP_YEAR", "").strip(),
            "CARD_CVC": os.getenv("TRIALPILOT_CARD_CVC", "").strip(),
            "CARDHOLDER_NAME": os.getenv("TRIALPILOT_CARDHOLDER_NAME", "").strip(),
            "CARD_ZIP": os.getenv("TRIALPILOT_CARD_ZIP", "").strip(),
        }
        for key, value in card_secrets_env.items():
            if value and key not in secrets:
                secrets[key] = value
    secrets = secrets or None
    keep_alive_raw = _arg("session_keep_alive")
    if keep_alive_raw is None:
        keep_alive_raw = os.getenv("BROWSER_USE_KEEP_ALIVE", "")
    persist_raw = _arg("session_persist_memory")
    if persist_raw is None:
        persist_raw = os.getenv("BROWSER_USE_PERSIST_MEMORY", "")

    return {
        "profile_id": profile_id,
        "proxy_country_code": proxy_country_code,
        "browser_screen_width": screen_width,
        "browser_screen_height": screen_height,
        "highlight_elements": highlight_elements,
        "flash_mode": flash_mode,
        "thinking_mode": thinking_mode,
        "vision_mode": vision_mode,
        "system_prompt_extension": str(prompt_extension or ""),
        "op_vault_id": op_vault_id,
        "secrets": secrets,
        "session_keep_alive": _parse_optional_bool(keep_alive_raw),
        "session_persist_memory": _parse_optional_bool(persist_raw),
    }


def _list_inboxes(agentmail_api_key: str, limit: int) -> dict[str, Any]:
    data = _http_json(
        method="GET",
        url=f"{AGENTMAIL_BASE}/inboxes?limit={int(limit)}",
        headers={"Authorization": f"Bearer {agentmail_api_key}"},
    )
    inboxes = []
    for item in data.get("inboxes", []) or []:
        inboxes.append(
            {
                "inbox_id": item.get("inbox_id"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
            }
        )
    return {"ok": True, "count": len(inboxes), "inboxes": inboxes}


def _pick_random_existing_inbox_id(agentmail_api_key: str, *, limit: int = 20) -> str | None:
    payload = _list_inboxes(agentmail_api_key, max(1, int(limit)))
    inboxes = [
        str((row or {}).get("inbox_id", "")).strip()
        for row in (payload.get("inboxes", []) or [])
    ]
    inboxes = [item for item in inboxes if item]
    if not inboxes:
        return None
    return random.choice(inboxes)


def _latest_otp(agentmail_api_key: str, inbox_id: str, otp_regex: str) -> dict[str, Any]:
    list_data = _http_json(
        method="GET",
        url=f"{AGENTMAIL_BASE}/inboxes/{quote(inbox_id, safe='')}/messages?limit=5",
        headers={"Authorization": f"Bearer {agentmail_api_key}"},
    )
    for item in list_data.get("messages", []) or []:
        message_id = str(item.get("message_id", "")).strip()
        if not message_id:
            continue
        detail = _http_json(
            method="GET",
            url=(
                f"{AGENTMAIL_BASE}/inboxes/{quote(inbox_id, safe='')}"
                f"/messages/{quote(message_id, safe='')}"
            ),
            headers={"Authorization": f"Bearer {agentmail_api_key}"},
        )
        blob = "\n".join(
            [
                str(detail.get("subject", "") or ""),
                str(detail.get("preview", "") or ""),
                str(detail.get("text", "") or ""),
            ]
        )
        otp = _extract_first_otp(blob, otp_regex)
        if otp:
            return {
                "ok": True,
                "inbox_id": inbox_id,
                "otp": otp,
                "message_id": message_id,
                "subject": detail.get("subject"),
                "timestamp": detail.get("timestamp"),
            }
    return {"ok": False, "inbox_id": inbox_id, "error": "otp_not_found"}


def _extract_links(blob: str) -> list[str]:
    links = re.findall(r"https?://[^\s\"'<>]+", blob)
    cleaned: list[str] = []
    for link in links:
        value = html.unescape(str(link or "").strip())
        value = value.rstrip(").,;")
        if value:
            cleaned.append(value)
    return cleaned


def _looks_non_verification_link(url: str) -> bool:
    parsed = urlparse(str(url or "").strip().lower())
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if not host:
        return True
    if host in {"img.clerk.com", "images.clerk.dev", "images.clerk.com"}:
        return True
    if path.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
        return True
    if "x-oss-process=image" in parsed.query or "image/format" in parsed.query:
        return True
    if "unsubscribe" in path:
        return True
    return False


def _resolve_redirect_link(url: str) -> str:
    """Resolve tracking URLs to final destination when possible."""
    lower = url.lower()
    # Never pre-open one-time auth links; fetching can consume the token.
    if (
        "stytch.com/v1/magic_links/redirect" in lower
        or "token=" in lower
        or "magic_link" in lower
        or "magic-links" in lower
    ):
        return url
    try:
        req = Request(
            url,
            method="GET",
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
            },
        )
        with urlopen(req, timeout=20) as resp:  # nosec B310 - explicit user-provided run context
            return resp.geturl() or url
    except Exception:
        return url


def _latest_verification_link(
    agentmail_api_key: str,
    inbox_id: str,
    *,
    provider_hint: str | None = None,
    not_before: datetime | None = None,
) -> dict[str, Any]:
    list_data = _http_json(
        method="GET",
        url=f"{AGENTMAIL_BASE}/inboxes/{quote(inbox_id, safe='')}/messages?limit=12",
        headers={"Authorization": f"Bearer {agentmail_api_key}"},
    )
    hint = (provider_hint or "").strip().lower()
    hint_host = hint if "." in hint and " " not in hint else ""
    hint_token = ""
    if hint_host:
        parts = [p for p in hint_host.split(".") if p and p != "www"]
        if len(parts) >= 2:
            hint_token = parts[-2]
        elif parts:
            hint_token = parts[0]
    elif hint:
        hint_token = re.sub(r"[^a-z0-9]+", "", hint)
    best: tuple[int, dict[str, Any]] | None = None

    for item in list_data.get("messages", []) or []:
        message_id = str(item.get("message_id", "")).strip()
        if not message_id:
            continue
        detail = _http_json(
            method="GET",
            url=(
                f"{AGENTMAIL_BASE}/inboxes/{quote(inbox_id, safe='')}"
                f"/messages/{quote(message_id, safe='')}"
            ),
            headers={"Authorization": f"Bearer {agentmail_api_key}"},
        )
        subject = str(detail.get("subject", "") or "")
        subject_low = subject.lower()
        preview = str(detail.get("preview", "") or "")
        text_body = str(detail.get("text", "") or "")
        html_body = str(detail.get("html", "") or "")
        sender = str(detail.get("from", "") or "")
        sent_at = _parse_iso(detail.get("timestamp"))
        if not_before is not None and sent_at is not None and sent_at < not_before:
            continue
        text_blob = "\n".join([subject, preview, text_body])
        blob = "\n".join(
            [
                subject,
                preview,
                text_body,
                html_body,
            ]
        )
        links = _extract_links(blob)
        if not links:
            continue
        for raw_link in links:
            resolved = _resolve_redirect_link(raw_link)
            if _looks_non_verification_link(resolved):
                continue
            parsed = urlparse(resolved)
            host = (parsed.hostname or "").lower()
            if not host:
                continue
            host_compact = re.sub(r"[^a-z0-9]+", "", host)
            text_compact = re.sub(r"[^a-z0-9]+", "", text_blob.lower())
            sender_compact = re.sub(r"[^a-z0-9]+", "", sender.lower())
            provider_relevant = False
            if hint_host and (
                host == hint_host
                or host.endswith(f".{hint_host}")
                or hint_host.endswith(f".{host}")
            ):
                provider_relevant = True
            if hint_token and (
                hint_token in host_compact
                or hint_token in text_compact
                or hint_token in sender_compact
            ):
                provider_relevant = True
            if hint and not provider_relevant:
                continue
            score = 0
            lower_blob = text_blob.lower()
            if "verify" in lower_blob or "confirm" in lower_blob:
                score += 3
            if "verify" in subject_low or "confirm" in subject_low:
                score += 4
            if "login request" in subject_low:
                score += 4
            if "password reset" in subject_low or "reset your" in subject_low:
                score -= 10
            if "magic" in lower_blob or "check your email" in lower_blob:
                score += 2
            if provider_relevant:
                score += 6
            if any(token in resolved.lower() for token in ("verify", "confirm", "token", "magic")):
                score += 2
            if "stytch_token_type=discovery" in resolved:
                score += 3
            if "passwords_discovery" in resolved:
                score -= 6
            if "unsubscribe" in resolved.lower():
                score -= 5
            candidate = {
                "inbox_id": inbox_id,
                "message_id": message_id,
                "subject": subject,
                "timestamp": detail.get("timestamp"),
                "raw_link": raw_link,
                "verification_link": resolved,
            }
            if best is None or score > best[0]:
                best = (score, candidate)

    if best is None:
        return {"ok": False, "inbox_id": inbox_id, "error": "verification_link_not_found"}
    payload = dict(best[1])
    payload["ok"] = True
    return payload


def _build_common_cfg(args: argparse.Namespace) -> DemoConfig:
    browser_runtime = _resolve_browser_runtime_fields(args)
    arg_inbox = getattr(args, "inbox", None)
    arg_inbox_username = getattr(args, "inbox_username", None)
    env_inbox = os.getenv("AGENTMAIL_INBOX_ID", "").strip() or None
    has_explicit_inbox_username = bool(str(arg_inbox_username or "").strip())
    resolved_inbox = arg_inbox or (None if has_explicit_inbox_username else env_inbox)
    return DemoConfig(
        target_url=args.target_url,
        password=getattr(args, "password", ""),
        first_name=getattr(args, "first_name", "Demo"),
        last_name=getattr(args, "last_name", "User"),
        phone=getattr(args, "phone", "+14155550123"),
        llm=getattr(args, "llm", "browser-use-2.0"),
        max_steps=int(getattr(args, "max_steps", 80)),
        timeout_seconds=int(getattr(args, "timeout_seconds", 600)),
        poll_seconds=int(getattr(args, "poll_seconds", 5)),
        otp_wait_seconds=int(getattr(args, "otp_wait_seconds", 300)),
        otp_regex=getattr(args, "otp_regex", r"\b(\d{6})\b"),
        inbox=resolved_inbox,
        inbox_username=arg_inbox_username,
        inbox_domain=getattr(args, "inbox_domain", "agentmail.to"),
        session_id=getattr(args, "session_id", None),
        dry_run=bool(getattr(args, "dry_run", False)),
        browser_use_api_key=os.getenv("BROWSER_USE_API_KEY", "").strip() or None,
        agentmail_api_key=os.getenv("AGENTMAIL_API_KEY", "").strip() or None,
        include_api_key_step=bool(getattr(args, "include_api_key_step", False)),
        manual_otp=(
            getattr(args, "manual_otp", "").strip()
            if isinstance(getattr(args, "manual_otp", None), str)
            else None
        )
        or None,
        pause_for_billing=bool(getattr(args, "pause_for_billing", False)),
        resume_only=bool(getattr(args, "resume_only", False)),
        verbose=bool(getattr(args, "verbose", False)),
        strict_fresh_inbox=bool(getattr(args, "strict_fresh_inbox", False)),
        **browser_runtime,
    )


def _browser_use_headers(api_key: str) -> dict[str, str]:
    return {"X-Browser-Use-API-Key": api_key}


def _list_profiles(browser_use_api_key: str, limit: int) -> dict[str, Any]:
    page_size = max(1, min(int(limit), 100))
    data = _http_json(
        method="GET",
        url=f"{BROWSER_USE_BASE}/profiles?pageSize={page_size}&pageNumber=1",
        headers=_browser_use_headers(browser_use_api_key),
    )
    items = data.get("items", []) or []
    profiles = []
    for item in items:
        profiles.append(
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "cookie_domains": item.get("cookieDomains"),
                "last_used_at": item.get("lastUsedAt"),
                "created_at": item.get("createdAt"),
            }
        )
    return {"ok": True, "count": len(profiles), "profiles": profiles}


def _create_profile(browser_use_api_key: str, name: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if name and str(name).strip():
        payload["name"] = str(name).strip()
    data = _http_json(
        method="POST",
        url=f"{BROWSER_USE_BASE}/profiles",
        headers=_browser_use_headers(browser_use_api_key),
        payload=payload if payload else None,
    )
    return {
        "ok": True,
        "id": data.get("id"),
        "name": data.get("name"),
        "cookie_domains": data.get("cookieDomains"),
        "created_at": data.get("createdAt"),
    }


def _get_session(browser_use_api_key: str, session_id: str) -> dict[str, Any]:
    data = _http_json(
        method="GET",
        url=f"{BROWSER_USE_BASE}/sessions/{quote(str(session_id).strip(), safe='')}",
        headers=_browser_use_headers(browser_use_api_key),
    )
    return {
        "ok": True,
        "id": data.get("id"),
        "status": data.get("status"),
        "live_url": data.get("liveUrl"),
        "public_share_url": data.get("publicShareUrl"),
        "started_at": data.get("startedAt"),
        "finished_at": data.get("finishedAt"),
        "persist_memory": data.get("persistMemory"),
        "task_count": len(data.get("tasks", []) or []),
        "raw": data,
    }


def _open_live_url_locally(url: str) -> dict[str, Any]:
    link = str(url or "").strip()
    if not link:
        return {"ok": False, "opened": False, "error": "live_url_missing"}
    try:
        opened = bool(webbrowser.open(link, new=2, autoraise=True))
        return {"ok": True, "opened": opened, "live_url": link}
    except Exception as exc:
        return {"ok": False, "opened": False, "live_url": link, "error": f"{type(exc).__name__}: {exc}"}


def _parse_iso(raw: str | None) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _duration_seconds(item: dict[str, Any]) -> float | None:
    start = _parse_iso(item.get("createdAt") or item.get("startedAt"))
    end = _parse_iso(item.get("updatedAt") or item.get("finishedAt") or item.get("endedAt"))
    if not start or not end:
        return None
    return max(0.0, (end - start).total_seconds())


def _task_hiccup(output: str) -> str | None:
    text = (output or "").lower()
    if not text:
        return None
    if "rejected" in text and "verification code" in text:
        return "verification_code_rejected"
    if "resend" in text and "verification" in text:
        return "verification_resend_requested"
    if "anti-bot" in text or "captcha" in text or "about:blank" in text:
        return "access_blocked_or_captcha"
    if "magic link" in text and "please click" in text:
        return "manual_magic_link_step"
    if "timed out" in text:
        return "task_timeout"
    return None


def _fetch_page_text(url: str, timeout_seconds: int) -> dict[str, Any]:
    target = str(url or "").strip()
    if not target:
        return {"ok": False, "url": target, "error": "url_missing"}
    req = Request(
        target,
        method="GET",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        },
    )
    try:
        with urlopen(req, timeout=max(5, int(timeout_seconds))) as resp:  # nosec B310 - explicit research usage
            status = int(getattr(resp, "status", 0) or resp.getcode() or 0)
            final_url = str(resp.geturl() or target)
            blob = resp.read(350_000)
            text = blob.decode("utf-8", errors="ignore")
            return {
                "ok": True,
                "url": target,
                "final_url": final_url,
                "status_code": status,
                "html": text,
                "bytes_read": len(blob),
            }
    except HTTPError as exc:
        return {
            "ok": False,
            "url": target,
            "status_code": int(exc.code),
            "error": f"HTTPError: {exc.reason}",
        }
    except URLError as exc:
        return {"ok": False, "url": target, "error": f"URLError: {exc.reason}"}


def _count_signals(text: str, tokens: tuple[str, ...]) -> int:
    low = str(text or "").lower()
    return sum(1 for token in tokens if token in low)


def _score_target_page(url: str, html: str) -> dict[str, Any]:
    low = str(html or "").lower()
    host = (urlparse(url).hostname or "").lower()
    signup_tokens = (
        "sign up",
        "create account",
        "get started",
        "start for free",
        "register",
    )
    trial_tokens = (
        "free trial",
        "start trial",
        "try free",
        "trial period",
        "14-day",
    )
    api_tokens = (
        "api key",
        "developer",
        "api docs",
        "documentation",
        "sdk",
        "access token",
    )
    payment_tokens = (
        "stripe",
        "checkout",
        "billing",
        "credit card",
        "payment method",
    )
    anti_bot_tokens = (
        "cloudflare",
        "turnstile",
        "captcha",
        "verify you are human",
        "challenge-platform",
        "cf-browser-verification",
    )
    enterprise_tokens = (
        "contact sales",
        "book a demo",
        "talk to sales",
        "request access",
    )
    no_card_tokens = ("no credit card", "without credit card", "card not required")

    signup_count = _count_signals(low, signup_tokens)
    trial_count = _count_signals(low, trial_tokens)
    api_count = _count_signals(low, api_tokens)
    payment_count = _count_signals(low, payment_tokens)
    anti_bot_count = _count_signals(low, anti_bot_tokens)
    enterprise_count = _count_signals(low, enterprise_tokens)
    no_card_count = _count_signals(low, no_card_tokens)

    score = 0
    score += signup_count * 3
    score += trial_count * 4
    score += api_count * 2
    score += payment_count
    score += no_card_count * 2
    score -= anti_bot_count * 5
    score -= enterprise_count * 2
    if host.endswith("stripe.com"):
        score -= 3

    recommendation = "low_priority"
    if anti_bot_count >= 1 and trial_count == 0:
        recommendation = "avoid_for_now"
    elif score >= 12:
        recommendation = "strong_candidate"
    elif score >= 7:
        recommendation = "candidate"

    return {
        "score": score,
        "recommendation": recommendation,
        "signals": {
            "signup": signup_count,
            "trial": trial_count,
            "api": api_count,
            "payment": payment_count,
            "anti_bot": anti_bot_count,
            "enterprise_gate": enterprise_count,
            "no_card_hint": no_card_count,
        },
    }


def _default_scout_urls() -> list[str]:
    urls = [str(p.get("target_url", "")).strip() for p in SITE_PRESETS.values()]
    urls.extend(
        [
            "https://laminar.sh/pricing",
            "https://supermemory.ai/pricing",
            "https://vibeflow.ai/pricing",
            "https://app.agentmail.to",
            "https://app.daytona.io",
        ]
    )
    out: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        value = str(raw or "").strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _run_scout_targets(args: argparse.Namespace) -> dict[str, Any]:
    urls: list[str] = []
    if bool(getattr(args, "include_defaults", True)):
        urls.extend(_default_scout_urls())
    for site_key in list(getattr(args, "site", []) or []):
        preset = SITE_PRESETS.get(str(site_key).strip().lower())
        if preset:
            urls.append(str(preset.get("target_url", "")).strip())
    for raw in list(getattr(args, "url", []) or []):
        value = str(raw or "").strip()
        if value:
            urls.append(value)

    ordered: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        if not raw:
            continue
        key = raw.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(raw)

    results: list[dict[str, Any]] = []
    for target in ordered:
        page = _fetch_page_text(target, int(args.timeout_seconds))
        row: dict[str, Any] = {
            "target_url": target,
            "ok": bool(page.get("ok", False)),
            "status_code": page.get("status_code"),
            "final_url": page.get("final_url") or target,
        }
        if page.get("ok"):
            analysis = _score_target_page(str(page.get("final_url") or target), str(page.get("html", "") or ""))
            row.update(analysis)
            row["html_bytes"] = page.get("bytes_read")
        else:
            row["score"] = -99
            row["recommendation"] = "error"
            row["error"] = page.get("error")
        results.append(row)

    results.sort(key=lambda item: int(item.get("score", -9999)), reverse=True)
    limit = max(1, int(getattr(args, "limit", 10)))
    top = results[:limit]

    return {
        "ok": True,
        "target_count": len(ordered),
        "limit": limit,
        "recommended_next": top[0] if top else None,
        "targets": top,
        "all_targets_scanned": results,
    }


def _monitor_session(browser_use_api_key: str, session_id: str, limit: int, include_output: bool) -> dict[str, Any]:
    page_size = max(1, min(int(limit), 100))
    data = _http_json(
        method="GET",
        url=f"{BROWSER_USE_BASE}/tasks?pageSize={page_size}&pageNumber=1",
        headers=_browser_use_headers(browser_use_api_key),
    )
    items = data.get("items", []) or []
    tasks = [it for it in items if str(it.get("sessionId", "")) == str(session_id)]

    task_rows: list[dict[str, Any]] = []
    durations: list[float] = []
    hiccups: list[dict[str, Any]] = []
    counts = {"success": 0, "failed": 0, "running": 0}

    for it in tasks:
        task_id = str(it.get("id", "")).strip()
        status = str(it.get("status", "")).lower()
        is_success = it.get("isSuccess")
        if status in {"created", "started"}:
            counts["running"] += 1
        elif is_success is True:
            counts["success"] += 1
        else:
            counts["failed"] += 1

        duration = _duration_seconds(it)
        if duration is not None:
            durations.append(duration)

        row = {
            "task_id": task_id,
            "status": status,
            "is_success": is_success,
            "created_at": it.get("createdAt"),
            "updated_at": it.get("updatedAt"),
            "duration_seconds": duration,
        }

        if include_output and task_id:
            detail = _http_json(
                method="GET",
                url=f"{BROWSER_USE_BASE}/tasks/{quote(task_id, safe='')}",
                headers=_browser_use_headers(browser_use_api_key),
            )
            output = str(detail.get("output", "") or "")
            row["output_preview"] = output[:400]
            hiccup = _task_hiccup(output)
            if hiccup:
                hiccups.append({"task_id": task_id, "hiccup": hiccup, "output_preview": output[:220]})
        task_rows.append(row)

    avg_duration = (sum(durations) / len(durations)) if durations else None
    return {
        "ok": True,
        "session_id": session_id,
        "task_count": len(tasks),
        "counts": counts,
        "avg_duration_seconds": avg_duration,
        "hiccups": hiccups,
        "tasks": task_rows,
    }


def _resolve_site(site: str | None, target_url: str | None) -> tuple[str | None, str]:
    if target_url:
        normalized = _normalize_target_url(target_url)
        host = (urlparse(normalized).hostname or "").strip().lower()
        if host in {"moss.dev", "www.moss.dev", "portal.usemoss.dev"}:
            return "moss", normalized
        return None, normalized
    if not site:
        raise ValueError("provide --site or --target-url")
    key = site.strip().lower()
    preset = SITE_PRESETS.get(key)
    if not preset:
        raise ValueError(f"unknown site preset: {site}")
    return key, preset["target_url"]


def _normalize_target_url(target_url: str) -> str:
    raw = str(target_url or "").strip()
    if not raw:
        return raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").strip().lower()
    path = (parsed.path or "/").strip() or "/"
    if host in {"moss.dev", "www.moss.dev"}:
        if path in {"", "/"}:
            return "https://portal.usemoss.dev/auth/sign-up"
    if host == "portal.usemoss.dev" and path in {"", "/"}:
        return "https://portal.usemoss.dev/auth/sign-up"
    return raw


def _apply_site_speed_defaults(args: argparse.Namespace, site_key: str | None) -> None:
    key = str(site_key or "").strip().lower()
    if key != "moss":
        return

    # Moss is materially faster/stabler on direct sign-up with low retry budgets.
    if int(getattr(args, "max_validation_variants", 3)) == 3:
        args.max_validation_variants = 1
    if int(getattr(args, "max_email_rotations", 2)) == 2:
        args.max_email_rotations = 0
    if int(getattr(args, "max_proxy_fallbacks", 2)) == 2:
        args.max_proxy_fallbacks = 0

    # Reuse an existing inbox by default on fast runs unless explicitly pinned.
    if (
        bool(getattr(args, "fast_onboard", True))
        and bool(getattr(args, "fresh_inbox", True))
        and not bool(getattr(args, "strict_fresh_inbox", False))
        and not str(getattr(args, "inbox", "") or "").strip()
        and not str(getattr(args, "inbox_username", "") or "").strip()
    ):
        args.fresh_inbox = False


def _default_proxy_fallback_country_codes(site_key: str | None, target_url: str) -> list[str]:
    key = str(site_key or "").strip().lower()
    host = (urlparse(str(target_url or "")).hostname or "").strip().lower()
    if key in {"windsurf", "daytona"}:
        return ["us", "ca", "nl", "sg"]
    if "apollo" in host:
        return ["us", "ca", "nl", "gb", "de", "sg"]
    return ["us", "ca", "nl", "sg"]


def _is_hard_block_target(site_key: str | None, target_url: str) -> bool:
    key = str(site_key or "").strip().lower()
    host = (urlparse(str(target_url or "")).hostname or "").strip().lower()
    return key in {"apollo", "daytona"} or "apollo" in host or "daytona" in host


def _extract_api_key(output_text: str) -> str | None:
    patterns = [
        r"\b(pmx_[A-Za-z0-9]{16,})\b",
        r"\b(ctx_(?:live|test)_[A-Za-z0-9_\-]{20,})\b",
        r"\b(moss_[A-Za-z0-9_\-]{16,})\b",
        r"\b(dsk-(?:live|test)-[A-Za-z0-9_\-]{20,})\b",
        r"\b(sk_[A-Za-z0-9_\-]{16,})\b",
        r"\b(gsk_[A-Za-z0-9]{16,})\b",
        r"\b(dg_[A-Za-z0-9]{16,})\b",
        r"\b(ws_[A-Za-z0-9]{16,})\b",
        r"\b(sk-api-[A-Za-z0-9_\-]+)\b",
        r"\b(sk-[A-Za-z0-9_\-]{20,})\b",
        r"\b(api[_ -]?key)\b[^A-Za-z0-9_\-]{0,12}([A-Za-z0-9_\-]{20,})",
    ]
    for raw in patterns:
        match = re.search(raw, output_text, flags=re.IGNORECASE)
        if not match:
            continue
        if match.lastindex and match.lastindex >= 2:
            return match.group(2)
        return match.group(1)
    return None


def _build_api_key_capture_instruction(site_ref: str) -> str:
    return (
        f"In this logged-in session for {site_ref}, open API/developer settings. "
        "Do not browse unrelated pages, do not use web search, and do not use write_file actions. "
        "If an existing key is fully visible, return the full key value and key name. "
        "If existing keys are masked, create exactly one NEW key named 'automation-export' "
        "and return its full unmasked value immediately plus the key name and URL."
    )


def _key_preview(api_key: str | None) -> str | None:
    key = str(api_key or "").strip()
    if not key:
        return None
    if len(key) <= 10:
        return key
    return f"{key[:6]}...{key[-4:]}"


def _infer_api_key_status(*, output_text: str, api_key: str | None) -> dict[str, Any]:
    text = str(output_text or "")
    lower = text.lower()
    key_found = bool(api_key)
    created_mentions = (
        "api key" in lower
        and any(token in lower for token in ("created", "generated", "new key", "key created"))
    )
    hidden_mentions = any(
        token in lower
        for token in (
            "copy this key now",
            "shown once",
            "cannot be shown again",
            "not shown again",
            "store it securely",
        )
    )
    return {
        "api_key_found": key_found,
        "api_key_preview": _key_preview(api_key),
        "api_key_created_reported": bool(created_mentions),
        "manual_copy_required": bool(created_mentions and not key_found) or bool(hidden_mentions and not key_found),
    }


def _infer_trial_status(text: str) -> dict[str, Any]:
    lower = str(text or "").lower()
    trial_available = any(
        token in lower
        for token in (
            "free trial is available",
            "trial available",
            "2-week free trial",
            "14-day free trial",
            "start free trial",
        )
    )
    trial_started = any(
        token in lower for token in ("trial started", "free trial activated", "started trial", "trial is active")
    )
    trial_started_negative = any(
        token in lower
        for token in (
            "trial started: no",
            "trial_started: no",
            "trial activation failed",
            "trial not started",
        )
    )
    trial_unavailable = any(
        token in lower
        for token in (
            "no free trial",
            "trial not available",
            "no trial",
            "free credits only",
            "not applicable",
        )
    )
    trial_canceled = any(
        token in lower
        for token in (
            "subscription canceled",
            "subscription cancelled",
            "recurring billing disabled",
            "cancellation confirmed",
            "trial canceled",
            "trial cancelled",
        )
    )
    return {
        "trial_available": bool(trial_available),
        "trial_started": bool(trial_started and not trial_started_negative),
        "trial_unavailable": bool(trial_unavailable),
        "trial_canceled": bool(trial_canceled),
    }


def _extract_billing_facts(text: str) -> dict[str, Any]:
    raw = str(text or "")
    lower = raw.lower()

    plan: str | None = None
    plan_patterns = (
        r"(?:current[_\s-]?plan)\s*[:=]\s*([A-Za-z0-9 _\-]+)",
        r"(?:plan)\s*[:=]\s*([A-Za-z0-9 _\-]+)",
    )
    for pattern in plan_patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = str(match.group(1) or "").strip(" .,\n\t")
        if candidate:
            plan = candidate
            break

    money_tokens = re.findall(r"\$[0-9][0-9,]*(?:\.[0-9]{1,2})?", raw)
    credit_tokens = re.findall(r"\b[0-9]+(?:\.[0-9]+)?\s*(?:credits?|usd|dollars?)\b", lower, flags=re.IGNORECASE)

    amount_mentions: list[str] = []
    seen: set[str] = set()
    for token in [*money_tokens, *credit_tokens]:
        norm = str(token).strip()
        key = norm.lower()
        if not norm or key in seen:
            continue
        seen.add(key)
        amount_mentions.append(norm)

    credit_balance: str | None = None
    cash_balance: str | None = None
    for token in amount_mentions:
        low = token.lower()
        if credit_balance is None and "credit" in low:
            credit_balance = token
        if cash_balance is None and ("$" in token or "usd" in low or "dollar" in low):
            cash_balance = token

    balance_label_match = re.search(
        r"(?:balance|credit[_\s-]?balance|credits[_\s-]?remaining|remaining[_\s-]?credits)\s*[:=]\s*([^\n,;]+)",
        raw,
        flags=re.IGNORECASE,
    )
    if balance_label_match:
        labeled = str(balance_label_match.group(1) or "").strip(" .,\n\t")
        if labeled:
            labeled_low = labeled.lower()
            if credit_balance is None and ("credit" in labeled_low or re.search(r"\b\d+(?:\.\d+)?\b", labeled)):
                credit_balance = labeled
            if cash_balance is None and ("$" in labeled or "usd" in labeled_low or "dollar" in labeled_low):
                cash_balance = labeled

    url_match = re.search(r"https?://[^\s)]+", raw)
    billing_url = str(url_match.group(0)).strip(".,") if url_match else None

    return {
        "current_plan": plan,
        "amount_mentions": amount_mentions[:8],
        "credit_balance": credit_balance,
        "cash_balance": cash_balance,
        "billing_url": billing_url,
        "has_payment_page_error": "something went wrong" in lower,
    }


def _collect_onboard_hiccups(
    *,
    start_result: dict[str, Any],
    resume_attempts: list[dict[str, Any]],
    blocked_recovery: dict[str, Any] | None,
    verification_link_attempt: dict[str, Any] | None,
    billing_retries: list[dict[str, Any]],
    api_key: str | None,
) -> list[str]:
    hiccups: list[str] = []
    blocked_reason = str(start_result.get("blocked_reason", "")).strip().lower()
    if blocked_reason == "invite_only_closed_signup":
        hiccups.append("invite_only_closed_signup")
    if blocked_reason == "signup_access_blocked":
        hiccups.append("signup_access_blocked")
    if blocked_reason == "signup_validation_failed":
        hiccups.append("signup_validation_failed")
    if blocked_reason == "verification_timeout":
        hiccups.append("verification_timeout")
    if int(start_result.get("verification_retries", 0) or 0) > 0:
        hiccups.append("verification_retry_needed")
    if int(len(start_result.get("verification_resend_attempts", []) or [])) > 0:
        hiccups.append("verification_resend_used")
    if any((entry.get("otp_payload") or {}).get("ok") is False for entry in resume_attempts):
        hiccups.append("otp_not_found_on_retry")
    if blocked_recovery:
        hiccups.append("manual_checkpoint_used")
    if verification_link_attempt and not ((verification_link_attempt.get("link_payload") or {}).get("ok")):
        hiccups.append("verification_link_not_found")
    if billing_retries:
        hiccups.append("billing_retry_needed")
    if not api_key:
        hiccups.append("api_key_not_captured")
    out: list[str] = []
    seen: set[str] = set()
    for item in hiccups:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _looks_like_auth_blocker(text: str) -> bool:
    lower = str(text or "").lower()
    return any(
        token in lower
        for token in (
            "incorrect email or password",
            "invalid credentials",
            "sign in",
            "log in",
            "session is not authenticated",
            "authentication required",
            "not authenticated",
        )
    )


def _looks_like_checkout_blocker(text: str) -> bool:
    lower = str(text or "").lower()
    return any(
        token in lower
        for token in (
            "stripe checkout",
            "checkout page",
            "something went wrong",
            "failed to render",
            "security policy block",
            "redirected to stripe",
        )
    )


def _looks_like_human_verification_blocker(text: str) -> bool:
    lower = str(text or "").lower()
    return any(
        token in lower
        for token in (
            "cloudflare",
            "verify that you are human",
            "please verify that you are human",
            "captcha",
            "security challenge",
            "human challenge",
            "are you human",
        )
    )


def _looks_like_signup_render_block(text: str) -> bool:
    lower = str(text or "").lower()
    return any(
        token in lower
        for token in (
            "about:blank",
            "failed to load",
            "page remained empty",
            "empty page",
            "target-detached",
            "security policy",
            "security block",
            "anti-bot",
            "did not render",
            "spa",
        )
    )


def _is_signup_access_issue_result(result: dict[str, Any]) -> bool:
    reason = str((result or {}).get("blocked_reason", "") or "").strip()
    if reason == "signup_access_blocked":
        return True
    if reason != "signup_validation_failed":
        return False
    output = str((result or {}).get("signup_output", "") or "")
    return _looks_like_signup_render_block(output)


def _build_reauth_trial_instruction(
    *,
    site_ref: str,
    login_url: str,
    email: str,
    password: str,
    has_card_secrets: bool,
) -> str:
    steps = [
        f"Re-authenticate for {site_ref}.",
        f"Go to login URL: {login_url}.",
        f"Use email: {email}.",
        f"Use password: {password}.",
        "After login, go to pricing/billing pages and inspect account state.",
        "Do not start a trial, do not upgrade, and do not open or complete checkout/payment flows.",
    ]
    if has_card_secrets:
        steps.append(
            "Card secrets may be available, but do NOT use them unless explicitly requested in a separate run."
        )
    steps.append(
        "Return: login_success yes/no, current_plan, trial_found yes/no, trial_started yes/no, "
        "credit_balance, cash_balance, blocker, final billing URL."
    )
    return " ".join(steps)


def _looks_like_otp_failure(output_text: str) -> bool:
    text = output_text.lower()
    return any(
        token in text
        for token in (
            "verification code error",
            "provide the latest verification code",
            "provide a new",
            "otp",
            "resend",
        )
    ) and ("error" in text or "provide" in text or "resend" in text)


def _build_balance_snapshot_instruction(site_ref: str, extra_hint: str = "") -> str:
    hint = str(extra_hint or "").strip()
    parts = [
        f"In this logged-in session for {site_ref}, inspect billing/usage state.",
        "Open account settings, billing, and usage pages to find plan and balances.",
        "Keep this minimal: no search, no unrelated pages, and no repeated page toggling.",
        "Do not click upgrade/buy/start-trial buttons and do not open checkout/payment pages.",
        "Return: current_plan, credit_balance, cash_balance, currency, blocker, and billing_url.",
    ]
    if hint:
        parts.append(f"Additional site context: {hint}")
    return " ".join(parts)


def _build_dynamic_trial_instruction(site_ref: str, extra_hint: str = "") -> str:
    hint = str(extra_hint or "").strip()
    parts = [
        f"In this logged-in session for {site_ref}, discover billing and trial options directly from the product UI.",
        "Do not assume trial name, trial length, or plan names.",
        "Open Billing, Plans, and Pricing pages and inspect visible options.",
        "Do not click upgrade/buy/start-trial buttons and do not open checkout/payment pages.",
        "Return: plans_seen, trial_found yes/no, trial_started yes/no, current_plan, credit_balance, cash_balance, blocker, and final billing state URL.",
    ]
    if hint:
        parts.append(f"Additional site context: {hint}")
    return " ".join(parts)


def _build_dynamic_trial_instruction_with_payment(
    site_ref: str,
    *,
    extra_hint: str = "",
    has_card_secrets: bool = False,
) -> str:
    base = _build_dynamic_trial_instruction(site_ref=site_ref, extra_hint=extra_hint)
    if has_card_secrets:
        return f"{base} Ignore payment secrets in this mode; this run is observe-only."
    return f"{base} If trial requires card, report blocker=card_required and stop."


def _run_custom_task(
    *,
    target_url: str,
    instruction: str,
    session_id: str | None,
    llm: str,
    max_steps: int,
    timeout_seconds: int,
    poll_seconds: int,
    dry_run: bool,
    verbose: bool,
    additional_allowed_domains: list[str] | None = None,
    browser_runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime = dict(browser_runtime or _resolve_browser_runtime_fields(None))
    cfg = DemoConfig(
        target_url=target_url,
        password=os.getenv("TRIALPILOT_PASSWORD", "").strip() or "DemoPass!123456789",
        first_name="Demo",
        last_name="User",
        phone="+14155550123",
        llm=llm,
        max_steps=max_steps,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
        otp_wait_seconds=300,
        otp_regex=r"\b(\d{6})\b",
        inbox=None,
        inbox_username=None,
        inbox_domain="agentmail.to",
        session_id=session_id,
        dry_run=dry_run,
        browser_use_api_key=os.getenv("BROWSER_USE_API_KEY", "").strip() or None,
        agentmail_api_key=os.getenv("AGENTMAIL_API_KEY", "").strip() or None,
        include_api_key_step=False,
        manual_otp=None,
        pause_for_billing=False,
        resume_only=False,
        verbose=verbose,
        profile_id=runtime.get("profile_id"),
        proxy_country_code=runtime.get("proxy_country_code"),
        browser_screen_width=runtime.get("browser_screen_width"),
        browser_screen_height=runtime.get("browser_screen_height"),
        highlight_elements=bool(runtime.get("highlight_elements", False)),
        flash_mode=bool(runtime.get("flash_mode", False)),
        thinking_mode=bool(runtime.get("thinking_mode", False)),
        vision_mode=runtime.get("vision_mode", True),
        system_prompt_extension=str(runtime.get("system_prompt_extension", "")),
        op_vault_id=runtime.get("op_vault_id"),
        secrets=runtime.get("secrets"),
        session_keep_alive=runtime.get("session_keep_alive"),
        session_persist_memory=runtime.get("session_persist_memory"),
    )
    host = _host_for_target(cfg.target_url)
    allowed_set = _domain_family(host)
    for domain in additional_allowed_domains or []:
        norm = str(domain).strip().lower()
        if norm:
            allowed_set.update(_domain_family(norm))
    if _should_add_payment_domains(instruction):
        for pay_host in (
            "checkout.stripe.com",
            "billing.stripe.com",
            "js.stripe.com",
            "m.stripe.network",
            "hooks.stripe.com",
            "stripe.com",
        ):
            allowed_set.update(_domain_family(pay_host))
    allowed_domains = sorted(allowed_set)
    retries = max(0, int(os.getenv("TRIALPILOT_CUSTOM_TASK_RETRIES", "2")))
    max_attempts = 1 + retries
    active_session_id = str(session_id or "").strip() or None
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        sid = active_session_id or _create_browser_session(cfg)
        try:
            tid, sid_effective, recovered, result, task_ids = _run_task_with_wait_and_recovery(
                cfg,
                session_id=sid,
                task_text=instruction,
                allowed_domains=allowed_domains,
                recovery_task_text=instruction,
                wait_seconds=cfg.timeout_seconds,
            )
            return {
                "session_id": sid_effective,
                "session_recovered": recovered,
                "task_id": tid,
                "task_ids": task_ids,
                "status": result.get("status"),
                "output": result.get("output"),
                "created_at": result.get("createdAt"),
                "started_at": result.get("startedAt"),
                "finished_at": result.get("finishedAt"),
                "is_success": result.get("isSuccess"),
            }
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts or not _looks_like_transient_run_demo_error(exc):
                raise
            # Force a new session on transient Browser Use task/session failures.
            active_session_id = None
            time.sleep(min(2**attempt, 5))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("custom task failed without a captured exception")


def _open_live_session(args: argparse.Namespace) -> dict[str, Any]:
    browser_use_api_key = _require_env("BROWSER_USE_API_KEY")
    runtime = _resolve_browser_runtime_fields(args)
    cfg = DemoConfig(
        target_url=args.target_url,
        password=os.getenv("TRIALPILOT_PASSWORD", "").strip() or "DemoPass!123456789",
        first_name="Demo",
        last_name="User",
        phone="+14155550123",
        llm=getattr(args, "llm", "browser-use-2.0"),
        max_steps=max(1, int(getattr(args, "max_steps", 5))),
        timeout_seconds=max(10, int(getattr(args, "timeout_seconds", 120))),
        poll_seconds=max(1, int(getattr(args, "poll_seconds", 2))),
        otp_wait_seconds=60,
        otp_regex=r"\b(\d{6})\b",
        inbox=None,
        inbox_username=None,
        inbox_domain="agentmail.to",
        session_id=None,
        dry_run=False,
        browser_use_api_key=browser_use_api_key,
        agentmail_api_key=os.getenv("AGENTMAIL_API_KEY", "").strip() or None,
        include_api_key_step=False,
        manual_otp=None,
        pause_for_billing=False,
        resume_only=False,
        verbose=bool(getattr(args, "verbose", False)),
        profile_id=runtime.get("profile_id"),
        proxy_country_code=runtime.get("proxy_country_code"),
        browser_screen_width=runtime.get("browser_screen_width"),
        browser_screen_height=runtime.get("browser_screen_height"),
        highlight_elements=bool(runtime.get("highlight_elements", False)),
        flash_mode=bool(runtime.get("flash_mode", False)),
        thinking_mode=bool(runtime.get("thinking_mode", False)),
        vision_mode=runtime.get("vision_mode", True),
        system_prompt_extension=str(runtime.get("system_prompt_extension", "")),
        op_vault_id=runtime.get("op_vault_id"),
        secrets=runtime.get("secrets"),
        session_keep_alive=runtime.get("session_keep_alive"),
        session_persist_memory=runtime.get("session_persist_memory"),
    )
    session_id = _create_browser_session(cfg)
    session_payload = _get_session(browser_use_api_key, session_id)
    payload = {
        "ok": True,
        "target_url": args.target_url,
        "session_id": session_id,
        "live_url": session_payload.get("live_url"),
        "status": session_payload.get("status"),
        "profile_id": runtime.get("profile_id"),
        "proxy_country_code": runtime.get("proxy_country_code"),
        "session": session_payload,
    }
    if bool(getattr(args, "open_live_url", True)):
        payload["open_live_url_attempt"] = _open_live_url_locally(str(session_payload.get("live_url", "") or ""))
    return payload


def _should_add_payment_domains(instruction: str) -> bool:
    text = str(instruction or "").lower()
    return any(
        token in text
        for token in (
            "stripe",
            "checkout",
            "card_number",
            "card_cvc",
            "payment form",
            "payment page",
            "trial_requires_card",
        )
    )


def _looks_like_transient_run_demo_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    transient_tokens = (
        "network error",
        "transport error",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "connection reset",
        "connection aborted",
        "http 408",
        "http 409",
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
        "browser session is stopped",
        "task not found",
        "http 404 for https://api.browser-use.com/api/v2/tasks/",
        "agentmail",
        "browser-use",
    )
    return any(token in text for token in transient_tokens)


def _run_demo_with_retries(
    cfg: DemoConfig,
    *,
    max_attempts: int = 3,
    sleep_seconds: int = 2,
) -> dict[str, Any]:
    attempts = max(1, int(max_attempts))
    working_cfg = cfg
    for idx in range(attempts):
        try:
            return run_demo(working_cfg)
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}".lower()
            if (
                "strict_fresh_inbox requested" in msg
                and "already exists" in msg
                and not str(working_cfg.inbox or "").strip()
                and str(working_cfg.inbox_username or "").strip()
                and idx < attempts - 1
            ):
                base_user = re.sub(r"[^a-z0-9]+", "", str(working_cfg.inbox_username or "").lower())[:24] or "acct"
                rotated_user = f"{base_user}{int(time.time() * 1000) % 1_000_000_000:09d}{random.randint(100,999)}"
                working_cfg = replace(working_cfg, inbox_username=rotated_user)
                time.sleep(1)
                continue
            last_attempt = idx >= attempts - 1
            if last_attempt or not _looks_like_transient_run_demo_error(exc):
                raise
            time.sleep(max(1, int(sleep_seconds)) + idx)
    raise RuntimeError("unreachable")


def _run_onboard(args: argparse.Namespace) -> dict[str, Any]:
    onboard_started_at = time.monotonic()
    verification_link_not_before = datetime.now(timezone.utc)
    site_key, target_url = _resolve_site(getattr(args, "site", None), getattr(args, "target_url", None))
    _apply_site_speed_defaults(args, site_key)
    preset = SITE_PRESETS.get(site_key or "", {})
    include_api_key_step_override = getattr(args, "include_api_key_step", None)
    if include_api_key_step_override is None:
        include_api_key_step = str(preset.get("include_api_key_step", "true")).strip().lower() != "false"
    else:
        include_api_key_step = bool(include_api_key_step_override)
    if not args.password:
        args.password = os.getenv("TRIALPILOT_PASSWORD", "").strip() or "DemoPass!123456789"
    args.target_url = target_url
    hard_block_target = _is_hard_block_target(site_key, target_url)
    if hard_block_target:
        args.fresh_inbox = True
        if not str(getattr(args, "profile_id", "") or "").strip():
            args.fresh_profile = True
    if bool(getattr(args, "strict_fresh_inbox", False)):
        if str(getattr(args, "inbox", "") or "").strip():
            raise ValueError("--strict-fresh-inbox cannot be combined with --inbox")
        args.fresh_inbox = True
    if (
        not bool(getattr(args, "fresh_inbox", False))
        and not str(getattr(args, "inbox", "") or "").strip()
        and not str(getattr(args, "inbox_username", "") or "").strip()
    ):
        agentmail_api_key = os.getenv("AGENTMAIL_API_KEY", "").strip()
        if agentmail_api_key:
            try:
                existing_inbox = _pick_random_existing_inbox_id(agentmail_api_key, limit=20)
                if existing_inbox:
                    args.inbox = existing_inbox
            except Exception:
                # Fall back to standard run_demo behavior if listing inboxes fails.
                pass

    created_profile: dict[str, Any] | None = None
    if bool(getattr(args, "fresh_profile", True)):
        browser_use_api_key = _require_env("BROWSER_USE_API_KEY")
        profile_name = str(getattr(args, "profile_name", "") or "").strip()
        if not profile_name:
            site_ref = (site_key or (urlparse(target_url).hostname or "site")).lower()
            site_slug = re.sub(r"[^a-z0-9]+", "-", site_ref).strip("-") or "site"
            profile_name = f"trialpilot-{site_slug}-{int(time.time())}"
        created_profile = _create_profile(browser_use_api_key, profile_name)
        args.profile_id = str(created_profile.get("id", "")).strip() or None
        if not args.profile_id:
            raise RuntimeError(f"fresh profile creation did not return id: {created_profile}")
    if bool(getattr(args, "fresh_inbox", False)) and not str(getattr(args, "inbox", "") or "").strip():
        if not str(getattr(args, "inbox_username", "") or "").strip():
            site_ref = (site_key or (urlparse(target_url).hostname or "acct")).lower()
            site_slug = re.sub(r"[^a-z0-9]+", "", site_ref)[:10] or "acct"
            args.inbox_username = f"{site_slug}{int(time.time() * 1000) % 1_000_000_000:09d}"
    args.include_api_key_step = include_api_key_step
    args.resume_only = False
    cfg = _build_common_cfg(args)
    browser_runtime = _resolve_browser_runtime_fields(args)
    start_result = _run_demo_with_retries(cfg)
    blocked_recovery: dict[str, Any] | None = None

    resume_attempts: list[dict[str, Any]] = []
    final_result = start_result
    session_id = str(start_result.get("session_id", "")).strip()
    inbox_id = str(start_result.get("inbox_id", "")).strip()
    api_key = _extract_api_key(str(start_result.get("continue_output", "") or ""))

    blocked_reason = str(start_result.get("blocked_reason", "")).strip()
    if blocked_reason == "signup_validation_failed" and not _is_signup_access_issue_result(start_result):
        blocked_recovery = {
            "initial_blocked_result": start_result,
            "session_id": session_id or None,
            "inbox_id": inbox_id or None,
            "strategy": "validation_retry_variants",
            "validation_retries": [],
            "email_rotation_retries": [],
        }
        raw_phone = str(args.phone or "").strip()
        phone_digits = re.sub(r"\D+", "", raw_phone)
        variant_phones: list[tuple[str, str]] = []
        if phone_digits and phone_digits != raw_phone:
            variant_phones.append(("digits_only_phone", phone_digits))
        if len(phone_digits) == 11 and phone_digits.startswith("1"):
            variant_phones.append(("drop_country_code", phone_digits[1:]))
        variant_phones.append(("omit_phone", ""))

        seen_phone: set[str] = set()
        max_validation_variants = max(0, int(getattr(args, "max_validation_variants", 3)))
        for label, phone_value in variant_phones[:max_validation_variants]:
            if phone_value in seen_phone:
                continue
            seen_phone.add(phone_value)
            retry_cfg = replace(
                cfg,
                session_id=None,
                inbox=inbox_id or cfg.inbox,
                phone=phone_value,
            )
            retry_result = _run_demo_with_retries(retry_cfg)
            blocked_recovery["validation_retries"].append(
                {
                    "label": label,
                    "phone": phone_value,
                    "result": retry_result,
                }
            )
            start_result = retry_result
            final_result = retry_result
            session_id = str(retry_result.get("session_id", "")).strip() or session_id
            inbox_id = str(retry_result.get("inbox_id", "")).strip() or inbox_id
            api_key = _extract_api_key(str(retry_result.get("continue_output", "") or "")) or api_key
            if bool(retry_result.get("ok", False)):
                break
            if str(retry_result.get("blocked_reason", "")).strip() != "signup_validation_failed":
                break

        # If validation retries still fail, rotate through existing inboxes (existing user requested behavior)
        # in case provider has email-level throttling/blacklisting.
        if str(start_result.get("blocked_reason", "")).strip() == "signup_validation_failed":
            try:
                agentmail_api = _require_env("AGENTMAIL_API_KEY")
                inbox_rows = _list_inboxes(agentmail_api, limit=20).get("inboxes", []) or []
                alternate_inboxes: list[str] = []
                for row in inbox_rows:
                    candidate = str((row or {}).get("inbox_id", "")).strip()
                    if not candidate or candidate == (inbox_id or "") or candidate == (cfg.inbox or ""):
                        continue
                    alternate_inboxes.append(candidate)
                max_email_rotations = max(0, int(getattr(args, "max_email_rotations", 2)))
                for alt_inbox in alternate_inboxes[:max_email_rotations]:
                    retry_cfg = replace(
                        cfg,
                        session_id=None,
                        inbox=alt_inbox,
                        phone=raw_phone,
                    )
                    retry_result = _run_demo_with_retries(retry_cfg)
                    blocked_recovery["email_rotation_retries"].append(
                        {
                            "inbox_id": alt_inbox,
                            "result": retry_result,
                        }
                    )
                    start_result = retry_result
                    final_result = retry_result
                    session_id = str(retry_result.get("session_id", "")).strip() or session_id
                    inbox_id = str(retry_result.get("inbox_id", "")).strip() or alt_inbox
                    api_key = _extract_api_key(str(retry_result.get("continue_output", "") or "")) or api_key
                    if bool(retry_result.get("ok", False)):
                        break
                    if str(retry_result.get("blocked_reason", "")).strip() != "signup_validation_failed":
                        break
            except Exception as exc:
                blocked_recovery["email_rotation_error"] = f"{type(exc).__name__}: {exc}"

    if str(start_result.get("blocked_reason", "")).strip() == "verification_timeout":
        blocked_recovery = {
            "initial_blocked_result": start_result,
            "session_id": session_id or None,
            "inbox_id": inbox_id or None,
            "strategy": "verification_timeout_inbox_failover",
            "timeout_retries": [],
            "email_rotation_retries": [],
        }
        # First retry same inbox with longer wait window.
        longer_wait = max(int(cfg.otp_wait_seconds), 900)
        retry_cfg = replace(cfg, session_id=None, inbox=inbox_id or cfg.inbox, otp_wait_seconds=longer_wait)
        retry_result = _run_demo_with_retries(retry_cfg)
        blocked_recovery["timeout_retries"].append(
            {
                "inbox_id": inbox_id or cfg.inbox,
                "otp_wait_seconds": longer_wait,
                "result": retry_result,
            }
        )
        start_result = retry_result
        final_result = retry_result
        session_id = str(retry_result.get("session_id", "")).strip() or session_id
        inbox_id = str(retry_result.get("inbox_id", "")).strip() or inbox_id
        api_key = _extract_api_key(str(retry_result.get("continue_output", "") or "")) or api_key

        # If still timing out, rotate through existing inboxes.
        if str(start_result.get("blocked_reason", "")).strip() == "verification_timeout":
            try:
                agentmail_api = _require_env("AGENTMAIL_API_KEY")
                inbox_rows = _list_inboxes(agentmail_api, limit=20).get("inboxes", []) or []
                alternate_inboxes: list[str] = []
                for row in inbox_rows:
                    candidate = str((row or {}).get("inbox_id", "")).strip()
                    if not candidate or candidate == (inbox_id or "") or candidate == (cfg.inbox or ""):
                        continue
                    alternate_inboxes.append(candidate)
                for alt_inbox in alternate_inboxes[:3]:
                    alt_cfg = replace(cfg, session_id=None, inbox=alt_inbox, otp_wait_seconds=longer_wait)
                    alt_result = _run_demo_with_retries(alt_cfg)
                    blocked_recovery["email_rotation_retries"].append(
                        {
                            "inbox_id": alt_inbox,
                            "otp_wait_seconds": longer_wait,
                            "result": alt_result,
                        }
                    )
                    start_result = alt_result
                    final_result = alt_result
                    session_id = str(alt_result.get("session_id", "")).strip() or session_id
                    inbox_id = str(alt_result.get("inbox_id", "")).strip() or alt_inbox
                    api_key = _extract_api_key(str(alt_result.get("continue_output", "") or "")) or api_key
                    if bool(alt_result.get("ok", False)):
                        break
                    if str(alt_result.get("blocked_reason", "")).strip() != "verification_timeout":
                        break
            except Exception as exc:
                blocked_recovery["email_rotation_error"] = f"{type(exc).__name__}: {exc}"

    if _is_signup_access_issue_result(start_result):
        blocked_recovery = blocked_recovery or {
            "initial_blocked_result": start_result,
            "session_id": session_id or None,
            "inbox_id": inbox_id or None,
        }
        blocked_output = str(start_result.get("signup_output", "") or "")
        challenge_blocker_detected = _looks_like_human_verification_blocker(blocked_output)
        blocked_recovery["challenge_blocker_detected"] = challenge_blocker_detected
        fallback_geos: list[str] = []
        for geo in list(getattr(args, "proxy_fallback_country", []) or []):
            value = str(geo or "").strip().lower()
            if value:
                fallback_geos.append(value)
        if not fallback_geos:
            raw_fallbacks = (
                os.getenv("TRIALPILOT_PROXY_FALLBACK_COUNTRIES", "")
                or os.getenv("BROWSER_USE_PROXY_FALLBACK_COUNTRIES", "")
            ).strip()
            if raw_fallbacks:
                for token in re.split(r"[,\s]+", raw_fallbacks):
                    value = str(token or "").strip().lower()
                    if value:
                        fallback_geos.append(value)
        if not fallback_geos:
            fallback_geos = _default_proxy_fallback_country_codes(site_key, target_url)
        current_geo = str(cfg.proxy_country_code or "").strip().lower()
        blocked_recovery["proxy_fallback_retries"] = []
        if challenge_blocker_detected and bool(getattr(args, "auto_human_checkpoint", True)):
            blocked_recovery["proxy_fallback_skipped"] = "human_verification_detected"
        else:
            max_proxy_fallbacks = max(0, int(getattr(args, "max_proxy_fallbacks", 2)))
            attempted = 0
            for alt_geo in fallback_geos:
                if attempted >= max_proxy_fallbacks:
                    break
                if not alt_geo or alt_geo == current_geo:
                    continue
                attempted += 1
                alt_cfg = replace(
                    cfg,
                    session_id=None,
                    inbox=inbox_id or cfg.inbox,
                    proxy_country_code=alt_geo,
                )
                alt_result = _run_demo_with_retries(alt_cfg)
                blocked_recovery["proxy_fallback_retries"].append(
                    {
                        "proxy_country_code": alt_geo,
                        "result": alt_result,
                    }
                )
                start_result = alt_result
                final_result = alt_result
                session_id = str(alt_result.get("session_id", "")).strip() or session_id
                inbox_id = str(alt_result.get("inbox_id", "")).strip() or inbox_id
                api_key = _extract_api_key(str(alt_result.get("continue_output", "") or "")) or api_key
                if bool(alt_result.get("ok", False)):
                    break
                if not _is_signup_access_issue_result(alt_result):
                    break

    if _is_signup_access_issue_result(start_result) and session_id:
        blocked_recovery = blocked_recovery or {}
        blocked_recovery.update(
            {
                "initial_blocked_result": start_result,
                "session_id": session_id,
                "inbox_id": inbox_id or None,
                "auto_human_checkpoint_enabled": bool(getattr(args, "auto_human_checkpoint", True)),
            }
        )
        if bool(getattr(args, "auto_human_checkpoint", True)) and not bool(args.dry_run):
            browser_use_api_key = _require_env("BROWSER_USE_API_KEY")
            session_info = _get_session(browser_use_api_key, session_id)
            blocked_recovery["session_info"] = session_info
            live_url = str(session_info.get("live_url", "")).strip()
            if bool(getattr(args, "open_live_url", True)) and live_url:
                blocked_recovery["open_live_url_attempt"] = _open_live_url_locally(live_url)

            wait_seconds = max(0, int(getattr(args, "human_checkpoint_seconds", 180)))
            blocked_recovery["human_checkpoint_wait_seconds"] = wait_seconds
            if wait_seconds:
                # Manual checkpoint: user can solve anti-bot/challenge in live session.
                time.sleep(wait_seconds)

            retry_cfg = replace(cfg, session_id=session_id, inbox=inbox_id or cfg.inbox)
            retry_result = _run_demo_with_retries(retry_cfg)
            blocked_recovery["retry_result"] = retry_result
            start_result = retry_result
            final_result = retry_result
            session_id = str(retry_result.get("session_id", "")).strip() or session_id
            inbox_id = str(retry_result.get("inbox_id", "")).strip() or inbox_id
            api_key = _extract_api_key(str(retry_result.get("continue_output", "") or "")) or api_key

    blocked_reason_final = str(start_result.get("blocked_reason", "")).strip()
    if blocked_reason_final in {
        "signup_access_blocked",
        "signup_validation_failed",
        "invite_only_closed_signup",
        "verification_timeout",
    }:
        blocked_live_url: str | None = None
        if session_id and not bool(args.dry_run):
            try:
                browser_use_api_key = _require_env("BROWSER_USE_API_KEY")
                blocked_live_url = str(_get_session(browser_use_api_key, session_id).get("live_url", "")).strip() or None
            except Exception:
                blocked_live_url = None
        return {
            "ok": False,
            "site": site_key,
            "target_url": target_url,
            "session_id": session_id or None,
            "blocked_live_url": blocked_live_url,
            "blocked_reason": blocked_reason_final,
            "inbox_id": inbox_id or None,
            "start_result": start_result,
            "blocked_recovery": blocked_recovery,
            "timing_seconds": round(max(0.0, time.monotonic() - onboard_started_at), 2),
        }

    if session_id and inbox_id and _looks_like_otp_failure(str(start_result.get("continue_output", "") or "")):
        otp_api = _require_env("AGENTMAIL_API_KEY")
        for attempt in range(1, int(args.resume_retries) + 1):
            otp_payload = _latest_otp(otp_api, inbox_id, cfg.otp_regex)
            entry: dict[str, Any] = {"attempt": attempt, "otp_payload": otp_payload}
            if not otp_payload.get("ok"):
                entry["result"] = {"ok": False, "error": "otp_not_found"}
                resume_attempts.append(entry)
                time.sleep(args.retry_wait_seconds)
                continue

            resume_args = argparse.Namespace(
                target_url=target_url,
                session_id=session_id,
                manual_otp=otp_payload.get("otp", ""),
                include_api_key_step=True,
                dry_run=bool(args.dry_run),
                timeout_seconds=int(args.timeout_seconds),
                poll_seconds=int(args.poll_seconds),
                max_steps=int(args.max_steps),
                llm=args.llm,
                otp_regex=cfg.otp_regex,
                otp_wait_seconds=int(args.otp_wait_seconds),
                first_name=args.first_name,
                last_name=args.last_name,
                phone=args.phone,
                password=args.password,
                inbox=inbox_id,
                inbox_username=args.inbox_username,
                inbox_domain=args.inbox_domain,
                pause_for_billing=False,
                resume_only=True,
                verbose=bool(args.verbose),
                profile_id=args.profile_id,
                proxy_country_code=args.proxy_country_code,
                screen_width=args.screen_width,
                screen_height=args.screen_height,
                highlight_elements=args.highlight_elements,
                flash_mode=args.flash_mode,
                thinking=args.thinking,
                vision=args.vision,
                system_prompt_extension=args.system_prompt_extension,
                op_vault_id=args.op_vault_id,
                secrets_json=args.secrets_json,
                session_keep_alive=args.session_keep_alive,
                session_persist_memory=args.session_persist_memory,
            )
            resume_cfg = _build_common_cfg(resume_args)
            resume_result = _run_demo_with_retries(resume_cfg)
            entry["result"] = resume_result
            resume_attempts.append(entry)
            final_result = resume_result
            api_key = _extract_api_key(str(resume_result.get("continue_output", "") or "")) or api_key
            if api_key:
                break
            if not _looks_like_otp_failure(str(resume_result.get("continue_output", "") or "")):
                break
            time.sleep(args.retry_wait_seconds)

    verification_link_attempt: dict[str, Any] | None = None
    if include_api_key_step and (not api_key) and session_id and inbox_id:
        output_text = str(final_result.get("continue_output", "") or "")
        lower_output = output_text.lower()
        recovery_signals = (
            "check your email",
            "magic link",
            "verification link",
            "otp_expired",
            "expired",
            "email is not yet confirmed",
            "invalid login credentials",
            "verification failed",
        )
        recovery_triggered = any(token in lower_output for token in recovery_signals)
        provider_hint = site_key or _host_for_target(target_url)
        verification_link_attempt = {
            "recovery_triggered": recovery_triggered,
            "provider_hint": provider_hint,
        }
        api_key_env = _require_env("AGENTMAIL_API_KEY")
        link_payload = _latest_verification_link(
            api_key_env,
            inbox_id,
            provider_hint=provider_hint,
            not_before=verification_link_not_before,
        )
        verification_link_attempt["link_payload"] = link_payload
        if verification_link_attempt.get("link_payload", {}).get("ok"):
            link_payload = verification_link_attempt["link_payload"]
            link = str(link_payload.get("verification_link", "")).strip()
            link_host = (urlparse(link).hostname or "").strip().lower()
            instruction = (
                f"Open this verification link exactly: {link}\n"
                "Complete verification/onboarding, then navigate to API settings and create a key. "
                "Return full key string and page URL."
            )
            link_result = _run_custom_task(
                target_url=target_url,
                instruction=instruction,
                session_id=session_id,
                llm=args.llm,
                max_steps=int(args.max_steps),
                timeout_seconds=int(args.timeout_seconds),
                poll_seconds=int(args.poll_seconds),
                dry_run=bool(args.dry_run),
                verbose=bool(args.verbose),
                additional_allowed_domains=[link_host] if link_host else None,
                browser_runtime=browser_runtime,
            )
            verification_link_attempt["task_result"] = link_result
            api_key = _extract_api_key(str(link_result.get("output", "") or "")) or api_key

    api_key_capture_attempt: dict[str, Any] | None = None
    if include_api_key_step and (not api_key) and session_id:
        capture_instruction = _build_api_key_capture_instruction(site_key or target_url)
        capture_result = _run_custom_task(
            target_url=target_url,
            instruction=capture_instruction,
            session_id=session_id,
            llm=args.llm,
            max_steps=int(args.max_steps),
            timeout_seconds=int(args.timeout_seconds),
            poll_seconds=int(args.poll_seconds),
            dry_run=bool(args.dry_run),
            verbose=bool(args.verbose),
            browser_runtime=browser_runtime,
        )
        api_key_capture_attempt = {"task_result": capture_result}
        api_key = _extract_api_key(str(capture_result.get("output", "") or "")) or api_key

    fast_onboard = bool(getattr(args, "fast_onboard", False))
    billing_check: dict[str, Any] | None = None
    billing_retries: list[dict[str, Any]] = []
    include_balance_snapshot = bool(getattr(args, "include_balance_snapshot", True))
    if session_id and (not fast_onboard or include_balance_snapshot):
        billing_hint = preset.get(
            "billing_hint",
            "Read-only only: capture current plan and visible credit/cash balance values.",
        )
        instruction = _build_balance_snapshot_instruction(site_ref=(site_key or target_url), extra_hint=billing_hint)
        billing_max_steps = min(int(args.max_steps), 30)
        billing_timeout_seconds = min(int(args.timeout_seconds), 240)
        billing_check = _run_custom_task(
            target_url=target_url,
            instruction=instruction,
            session_id=session_id,
            llm=args.llm,
            max_steps=billing_max_steps,
            timeout_seconds=billing_timeout_seconds,
            poll_seconds=int(args.poll_seconds),
            dry_run=bool(args.dry_run),
            verbose=bool(args.verbose),
            browser_runtime=browser_runtime,
        )
        billing_output_text = str((billing_check or {}).get("output", "") or "")
        if (not fast_onboard) and _looks_like_auth_blocker(billing_output_text):
            reauth_instruction = _build_reauth_trial_instruction(
                site_ref=(site_key or target_url),
                login_url=target_url,
                email=(inbox_id or ""),
                password=(args.password or ""),
                has_card_secrets=False,
            )
            retry_result = _run_custom_task(
                target_url=target_url,
                instruction=reauth_instruction,
                session_id=session_id,
                llm=args.llm,
                max_steps=int(args.max_steps),
                timeout_seconds=int(args.timeout_seconds),
                poll_seconds=int(args.poll_seconds),
                dry_run=bool(args.dry_run),
                verbose=bool(args.verbose),
                browser_runtime=browser_runtime,
            )
            billing_retries.append({"reason": "auth_blocker", "result": retry_result})
            billing_check = retry_result

    combined_key_text_parts = [
        str(start_result.get("continue_output", "") or ""),
        str(final_result.get("continue_output", "") or ""),
    ]
    if verification_link_attempt:
        link_task_output = str((verification_link_attempt.get("task_result") or {}).get("output", "") or "")
        if link_task_output:
            combined_key_text_parts.append(link_task_output)
    if api_key_capture_attempt:
        capture_output = str((api_key_capture_attempt.get("task_result") or {}).get("output", "") or "")
        if capture_output:
            combined_key_text_parts.append(capture_output)
    combined_key_text = "\n".join(part for part in combined_key_text_parts if part)

    billing_output = str((billing_check or {}).get("output", "") or "")
    billing_facts = _extract_billing_facts(billing_output) if billing_output else None
    signup_email = str(final_result.get("signup_email", "") or start_result.get("signup_email", "") or "").strip()
    login = {
        "email": signup_email or inbox_id or None,
        "password": args.password or None,
        "login_url": target_url,
    }
    hiccups = _collect_onboard_hiccups(
        start_result=start_result,
        resume_attempts=resume_attempts,
        blocked_recovery=blocked_recovery,
        verification_link_attempt=verification_link_attempt,
        billing_retries=billing_retries,
        api_key=api_key,
    )
    summary = {
        "account_email": login["email"],
        "account_password": login["password"],
        "login_url": login["login_url"],
        "api_key_status": _infer_api_key_status(output_text=combined_key_text, api_key=api_key),
        "trial_status": _infer_trial_status(billing_output) if billing_output else None,
        "billing_facts": billing_facts,
        "session_id": session_id or None,
        "fast_onboard": fast_onboard,
        "include_balance_snapshot": include_balance_snapshot,
        "speed_profile": "super_fast" if bool(getattr(args, "super_fast", False)) else "default",
        "timing_seconds": round(max(0.0, time.monotonic() - onboard_started_at), 2),
        "profile_id": args.profile_id,
        "hiccups": hiccups,
    }

    return {
        "ok": True,
        "site": site_key,
        "target_url": target_url,
        "inbox_id": inbox_id or None,
        "session_id": session_id or None,
        "created_profile": created_profile,
        "include_api_key_step": include_api_key_step,
        "start_result": start_result,
        "blocked_recovery": blocked_recovery,
        "resume_attempts": resume_attempts,
        "final_result": final_result,
        "login": login,
        "api_key": api_key,
        "verification_link_attempt": verification_link_attempt,
        "api_key_capture_attempt": api_key_capture_attempt,
        "billing_check": billing_check,
        "billing_retries": billing_retries,
        "summary": summary,
    }


def _apply_super_fast_onboard_args(args: argparse.Namespace) -> None:
    if not bool(getattr(args, "super_fast", False)):
        return
    # Latency-first preset: keep only essential steps and constrain retries.
    if getattr(args, "include_api_key_step", None) is None:
        args.include_api_key_step = True
    args.fast_onboard = True
    args.thinking = False
    args.flash_mode = True
    if getattr(args, "vision", None) in {None, "auto"}:
        args.vision = "false"
    args.max_steps = min(int(args.max_steps), 35)
    args.timeout_seconds = min(int(args.timeout_seconds), 180)
    args.otp_wait_seconds = min(int(args.otp_wait_seconds), 120)
    args.poll_seconds = min(int(args.poll_seconds), 2)
    args.resume_retries = min(int(args.resume_retries), 1)
    args.retry_wait_seconds = min(int(args.retry_wait_seconds), 1)
    args.max_validation_variants = min(int(args.max_validation_variants), 1)
    args.max_email_rotations = 0
    args.include_balance_snapshot = False
    site_key = str(getattr(args, "site", "") or "").strip().lower() or None
    target_url = str(getattr(args, "target_url", "") or "").strip()
    if not target_url and site_key:
        preset = SITE_PRESETS.get(site_key)
        if preset:
            target_url = str(preset.get("target_url", "")).strip()
    hard_block_target = _is_hard_block_target(site_key, target_url)
    if hard_block_target:
        args.max_proxy_fallbacks = max(int(getattr(args, "max_proxy_fallbacks", 2)), 4)
        args.auto_human_checkpoint = True
        args.open_live_url = True
        args.human_checkpoint_seconds = min(int(getattr(args, "human_checkpoint_seconds", 180)), 90)
        if not str(getattr(args, "profile_id", "") or "").strip():
            args.fresh_profile = True
    else:
        args.max_proxy_fallbacks = 0
        if not str(getattr(args, "profile_id", "") or "").strip():
            args.fresh_profile = False
        args.auto_human_checkpoint = False
        args.open_live_url = False


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trialpilot-live", description="Live signup demo helper CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_browser_runtime_flags(cmd: argparse.ArgumentParser) -> None:
        cmd.add_argument("--profile-id")
        cmd.add_argument(
            "--proxy-country-code",
            "--cloud-proxy-country-code",
            dest="proxy_country_code",
            help="Browser Use proxy country code (e.g. us, uk, de).",
        )
        cmd.add_argument("--screen-width", type=int)
        cmd.add_argument("--screen-height", type=int)
        cmd.add_argument("--highlight-elements", action="store_true")
        cmd.add_argument("--flash-mode", action="store_true")
        cmd.add_argument("--thinking", action="store_true")
        cmd.add_argument("--vision", choices=["true", "false", "auto"])
        cmd.add_argument("--system-prompt-extension")
        cmd.add_argument("--op-vault-id")
        cmd.add_argument("--secrets-json")
        cmd.add_argument("--session-keep-alive", choices=["true", "false"])
        cmd.add_argument("--session-persist-memory", choices=["true", "false"])

    profiles = sub.add_parser("profiles", help="List Browser Use profiles")
    profiles.add_argument("--limit", type=int, default=20)

    create_profile = sub.add_parser("create-profile", help="Create Browser Use profile")
    create_profile.add_argument("--name")

    session_info = sub.add_parser("session-info", help="Inspect Browser Use session and optionally open live URL")
    session_info.add_argument("--session-id", required=True)
    session_info.add_argument("--open-live-url", action="store_true")

    open_live = sub.add_parser("open-live", help="Create Browser Use session for a URL and return live browser link")
    open_live.add_argument("--target-url", required=True)
    open_live.add_argument("--open-live-url", action=argparse.BooleanOptionalAction, default=True)
    open_live.add_argument("--llm", default="browser-use-2.0")
    open_live.add_argument("--max-steps", type=int, default=5)
    open_live.add_argument("--timeout-seconds", type=int, default=120)
    open_live.add_argument("--poll-seconds", type=int, default=2)
    open_live.add_argument("--verbose", action="store_true")
    add_browser_runtime_flags(open_live)

    monitor = sub.add_parser("monitor-session", help="Analyze recent Browser Use tasks for one session")
    monitor.add_argument("--session-id", required=True)
    monitor.add_argument("--limit", type=int, default=30)
    monitor.add_argument("--include-output", action="store_true")

    scout = sub.add_parser("scout-targets", help="Rank likely onboarding/free-trial targets before live runs")
    scout.add_argument("--site", action="append", choices=sorted(SITE_PRESETS.keys()), default=[])
    scout.add_argument("--url", action="append", default=[])
    scout.add_argument(
        "--include-defaults",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include built-in target list in addition to explicit --site/--url entries.",
    )
    scout.add_argument("--timeout-seconds", type=int, default=15)
    scout.add_argument("--limit", type=int, default=8)

    inboxes = sub.add_parser("inboxes", help="List AgentMail inboxes")
    inboxes.add_argument("--limit", type=int, default=20)

    otp = sub.add_parser("latest-otp", help="Fetch latest OTP for inbox")
    otp.add_argument("--inbox", required=True)
    otp.add_argument("--otp-regex", default=r"\b(\d{6})\b")

    start = sub.add_parser("start", help="Start signup + OTP flow")
    start.add_argument("--target-url", required=True)
    start.add_argument("--password", default="")
    start.add_argument("--first-name", default="Demo")
    start.add_argument("--last-name", default="User")
    start.add_argument("--phone", default="+14155550123")
    start.add_argument("--llm", default="browser-use-2.0")
    start.add_argument("--max-steps", type=int, default=80)
    start.add_argument("--timeout-seconds", type=int, default=600)
    start.add_argument("--poll-seconds", type=int, default=5)
    start.add_argument("--otp-wait-seconds", type=int, default=300)
    start.add_argument("--otp-regex", default=r"\b(\d{6})\b")
    start.add_argument("--inbox")
    start.add_argument("--inbox-username")
    start.add_argument("--inbox-domain", default="agentmail.to")
    start.add_argument("--dry-run", action="store_true")
    start.add_argument("--pause-for-billing", action="store_true")
    start.add_argument("--include-api-key-step", action="store_true")
    start.add_argument("--manual-otp")
    start.add_argument("--verbose", action="store_true")
    add_browser_runtime_flags(start)

    resume = sub.add_parser("resume", help="Resume existing session")
    resume.add_argument("--target-url", required=True)
    resume.add_argument("--session-id", required=True)
    resume.add_argument("--manual-otp")
    resume.add_argument("--include-api-key-step", action="store_true")
    resume.add_argument("--dry-run", action="store_true")
    resume.add_argument("--timeout-seconds", type=int, default=600)
    resume.add_argument("--poll-seconds", type=int, default=5)
    resume.add_argument("--max-steps", type=int, default=80)
    resume.add_argument("--llm", default="browser-use-2.0")
    resume.add_argument("--otp-regex", default=r"\b(\d{6})\b")
    resume.add_argument("--otp-wait-seconds", type=int, default=300)
    resume.add_argument("--first-name", default="Demo")
    resume.add_argument("--last-name", default="User")
    resume.add_argument("--phone", default="+14155550123")
    resume.add_argument("--password", default="")
    resume.add_argument("--inbox")
    resume.add_argument("--inbox-username")
    resume.add_argument("--inbox-domain", default="agentmail.to")
    resume.add_argument("--verbose", action="store_true")
    add_browser_runtime_flags(resume)

    task = sub.add_parser("task", help="Run one Browser Use task (custom instruction)")
    task.add_argument("--target-url", required=True)
    task.add_argument("--instruction", required=True)
    task.add_argument("--session-id")
    task.add_argument("--llm", default="browser-use-2.0")
    task.add_argument("--max-steps", type=int, default=80)
    task.add_argument("--timeout-seconds", type=int, default=600)
    task.add_argument("--poll-seconds", type=int, default=5)
    task.add_argument("--dry-run", action="store_true")
    task.add_argument("--verbose", action="store_true")
    task.add_argument(
        "--allow-domain",
        action="append",
        default=[],
        help="Additional allowed domain(s) for this task (repeatable).",
    )
    add_browser_runtime_flags(task)

    onboard = sub.add_parser("onboard", help="One-shot site onboarding: signup -> API key -> balance snapshot")
    onboard.add_argument("--site", choices=sorted(SITE_PRESETS.keys()))
    onboard.add_argument("--target-url")
    onboard.add_argument("--inbox")
    onboard.add_argument("--inbox-username")
    onboard.add_argument("--inbox-domain", default="agentmail.to")
    onboard.add_argument(
        "--fresh-inbox",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Attempt to create a new AgentMail inbox for each onboard run.",
    )
    onboard.add_argument(
        "--strict-fresh-inbox",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Require creating a brand-new inbox; fail instead of reusing an existing inbox on quota/username conflicts.",
    )
    onboard.add_argument("--password", default="")
    onboard.add_argument("--first-name", default="Demo")
    onboard.add_argument("--last-name", default="User")
    onboard.add_argument("--phone", default="+14155550123")
    onboard.add_argument(
        "--fresh-profile",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Create and use a brand-new Browser Use profile for each onboard run (default: reuse existing profile/session state).",
    )
    onboard.add_argument(
        "--profile-name",
        default="",
        help="Optional explicit name for a newly created profile when --fresh-profile is enabled.",
    )
    onboard.add_argument("--llm", default="browser-use-2.0")
    onboard.add_argument("--max-steps", type=int, default=80)
    onboard.add_argument("--timeout-seconds", type=int, default=600)
    onboard.add_argument("--poll-seconds", type=int, default=2)
    onboard.add_argument("--otp-wait-seconds", type=int, default=300)
    onboard.add_argument("--otp-regex", default=r"\b(\d{6})\b")
    onboard.add_argument("--resume-retries", type=int, default=3)
    onboard.add_argument("--retry-wait-seconds", type=int, default=3)
    onboard.add_argument(
        "--include-api-key-step",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Force API-key capture step on/off for onboard flow. Default follows site preset.",
    )
    onboard.add_argument(
        "--max-validation-variants",
        type=int,
        default=3,
        help="Maximum signup validation retry variants (phone format/omission) before stopping.",
    )
    onboard.add_argument(
        "--max-email-rotations",
        type=int,
        default=2,
        help="Maximum alternate existing inboxes to try after validation retries fail.",
    )
    onboard.add_argument(
        "--auto-human-checkpoint",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="When signup is blocked, pause for manual challenge solve in live session, then retry automatically.",
    )
    onboard.add_argument(
        "--human-checkpoint-seconds",
        type=int,
        default=180,
        help="How long to wait for manual challenge solve before retrying blocked signup.",
    )
    onboard.add_argument(
        "--open-live-url",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Auto-open Browser Use live session URL locally during blocked-signup recovery.",
    )
    onboard.add_argument(
        "--proxy-fallback-country",
        action="append",
        default=[],
        help="Proxy country fallback(s) to retry when signup is access-blocked (repeatable).",
    )
    onboard.add_argument(
        "--max-proxy-fallbacks",
        type=int,
        default=2,
        help="Maximum proxy-country fallback attempts for signup_access_blocked recovery.",
    )
    onboard.add_argument(
        "--fast-onboard",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use shorter read-only balance checks and avoid re-auth retries for lowest latency.",
    )
    onboard.add_argument(
        "--include-balance-snapshot",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run a short read-only account snapshot for plan/credit/cash balances.",
    )
    onboard.add_argument(
        "--super-fast",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Latency-first preset: fewer retries, shorter waits, and no manual-checkpoint pauses.",
    )
    onboard.add_argument("--dry-run", action="store_true")
    onboard.add_argument("--verbose", action="store_true")
    add_browser_runtime_flags(onboard)

    return parser


def main(argv: list[str] | None = None) -> int:
    _load_local_env()
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "inboxes":
            api_key = _require_env("AGENTMAIL_API_KEY")
            _emit(_list_inboxes(api_key, args.limit))
            return 0

        if args.command == "profiles":
            api_key = _require_env("BROWSER_USE_API_KEY")
            _emit(_list_profiles(api_key, args.limit))
            return 0

        if args.command == "create-profile":
            api_key = _require_env("BROWSER_USE_API_KEY")
            _emit(_create_profile(api_key, args.name))
            return 0

        if args.command == "session-info":
            api_key = _require_env("BROWSER_USE_API_KEY")
            payload = _get_session(api_key, args.session_id)
            if bool(args.open_live_url):
                payload["open_live_url_attempt"] = _open_live_url_locally(str(payload.get("live_url", "") or ""))
            _emit(payload)
            return 0

        if args.command == "open-live":
            payload = _open_live_session(args)
            _emit(payload)
            return 0 if bool(payload.get("ok", False)) else 1

        if args.command == "monitor-session":
            api_key = _require_env("BROWSER_USE_API_KEY")
            _emit(_monitor_session(api_key, args.session_id, args.limit, bool(args.include_output)))
            return 0

        if args.command == "scout-targets":
            _emit(_run_scout_targets(args))
            return 0

        if args.command == "latest-otp":
            api_key = _require_env("AGENTMAIL_API_KEY")
            payload = _latest_otp(api_key, args.inbox, args.otp_regex)
            _emit(payload)
            return 0 if payload.get("ok") else 1

        if args.command == "start":
            if not args.password:
                args.password = os.getenv("TRIALPILOT_PASSWORD", "").strip() or "DemoPass!123456789"
            args.resume_only = False
            cfg = _build_common_cfg(args)
            result = _run_demo_with_retries(cfg)
            _emit({"ok": True, "command": "start", "result": result, "config": {"target_url": cfg.target_url}})
            return 0

        if args.command == "resume":
            if not args.password:
                args.password = os.getenv("TRIALPILOT_PASSWORD", "").strip() or "DemoPass!123456789"
            args.pause_for_billing = False
            args.resume_only = True
            cfg = _build_common_cfg(args)
            result = _run_demo_with_retries(cfg)
            _emit({"ok": True, "command": "resume", "result": result, "config": {"session_id": cfg.session_id}})
            return 0

        if args.command == "task":
            result = _run_custom_task(
                target_url=args.target_url,
                instruction=args.instruction,
                session_id=args.session_id,
                llm=args.llm,
                max_steps=int(args.max_steps),
                timeout_seconds=int(args.timeout_seconds),
                poll_seconds=int(args.poll_seconds),
                dry_run=bool(args.dry_run),
                verbose=bool(args.verbose),
                additional_allowed_domains=list(args.allow_domain or []),
                browser_runtime=_resolve_browser_runtime_fields(args),
            )
            _emit(
                {
                    "ok": True,
                    "command": "task",
                    "session_id": result.get("session_id"),
                    "task_id": result.get("task_id"),
                    "status": result.get("status"),
                    "output": result.get("output"),
                }
            )
            return 0

        if args.command == "onboard":
            _apply_super_fast_onboard_args(args)
            onboard_retries = max(0, int(os.getenv("TRIALPILOT_ONBOARD_RETRIES", "1")))
            attempts = 1 + onboard_retries
            payload: dict[str, Any] | None = None
            for attempt in range(1, attempts + 1):
                try:
                    payload = _run_onboard(args)
                    break
                except Exception as exc:
                    if attempt >= attempts or not _looks_like_transient_run_demo_error(exc):
                        raise
                    time.sleep(min(2**attempt, 5))
            if payload is None:
                raise RuntimeError("onboard failed without result")
            _emit({"ok": bool(payload.get("ok", True)), "command": "onboard", "result": payload})
            return 0 if bool(payload.get("ok", True)) else 1

        _emit({"ok": False, "error": "unknown_command"})
        return 2
    except Exception as exc:
        _emit({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
