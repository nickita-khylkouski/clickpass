from __future__ import annotations

import json
from urllib.parse import urlparse

from ghostapi.models import Endpoint, Finding
from ghostapi.utils import has_binary, iter_jsonl_lines, run_cmd


def run_nuclei(target_url: str, timeout_seconds: int = 120) -> list[Finding]:
    if not has_binary("nuclei"):
        return []
    cmd = ["nuclei", "-u", target_url, "-silent", "-jsonl"]
    completed = run_cmd(cmd, timeout_seconds=timeout_seconds)
    if completed.returncode != 0:
        return [
            Finding(
                severity="info",
                title="nuclei failed",
                description=completed.stderr.strip() or "nuclei execution failed",
                source="nuclei",
            )
        ]
    findings: list[Finding] = []
    for payload in iter_jsonl_lines(completed.stdout):
        info = payload.get("info", {})
        findings.append(
            Finding(
                severity=str(info.get("severity", "info")).lower(),
                title=str(info.get("name", payload.get("template-id", "nuclei finding"))),
                description=str(payload.get("matched-at", "")),
                source="nuclei",
                endpoint=str(payload.get("matched-at", "")) or None,
                raw=payload,
            )
        )
    return findings


def baseline_endpoint_checks(endpoints: list[Endpoint]) -> list[Finding]:
    findings: list[Finding] = []
    for ep in endpoints:
        parsed = urlparse(ep.url)
        if parsed.scheme == "http":
            findings.append(
                Finding(
                    severity="medium",
                    title="Insecure transport",
                    description=f"Endpoint uses HTTP: {ep.url}",
                    source="baseline",
                    endpoint=ep.path,
                )
            )
        lower_path = ep.path.lower()
        if "/admin" in lower_path or "/internal" in lower_path or "/debug" in lower_path:
            findings.append(
                Finding(
                    severity="info",
                    title="Sensitive-looking endpoint discovered",
                    description=f"Review access controls for {ep.path}",
                    source="baseline",
                    endpoint=ep.path,
                )
            )
    return findings


def findings_to_json(findings: list[Finding]) -> str:
    return json.dumps([f.to_dict() for f in findings], indent=2)

