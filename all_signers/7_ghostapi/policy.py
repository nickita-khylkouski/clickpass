from __future__ import annotations

import json
import os
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class PolicyConfig:
    allowed_domains: tuple[str, ...]

    @classmethod
    def from_sources(cls, policy_file: str | None = None) -> "PolicyConfig":
        domains: list[str] = []
        env_domains = os.getenv("GHOSTAPI_ALLOWED_DOMAINS", "").strip()
        if env_domains:
            domains.extend([d.strip() for d in env_domains.split(",") if d.strip()])
        if policy_file:
            payload = json.loads(Path(policy_file).read_text())
            file_domains = payload.get("allowed_domains", [])
            if isinstance(file_domains, list):
                domains.extend([str(d).strip() for d in file_domains if str(d).strip()])
        return cls(allowed_domains=tuple(dict.fromkeys(domains)))


def extract_host(target_url: str) -> str:
    parsed = urlparse(target_url)
    host = parsed.hostname or ""
    if not host and parsed.path and "." in parsed.path and " " not in parsed.path:
        host = parsed.path
    if not host:
        raise ValueError(f"Invalid target URL: {target_url}")
    return host.lower()


def _host_allowed(host: str, allowed_domains: tuple[str, ...]) -> bool:
    if not allowed_domains:
        return True
    for pattern in allowed_domains:
        pat = pattern.lower()
        if pat.startswith("*."):
            if host.endswith(pat[1:]) or host == pat[2:]:
                return True
        if fnmatch(host, pat):
            return True
    return False


def enforce_authorized_target(
    target_url: str,
    confirm_authorized: bool,
    policy_file: str | None = None,
) -> str:
    if not confirm_authorized:
        raise ValueError(
            "Refusing to run without explicit authorization. "
            "Pass --confirm-authorized only for systems you own or have permission to assess."
        )
    host = extract_host(target_url)
    config = PolicyConfig.from_sources(policy_file=policy_file)
    if not _host_allowed(host, config.allowed_domains):
        raise ValueError(
            f"Target host '{host}' is not allowed by policy. "
            "Set GHOSTAPI_ALLOWED_DOMAINS or provide --policy-file."
        )
    return host
