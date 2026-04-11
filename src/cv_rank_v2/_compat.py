"""Small compatibility helpers for v2 modules.

These helpers let v2 operate on dataclasses or plain dicts while the
rewrite is still being built out in stages.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any


def to_plain_dict(value: Any) -> dict[str, Any]:
    """Convert a dataclass-like value into a plain dict when possible."""
    if isinstance(value, dict):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    raise TypeError(f"Unsupported value type for conversion: {type(value)!r}")


def get_nested(mapping: dict[str, Any], *path: str, default: Any = None) -> Any:
    """Traverse nested dictionaries safely."""
    current: Any = mapping
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def applicant_id_of(applicant: Any) -> str:
    """Extract a stable applicant identifier."""
    if isinstance(applicant, dict):
        return str(applicant.get("applicant_id") or applicant.get("candidate_id") or applicant.get("name") or "")
    if hasattr(applicant, "applicant_id"):
        return str(getattr(applicant, "applicant_id"))
    if hasattr(applicant, "candidate_id"):
        return str(getattr(applicant, "candidate_id"))
    raise TypeError(f"Applicant has no stable identifier: {type(applicant)!r}")


def display_name_of(applicant: Any) -> str:
    """Extract the human-readable display name for prompts and exports."""
    data = to_plain_dict(applicant)
    return str(
        data.get("display_name")
        or get_nested(data, "basic", "display_name")
        or get_nested(data, "basic", "name")
        or data.get("name")
        or data.get("candidate_id")
        or data.get("applicant_id")
        or "Unknown"
    )


def email_of(applicant: Any) -> str:
    """Extract the best email field available."""
    data = to_plain_dict(applicant)
    return str(
        get_nested(data, "basic", "email")
        or data.get("email")
        or ""
    )


def raw_csv_of(applicant: Any) -> dict[str, Any]:
    """Return the preserved raw CSV payload if present."""
    data = to_plain_dict(applicant)
    raw = data.get("raw_csv") or data.get("_raw_csv") or get_nested(data, "application", "raw_csv") or {}
    return raw if isinstance(raw, dict) else {}
