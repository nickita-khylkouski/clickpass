from __future__ import annotations

import csv
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def publish_outputs(
    *,
    event_slug: str,
    event_name: str | None,
    run_id: str,
    output_root: Path,
    repo_root: Path,
    narrative_markdown: str,
    claims: list[dict[str, Any]],
    required_csv_paths: list[Path],
    headline_bank_path: Path | None,
    qa_report: dict[str, Any],
    stage_audit: list[dict[str, Any]],
) -> dict[str, Any]:
    output_dir = output_root / event_slug
    output_dir.mkdir(parents=True, exist_ok=True)

    copied_csv_paths: list[Path] = []
    for src in required_csv_paths:
        dest = output_dir / src.name
        shutil.copy2(src, dest)
        copied_csv_paths.append(dest)

    claims_path = output_dir / "CLAIMS_GENERATED.csv"
    _write_claims_csv(claims_path, claims)

    packet_path = output_dir / "SPONSOR_PACKET_ONE.md"
    packet_path.write_text(
        _render_packet(
            event_name=event_name,
            event_slug=event_slug,
            run_id=run_id,
            narrative_markdown=narrative_markdown,
            headline_bank_text=_read_text_or_none(headline_bank_path),
            copied_csv_paths=copied_csv_paths,
            claims_path=claims_path,
        ),
        encoding="utf-8",
    )

    run_audit_path = write_run_audit(
        event_name=event_name,
        event_slug=event_slug,
        run_id=run_id,
        output_dir=output_dir,
        qa_report=qa_report,
        stage_audit=stage_audit,
        copied_csv_paths=copied_csv_paths,
        claims_path=claims_path,
        packet_path=packet_path,
    )

    return {
        "output_dir": str(output_dir),
        "packet_path": str(packet_path),
        "run_audit_path": str(run_audit_path),
        "copied_csv_paths": [str(p) for p in copied_csv_paths],
        "claims_path": str(claims_path),
    }


def write_run_audit(
    *,
    event_name: str | None,
    event_slug: str,
    run_id: str,
    output_dir: Path,
    qa_report: dict[str, Any],
    stage_audit: list[dict[str, Any]],
    copied_csv_paths: list[Path],
    claims_path: Path,
    packet_path: Path,
) -> Path:
    run_audit_path = output_dir / "RUN_AUDIT.md"
    run_audit_path.write_text(
        _render_run_audit(
            event_name=event_name,
            event_slug=event_slug,
            run_id=run_id,
            output_dir=output_dir,
            qa_report=qa_report,
            stage_audit=stage_audit,
            copied_csv_paths=copied_csv_paths,
            claims_path=claims_path,
            packet_path=packet_path,
        ),
        encoding="utf-8",
    )
    return run_audit_path


