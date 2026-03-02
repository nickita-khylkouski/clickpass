from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore[assignment]

try:
    import openai as openai_mod
except ImportError:
    openai_mod = None  # type: ignore[assignment]


@dataclass
class BypassFinding:
    category: str  # client_side_paywall, unprotected_endpoint, auth_bypass, free_tier_leak, rate_limit_gap
    severity: str  # critical, high, medium, low, info
    title: str
    description: str
    endpoint: str  # e.g. "GET /api/v1/content/{id}"
    evidence: str
    curl_command: str = ""
    confidence: float = 0.0


@dataclass
class BypassReport:
    target: str
    total_endpoints: int
    findings: list[BypassFinding]
    summary: str = ""
    raw_llm_response: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "total_endpoints": self.total_endpoints,
            "findings": [asdict(f) for f in self.findings],
            "summary": self.summary,
            "raw_llm_response": self.raw_llm_response,
        }


BYPASS_ANALYSIS_PROMPT = """You are a security researcher analyzing API endpoints captured during a signup/trial flow on a web application.

Target: {target_url}
Auth summary: {auth_summary}

Below are the API endpoints observed during the signup and browsing flow. Analyze them for bypass opportunities in these 5 categories:

1. **Client-side paywalls**: Endpoints that return premium/paid content without server-side enforcement. Look for content endpoints that don't require auth or return full data on free-tier accounts.
2. **Unprotected write endpoints**: POST/PUT/PATCH/DELETE endpoints that lack authentication. State-changing operations accessible without auth tokens.
3. **Free-tier data leaks**: Endpoints that expose more data than the UI shows (e.g., full articles behind a "read more" gate, user PII, internal IDs). Compare response schemas for over-fetching.
4. **Auth bypass patterns**: Endpoints where removing or modifying auth headers still returns data. Look for inconsistent auth requirements across related endpoints.
5. **Rate limit gaps**: Endpoints with no apparent rate limiting that could be enumerated or scraped. High-value data endpoints without throttling.

For each finding, provide:
- category: one of (client_side_paywall, unprotected_endpoint, auth_bypass, free_tier_leak, rate_limit_gap)
- severity: one of (critical, high, medium, low, info)
- title: short descriptive title
- description: detailed explanation of the bypass
- endpoint: the affected endpoint (e.g. "GET /api/v1/content/{{id}}")
- evidence: what in the captured data supports this finding
- curl_command: a ready-to-run curl command to test this (if applicable, otherwise empty string)
- confidence: float 0.0-1.0 indicating how confident you are

Return ONLY valid JSON in this format:
{{
  "findings": [
    {{
      "category": "...",
      "severity": "...",
      "title": "...",
      "description": "...",
      "endpoint": "...",
      "evidence": "...",
      "curl_command": "...",
      "confidence": 0.0
    }}
  ],
  "summary": "Brief summary of overall findings"
}}

Endpoint data:
{endpoint_data}
"""


_ENDPOINT_CAP = 80
_CODE_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def _format_endpoint(ep: dict[str, Any]) -> str:
    """Format a single endpoint dict into a human-readable block."""
    method = ep.get("method", "GET")
    path = ep.get("path_template", ep.get("path", "/"))
    auth = ep.get("auth_required", False)
    statuses = ep.get("statuses", [])
    calls = ep.get("calls", 0)
    params = ep.get("query_params", [])

    lines = [
        f"  {method} {path}",
        f"    auth_required: {auth}",
        f"    statuses: {statuses}",
        f"    calls: {calls}",
    ]
    if params:
        lines.append(f"    query_params: {params}")

    resp_schema = ep.get("response_schema")
    if resp_schema:
        schema_str = json.dumps(resp_schema)
        if len(schema_str) > 500:
            schema_str = schema_str[:500] + "..."
        lines.append(f"    response_schema: {schema_str}")

    req_schema = ep.get("request_schema")
    if req_schema:
        schema_str = json.dumps(req_schema)
        if len(schema_str) > 300:
            schema_str = schema_str[:300] + "..."
        lines.append(f"    request_schema: {schema_str}")

    return "\n".join(lines)


