from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from ghostapi.models import Endpoint


STATIC_EXTENSIONS = (
    ".css",
    ".js",
    ".map",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".woff",
    ".woff2",
    ".ttf",
    ".ico",
)

TRACKING_HOST_MARKERS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "segment.io",
    "datadog",
    "sentry.io",
    "newrelic",
    "hotjar",
    "mixpanel",
)


def _looks_like_api(path: str, content_type: str | None) -> bool:
    p = path.lower()
    if p.endswith(STATIC_EXTENSIONS):
        return False
    if "/api/" in p or p.startswith("/api") or "/graphql" in p:
        return True
    if any(x in p for x in ("/v1/", "/v2/", "/v3/", "/rest/")):
        return True
    if content_type and "json" in content_type.lower():
        return True
    return False


def _is_tracking_host(host: str) -> bool:
    h = host.lower()
    return any(marker in h for marker in TRACKING_HOST_MARKERS)


def parse_har_endpoints(har_path: str, target_host: str | None = None) -> list[Endpoint]:
    payload = json.loads(Path(har_path).read_text())
    entries = payload.get("log", {}).get("entries", [])
    results: list[Endpoint] = []
    for entry in entries:
        request = entry.get("request", {})
        response = entry.get("response", {})
        url = request.get("url", "")
        method = str(request.get("method", "GET")).upper()
        if not url:
            continue
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path or "/"
        content_type = response.get("content", {}).get("mimeType")
        status = response.get("status")

        if target_host and host and host != target_host and not host.endswith(f".{target_host}"):
            continue
        if _is_tracking_host(host):
            continue
        if not _looks_like_api(path, content_type):
            continue
        results.append(
            Endpoint(
                method=method,
                url=url,
                path=path,
                source="har",
                status=int(status) if isinstance(status, int) else None,
                content_type=str(content_type) if content_type else None,
            )
        )
    return results

