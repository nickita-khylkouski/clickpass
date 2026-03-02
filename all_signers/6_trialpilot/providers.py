"""Provider interfaces and local provider implementations for TrialPilot.

The local providers in this module are safe defaults for development and
integration testing. They do not make live network calls and explicitly mark
their behavior as mocked.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
import time
from typing import Any, Iterator, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _env_bool(
    name: str,
    *,
    default: bool = False,
    environ: Mapping[str, str] | None = None,
) -> bool:
    env = environ if environ is not None else os.environ
    raw = str(env.get(name, "")).strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _env_first(env: Mapping[str, str], *names: str, default: str = "") -> str:
    for name in names:
        value = str(env.get(name, "")).strip()
        if value:
            return value
    return default


@dataclass(frozen=True)
class ProviderRuntimeConfig:
    """Provider runtime configuration derived from env vars."""

    browseruse_api_key: str | None
    openai_api_key: str | None
    dry_run: bool
    browseruse_api_base: str = "https://api.browser-use.com"
    browseruse_poll_seconds: float = 2.0
    browseruse_timeout_seconds: float = 90.0
    browseruse_max_steps: int = 25
    browseruse_profile_id: str | None = None
    agentmail_api_key: str | None = None
    agentmail_base_url: str = "https://api.agentmail.to"
    agentmail_inbox_id: str | None = None
    agentmail_poll_seconds: float = 2.0
    agentmail_timeout_seconds: float = 120.0

    @property
    def browseruse_key_configured(self) -> bool:
        return bool((self.browseruse_api_key or "").strip())

    @property
    def openai_key_configured(self) -> bool:
        return bool((self.openai_api_key or "").strip())

    @property
    def agentmail_key_configured(self) -> bool:
        return bool((self.agentmail_api_key or "").strip())

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ProviderRuntimeConfig":
        env = environ if environ is not None else os.environ
        browseruse_api_key = _env_first(env, "BROWSER_USE_API_KEY", "BROWSERUSE_API_KEY") or None
        openai_api_key = _env_first(env, "OPENAI_API_KEY") or None
        agentmail_api_key = _env_first(env, "AGENTMAIL_API_KEY") or None
        dry_run = _env_bool("TRIALPILOT_DRY_RUN", environ=env, default=_env_bool("DRY_RUN", environ=env))
        browseruse_api_base = _env_first(env, "BROWSER_USE_API_BASE", "BROWSERUSE_API_BASE", default="https://api.browser-use.com")
        poll_seconds = float(_env_first(env, "BROWSER_USE_POLL_SECONDS", "BROWSERUSE_POLL_SECONDS", default="2.0"))
        timeout_seconds = float(_env_first(env, "BROWSER_USE_TIMEOUT_SECONDS", "BROWSERUSE_TIMEOUT_SECONDS", default="90.0"))
        max_steps = int(_env_first(env, "BROWSER_USE_MAX_STEPS", "BROWSERUSE_MAX_STEPS", default="25"))
        profile_id = _env_first(env, "BROWSER_USE_PROFILE_ID", "BROWSERUSE_PROFILE_ID") or None
        agentmail_base_url = _env_first(env, "AGENTMAIL_API_BASE", default="https://api.agentmail.to")
        agentmail_inbox_id = str(env.get("AGENTMAIL_INBOX_ID", "")).strip() or None
        agentmail_poll_seconds = float(env.get("AGENTMAIL_POLL_SECONDS", "2.0"))
        agentmail_timeout_seconds = float(env.get("AGENTMAIL_TIMEOUT_SECONDS", "120.0"))
        return cls(
            browseruse_api_key=browseruse_api_key,
            openai_api_key=openai_api_key,
            dry_run=dry_run,
            browseruse_api_base=browseruse_api_base,
            browseruse_poll_seconds=poll_seconds,
            browseruse_timeout_seconds=timeout_seconds,
            browseruse_max_steps=max_steps,
            browseruse_profile_id=profile_id,
            agentmail_api_key=agentmail_api_key,
            agentmail_base_url=agentmail_base_url,
            agentmail_inbox_id=agentmail_inbox_id,
            agentmail_poll_seconds=agentmail_poll_seconds,
            agentmail_timeout_seconds=agentmail_timeout_seconds,
        )


@dataclass(frozen=True)
class ProviderStatus:
    """Public provider status payload for diagnostics and health checks."""

    browseruse_api_key: bool
    openai_api_key: bool
    dry_run: bool

    @property
    def ready_for_browseruse(self) -> bool:
        return self.browseruse_api_key and not self.dry_run

    def to_dict(self) -> dict[str, bool]:
        return {
            "browseruse_api_key": self.browseruse_api_key,
            "openai_api_key": self.openai_api_key,
            "dry_run": self.dry_run,
            "ready_for_browseruse": self.ready_for_browseruse,
        }


def get_provider_status(environ: Mapping[str, str] | None = None) -> ProviderStatus:
    cfg = ProviderRuntimeConfig.from_env(environ)
    return ProviderStatus(
        browseruse_api_key=cfg.browseruse_key_configured,
        openai_api_key=cfg.openai_key_configured,
        dry_run=cfg.dry_run,
    )


class BrowserUseClient(Protocol):
    """Interface for BrowserUse-backed automation."""

    def run_task(
        self,
        *,
        instruction: str,
        url: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute an automation task and return a structured result."""


