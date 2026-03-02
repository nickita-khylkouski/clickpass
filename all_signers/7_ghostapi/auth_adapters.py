from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Protocol

from .auth import latest_imessage_code


@dataclass
class OTPResolution:
    code: str
    source: str
    confidence: float


class OTPAdapter(Protocol):
    name: str

    async def poll_code(self, *, phone: str | None, email: str | None) -> OTPResolution | None:
        ...


class SMSIMessageAdapter:
    name = "sms_imessage"

    async def poll_code(self, *, phone: str | None, email: str | None) -> OTPResolution | None:
        _ = email
        if not phone:
            return None
        code = latest_imessage_code(expected_phone=phone)
        if not code:
            return None
        return OTPResolution(code=code, source=self.name, confidence=0.82)


class TOTPAdapter:
    name = "totp"

    def __init__(self, secret: str | None):
        self.secret = secret

    async def poll_code(self, *, phone: str | None, email: str | None) -> OTPResolution | None:
        _ = (phone, email)
        if not self.secret:
            return None
        try:
            import pyotp  # type: ignore
        except Exception:
            return None
        code = pyotp.TOTP(self.secret).now()
        return OTPResolution(code=code, source=self.name, confidence=0.95)


class AgentmailAdapter:
    name = "agentmail_email"

    def __init__(self) -> None:
        self.inbox_id = os.getenv("GHOSTAPI_AGENTMAIL_INBOX_ID", "").strip()

    async def poll_code(self, *, phone: str | None, email: str | None) -> OTPResolution | None:
        _ = phone
        if not self.inbox_id or not email:
            return None
        try:
            from agentmail import AgentMail  # type: ignore
        except Exception:
            return None
        client = AgentMail()
        try:
            messages = client.inboxes.messages.list(inbox_id=self.inbox_id)
        except Exception:
            return None
        for msg in messages:
            text = f"{getattr(msg, 'subject', '')} {getattr(msg, 'text', '')}"
            for token in text.split():
                stripped = token.strip()
                if stripped.isdigit() and 4 <= len(stripped) <= 8:
                    return OTPResolution(code=stripped, source=self.name, confidence=0.72)
        return None


async def resolve_otp_with_adapters(
    *,
    phone: str | None,
    email: str | None,
    totp_secret: str | None,
    timeout_seconds: int = 30,
) -> OTPResolution | None:
    adapters: list[OTPAdapter] = [
        TOTPAdapter(secret=totp_secret),
        SMSIMessageAdapter(),
        AgentmailAdapter(),
    ]
    start = time.time()
    while time.time() - start < timeout_seconds:
        for adapter in adapters:
            result = await adapter.poll_code(phone=phone, email=email)
            if result and result.code:
                return result
        await asyncio.sleep(1.4)
    return None

