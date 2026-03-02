from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .models import ApiEndpoint, AuthSummary, EndpointRecord, SiteAnalysis

HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
STATIC_EXTENSIONS = {
    ".css",
    ".js",
    ".mjs",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".woff",
    ".woff2",
    ".ttf",
    ".ico",
    ".map",
    ".mp4",
    ".webm",
    ".txt",
}
NOISE_DOMAINS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "segment.io",
    "sentry.io",
    "datadoghq.com",
    "hotjar.com",
)

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
HEXISH_RE = re.compile(r"^[0-9a-fA-F]{12,}$")


def load_har(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def analyze_har(har: dict[str, Any], include_hosts: set[str] | None = None) -> dict[str, Any]:
    entries = ((har.get("log") or {}).get("entries")) or []
    endpoints: dict[tuple[str, str, str], EndpointRecord] = {}
    server_counter: Counter[str] = Counter()

    for entry in entries:
        request = entry.get("request") or {}
        response = entry.get("response") or {}

        method = str(request.get("method", "")).upper()
        raw_url = str(request.get("url", ""))
        if method not in HTTP_METHODS or not raw_url:
            continue

        parsed = urlsplit(raw_url)
        host = parsed.netloc.lower()
        if include_hosts and host not in include_hosts:
            continue
        if _is_noise_host(host):
            continue
        if _is_static_path(parsed.path):
            continue
        if not _looks_like_data_endpoint(parsed.path, response):
            continue

        server_counter[f"{parsed.scheme}://{host}"] += 1
        path_template = _normalize_path_template(parsed.path)
        key = (method, host, path_template)
        endpoint = endpoints.get(key)
        if endpoint is None:
            endpoint = EndpointRecord(
                method=method,
                host=host,
                path_template=path_template,
                sample_url=raw_url,
            )
            endpoints[key] = endpoint

        endpoint.calls += 1
        status = _as_int(response.get("status"))
        if status is not None:
            endpoint.statuses.add(status)
        endpoint.auth_required = endpoint.auth_required or _has_auth(request)

        for k, _v in parse_qsl(parsed.query, keep_blank_values=True):
            endpoint.query_params.add(k)
        for qs in request.get("queryString") or []:
            if isinstance(qs, dict) and qs.get("name"):
                endpoint.query_params.add(str(qs["name"]))

        request_schema = _parse_request_schema(request)
        if request_schema is not None:
            endpoint.request_schema = _merge_schema(endpoint.request_schema, request_schema)

        response_schema = _parse_response_schema(response)
        if response_schema is not None:
            endpoint.response_schema = _merge_schema(endpoint.response_schema, response_schema)

    server_url = server_counter.most_common(1)[0][0] if server_counter else ""
    serialized = [_serialize_endpoint(ep) for ep in sorted(endpoints.values(), key=lambda e: e.operation_key)]
    return {
        "summary": {
            "entries_seen": len(entries),
            "endpoints_found": len(serialized),
            "server_url": server_url,
        },
        "endpoints": serialized,
    }


def analyze_har_file(har_path: str, site: str) -> SiteAnalysis:
    parsed_site = urlsplit(site)
    include_hosts = {parsed_site.netloc.lower()} if parsed_site.netloc else None
    analysis = analyze_har(load_har(har_path), include_hosts=include_hosts)

    endpoint_models = [
        ApiEndpoint(
            method=str(ep.get("method", "GET")).upper(),
            path=str(ep.get("path_template", "/")),
            host=str(ep.get("host", "")),
            sample_url=str(ep.get("sample_url", "")),
            query_params=[str(x) for x in ep.get("query_params", [])],
            auth_required=bool(ep.get("auth_required", False)),
            statuses=[int(s) for s in ep.get("statuses", []) if isinstance(s, int) or str(s).isdigit()],
            request_schema=ep.get("request_schema"),
            response_schema=ep.get("response_schema"),
        )
        for ep in analysis.get("endpoints", [])
        if isinstance(ep, dict)
    ]

    auth_summary = _auth_summary_from_endpoints(endpoint_models)
    summary = analysis.get("summary", {}) if isinstance(analysis.get("summary"), dict) else {}
    base_url = str(summary.get("server_url", "")).strip()
    if not base_url and parsed_site.scheme and parsed_site.netloc:
        base_url = f"{parsed_site.scheme}://{parsed_site.netloc}"

    return SiteAnalysis(
        site=site,
        base_url=base_url,
        auth=auth_summary,
        endpoints=endpoint_models,
        summary=summary,
    )


def _auth_summary_from_endpoints(endpoints: list[ApiEndpoint]) -> AuthSummary:
    if not endpoints:
        return AuthSummary(auth_type="none", notes="No endpoints found")

    auth_required = [ep for ep in endpoints if ep.auth_required]
    if not auth_required:
        return AuthSummary(auth_type="none", notes="No auth headers/cookies detected in captured traffic")

    methods = {ep.method for ep in auth_required}
    if "POST" in methods or "PATCH" in methods or "PUT" in methods or "DELETE" in methods:
        return AuthSummary(auth_type="bearer", notes="Auth likely required for write operations")
    return AuthSummary(auth_type="session", notes="Auth observed on captured requests")


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_noise_host(host: str) -> bool:
    return any(domain in host for domain in NOISE_DOMAINS)


def _is_static_path(path: str) -> bool:
    lower = path.lower()
    return any(lower.endswith(ext) for ext in STATIC_EXTENSIONS)


def _looks_like_data_endpoint(path: str, response: dict[str, Any]) -> bool:
    lowered = path.lower()
    if "/api/" in lowered or lowered.startswith("/api"):
        return True
    mime = str(((response.get("content") or {}).get("mimeType")) or "").lower()
    if "json" in mime or "graphql" in mime:
        return True
    return False


def _normalize_path_template(path: str) -> str:
    parts = [part for part in path.split("/") if part]
    normalized: list[str] = []
    for part in parts:
        if part.isdigit() or UUID_RE.match(part) or HEXISH_RE.match(part):
            normalized.append("{id}")
            continue
        normalized.append(part)
    return "/" + "/".join(normalized)


def _has_auth(request: dict[str, Any]) -> bool:
    headers = request.get("headers") or []
    for header in headers:
        if not isinstance(header, dict):
            continue
        name = str(header.get("name", "")).lower()
        if name in {"authorization", "cookie", "x-api-key"}:
            return True
    return False


def _parse_request_schema(request: dict[str, Any]) -> dict[str, Any] | None:
    post_data = request.get("postData") or {}
    text = post_data.get("text")
    if not text or not isinstance(text, str):
        return None
    return _schema_from_text(text)


def _parse_response_schema(response: dict[str, Any]) -> dict[str, Any] | None:
    content = response.get("content") or {}
    text = content.get("text")
    mime = str(content.get("mimeType", "")).lower()
    if not isinstance(text, str):
        return None
    if "json" not in mime and not text.strip().startswith(("{", "[")):
        return None
    return _schema_from_text(text)


def _schema_from_text(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return _schema_from_value(value)


def _schema_from_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {k: _schema_from_value(v) for k, v in value.items()},
            "required": sorted(value.keys()),
        }
    if isinstance(value, list):
        item_schema = _schema_from_value(value[0]) if value else {"type": "string"}
        return {"type": "array", "items": item_schema}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if value is None:
        return {"nullable": True}
    return {"type": "string"}


def _merge_schema(current: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    if current is None:
        return new
    if current == new:
        return current
    if current.get("oneOf") and isinstance(current["oneOf"], list):
        existing = current["oneOf"]
        if new not in existing:
            return {"oneOf": [*existing, new]}
        return current
    return {"oneOf": [current, new]}


def _serialize_endpoint(ep: EndpointRecord) -> dict[str, Any]:
    return {
        "method": ep.method,
        "host": ep.host,
        "path_template": ep.path_template,
        "sample_url": ep.sample_url,
        "calls": ep.calls,
        "auth_required": ep.auth_required,
        "query_params": sorted(ep.query_params),
        "statuses": sorted(ep.statuses),
        "request_schema": ep.request_schema,
        "response_schema": ep.response_schema,
    }