class CardProvider(Protocol):
    """Interface for card tokenization/payment method setup."""

    def tokenize_card(
        self,
        *,
        card_number: str,
        exp_month: int | str,
        exp_year: int | str,
        cvc: str,
        cardholder_name: str | None = None,
    ) -> dict[str, Any]:
        """Tokenize card details and return a provider token."""


class VerificationProvider(Protocol):
    """Interface for user verification workflows."""

    def send_code(self, *, destination: str, channel: str = "email") -> dict[str, Any]:
        """Issue a one-time verification code."""

    def verify_code(self, *, destination: str, code: str) -> dict[str, Any]:
        """Validate a previously issued verification code."""


class LocalBrowserUseClientStub:
    """BrowserUse client stub.

    This implementation is intentionally mocked for now and does not perform
    network calls.
    """

    def __init__(self, *, config: ProviderRuntimeConfig | None = None) -> None:
        self.config = config or ProviderRuntimeConfig.from_env()

    def run_task(
        self,
        *,
        instruction: str,
        url: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        task_input = f"{instruction}|{url or ''}|{context or {}}|{self.config.dry_run}"
        return {
            "ok": True,
            "provider": "browseruse",
            "mock": True,
            "dry_run": self.config.dry_run,
            "task_id": f"bru_mock_{_digest(task_input)}",
            "instruction": instruction,
            "url": url,
            "context": dict(context or {}),
            "message": (
                "MOCK BEHAVIOR: local BrowserUse stub is active; no external BrowserUse/OpenAI call was made."
            ),
            "credentials_configured": {
                "browseruse_api_key": self.config.browseruse_key_configured,
                "openai_api_key": self.config.openai_key_configured,
            },
        }


class LocalCardProvider:
    """Local card provider stub with deterministic token generation."""

    def __init__(self, *, dry_run: bool = True) -> None:
        self.dry_run = dry_run

    def tokenize_card(
        self,
        *,
        card_number: str,
        exp_month: int | str,
        exp_year: int | str,
        cvc: str,
        cardholder_name: str | None = None,
    ) -> dict[str, Any]:
        digits = "".join(ch for ch in str(card_number) if ch.isdigit())
        if len(digits) < 12 or len(digits) > 19:
            return {
                "ok": False,
                "provider": "card",
                "mock": True,
                "dry_run": self.dry_run,
                "message": "MOCK BEHAVIOR: local card provider rejected invalid card format.",
                "error": {"code": "invalid_card_number"},
            }

        last4 = digits[-4:]
        brand = _infer_card_brand(digits)
        token_source = f"{digits}|{exp_month}|{exp_year}|{cvc}|{cardholder_name or ''}"
        token = f"card_tok_{_digest(token_source)}"
        return {
            "ok": True,
            "provider": "card",
            "mock": True,
            "dry_run": self.dry_run,
            "token": token,
            "last4": last4,
            "brand": brand,
            "message": "MOCK BEHAVIOR: token generated locally; no payment processor was contacted.",
        }


class LocalVerificationProvider:
    """Local verification provider stub."""

    def __init__(self, *, dry_run: bool = True, fixed_code: str = "000000") -> None:
        self.dry_run = dry_run
        self.fixed_code = fixed_code
        self._codes: dict[str, str] = {}

    def send_code(self, *, destination: str, channel: str = "email") -> dict[str, Any]:
        destination_key = destination.strip().lower()
        if not destination_key:
            return {
                "ok": False,
                "provider": "verification",
                "mock": True,
                "dry_run": self.dry_run,
                "message": "MOCK BEHAVIOR: destination was empty.",
                "error": {"code": "invalid_destination"},
            }

        # Keep verification deterministic for local workflows.
        code = self.fixed_code
        self._codes[destination_key] = code
        return {
            "ok": True,
            "provider": "verification",
            "mock": True,
            "dry_run": self.dry_run,
            "channel": channel,
            "destination_masked": _mask_destination(destination_key),
            "message": "MOCK BEHAVIOR: verification code generated locally; no SMS/email provider was contacted.",
            "mock_code": code,
        }

    def verify_code(self, *, destination: str, code: str) -> dict[str, Any]:
        destination_key = destination.strip().lower()
        expected = self._codes.get(destination_key, self.fixed_code if self.dry_run else "")
        verified = bool(expected and code == expected)
        return {
            "ok": verified,
            "provider": "verification",
            "mock": True,
            "dry_run": self.dry_run,
            "destination_masked": _mask_destination(destination_key),
            "message": "MOCK BEHAVIOR: verification result produced locally from in-memory state.",
            "verified": verified,
        }


class AgentMailVerificationProvider:
    """Verification provider backed by AgentMail inbox polling."""

    OTP_PATTERN = re.compile(r"\b(\d{4,8})\b")
    OTP_6_PATTERN = re.compile(r"\b(\d{6})\b")
    IGNORE_CODES = {"000000", "111111", "222222", "123456", "654321"}

    def __init__(self, *, config: ProviderRuntimeConfig | None = None) -> None:
        self.config = config or ProviderRuntimeConfig.from_env()
        if not self.config.agentmail_key_configured:
            raise ValueError("AGENTMAIL_API_KEY is required for AgentMailVerificationProvider")
        self._last_code_by_destination: dict[str, str] = {}

    def send_code(self, *, destination: str, channel: str = "email") -> dict[str, Any]:
        # AgentMail receives codes from third-party providers; we do not "send" here.
        return {
            "ok": True,
            "provider": "agentmail",
            "mock": False,
            "dry_run": self.config.dry_run,
            "channel": channel,
            "destination_masked": _mask_destination(destination),
            "message": "AgentMail provider is active; waiting for inbound verification email.",
        }

    def verify_code(self, *, destination: str, code: str) -> dict[str, Any]:
        expected = self._last_code_by_destination.get(destination.strip().lower(), "")
        verified = bool(expected and code == expected)
        return {
            "ok": verified,
            "provider": "agentmail",
            "mock": False,
            "dry_run": self.config.dry_run,
            "destination_masked": _mask_destination(destination),
            "verified": verified,
            "message": "Compared supplied OTP with latest AgentMail OTP.",
        }

    def ensure_inbox(self) -> dict[str, Any]:
        if self.config.agentmail_inbox_id:
            inbox = self._get_inbox(self.config.agentmail_inbox_id)
            if inbox.get("ok"):
                return inbox
        created = self._create_inbox()
        if not created.get("ok") and "limit exceeded" in str(created.get("error", "")).lower():
            listed = self._list_inboxes()
            if listed.get("ok") and listed.get("inboxes"):
                first = listed["inboxes"][0]
                inbox_id = str(first.get("inbox_id") or first.get("id") or "")
                if inbox_id:
                    fetched = self._get_inbox(inbox_id)
                    if fetched.get("ok"):
                        return fetched
        return created

    def fetch_latest_otp(
        self,
        *,
        destination: str,
        provider_hint: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        inbox = self.ensure_inbox()
        if not inbox.get("ok"):
            return inbox
        inbox_id = str(inbox.get("inbox_id") or "")
        if not inbox_id:
            return {"ok": False, "provider": "agentmail", "error": "missing_inbox_id"}

        deadline = time.time() + (timeout_seconds if timeout_seconds is not None else self.config.agentmail_timeout_seconds)
        while time.time() < deadline:
            messages = self._list_messages(inbox_id=inbox_id)
            if messages.get("ok"):
                code = self._extract_otp_from_messages(messages.get("messages", []), provider_hint=provider_hint)
                if code:
                    key = destination.strip().lower()
                    self._last_code_by_destination[key] = code
                    return {
                        "ok": True,
                        "provider": "agentmail",
                        "inbox_id": inbox_id,
                        "otp": code,
                        "message_count": len(messages.get("messages", [])),
                    }
            time.sleep(max(0.2, self.config.agentmail_poll_seconds))

        return {
            "ok": False,
            "provider": "agentmail",
            "inbox_id": inbox_id,
            "error": "otp_timeout",
            "message": "Timed out waiting for OTP email.",
        }

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.agentmail_api_key or ''}"}

    def _create_inbox(self) -> dict[str, Any]:
        url = f"{self.config.agentmail_base_url.rstrip('/')}/v0/inboxes"
        try:
            data = _http_json(
                method="POST",
                url=url,
                payload={"display_name": "TrialPilot OTP Inbox"},
                headers=self._auth_headers(),
            )
        except Exception as exc:
            return {"ok": False, "provider": "agentmail", "error": f"{type(exc).__name__}: {exc}"}
        inbox = _pick_inbox_payload(data)
        if not inbox:
            return {"ok": False, "provider": "agentmail", "error": f"unexpected_response:{data}"}
        inbox_id = str(inbox.get("inbox_id") or inbox.get("id") or "")
        return {
            "ok": True,
            "provider": "agentmail",
            "inbox_id": inbox_id,
            "email": inbox_id,
            "raw": inbox,
        }

    def _get_inbox(self, inbox_id: str) -> dict[str, Any]:
        url = f"{self.config.agentmail_base_url.rstrip('/')}/v0/inboxes/{inbox_id}"
        try:
            data = _http_json(method="GET", url=url, headers=self._auth_headers())
        except Exception as exc:
            return {"ok": False, "provider": "agentmail", "error": f"{type(exc).__name__}: {exc}"}
        inbox = _pick_inbox_payload(data) or (data if isinstance(data, Mapping) else {})
        if not inbox:
            return {"ok": False, "provider": "agentmail", "error": f"unexpected_response:{data}"}
        return {
            "ok": True,
            "provider": "agentmail",
            "inbox_id": str(inbox.get("inbox_id", inbox_id)),
            "email": str(inbox.get("inbox_id", inbox_id)),
            "raw": inbox,
        }

    def _list_inboxes(self) -> dict[str, Any]:
        url = f"{self.config.agentmail_base_url.rstrip('/')}/v0/inboxes"
        try:
            data = _http_json(method="GET", url=url, headers=self._auth_headers())
        except Exception as exc:
            return {"ok": False, "provider": "agentmail", "error": f"{type(exc).__name__}: {exc}"}
        inboxes = _pick_inbox_list(data)
        return {"ok": True, "provider": "agentmail", "inboxes": inboxes}

    def _list_messages(self, *, inbox_id: str) -> dict[str, Any]:
        inbox_enc = quote(inbox_id, safe="")
        url = f"{self.config.agentmail_base_url.rstrip('/')}/v0/inboxes/{inbox_enc}/messages"
        try:
            data = _http_json(method="GET", url=url, headers=self._auth_headers())
        except Exception as exc:
            return {"ok": False, "provider": "agentmail", "error": f"{type(exc).__name__}: {exc}"}
        messages = _pick_message_list(data)
        enriched: list[dict[str, Any]] = []
        for message in messages:
            message_id = str(message.get("message_id") or "").strip()
            if not message_id:
                enriched.append(message)
                continue
            detail = self._get_message_detail(inbox_id=inbox_id, message_id=message_id)
            if detail.get("ok") and isinstance(detail.get("message"), Mapping):
                merged = dict(message)
                merged.update(dict(detail["message"]))
                enriched.append(merged)
            else:
                enriched.append(message)
        return {"ok": True, "provider": "agentmail", "messages": enriched}

    def _get_message_detail(self, *, inbox_id: str, message_id: str) -> dict[str, Any]:
        inbox_enc = quote(inbox_id, safe="")
        msg_enc = quote(message_id, safe="")
        url = f"{self.config.agentmail_base_url.rstrip('/')}/v0/inboxes/{inbox_enc}/messages/{msg_enc}"
        try:
            data = _http_json(method="GET", url=url, headers=self._auth_headers())
        except Exception as exc:
            return {"ok": False, "provider": "agentmail", "error": f"{type(exc).__name__}: {exc}"}
        if isinstance(data, Mapping):
            return {"ok": True, "provider": "agentmail", "message": dict(data)}
        return {"ok": False, "provider": "agentmail", "error": "unexpected_message_detail_response"}

    def _extract_otp_from_messages(self, messages: list[dict[str, Any]], provider_hint: str | None) -> str:
        hint = (provider_hint or "").strip().lower()
        candidates: list[dict[str, Any]] = []
        for message in messages:
            sender = str(message.get("from") or message.get("sender") or "").lower()
            subject = str(message.get("subject") or "").lower()
            body = str(message.get("text") or message.get("body") or message.get("html") or "")
            if hint and hint not in sender and hint not in subject and hint not in body.lower():
                continue
            candidates.append(message)

        ordered = candidates if candidates else messages
        for message in ordered:
            body = " ".join(
                [
                    str(message.get("subject") or ""),
                    str(message.get("text") or ""),
                    str(message.get("body") or ""),
                    str(message.get("html") or ""),
                    str(message.get("extracted_html") or ""),
                    str(message.get("snippet") or ""),
                ]
            )
            six_digit = [m.group(1) for m in self.OTP_6_PATTERN.finditer(body)]
            for code in six_digit:
                if code not in self.IGNORE_CODES:
                    return code
            generic = [m.group(1) for m in self.OTP_PATTERN.finditer(body)]
            for code in generic:
                if code not in self.IGNORE_CODES:
                    return code
        return ""


class LocalProviderRegistry(Mapping[str, object]):
    """Mapping-style provider registry used by local trial flows."""

    def __init__(self, providers: Mapping[str, object]) -> None:
        self._providers = dict(providers)

    def __getitem__(self, key: str) -> object:
        return self._providers[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._providers)

    def __len__(self) -> int:
        return len(self._providers)

    def get(self, key: str, default: object | None = None) -> object | None:
        return self._providers.get(key, default)


def build_local_providers(
    *,
    config: ProviderRuntimeConfig | None = None,
    environ: Mapping[str, str] | None = None,
) -> LocalProviderRegistry:
    cfg = config or ProviderRuntimeConfig.from_env(environ)
    browseruse_client: BrowserUseClient
    if cfg.dry_run or not cfg.browseruse_key_configured:
        browseruse_client = LocalBrowserUseClientStub(config=cfg)
    else:
        browseruse_client = BrowserUseCloudClient(config=cfg)
    card_provider = LocalCardProvider(dry_run=cfg.dry_run)
    if cfg.agentmail_key_configured and not cfg.dry_run:
        verification_provider = AgentMailVerificationProvider(config=cfg)
    else:
        verification_provider = LocalVerificationProvider(dry_run=cfg.dry_run)
    return LocalProviderRegistry(
        {
            "browseruse": browseruse_client,
            "card": card_provider,
            "billing": card_provider,
            "verification": verification_provider,
        }
    )


def _deterministic_otp(seed: str) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    value = int(digest[:12], 16) % 1_000_000
    return f"{value:06d}"


def _mask_destination(destination: str) -> str:
    if not destination:
        return ""
    if "@" in destination:
        local, _, domain = destination.partition("@")
        if len(local) <= 2:
            local_masked = "*" * len(local)
        else:
            local_masked = local[0] + ("*" * (len(local) - 2)) + local[-1]
        return f"{local_masked}@{domain}"
    digits = "".join(ch for ch in destination if ch.isdigit())
    if digits:
        return f"***{digits[-2:]}"
    if len(destination) <= 4:
        return "*" * len(destination)
    return ("*" * (len(destination) - 4)) + destination[-4:]


def _infer_card_brand(card_number_digits: str) -> str:
    if card_number_digits.startswith("4"):
        return "visa"
    if card_number_digits[:2] in {"51", "52", "53", "54", "55"}:
        return "mastercard"
    if card_number_digits[:2] in {"34", "37"}:
        return "amex"
    if card_number_digits.startswith("6"):
        return "discover"
    return "unknown"


class BrowserUseCloudClient:
    """Live Browser Use cloud adapter via HTTP API.

    This client uses v2 task endpoints first and falls back to v1 endpoints.
    """

    def __init__(self, *, config: ProviderRuntimeConfig | None = None) -> None:
        self.config = config or ProviderRuntimeConfig.from_env()
        if not self.config.browseruse_key_configured:
            raise ValueError("BROWSERUSE_API_KEY is required for BrowserUseCloudClient")

    def run_task(
        self,
        *,
        instruction: str,
        url: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            return self._run_task_v2(instruction=instruction, url=url, context=context)
        except Exception as v2_exc:
            try:
                v1 = self._run_task_v1(instruction=instruction, url=url, context=context)
                v1["fallback"] = "v1"
                v1["v2_error"] = f"{type(v2_exc).__name__}: {v2_exc}"
                return v1
            except Exception as v1_exc:
                return {
                    "ok": False,
                    "provider": "browseruse",
                    "mock": False,
                    "dry_run": self.config.dry_run,
                    "message": "Browser Use live task failed in v2 and v1 modes.",
                    "error": {
                        "v2": f"{type(v2_exc).__name__}: {v2_exc}",
                        "v1": f"{type(v1_exc).__name__}: {v1_exc}",
                    },
                }

    def _run_task_v2(
        self,
        *,
        instruction: str,
        url: str | None,
        context: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        endpoint = f"{self.config.browseruse_api_base.rstrip('/')}/api/v2/tasks"
        full_task = instruction if not url else f"{instruction} Start at: {url}"
        browser_options: dict[str, Any] = {}
        forwarded_context: dict[str, Any] = {}
        if context:
            for key, value in dict(context).items():
                if key == "_browseruse" and isinstance(value, Mapping):
                    browser_options = dict(value)
                else:
                    forwarded_context[key] = value
        payload: dict[str, Any] = {
            "task": full_task,
            "maxSteps": max(5, self.config.browseruse_max_steps),
            "saveBrowserData": True,
        }
        if forwarded_context:
            payload["context"] = forwarded_context
        if "sessionId" in browser_options:
            payload["sessionId"] = browser_options["sessionId"]
        elif "session_id" in browser_options:
            payload["sessionId"] = browser_options["session_id"]
        if "maxSteps" in browser_options:
            payload["maxSteps"] = int(browser_options["maxSteps"])
        if "saveBrowserData" in browser_options:
            payload["saveBrowserData"] = bool(browser_options["saveBrowserData"])
        if "browserProfileId" in browser_options:
            payload["browserProfileId"] = browser_options["browserProfileId"]
        elif self.config.browseruse_profile_id:
            payload["browserProfileId"] = self.config.browseruse_profile_id
        create = _http_json(
            method="POST",
            url=endpoint,
            payload=payload,
            headers={"x-browser-use-api-key": self.config.browseruse_api_key or ""},
        )
        task_id = str(create.get("id") or create.get("taskId") or create.get("task_id") or "")
        session_id = str(create.get("sessionId") or create.get("session_id") or "")
        if not task_id:
            raise RuntimeError(f"v2_create_missing_task_id: {create}")
        return self._poll_v2_task(task_id=task_id, session_id=session_id)

    def _poll_v2_task(self, *, task_id: str, session_id: str) -> dict[str, Any]:
        deadline = time.time() + max(5.0, self.config.browseruse_timeout_seconds)
        task_url = f"{self.config.browseruse_api_base.rstrip('/')}/api/v2/tasks/{task_id}"
        session_url = (
            f"{self.config.browseruse_api_base.rstrip('/')}/api/v2/sessions/{session_id}"
            if session_id
            else ""
        )
        last_payload: dict[str, Any] = {}
        while time.time() < deadline:
            try:
                payload = _http_json(
                    method="GET",
                    url=task_url,
                    headers={"x-browser-use-api-key": self.config.browseruse_api_key or ""},
                )
                last_payload = payload
            except Exception:
                if session_url:
                    payload = _http_json(
                        method="GET",
                        url=session_url,
                        headers={"x-browser-use-api-key": self.config.browseruse_api_key or ""},
                    )
                    last_payload = payload
                else:
                    raise

            status = _extract_status(last_payload)
            if status in {"completed", "complete", "success", "succeeded", "done", "finished"}:
                return {
                    "ok": True,
                    "provider": "browseruse",
                    "mock": False,
                    "dry_run": self.config.dry_run,
                    "task_id": task_id,
                    "session_id": session_id,
                    "status": status,
                    "raw": last_payload,
                    "message": "Browser Use task completed.",
                }
            if status in {"failed", "error", "cancelled", "canceled"}:
                return {
                    "ok": False,
                    "provider": "browseruse",
                    "mock": False,
                    "dry_run": self.config.dry_run,
                    "task_id": task_id,
                    "session_id": session_id,
                    "status": status,
                    "raw": last_payload,
                    "message": "Browser Use task reported failure.",
                }
            time.sleep(max(0.2, self.config.browseruse_poll_seconds))

        return {
            "ok": False,
            "provider": "browseruse",
            "mock": False,
            "dry_run": self.config.dry_run,
            "task_id": task_id,
            "session_id": session_id,
            "status": _extract_status(last_payload) or "timeout",
            "raw": last_payload,
            "message": "Browser Use task polling timed out.",
        }

    def _run_task_v1(
        self,
        *,
        instruction: str,
        url: str | None,
        context: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        base = self.config.browseruse_api_base.rstrip("/")
        run_url = f"{base}/api/v1/run-task"
        full_task = instruction if not url else f"{instruction} Start at: {url}"
        payload: dict[str, Any] = {"task": full_task}
        if context:
            payload["context"] = dict(context)
        create = _http_json(
            method="POST",
            url=run_url,
            payload=payload,
            headers={"Authorization": f"Bearer {self.config.browseruse_api_key or ''}"},
        )
        task_id = str(create.get("id") or create.get("taskId") or create.get("task_id") or "")
        if not task_id:
            raise RuntimeError(f"v1_create_missing_task_id: {create}")

        deadline = time.time() + max(5.0, self.config.browseruse_timeout_seconds)
        get_url = f"{base}/api/v1/task/{task_id}"
        last_payload: dict[str, Any] = {}
        while time.time() < deadline:
            last_payload = _http_json(
                method="GET",
                url=get_url,
                headers={"Authorization": f"Bearer {self.config.browseruse_api_key or ''}"},
            )
            status = _extract_status(last_payload)
            if status in {"completed", "complete", "success", "succeeded", "done", "finished"}:
                return {
                    "ok": True,
                    "provider": "browseruse",
                    "mock": False,
                    "dry_run": self.config.dry_run,
                    "task_id": task_id,
                    "status": status,
                    "raw": last_payload,
                    "message": "Browser Use task completed.",
                }
            if status in {"failed", "error", "cancelled", "canceled"}:
                return {
                    "ok": False,
                    "provider": "browseruse",
                    "mock": False,
                    "dry_run": self.config.dry_run,
                    "task_id": task_id,
                    "status": status,
                    "raw": last_payload,
                    "message": "Browser Use task reported failure.",
                }
            time.sleep(max(0.2, self.config.browseruse_poll_seconds))

        return {
            "ok": False,
            "provider": "browseruse",
            "mock": False,
            "dry_run": self.config.dry_run,
            "task_id": task_id,
            "status": _extract_status(last_payload) or "timeout",
            "raw": last_payload,
            "message": "Browser Use task polling timed out.",
        }


def _http_json(
    *,
    method: str,
    url: str,
    payload: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    final_headers = {"content-type": "application/json", "accept": "application/json"}
    if headers:
        final_headers.update(headers)

    body = None
    if payload is not None:
        body = json.dumps(dict(payload)).encode("utf-8")

    req = Request(url=url, method=method.upper(), headers=final_headers, data=body)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        raise RuntimeError(f"http_{exc.code} {exc.reason}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"url_error: {exc}") from exc


def _extract_status(payload: Mapping[str, Any]) -> str:
    # Common shapes from task APIs.
    direct = str(payload.get("status", "")).strip().lower()
    if direct:
        return direct

    task = payload.get("task")
    if isinstance(task, Mapping):
        nested = str(task.get("status", "")).strip().lower()
        if nested:
            return nested

    tasks = payload.get("tasks")
    if isinstance(tasks, list) and tasks:
        latest = tasks[-1]
        if isinstance(latest, Mapping):
            nested = str(latest.get("status", "")).strip().lower()
            if nested:
                return nested
    return ""


def _pick_inbox_payload(data: Any) -> Mapping[str, Any] | None:
    if isinstance(data, Mapping):
        if "inbox_id" in data or ("id" in data and "email" in data):
            return data
        for key in ("inbox", "data", "result"):
            value = data.get(key)
            if isinstance(value, Mapping) and ("inbox_id" in value or "id" in value):
                return value
    return None


def _pick_message_list(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [dict(x) for x in data if isinstance(x, Mapping)]
    if isinstance(data, Mapping):
        for key in ("messages", "data", "items", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return [dict(x) for x in value if isinstance(x, Mapping)]
    return []


def _pick_inbox_list(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [dict(x) for x in data if isinstance(x, Mapping)]
    if isinstance(data, Mapping):
        for key in ("inboxes", "data", "items", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return [dict(x) for x in value if isinstance(x, Mapping)]
    return []


__all__ = [
    "AgentMailVerificationProvider",
    "BrowserUseCloudClient",
    "BrowserUseClient",
    "CardProvider",
    "VerificationProvider",
    "LocalBrowserUseClientStub",
    "LocalCardProvider",
    "LocalVerificationProvider",
    "LocalProviderRegistry",
    "ProviderRuntimeConfig",
    "ProviderStatus",
    "build_local_providers",
    "get_provider_status",
]
