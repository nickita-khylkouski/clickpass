from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from ghostapi.models import Endpoint, Finding


def build_output_dir(base_dir: str, host: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = Path(base_dir) / host / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2))


def write_report_markdown(
    out_path: Path,
    target_url: str,
    endpoints: list[Endpoint],
    findings: list[Finding],
) -> None:
    sev_counts = Counter([f.severity.lower() for f in findings])
    lines: list[str] = []
    lines.append(f"# GhostAPI Report: {target_url}")
    lines.append("")
    lines.append("## Summary")
    lines.append(f"- Endpoints: {len(endpoints)}")
    lines.append(f"- Findings: {len(findings)}")
    lines.append(f"- Critical: {sev_counts.get('critical', 0)}")
    lines.append(f"- High: {sev_counts.get('high', 0)}")
    lines.append(f"- Medium: {sev_counts.get('medium', 0)}")
    lines.append(f"- Low: {sev_counts.get('low', 0)}")
    lines.append(f"- Info: {sev_counts.get('info', 0)}")
    lines.append("")
    lines.append("## Endpoints")
    for ep in endpoints:
        lines.append(f"- `{ep.method}` `{ep.path}` ({ep.source})")
    lines.append("")
    lines.append("## Findings")
    if not findings:
        lines.append("- No findings from configured checks.")
    for finding in findings:
        endpoint_part = f" (`{finding.endpoint}`)" if finding.endpoint else ""
        lines.append(f"- **{finding.severity.upper()}** {finding.title}{endpoint_part}")
        lines.append(f"  - Source: `{finding.source}`")
        lines.append(f"  - {finding.description}")
    out_path.write_text("\n".join(lines) + "\n")

