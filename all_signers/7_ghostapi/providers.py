from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderStatus:
    browseruse_api_key: bool
    openai_api_key: bool
    anthropic_api_key: bool

    def to_dict(self) -> dict[str, bool]:
        return {
            "browseruse_api_key": self.browseruse_api_key,
            "openai_api_key": self.openai_api_key,
            "anthropic_api_key": self.anthropic_api_key,
        }


def get_provider_status() -> ProviderStatus:
    return ProviderStatus(
        browseruse_api_key=bool(os.getenv("BROWSERUSE_API_KEY", "").strip()),
        openai_api_key=bool(os.getenv("OPENAI_API_KEY", "").strip()),
        anthropic_api_key=bool(os.getenv("ANTHROPIC_API_KEY", "").strip()),
    )
