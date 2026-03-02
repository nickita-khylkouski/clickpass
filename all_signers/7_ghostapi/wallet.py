"""Identity wallet for AI agent signup automation.

Stores user credentials, payment info, and related identity data
used by Browser Use Agent for automated account creation.
"""
from __future__ import annotations

import json
import secrets
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict


@dataclass
class VirtualCard:
    """Virtual payment card details."""

    number: str = ""
    exp_month: str = ""
    exp_year: str = ""
    cvv: str = ""
    zip_code: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {
            "number": self.number,
            "exp_month": self.exp_month,
            "exp_year": self.exp_year,
            "cvv": self.cvv,
            "zip_code": self.zip_code,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> VirtualCard:
        return cls(
            number=data.get("number", ""),
            exp_month=data.get("exp_month", ""),
            exp_year=data.get("exp_year", ""),
            cvv=data.get("cvv", ""),
            zip_code=data.get("zip_code", ""),
        )


@dataclass
class IdentityWallet:
    """Full identity for agent-driven signups."""

    email: str = ""
    password: str = ""
    first_name: str = ""
    last_name: str = ""
    phone: str = ""
    card: VirtualCard = field(default_factory=VirtualCard)
    agentmail_inbox_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "email": self.email,
            "password": self.password,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "phone": self.phone,
            "card": self.card.to_dict(),
            "agentmail_inbox_id": self.agentmail_inbox_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> IdentityWallet:
        card_data = data.get("card", {})
        card = VirtualCard.from_dict(card_data) if isinstance(card_data, dict) else VirtualCard()
        return cls(
            email=data.get("email", ""),
            password=data.get("password", ""),
            first_name=data.get("first_name", ""),
            last_name=data.get("last_name", ""),
            phone=data.get("phone", ""),
            card=card,
            agentmail_inbox_id=data.get("agentmail_inbox_id", ""),
        )

    def to_sensitive_data(self, domain: str) -> Dict[str, Dict[str, str]]:
        """Return sensitive data in Browser Use Agent format.

        Format: {domain: {"x_email": ..., "x_password": ..., ...}}
        """
        return {
            domain: {
                "x_email": self.email,
                "x_password": self.password,
                "x_first_name": self.first_name,
                "x_last_name": self.last_name,
                "x_phone": self.phone,
                "x_card_number": self.card.number,
                "x_card_exp_month": self.card.exp_month,
                "x_card_exp_year": self.card.exp_year,
                "x_card_cvv": self.card.cvv,
                "x_card_zip_code": self.card.zip_code,
            }
        }


def generate_password(length: int = 16) -> str:
    """Generate a cryptographically secure random password."""
    alphabet = string.ascii_letters + string.digits + string.punctuation
    # Ensure at least one of each required category
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.isupper() for c in pw)
            and any(c.islower() for c in pw)
            and any(c.isdigit() for c in pw)
        ):
            return pw


def default_wallet_path() -> Path:
    """Return default wallet storage path: ~/.ghostapi/identity_wallet.json"""
    return Path.home() / ".ghostapi" / "identity_wallet.json"


def load_wallet(path: Path | None = None) -> IdentityWallet:
    """Load wallet from JSON file. Returns empty wallet if file doesn't exist."""
    p = path or default_wallet_path()
    if not p.exists():
        return IdentityWallet()
    data = json.loads(p.read_text())
    return IdentityWallet.from_dict(data)


def save_wallet(wallet: IdentityWallet, path: Path | None = None) -> Path:
    """Save wallet to JSON file. Creates parent directories as needed."""
    p = path or default_wallet_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(wallet.to_dict(), indent=2))
    return p
