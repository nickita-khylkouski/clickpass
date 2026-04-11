"""Deterministic sponsor-automation transforms extracted from legacy bundle script."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlparse

from .contracts import (
    SPONSOR_TOOLS,
    CanonicalClaimRow,
    MarketingMetricClaim,
    ProjectTechEvidenceRow,
    SponsorLeadRow,
    TopLeadCandidate,
)


def split_metric(metric: str) -> tuple[str, str]:
    """Split ``segment.metric_name`` into ``(metric_name, segment)``."""

    if "." in metric:
        left, right = metric.split(".", 1)
        if left and right:
            return right, left
    return metric, "all"


def normalize_coverage_pct(value: str) -> str:
    """Normalize coverage percentages to numeric-looking strings (without ``%``)."""

    normalized = (value or "").strip()
    if not normalized:
        return ""
    if normalized.endswith("%"):
        normalized = normalized[:-1].strip()
    return normalized


def canonicalize_metric_claim(row: MarketingMetricClaim) -> CanonicalClaimRow:
    """Convert one raw metric claim into canonical long-form schema."""

    metric_name, segment = split_metric(row.metric)
    return CanonicalClaimRow(
        section=row.section,
        metric_name=metric_name,
        segment=segment,
        value=row.value,
        numerator=row.numerator,
        denominator=row.denominator,
        denominator_cohort=row.denominator_cohort,
        coverage_pct=normalize_coverage_pct(row.coverage_pct),
        source_table_or_file=row.source,
        calculation_note=row.notes,
        confidence=row.confidence,
        pii_classification=row.pii_classification,
        claim_label=row.claim_label,
        raw_metric=row.metric,
    )


def build_canonical_claim_rows(rows: Iterable[MarketingMetricClaim]) -> list[CanonicalClaimRow]:
    """Canonicalize all rows from ``marketing_master.csv`` with no row drops."""

    return [canonicalize_metric_claim(row) for row in rows]


def canonical_claim_table_rows(
    rows: Iterable[MarketingMetricClaim],
    *,
    include_internal_fields: bool = True,
) -> list[dict[str, str]]:
    """Return canonical claim table rows as dicts suitable for CSV output."""

    canonical = build_canonical_claim_rows(rows)
    if include_internal_fields:
        return [row.as_full_row() for row in canonical]
    return [row.as_strict_row() for row in canonical]


def metric_lookup(rows: Iterable[MarketingMetricClaim]) -> dict[tuple[str, str], MarketingMetricClaim]:
    """Lookup map keyed by ``(section, metric)``."""

    return {(row.section, row.metric): row for row in rows}


def sanitize_source(source: str) -> str:
    """Normalize source lineage strings for external-safe rendering."""

    normalized = (source or "").strip()
    normalized = re.sub(
        r"platform_db\.EventApplicant\(eventId=[0-9a-f\-]+\)",
        "platform_db.EventApplicant (Gemini 3 NYC)",
        normalized,
        flags=re.I,
    )
    normalized = re.sub(
        r"platform_db\.HackathonSubmission\+HackathonTeamMember\(eventId=[0-9a-f\-]+;placement!=NULL\)",
        "platform_db.HackathonSubmission+HackathonTeamMember (Gemini 3 NYC)",
        normalized,
        flags=re.I,
    )
    normalized = re.sub(
        r"platform_db\.EventApplicant\+HackathonSubmission\+HackathonTeamMember\(eventId=[0-9a-f\-]+\)",
        "platform_db.EventApplicant+HackathonSubmission+HackathonTeamMember (Gemini 3 NYC)",
        normalized,
        flags=re.I,
    )
    return normalized.replace("/Users/nickita/cv-rank/", "")


def format_metric_claim_line(row: MarketingMetricClaim) -> str:
    """Render one metric line in the same report format as the legacy script."""

    return (
        f"- `{row.section}.{row.metric}`: value={row.value}, numerator={row.numerator}, "
        f"denominator={row.denominator}, denominator_cohort={row.denominator_cohort}, "
        f"coverage_pct={normalize_coverage_pct(row.coverage_pct)}, confidence={row.confidence}, "
        f"source={sanitize_source(row.source)}"
    )


def parse_num(value: str) -> float:
    """Parse string values into floats with legacy fallbacks."""

    if not value:
        return 0.0

    normalized = value.strip().replace("%", "")
    try:
        return float(normalized)
    except ValueError:
        return 0.0


def top_claim_rows(rows: Iterable[MarketingMetricClaim], n: int = 6) -> list[MarketingMetricClaim]:
    """Top-N deterministic rows sorted by numerator then value (descending)."""

    sortable: list[tuple[float, float, MarketingMetricClaim]] = []
    for row in rows:
        sortable.append((parse_num(row.numerator), parse_num(row.value), row))

    sortable.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in sortable[:n]]


def normalize_yes_no_other(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"yes", "y", "true", "1", "open", "looking"}:
        return "yes"
    if normalized in {"no", "n", "false", "0", "not looking"}:
        return "no"
    return "other"


def parse_partner_tools(text: str) -> list[str]:
    normalized = (text or "").lower()
    found: list[str] = []
    for tool in SPONSOR_TOOLS:
        if re.search(re.escape(tool.lower()), normalized):
            found.append(tool)
    return found


def safe_str(value: Any) -> str:
    return "" if value is None else str(value)


def pick_profile_urls(profile: Mapping[str, Any]) -> tuple[str, str]:
    github_url = safe_str(profile.get("qa_github_url") or profile.get("github_url"))
    linkedin_url = safe_str(profile.get("qa_linkedin_url") or profile.get("linkedin_url"))
    return github_url, linkedin_url


def build_internal_sponsor_leads(
    enriched_profiles: Iterable[Mapping[str, Any]],
    top_candidates: Iterable[TopLeadCandidate],
) -> list[SponsorLeadRow]:
    """Build deterministic internal lead rows from enriched profiles + top-50 ranking."""

    by_email: dict[str, Mapping[str, Any]] = {}
    by_name: dict[str, list[Mapping[str, Any]]] = defaultdict(list)

    for profile in enriched_profiles:
        email = safe_str(profile.get("email")).lower()
        if email and email not in by_email:
            by_email[email] = profile

        name = safe_str(profile.get("name")).strip().lower()
        if name:
            by_name[name].append(profile)

    leads: list[SponsorLeadRow] = []
    for candidate in top_candidates:
        profile = by_email.get(candidate.email)
        if profile is None:
            candidates = by_name.get(candidate.name.strip().lower(), [])
            profile = candidates[0] if candidates else {}

        github_url, linkedin_url = pick_profile_urls(profile)
        submissions = profile.get("current_event_submissions") or []

        submission_names: list[str] = []
        sponsor_union: set[str] = set()
        for submission in submissions:
            team_name = safe_str(submission.get("team_name")).strip()
            if team_name:
                submission_names.append(team_name)

            sponsor_union.update(parse_partner_tools(safe_str(submission.get("Partner Technologies Used"))))
            for question_field, tool in [
                ("Did you try out Gemini 3? If so, what was your experience", "Gemini"),
                ("Did you try out Antigravity? If so, what was your experience", "Antigravity"),
                ("Did you try out LlamaIndex? If so, what was your experience?", "LlamaIndex"),
                ("Did you try out Temporal? If so, what was your experience?", "Temporal"),
                ("Did you try out Agno? If so, what was your experience", "Agno"),
            ]:
                text = safe_str(submission.get(question_field)).strip().lower()
                if text and text not in {"no", "n/a", "none", "not used"}:
                    sponsor_union.add(tool)

        project_name = sorted(set(submission_names))[0] if submission_names else ""
        sponsor_used = ", ".join(sorted(sponsor_union))

        reasons = [value for value in candidate.reason_codes.split(";") if value][:3]
        while len(reasons) < 3:
            reasons.append("")

        reason_codes = candidate.reason_codes
        if "JOB_SEEKING_YES" in reason_codes:
            goal_fit = "hiring"
        elif "DECISION_MAKER" in reason_codes or "ROLE_FOUNDER" in reason_codes:
            goal_fit = "sales_bd"
        elif "EVENT_SUBMITTER" in reason_codes:
            goal_fit = "marketing"
        else:
            goal_fit = "research"

        if candidate.rank <= 25:
            priority_tier = "A"
        elif candidate.rank <= 50:
            priority_tier = "B"
        else:
            priority_tier = "C"

        leads.append(
            SponsorLeadRow(
                lead_id=f"gem3nyc_lead_{candidate.rank:03d}",
                name=candidate.name,
                email=candidate.email,
                linkedin_url=linkedin_url,
                github_url=github_url,
                company=candidate.company or safe_str(profile.get("company")),
                role=candidate.role_bucket,
                goal_fit=goal_fit,
                lead_score=candidate.lead_score,
                reason_code_1=reasons[0],
                reason_code_2=reasons[1],
                reason_code_3=reasons[2],
                sponsor_tech_used=sponsor_used,
                project_name=project_name,
                judging_weighted_avg=safe_str(profile.get("current_event_judging_weighted_avg")),
                priority_tier=priority_tier,
            )
        )

    return leads


def iter_unique_projects(enriched_profiles: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return unique project submissions by ``submission_id`` preserving first-seen order."""

    by_submission_id: dict[str, dict[str, Any]] = {}
    for profile in enriched_profiles:
        for submission in profile.get("current_event_submissions") or []:
            submission_id = safe_str(submission.get("submission_id"))
            if submission_id and submission_id not in by_submission_id:
                by_submission_id[submission_id] = dict(submission)
    return list(by_submission_id.values())