def _write_claims_csv(path: Path, claims: list[dict[str, Any]]) -> None:
    fieldnames = [
        "claim_id",
        "section",
        "metric_name",
        "segment",
        "value",
        "numerator",
        "denominator",
        "confidence",
        "source_table_or_file",
        "claim_text",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for claim in claims:
            writer.writerow({k: claim.get(k, "") for k in fieldnames})


def _render_packet(
    *,
    event_name: str | None,
    event_slug: str,
    run_id: str,
    narrative_markdown: str,
    headline_bank_text: str | None,
    copied_csv_paths: list[Path],
    claims_path: Path,
) -> str:
    title = event_name or event_slug.replace("-", " ").title()

    lines = [
        f"# {title} - Sponsor Packet",
        "",
        "This is the single sponsor packet.",
        "",
        "## Sponsor-Ready Narrative Report",
        "",
        narrative_markdown.strip(),
        "",
    ]

    already_has_headlines = "## Sponsor Headline Options" in narrative_markdown
    if headline_bank_text and not already_has_headlines:
        formatted_headlines = _format_headline_bank_for_packet(headline_bank_text)
        lines.extend(
            [
                "## Sponsor Headline Options (Big-Number Angles)",
                "",
                formatted_headlines.strip(),
                "",
            ]
        )

    return "\n".join(lines).strip() + "\n"


def _render_run_audit(
    *,
    event_name: str | None,
    event_slug: str,
    run_id: str,
    output_dir: Path,
    qa_report: dict[str, Any],
    stage_audit: list[dict[str, Any]],
    copied_csv_paths: list[Path],
    claims_path: Path,
    packet_path: Path,
) -> str:
    lines = [
        "# RUN_AUDIT",
        "",
        "## Metadata",
        f"- event_name: `{event_name or ''}`",
        f"- event_slug: `{event_slug}`",
        f"- run_id: `{run_id}`",
        f"- output_dir: `{output_dir}`",
        f"- generated_utc: `{_now_iso()}`",
        "",
        "## QA Summary",
        f"- passed: `{bool(qa_report.get('passed'))}`",
    ]

    errors = qa_report.get("errors") or []
    warnings = qa_report.get("warnings") or []
    if errors:
        lines.append("- errors:")
        for err in errors:
            lines.append(f"  - {err}")
    else:
        lines.append("- errors: `none`")

    if warnings:
        lines.append("- warnings:")
        for warn in warnings:
            lines.append(f"  - {warn}")
    else:
        lines.append("- warnings: `none`")

    lines.extend(["", "## Stage Audit", ""])
    lines.append("| stage | status | duration_sec | detail | error |")
    lines.append("|---|---:|---:|---|---|")
    for item in stage_audit:
        lines.append(
            "| {stage} | {status} | {duration} | {detail} | {error} |".format(
                stage=item.get("stage", ""),
                status=item.get("status", ""),
                duration=item.get("duration_sec", ""),
                detail=(item.get("detail", "") or "").replace("|", "/"),
                error=(item.get("error", "") or "").replace("|", "/"),
            )
        )

    lines.extend(["", "## Output Files", ""])
    files = [packet_path, claims_path, *copied_csv_paths]
    files_sorted = sorted(files, key=lambda p: p.name)
    for path in files_sorted:
        size = path.stat().st_size if path.exists() else 0
        lines.append(f"- `{path.name}` ({size} bytes)")

    return "\n".join(lines).strip() + "\n"


def _read_text_or_none(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_headline_bank_for_packet(headline_bank_text: str) -> str:
    lines = [ln.rstrip() for ln in headline_bank_text.strip().splitlines()]
    lines = [ln for ln in lines if ln.strip()]
    if not lines:
        return ""

    event_line = ""
    for ln in lines:
        if ln.lower().startswith("event:"):
            event_line = ln
            break

    table_rows = [ln for ln in lines if ln.lstrip().startswith("|")]
    if len(table_rows) < 3:
        cleaned = [ln for ln in lines if not ln.lstrip().startswith("#")]
        return "\n".join(cleaned).strip()

    bullets: list[str] = []
    for row in table_rows[2:]:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        headline = cells[1]
        numbers = cells[2].rstrip(".")
        why = cells[3]
        headline_plain = re.sub(r"\*\*(.*?)\*\*", r"\1", headline).strip().rstrip(".")
        combined_low = f"{headline_plain} {numbers} {why}".lower()
        # Drop low-value tautology lines that read like operational logging.
        if (
            ("showed up" in combined_low and "teams shipped" in combined_low)
            or ("checked-in participants" in combined_low and "team submissions" in combined_low)
            or ("not just registrations" in combined_low)
        ):
            continue
        if re.search(r"`0(?:[./]|`)", numbers):
            continue
        numeric_tokens = re.findall(r"\b\d[\d,]*\b", f"{headline_plain} {numbers}")
        abs_values: list[int] = []
        for tok in numeric_tokens:
            try:
                abs_values.append(int(tok.replace(",", "")))
            except Exception:
                continue
        # Headline bank is for "big-number" angles; drop very small-value lines.
        if abs_values and max(abs_values) < 50:
            continue
        sentence = f"- {headline_plain}."
        sentence = re.sub(r"`", "", sentence)
        sentence = re.sub(r"\s{2,}", " ", sentence).strip()
        bullets.append(sentence)

    bullets = bullets[:8]

    output_parts: list[str] = []
    if event_line:
        output_parts.append(event_line)
        output_parts.append("")
    output_parts.extend(bullets)
    return "\n".join(output_parts).strip()
