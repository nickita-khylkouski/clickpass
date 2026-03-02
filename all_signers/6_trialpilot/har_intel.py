"""HAR onboarding-flow intelligence helpers.

This module provides a compact, resilient HAR summary intended for onboarding
flow analysis.
"""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


_AUTH_KEYWORDS = {
    "auth",
    "authorize",
    "authorization",
    "login",
    "logout",
    "signin",
    "sign-in",
    "signup",
    "sign-up",
    "session",
    "token",
    "oauth",
    "sso",
    "verify",
    "password",
    "mfa",
    "otp",
}

_BILLING_KEYWORDS = {
    "billing",
    "payment",
    "checkout",
    "invoice",
    "subscription",
    "plan",
    "pricing",
    "stripe",
    "card",
    "customer-portal",
}

_API_KEY_KEYWORDS = {
    "api-key",
    "api_key",
    "apikey",
    "x-api-key",
    "client-secret",
    "client_id",
    "client-id",
}


def _empty_result() -> dict[str, Any]:
    return {
        "total_requests": 0,
        "methods_histogram": {},
        "top_hosts": [],
        "suspected_auth_endpoints": [],
        "suspected_billing_endpoints": [],
        "suspected_api_key_endpoints": [],
        "error": None,
    }


def _with_error(code: str, message: str) -> dict[str, Any]:
    result = _empty_result()
    result["error"] = {"code": code, "message": message}
    return result


def _unique_append(target: list[str], seen: set[str], value: str, limit: int) -> None:
    if not value:
        return
    if value in seen:
        return
    if len(target) >= limit:
        return
    target.append(value)
    seen.add(value)


def _collect_text(blob: Any, out: list[str]) -> None:
    if blob is None:
        return
    if isinstance(blob, str):
        out.append(blob)
        return
    if isinstance(blob, (int, float, bool)):
        out.append(str(blob))
        return
    if isinstance(blob, dict):
        for key, value in blob.items():
            out.append(str(key))
            _collect_text(value, out)
        return
    if isinstance(blob, list):
        for item in blob:
            _collect_text(item, out)


def summarize_onboarding_har(
    har_path: str | Path,
    *,
    top_hosts_limit: int = 8,
    endpoint_limit: int = 20,
) -> dict[str, Any]:
    """Return a compact HAR summary for onboarding-flow investigation.

    The function is defensive: it returns a structured ``error`` field for
    malformed or unreadable HAR files rather than raising in normal failure
    modes.
    """

    path = Path(har_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _with_error("file_not_found", f"HAR file not found: {path}")
    except PermissionError:
        return _with_error("permission_denied", f"Cannot read HAR file: {path}")
    except OSError as exc:
        return _with_error("read_error", f"Failed reading HAR file: {exc}")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _with_error("invalid_json", f"HAR is not valid JSON: {exc.msg}")

    if not isinstance(payload, dict):
        return _with_error("invalid_har", "HAR root must be a JSON object")

    log = payload.get("log")
    if not isinstance(log, dict):
        return _with_error("invalid_har", "HAR is missing object field 'log'")

    entries = log.get("entries")
    if not isinstance(entries, list):
        return _with_error("invalid_har", "HAR 'log.entries' must be a list")

    methods: Counter[str] = Counter()
    hosts: Counter[str] = Counter()
    auth_endpoints: list[str] = []
    billing_endpoints: list[str] = []
    api_key_endpoints: list[str] = []
    auth_seen: set[str] = set()
    billing_seen: set[str] = set()
    api_key_seen: set[str] = set()
    valid_requests = 0
    skipped_entries = 0

    for entry in entries:
        if not isinstance(entry, dict):
            skipped_entries += 1
            continue
        request = entry.get("request")
        if not isinstance(request, dict):
            skipped_entries += 1
            continue

        method_raw = request.get("method")
        method = method_raw.upper() if isinstance(method_raw, str) and method_raw else "UNKNOWN"
        methods[method] += 1

        url = request.get("url") if isinstance(request.get("url"), str) else ""
        if url:
            valid_requests += 1

        host = ""
        if url:
            try:
                split = urlsplit(url)
                host = split.hostname or ""
            except ValueError:
                host = ""
        if host:
            hosts[host] += 1

        text_parts: list[str] = [method]
        if url:
            text_parts.append(url)
        _collect_text(request.get("headers"), text_parts)
        _collect_text(request.get("queryString"), text_parts)
        _collect_text(request.get("postData"), text_parts)
        lowered = " ".join(text_parts).lower()

        endpoint_label = f"{method} {url}".strip()

        if any(keyword in lowered for keyword in _AUTH_KEYWORDS):
            _unique_append(auth_endpoints, auth_seen, endpoint_label, endpoint_limit)

        if any(keyword in lowered for keyword in _BILLING_KEYWORDS):
            _unique_append(billing_endpoints, billing_seen, endpoint_label, endpoint_limit)

        if any(keyword in lowered for keyword in _API_KEY_KEYWORDS):
            _unique_append(api_key_endpoints, api_key_seen, endpoint_label, endpoint_limit)

    result = _empty_result()
    result["total_requests"] = len(entries)
    result["methods_histogram"] = dict(methods)
    result["top_hosts"] = [
        {"host": host, "count": count}
        for host, count in hosts.most_common(max(0, top_hosts_limit))
    ]
    result["suspected_auth_endpoints"] = auth_endpoints
    result["suspected_billing_endpoints"] = billing_endpoints
    result["suspected_api_key_endpoints"] = api_key_endpoints

    if skipped_entries > 0:
        result["error"] = {
            "code": "partial_parse",
            "message": f"Skipped {skipped_entries} malformed HAR entr{'y' if skipped_entries == 1 else 'ies'}",
            "details": {
                "skipped_entries": skipped_entries,
                "valid_requests_with_url": valid_requests,
            },
        }

    return result


def analyze_onboarding_har(har_path: str | Path) -> dict[str, Any]:
    """Alias for summarize_onboarding_har."""

    return summarize_onboarding_har(har_path)


def analyze_har(har_path: str | Path) -> dict[str, Any]:
    """Alias for summarize_onboarding_har."""

    return summarize_onboarding_har(har_path)


__all__ = [
    "summarize_onboarding_har",
    "analyze_onboarding_har",
    "analyze_har",
]