def extract_urls(text: str) -> list[str]:
    if not text:
        return []

    urls = re.findall(r'https?://[^\s\)\]\|>"]+', text)
    return [url.rstrip(".,;") for url in urls]


def make_snippet(text: str, match: str, width: int = 140) -> str:
    lowered = text.lower()
    index = lowered.find(match.lower())
    if index < 0:
        return match

    start = max(0, index - 40)
    end = min(len(text), index + len(match) + 40)
    snippet = text[start:end].replace("\n", " ").strip()
    return snippet[:width]


def build_project_tech_evidence_rows(
    enriched_profiles: Iterable[Mapping[str, Any]],
) -> list[ProjectTechEvidenceRow]:
    """Build deterministic project-tech evidence rows from submission text."""

    projects = iter_unique_projects(enriched_profiles)

    provider_patterns = {
        "Gemini": re.compile(r"\bgemini\b", re.I),
        "Antigravity": re.compile(r"\bantigravity\b", re.I),
        "LlamaIndex": re.compile(r"\bllama\s*index\b|\bllamaindex\b", re.I),
        "Temporal": re.compile(r"\btemporal\b", re.I),
        "Agno": re.compile(r"\bagno\b", re.I),
        "Veo": re.compile(r"\bveo\b", re.I),
        "Lyria": re.compile(r"\blyria\b", re.I),
    }
    sdk_patterns = {
        "Genkit": re.compile(r"\bgenkit\b", re.I),
        "React": re.compile(r"\breact\b", re.I),
        "Next.js": re.compile(r"\bnext\.?(?:js)?\b", re.I),
        "FastAPI": re.compile(r"\bfastapi\b", re.I),
        "LangChain": re.compile(r"\blangchain\b", re.I),
        "Vite": re.compile(r"\bvite\b", re.I),
    }
    infra_patterns = {
        "Supabase": re.compile(r"\bsupabase\b", re.I),
        "Firebase": re.compile(r"\bfirebase\b", re.I),
        "Vercel": re.compile(r"\bvercel\b", re.I),
        "Railway": re.compile(r"\brailway\b", re.I),
        "Cloud Run": re.compile(r"\bcloud\s*run\b", re.I),
        "Docker": re.compile(r"\bdocker\b", re.I),
        "Postgres": re.compile(r"\bpostgres(?:ql)?\b", re.I),
        "MongoDB": re.compile(r"\bmongodb\b", re.I),
    }
    model_patterns = {
        "gemini-3-flash": re.compile(r"gemini\s*3(?:\.0)?\s*flash", re.I),
        "gemini-2.5-flash": re.compile(r"gemini\s*2\.5\s*flash", re.I),
        "gemini-3.1-pro": re.compile(r"gemini\s*3\.1\s*pro", re.I),
        "gemini-3": re.compile(r"gemini\s*3(?!\.1)", re.I),
        "gemini-2.0-flash": re.compile(r"gemini\s*2\.0\s*flash", re.I),
        "gemini-pro": re.compile(r"gemini\s*(?:pro|ultra)", re.I),
        "claude": re.compile(r"\bclaude\b", re.I),
        "llama": re.compile(r"\bllama\b", re.I),
        "whisper": re.compile(r"\bwhisper\b", re.I),
        "imagen": re.compile(r"\bimagen\b", re.I),
        "veo": re.compile(r"\bveo\b", re.I),
        "lyria": re.compile(r"\blyria\b", re.I),
    }

    evidence_rows: list[ProjectTechEvidenceRow] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()

    def emit(
        project_id: str,
        project_name: str,
        repo_url: str,
        tool_category: str,
        tool_name: str,
        evidence_type: str,
        evidence_file: str,
        evidence_snippet: str,
        confidence: str,
        self_report_match: str,
    ) -> None:
        key = (project_id, tool_category, tool_name, evidence_type, evidence_file, evidence_snippet)
        if key in seen:
            return

        seen.add(key)
        evidence_rows.append(
            ProjectTechEvidenceRow(
                project_id=project_id,
                project_name=project_name,
                repo_url=repo_url,
                tool_category=tool_category,
                tool_name=tool_name,
                evidence_type=evidence_type,
                evidence_file=evidence_file,
                evidence_line="",
                evidence_snippet=evidence_snippet,
                confidence=confidence,
                self_report_match=self_report_match,
            )
        )

    for submission in projects:
        project_id = safe_str(submission.get("submission_id"))
        project_name = safe_str(submission.get("team_name"))
        repo_url = safe_str(submission.get("Public GitHub Repository"))

        fields = {
            "Project Description": safe_str(submission.get("Project Description")),
            "Partner Technologies Used": safe_str(submission.get("Partner Technologies Used")),
            "Gemini Response": safe_str(submission.get("Did you try out Gemini 3? If so, what was your experience")),
            "Antigravity Response": safe_str(submission.get("Did you try out Antigravity? If so, what was your experience")),
            "LlamaIndex Response": safe_str(submission.get("Did you try out LlamaIndex? If so, what was your experience?")),
            "Temporal Response": safe_str(submission.get("Did you try out Temporal? If so, what was your experience?")),
            "Agno Response": safe_str(submission.get("Did you try out Agno? If so, what was your experience")),
            "Demo Video": safe_str(submission.get("Demo Video")),
            "Public GitHub Repository": repo_url,
        }

        self_reported = set(parse_partner_tools(fields["Partner Technologies Used"]))
        for field_name, tool in [
            ("Gemini Response", "Gemini"),
            ("Antigravity Response", "Antigravity"),
            ("LlamaIndex Response", "LlamaIndex"),
            ("Temporal Response", "Temporal"),
            ("Agno Response", "Agno"),
        ]:
            text = fields[field_name].strip().lower()
            if text and text not in {"no", "n/a", "none", "not used"}:
                self_reported.add(tool)

        for field_name, text in fields.items():
            if not text:
                continue

            for tool, pattern in provider_patterns.items():
                match = pattern.search(text)
                if match:
                    self_report_match = "unknown"
                    if tool in SPONSOR_TOOLS:
                        self_report_match = "yes" if tool in self_reported else "no"

                    confidence = (
                        "high"
                        if field_name
                        in {
                            "Partner Technologies Used",
                            "Gemini Response",
                            "Antigravity Response",
                            "LlamaIndex Response",
                            "Temporal Response",
                            "Agno Response",
                        }
                        else "medium"
                    )
                    emit(
                        project_id,
                        project_name,
                        repo_url,
                        "provider",
                        tool,
                        "string",
                        f"submission:{field_name}",
                        make_snippet(text, match.group(0)),
                        confidence,
                        self_report_match,
                    )

            for tool, pattern in sdk_patterns.items():
                match = pattern.search(text)
                if match:
                    emit(
                        project_id,
                        project_name,
                        repo_url,
                        "sdk",
                        tool,
                        "dependency",
                        f"submission:{field_name}",
                        make_snippet(text, match.group(0)),
                        "medium",
                        "unknown",
                    )

            for tool, pattern in infra_patterns.items():
                match = pattern.search(text)
                if match:
                    emit(
                        project_id,
                        project_name,
                        repo_url,
                        "infra",
                        tool,
                        "config",
                        f"submission:{field_name}",
                        make_snippet(text, match.group(0)),
                        "medium",
                        "unknown",
                    )

            for model, pattern in model_patterns.items():
                match = pattern.search(text)
                if match:
                    confidence = "high" if re.search(r"\d", match.group(0)) else "medium"
                    emit(
                        project_id,
                        project_name,
                        repo_url,
                        "model",
                        model,
                        "string",
                        f"submission:{field_name}",
                        make_snippet(text, match.group(0)),
                        confidence,
                        "unknown",
                    )

            for url in extract_urls(text):
                host = urlparse(url).netloc or url
                emit(
                    project_id,
                    project_name,
                    repo_url,
                    "endpoint",
                    host,
                    "http_call",
                    f"submission:{field_name}",
                    url[:180],
                    "high",
                    "unknown",
                )

            for match in re.finditer(r"/api/[A-Za-z0-9_\-/]+", text):
                emit(
                    project_id,
                    project_name,
                    repo_url,
                    "app_endpoint",
                    match.group(0),
                    "string",
                    f"submission:{field_name}",
                    make_snippet(text, match.group(0)),
                    "medium",
                    "unknown",
                )

            if re.search(r"\bwebhook\b", text, re.I):
                emit(
                    project_id,
                    project_name,
                    repo_url,
                    "app_endpoint",
                    "webhook",
                    "string",
                    f"submission:{field_name}",
                    make_snippet(text, "webhook"),
                    "medium",
                    "unknown",
                )

            if re.search(r"\bworker(s)?\b", text, re.I):
                emit(
                    project_id,
                    project_name,
                    repo_url,
                    "app_endpoint",
                    "worker",
                    "string",
                    f"submission:{field_name}",
                    make_snippet(text, "worker"),
                    "medium",
                    "unknown",
                )

    return evidence_rows
