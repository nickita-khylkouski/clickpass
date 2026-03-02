from __future__ import annotations

import json

from .models import SiteAnalysis
from .openapi import build_openapi


def generate_openapi(analysis: SiteAnalysis) -> str:
    payload = {
        "summary": {
            "server_url": analysis.base_url,
            "entries_seen": analysis.summary.get("entries_seen", 0),
            "endpoints_found": len(analysis.endpoints),
        },
        "endpoints": [
            {
                "method": endpoint.method,
                "host": endpoint.host,
                "path_template": endpoint.path,
                "sample_url": endpoint.sample_url,
                "query_params": endpoint.query_params,
                "auth_required": endpoint.auth_required,
                "statuses": endpoint.statuses,
                "request_schema": endpoint.request_schema,
                "response_schema": endpoint.response_schema,
            }
            for endpoint in analysis.endpoints
        ],
    }
    doc = build_openapi(payload, title=f"{analysis.site} Discovered API")
    return json.dumps(doc, indent=2)