async def analyze_for_bypasses(
    target_url: str,
    analysis: dict[str, Any],
    openapi_spec: dict[str, Any] | None = None,
) -> BypassReport:
    """Send endpoint analysis to an LLM for security bypass detection.

    Prefers Anthropic (Claude), falls back to OpenAI (GPT-4o).
    """
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    endpoints = analysis.get("endpoints", [])
    total = len(endpoints)

    if not anthropic_key and not openai_key:
        return BypassReport(target=target_url, total_endpoints=total, findings=[])

    # Cap endpoints to avoid overly long prompts
    capped = endpoints[:_ENDPOINT_CAP]
    endpoint_data = "\n".join(_format_endpoint(ep) for ep in capped)

    # Build auth summary from analysis
    summary_info = analysis.get("summary", {})
    auth_parts: list[str] = []
    auth_count = sum(1 for ep in endpoints if ep.get("auth_required"))
    noauth_count = total - auth_count
    auth_parts.append(f"Total endpoints: {total}")
    auth_parts.append(f"Endpoints requiring auth: {auth_count}")
    auth_parts.append(f"Endpoints without auth: {noauth_count}")
    auth_parts.append(f"Server URL: {summary_info.get('server_url', 'unknown')}")
    auth_summary = "\n".join(auth_parts)

    prompt = BYPASS_ANALYSIS_PROMPT.format(
        target_url=target_url,
        endpoint_data=endpoint_data,
        auth_summary=auth_summary,
    )

    raw_text = ""
    if anthropic_key and anthropic is not None:
        client = anthropic.Anthropic(api_key=anthropic_key)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = response.content[0].text
    elif openai_key and openai_mod is not None:
        client = openai_mod.OpenAI(api_key=openai_key)
        response = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = response.choices[0].message.content or ""
    else:
        return BypassReport(target=target_url, total_endpoints=total, findings=[])

    report = _parse_bypass_response(target_url, total, raw_text)
    return report


def _parse_bypass_response(
    target_url: str,
    total_endpoints: int,
    raw_text: str,
) -> BypassReport:
    """Parse the LLM JSON response into a BypassReport."""
    text = raw_text.strip()

    # Strip markdown code blocks if present
    match = _CODE_BLOCK_RE.search(text)
    if match:
        text = match.group(1).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return BypassReport(
            target=target_url,
            total_endpoints=total_endpoints,
            findings=[],
            raw_llm_response=raw_text,
        )

    findings: list[BypassFinding] = []
    for item in data.get("findings", []):
        if not isinstance(item, dict):
            continue
        findings.append(
            BypassFinding(
                category=str(item.get("category", "unknown")),
                severity=str(item.get("severity", "info")),
                title=str(item.get("title", "")),
                description=str(item.get("description", "")),
                endpoint=str(item.get("endpoint", "")),
                evidence=str(item.get("evidence", "")),
                curl_command=str(item.get("curl_command", "")),
                confidence=float(item.get("confidence", 0.0)),
            )
        )

    summary = str(data.get("summary", ""))

    return BypassReport(
        target=target_url,
        total_endpoints=total_endpoints,
        findings=findings,
        summary=summary,
        raw_llm_response=raw_text,
    )


def generate_curl_commands(analysis: dict[str, Any], bypass_report: BypassReport) -> str:
    """Generate a bash script with curl commands for each finding."""
    server_url = analysis.get("summary", {}).get("server_url", "")
    lines: list[str] = [
        "#!/bin/bash",
        f"# GhostAPI Bypass Test Script for {bypass_report.target}",
        f"# Generated from {bypass_report.total_endpoints} analyzed endpoints",
        f"# Server: {server_url}",
        "",
    ]

    if not bypass_report.findings:
        lines.append("# No findings to test.")
        lines.append('echo "No findings generated."')
        return "\n".join(lines) + "\n"

    for i, finding in enumerate(bypass_report.findings, 1):
        lines.append(f"# --- Finding {i}: [{finding.severity.upper()}] {finding.title} ---")
        lines.append(f"# Category: {finding.category}")
        lines.append(f"# Endpoint: {finding.endpoint}")
        lines.append(f"# Confidence: {finding.confidence}")
        lines.append(f'echo "Testing: {finding.title}"')

        if finding.curl_command:
            lines.append(finding.curl_command)
        else:
            # Auto-generate from endpoint + server_url
            parts = finding.endpoint.split(None, 1)
            method = parts[0] if parts else "GET"
            path = parts[1] if len(parts) > 1 else "/"
            url = f"{server_url}{path}" if server_url else path
            if method == "GET":
                lines.append(f'curl -s "{url}"')
            else:
                lines.append(f'curl -s -X {method} "{url}"')

        lines.append('echo ""')
        lines.append("")

    return "\n".join(lines) + "\n"
