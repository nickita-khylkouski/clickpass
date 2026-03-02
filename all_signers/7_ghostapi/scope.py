from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse


class ScopeError(ValueError):
    pass


def _normalize_domain(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("http://") or value.startswith("https://"):
        value = urlparse(value).netloc.lower()
    return value.removeprefix("www.")


def assert_authorized_target(
    url: str,
    authorized: bool,
    scope_file: str | None = None,
) -> None:
    """Guardrail to ensure the user explicitly confirms authorization.

    If scope_file is provided, the target host must match one of the entries.
    """
    if not authorized:
        raise ScopeError(
            "Refusing to run without explicit authorization. "
            "Pass --authorized and only test assets you own or have permission to assess."
        )

    if not scope_file:
        return

    target = _normalize_domain(urlparse(url).netloc)
    allowed = {
        _normalize_domain(line)
        for line in Path(scope_file).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    if target not in allowed:
        raise ScopeError(
            f"Target '{target}' is not present in scope file '{scope_file}'. "
            "Add the domain first, then retry."
        )

