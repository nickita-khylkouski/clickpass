"""IO helpers for deterministic sponsor-automation data loading/parsing."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .contracts import (
    CANONICAL_CLAIMS_FULL_FIELDNAMES,
    CANONICAL_CLAIMS_STRICT_FIELDNAMES,
    LEADS_FIELDNAMES,
    MARKETING_MASTER_FIELDNAMES,
    PROJECT_TECH_EVIDENCE_FIELDNAMES,
    CanonicalClaimRow,
    MarketingMetricClaim,
    ProjectTechEvidenceRow,
    SponsorLeadRow,
    TopLeadCandidate,
)
from .deterministic import build_canonical_claim_rows


def load_marketing_master(marketing_master_csv: str | Path) -> list[MarketingMetricClaim]:
    """Load ``marketing_master.csv`` into typed deterministic claim rows."""

    path = Path(marketing_master_csv)
    rows: list[MarketingMetricClaim] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows.append(
                MarketingMetricClaim(
                    section=_as_text(row.get("section")),
                    metric=_as_text(row.get("metric")),
                    value=_as_text(row.get("value")),
                    numerator=_as_text(row.get("numerator")),
                    denominator=_as_text(row.get("denominator")),
                    denominator_cohort=_as_text(row.get("denominator_cohort")),
                    coverage_pct=_as_text(row.get("coverage_pct")),
                    source=_as_text(row.get("source")),
                    pii_classification=_as_text(row.get("pii_classification")),
                    confidence=_as_text(row.get("confidence")),
                    claim_label=_as_text(row.get("claim_label")),
                    notes=_as_text(row.get("notes")),
                )
            )
    return rows


def load_enriched_json(enriched_json_path: str | Path) -> list[dict[str, Any]]:
    """Load enriched profile JSON from disk.

    Returns an empty list only when the file contains a non-list payload.
    """

    path = Path(enriched_json_path)
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, list) else []


def parse_top50_from_q18_markdown(q18_markdown: str | Path) -> list[TopLeadCandidate]:
    """Parse top-50 ranked lead rows from ``sectionC_agent5_q18.md``."""

    lines = Path(q18_markdown).read_text(encoding="utf-8").splitlines()
    rows: list[TopLeadCandidate] = []
    in_table = False

    for line in lines:
        if line.startswith("| rank | name | email | company | role_bucket |"):
            in_table = True
            continue
        if in_table and line.startswith("|---"):
            continue
        if in_table:
            if not line.startswith("|"):
                break

            content = line.strip().strip("|").strip()
            if not content:
                continue

            parts_right = content.rsplit(" | ", 10)
            if len(parts_right) != 11:
                continue

            left = parts_right[0]
            tail = parts_right[1:]
            left_parts = [part.strip() for part in left.split(" | ")]
            if len(left_parts) < 5:
                continue

            rank = left_parts[0]
            name = left_parts[1]
            email = left_parts[2]
            company = " | ".join(left_parts[3:-1]).strip()
            role_bucket = left_parts[-1]
            (
                metric_name,
                value,
                numerator,
                denominator,
                denominator_cohort,
                coverage_pct,
                source,
                confidence,
                claim_label,
                reason_codes,
            ) = [part.strip() for part in tail]

            if not rank.isdigit():
                continue

            rows.append(
                TopLeadCandidate(
                    rank=int(rank),
                    name=name,
                    email=email.lower(),
                    company=company,
                    role_bucket=role_bucket,
                    metric_name=metric_name,
                    lead_score=value,
                    numerator=numerator,
                    denominator=denominator,
                    denominator_cohort=denominator_cohort,
                    coverage_pct=coverage_pct,
                    source=source,
                    confidence=confidence,
                    claim_label=claim_label,
                    reason_codes=reason_codes,
                )
            )

    rows.sort(key=lambda row: row.rank)
    return rows[:50]


def load_canonical_claim_rows(marketing_master_csv: str | Path) -> list[CanonicalClaimRow]:
    """Convenience helper to load + canonicalize all marketing claims."""

    raw_rows = load_marketing_master(marketing_master_csv)
    return build_canonical_claim_rows(raw_rows)


def write_marketing_master_rows(path: str | Path, rows: Iterable[MarketingMetricClaim]) -> None:
    _write_dict_csv(path, (row.as_marketing_master_row() for row in rows), MARKETING_MASTER_FIELDNAMES)


def write_canonical_claim_rows_strict(path: str | Path, rows: Iterable[CanonicalClaimRow]) -> None:
    _write_dict_csv(path, (row.as_strict_row() for row in rows), CANONICAL_CLAIMS_STRICT_FIELDNAMES)


def write_canonical_claim_rows_full(path: str | Path, rows: Iterable[CanonicalClaimRow]) -> None:
    _write_dict_csv(path, (row.as_full_row() for row in rows), CANONICAL_CLAIMS_FULL_FIELDNAMES)


def write_sponsor_leads(path: str | Path, rows: Iterable[SponsorLeadRow]) -> None:
    _write_dict_csv(path, (row.as_row() for row in rows), LEADS_FIELDNAMES)


def write_project_tech_evidence(path: str | Path, rows: Iterable[ProjectTechEvidenceRow]) -> None:
    _write_dict_csv(path, (row.as_row() for row in rows), PROJECT_TECH_EVIDENCE_FIELDNAMES)


def _write_dict_csv(path: str | Path, rows: Iterable[dict[str, str]], fieldnames: Iterable[str]) -> None:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_fieldnames = list(fieldnames)
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=normalized_fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)
