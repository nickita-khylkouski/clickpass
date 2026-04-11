"""Typed contracts for deterministic sponsor-automation exports."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

SPONSOR_TOOLS: tuple[str, ...] = ("Gemini", "Antigravity", "LlamaIndex", "Temporal", "Agno")

MARKETING_MASTER_FIELDNAMES: tuple[str, ...] = (
    "section",
    "metric",
    "value",
    "numerator",
    "denominator",
    "denominator_cohort",
    "coverage_pct",
    "source",
    "pii_classification",
    "confidence",
    "claim_label",
    "notes",
)

CANONICAL_CLAIMS_STRICT_FIELDNAMES: tuple[str, ...] = (
    "section",
    "metric_name",
    "segment",
    "value",
    "numerator",
    "denominator",
    "denominator_cohort",
    "coverage_pct",
    "source_table_or_file",
    "calculation_note",
    "confidence",
)

CANONICAL_CLAIMS_FULL_FIELDNAMES: tuple[str, ...] = (
    *CANONICAL_CLAIMS_STRICT_FIELDNAMES,
    "pii_classification",
    "claim_label",
    "raw_metric",
)

LEADS_FIELDNAMES: tuple[str, ...] = (
    "lead_id",
    "name",
    "email",
    "linkedin_url",
    "github_url",
    "company",
    "role",
    "goal_fit",
    "lead_score",
    "reason_code_1",
    "reason_code_2",
    "reason_code_3",
    "sponsor_tech_used",
    "project_name",
    "judging_weighted_avg",
    "priority_tier",
)

PROJECT_TECH_EVIDENCE_FIELDNAMES: tuple[str, ...] = (
    "project_id",
    "project_name",
    "repo_url",
    "tool_category",
    "tool_name",
    "evidence_type",
    "evidence_file",
    "evidence_line",
    "evidence_snippet",
    "confidence",
    "self_report_match",
)


@dataclass(frozen=True, slots=True)
class SponsorAutomationInputs:
    """Filesystem inputs consumed by deterministic sponsor-automation logic."""

    marketing_master_csv: Path
    enriched_json: Path
    q18_markdown: Path


@dataclass(frozen=True, slots=True)
class MarketingMetricClaim:
    """One row from ``marketing_master.csv`` with deterministic claim metadata."""

    section: str
    metric: str
    value: str
    numerator: str
    denominator: str
    denominator_cohort: str
    coverage_pct: str
    source: str
    pii_classification: str
    confidence: str
    claim_label: str
    notes: str

    def as_marketing_master_row(self) -> dict[str, str]:
        return {
            "section": self.section,
            "metric": self.metric,
            "value": self.value,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "denominator_cohort": self.denominator_cohort,
            "coverage_pct": self.coverage_pct,
            "source": self.source,
            "pii_classification": self.pii_classification,
            "confidence": self.confidence,
            "claim_label": self.claim_label,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class CanonicalClaimRow:
    """Canonical row used for ``sponsor_metrics_long``-style exports."""

    section: str
    metric_name: str
    segment: str
    value: str
    numerator: str
    denominator: str
    denominator_cohort: str
    coverage_pct: str
    source_table_or_file: str
    calculation_note: str
    confidence: str
    pii_classification: str
    claim_label: str
    raw_metric: str

    def as_strict_row(self) -> dict[str, str]:
        return {
            "section": self.section,
            "metric_name": self.metric_name,
            "segment": self.segment,
            "value": self.value,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "denominator_cohort": self.denominator_cohort,
            "coverage_pct": self.coverage_pct,
            "source_table_or_file": self.source_table_or_file,
            "calculation_note": self.calculation_note,
            "confidence": self.confidence,
        }

    def as_full_row(self) -> dict[str, str]:
        return {
            **self.as_strict_row(),
            "pii_classification": self.pii_classification,
            "claim_label": self.claim_label,
            "raw_metric": self.raw_metric,
        }


@dataclass(frozen=True, slots=True)
class TopLeadCandidate:
    """Ranked lead row parsed from ``sectionC_agent5_q18.md``."""

    rank: int
    name: str
    email: str
    company: str
    role_bucket: str
    metric_name: str
    lead_score: str
    numerator: str
    denominator: str
    denominator_cohort: str
    coverage_pct: str
    source: str
    confidence: str
    claim_label: str
    reason_codes: str


@dataclass(frozen=True, slots=True)
class SponsorLeadRow:
    """Internal-only sponsor lead row."""

    lead_id: str
    name: str
    email: str
    linkedin_url: str
    github_url: str
    company: str
    role: str
    goal_fit: str
    lead_score: str
    reason_code_1: str
    reason_code_2: str
    reason_code_3: str
    sponsor_tech_used: str
    project_name: str
    judging_weighted_avg: str
    priority_tier: str

    def as_row(self) -> dict[str, str]:
        return {
            "lead_id": self.lead_id,
            "name": self.name,
            "email": self.email,
            "linkedin_url": self.linkedin_url,
            "github_url": self.github_url,
            "company": self.company,
            "role": self.role,
            "goal_fit": self.goal_fit,
            "lead_score": self.lead_score,
            "reason_code_1": self.reason_code_1,
            "reason_code_2": self.reason_code_2,
            "reason_code_3": self.reason_code_3,
            "sponsor_tech_used": self.sponsor_tech_used,
            "project_name": self.project_name,
            "judging_weighted_avg": self.judging_weighted_avg,
            "priority_tier": self.priority_tier,
        }


@dataclass(frozen=True, slots=True)
class ProjectTechEvidenceRow:
    """Deterministic project technology evidence row."""

    project_id: str
    project_name: str
    repo_url: str
    tool_category: str
    tool_name: str
    evidence_type: str
    evidence_file: str
    evidence_line: str
    evidence_snippet: str
    confidence: str
    self_report_match: str

    def as_row(self) -> dict[str, str]:
        return {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "repo_url": self.repo_url,
            "tool_category": self.tool_category,
            "tool_name": self.tool_name,
            "evidence_type": self.evidence_type,
            "evidence_file": self.evidence_file,
            "evidence_line": self.evidence_line,
            "evidence_snippet": self.evidence_snippet,
            "confidence": self.confidence,
            "self_report_match": self.self_report_match,
        }


@dataclass(frozen=True, slots=True)
class DeterministicSponsorOutputs:
    """Container with deterministic outputs consumed by downstream orchestration."""

    canonical_claims: tuple[CanonicalClaimRow, ...]
    sponsor_leads: tuple[SponsorLeadRow, ...]
    project_evidence: tuple[ProjectTechEvidenceRow, ...]
