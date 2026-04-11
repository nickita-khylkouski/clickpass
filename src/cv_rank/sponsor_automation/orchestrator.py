from __future__ import annotations

import csv
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from cv_rank.sponsor_automation.cache import read_cache, write_cache
from cv_rank.sponsor_automation.narrative import (
    SECTION_ORDER as NARRATIVE_SECTION_ORDER,
    generate_narrative_sections,
    render_report_markdown,
    synthesize_final_document,
)
from cv_rank.sponsor_automation.policy import filter_allowed_claims
from cv_rank.sponsor_automation.qa import run_qa_checks

REQUIRED_CSV_FILENAMES = [
    "sponsor_metrics_long.csv",
    "sponsor_metrics_long_full.csv",
    "sponsor_leads_internal.csv",
    "project_tech_evidence.csv",
]

ALLOWED_DENOMINATOR_COHORTS = {
    "all_applicants",
    "approved",
    "checked_in",
    "submitters",
    "placed",
}

PACKET_SECTION_ORDER = (
    "executive",
    "adoption",
    "marketing",
    "hiring",
    "sales_bd",
    "funnel",
    "market_research",
)

SECTION_LIMITS: dict[str, int] = {
    "funnel": 12,
    "hiring": 12,
    "sales_bd": 12,
    "market_research": 18,
    "marketing": 12,
    "adoption": 20,
}

SECTION_PRIORITY_METRICS: dict[str, set[str]] = {
    "funnel": {
        "applied_to_approved",
        "approved_to_checked_in",
        "checked_in_to_submitted",
        "submitted_to_placed",
        "conversion_applied_to_approved_pct",
        "conversion_approved_to_checked_in_pct",
        "conversion_checked_in_to_submitted_pct",
        "conversion_submitted_to_placed_pct",
        "largest_dropoff_absolute_count",
        "largest_dropoff_percentage",
    },
    "hiring": {
        "q1_hiring_ready_checked_in",
        "q2_hiring_ready_with_gh_li",
        "q6_high_judging_strong_github_overlap_submitters",
        "q17_top25_hiring_leads_with_reason_codes",
        "q18_top50_hiring_leads_with_reason_codes",
    },
    "sales_bd": {
        "q13_top_performers_using_sponsor_tech",
        "q17_top25_hiring_leads_with_reason_codes",
        "q18_top50_hiring_leads_with_reason_codes",
        "founder",
        "decision_maker",
        "checked_in_count",
        "submitters_count",
        "placed_from_submitters",
    },
    "adoption": {
        "q1_submitter_gemini_adoption",
        "q2_submitter_antigravity_adoption",
        "q3_submitter_llamaindex_adoption",
        "q4_submitter_temporal_adoption",
        "q5_submitter_agno_adoption",
        "q7_top_pair_gemini_antigravity_team_frequency",
        "q8_teams_using_multiple_tools_2plus",
        "q6_placed_gemini_antigravity_adoption",
        "q9_projects_with_sdk_package_evidence",
        "q11_projects_with_model_ids",
        "q12_projects_with_app_endpoint_signals",
        "projects_with_any_sdk_package_evidence",
        "projects_with_any_model_id",
        "lyria",
        "veo",
        "gemini-3-flash",
        "gemini-2.5-flash",
        "gemini-3.1-pro",
    },
    "marketing": {
        "q13_gemini_plus_any_partner_story_anchor",
        "h25_proof_point_projects_with_sdk_evidence_share",
        "q9_projects_with_sdk_package_evidence",
        "q11_projects_with_model_ids",
        "q7_top_pair_gemini_antigravity_team_frequency",
        "q10_top_school_nyu_in_top_performers",
    },
    "market_research": {
        "q1_largest_experience_bucket_2_4_years",
        "q2_largest_exclusive_role_bucket_student",
        "q3_us_country_share_all_applicants",
        "q4_startup_employer_share_all_applicants",
        "q5_primary_role_student_share",
        "q6_language_signal_coverage_all_applicants",
        "q10_top_theme_media_music_creator_share",
        "q11_top_avg_score_theme_climate_sustainability",
        "q12_top_unmet_need_agent_orchestration_reliability",
        "q13_underrepresented_high_performing_segment_count",
        "Python.count.checked_in",
        "JavaScript.count.checked_in",
        "TypeScript.count.checked_in",
        "FastAPI.count.checked_in",
        "React.count.checked_in",
        "Next.js.count.checked_in",
        "startup.count.all_applicants",
        "academic.count.all_applicants",
        "big_tech.count.all_applicants",
        "other_or_unknown.count.all_applicants",
        "DevTools/Infra",
        "Climate/Sustainability",
    },
}


@dataclass
class PipelineConfig:
    repo_root: Path
    output_root: Path
    event_name: str | None = None
    run_id: str | None = None
    enriched_json: str | None = None
    mode: str = "auto"
    reviewer_mode: str = "strict"
    pause_after_metrics: bool = False
    pause_after_review: bool = False
    openai_model: str | None = None
    openai_editor_model: str | None = None


@dataclass
class ExtractionArtifacts:
    required_csv_paths: list[Path]
    sponsor_report_path: Path | None
    headline_bank_path: Path | None


@dataclass
class Claim:
    claim_id: str
    section: str
    metric_name: str
    segment: str
    value: str
    numerator: str
    denominator: str
    denominator_cohort: str
    coverage_pct: str
    confidence: str
    source_table_or_file: str
    claim_label: str
    notes: str
    claim_text: str


@dataclass
class QAReport:
    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class PipelineResult:
    event_slug: str
    output_dir: Path
    packet_path: Path
    run_audit_path: Path
    copied_csv_paths: list[Path]
    claims_path: Path
    qa_report: QAReport
    stage_audit: list[dict[str, Any]]


def run_pipeline(config: PipelineConfig) -> PipelineResult:
    repo_root = config.repo_root.resolve()
    output_root = config.output_root.resolve()
    stage_audit: list[dict[str, Any]] = []

    _run_stage(stage_audit, "load_env", lambda: _load_env(repo_root), detail="load .env")

    event_slug = _derive_event_slug(config.event_name, config.run_id)
    run_id = _normalize_run_id(config.run_id)

    extraction = _run_stage(
        stage_audit,
        "deterministic_extraction",
        lambda: _run_deterministic_extraction(
            repo_root,
            config.run_id,
            config.event_name,
            config.enriched_json,
        ),
        detail=f"event_slug={event_slug}",
    )

    if config.pause_after_metrics:
        _maybe_pause(config, "Pause requested after deterministic metrics extraction.")

    claims = _run_stage(
        stage_audit,
        "claims_generation",
        lambda: _generate_claims(extraction.required_csv_paths[0]),
        detail=f"metrics_csv={extraction.required_csv_paths[0]}",
    )

    policy_report, allowed_claims = _run_stage(
        stage_audit,
        "policy_filtering",
        lambda: _run_policy_filter(claims),
        detail=f"input_claims={len(claims)}",
    )

    reviewer_report, reviewed_claims = _run_stage(
        stage_audit,
        "claim_reviewer",
        lambda: _run_claim_reviewer(allowed_claims, mode=config.reviewer_mode),
        detail=f"mode={config.reviewer_mode}, input_claims={len(allowed_claims)}",
    )

    if config.pause_after_review:
        _maybe_pause(config, "Pause requested after claim reviewer stage.")

    if config.mode == "semi":
        _maybe_pause(config, "Semi mode pause before OpenAI narrative generation.")

    narrative = _run_stage(
        stage_audit,
        "openai_narrative_generation",
        lambda: _generate_narrative(
            reviewed_claims,
            config.event_name,
            run_id,
            config.openai_model,
            config.openai_editor_model,
        ),
        detail=f"claims={len(reviewed_claims)}",
    )

    narrative = _run_stage(
        stage_audit,
        "dense_packet_generation",
        lambda: _generate_dense_packet_markdown(
            repo_root=repo_root,
            event_name=config.event_name,
            run_id=config.run_id,
            enriched_json=config.enriched_json,
            metrics_csv=extraction.required_csv_paths[0],
            metrics_full_csv=extraction.required_csv_paths[1],
            evidence_csv=extraction.required_csv_paths[3],
            fallback_markdown=narrative,
        ),
        detail="build deterministic long-form sponsor packet from enriched event + tool evidence",
    )

    qa_report = _run_stage(
        stage_audit,
        "qa_validation",
        lambda: _run_qa(
            claims=reviewed_claims,
            policy_report=policy_report,
            reviewer_report=reviewer_report,
            narrative=narrative,
            required_csv_paths=extraction.required_csv_paths,
        ),
        detail="qa checks on filtered claims + narrative + csv artifacts",
    )

    publish_module = _load_publish_module()
    publish_result = _run_stage(
        stage_audit,
        "output_assembly_publish",
        lambda: publish_module.publish_outputs(
            event_slug=event_slug,
            event_name=config.event_name,
            run_id=run_id,
            output_root=output_root,
            repo_root=repo_root,
            narrative_markdown=narrative,
            claims=[c.__dict__ for c in reviewed_claims],
            required_csv_paths=extraction.required_csv_paths,
            headline_bank_path=extraction.headline_bank_path,
            qa_report=qa_report.__dict__,
            stage_audit=stage_audit,
        ),
        detail=f"output_dir={output_root / event_slug}",
    )

    publish_module.write_run_audit(
        event_name=config.event_name,
        event_slug=event_slug,
        run_id=run_id,
        output_dir=Path(publish_result["output_dir"]),
        qa_report=qa_report.__dict__,
        stage_audit=stage_audit,
        copied_csv_paths=[Path(p) for p in publish_result["copied_csv_paths"]],
        claims_path=Path(publish_result["claims_path"]),
        packet_path=Path(publish_result["packet_path"]),
    )

    return PipelineResult(
        event_slug=event_slug,
        output_dir=Path(publish_result["output_dir"]),
        packet_path=Path(publish_result["packet_path"]),
        run_audit_path=Path(publish_result["run_audit_path"]),
        copied_csv_paths=[Path(p) for p in publish_result["copied_csv_paths"]],
        claims_path=Path(publish_result["claims_path"]),
        qa_report=qa_report,
        stage_audit=stage_audit,
    )


def _run_stage(stage_audit: list[dict[str, Any]], stage_name: str, fn, *, detail: str = ""):
    started_at = _now_iso()
    t0 = time.perf_counter()
    try:
        result = fn()
    except Exception as exc:
        stage_audit.append(
            {
                "stage": stage_name,
                "status": "failed",
                "started_at": started_at,
                "ended_at": _now_iso(),
                "duration_sec": round(time.perf_counter() - t0, 3),
                "detail": detail,
                "error": str(exc),
            }
        )
        raise

    stage_audit.append(
        {
            "stage": stage_name,
            "status": "ok",
            "started_at": started_at,
            "ended_at": _now_iso(),
            "duration_sec": round(time.perf_counter() - t0, 3),
            "detail": detail,
        }
    )
    return result


def _load_env(repo_root: Path) -> None:
    load_dotenv(repo_root / ".env", override=False)


def _run_deterministic_extraction(
    repo_root: Path,
    run_id: str | None,
    event_name: str | None,
    enriched_json: str | None,
) -> ExtractionArtifacts:
    script_path = repo_root / "scripts" / "build_sponsor_delivery_bundle.py"
    if not script_path.exists():
        raise FileNotFoundError(f"Missing deterministic extraction script: {script_path}")

    cmd = [sys.executable, str(script_path), "--base", str(repo_root)]
    if run_id:
        cmd.extend(["--run-id", run_id])
    if event_name:
        cmd.extend(["--event-label", event_name])
    if enriched_json:
        cmd.extend(["--enriched-json", enriched_json])

    proc = subprocess.run(
        cmd,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "Deterministic extraction failed "
            f"(exit={proc.returncode}). stderr={proc.stderr.strip()[:800]}"
        )

    required_csv_paths = [repo_root / name for name in REQUIRED_CSV_FILENAMES]
    missing = [p for p in required_csv_paths if not p.exists()]
    if missing:
        missing_str = ", ".join(str(p) for p in missing)
        raise FileNotFoundError(f"Deterministic extraction did not emit required CSVs: {missing_str}")

    sponsor_report_path = repo_root / "sponsor_report_marketing_readable.md"
    headline_bank_path = repo_root / "SPONSOR_HEADLINE_BANK.md"

    return ExtractionArtifacts(
        required_csv_paths=required_csv_paths,
        sponsor_report_path=sponsor_report_path if sponsor_report_path.exists() else None,
        headline_bank_path=headline_bank_path if headline_bank_path.exists() else None,
    )


def _parse_float(value: str) -> float:
    try:
        return float((value or "").strip().replace("%", ""))
    except ValueError:
        return 0.0


def _derive_narrative_section(row: dict[str, str]) -> str | None:
    sec = (row.get("section") or "").strip().lower()
    metric_name = (row.get("metric_name") or "").strip().lower()
    segment = (row.get("segment") or "").strip().lower()

    if sec in {"funnel", "q1", "q2", "q3_segment", "q4_company_type", "q5_years_experience", "q6_geography"}:
        return "funnel"
    if sec == "sectionb_exec":
        return "hiring"
    if sec == "sectionc_exec":
        return "sales_bd"
    if sec == "sectiond_exec":
        return "market_research"
    if sec in {"sectione_exec", "sectiong_exec", "sectionh_exec"}:
        return "adoption"
    if sec == "sectionf_exec":
        return "marketing"
    if sec in {"q7", "q8", "q9", "q11", "q12", "h20", "h21", "h24", "q22", "q23"}:
        return "adoption"
    if sec in {"h25"}:
        return "marketing"
    if sec in {"q3", "q4", "q10"}:
        return "market_research"
    if sec in {"q17", "q18", "q19"}:
        return "hiring"
    if sec in {"q14", "q15", "q16", "q20"}:
        return "sales_bd"

    if "funnel" in metric_name or "dropoff" in metric_name:
        return "funnel"
    if (
        "adoption" in metric_name
        or "co_usage" in metric_name
        or "sponsor_tool" in metric_name
        or "model_id" in metric_name
        or "sdk" in metric_name
        or "lyria" in metric_name
        or "veo" in metric_name
        or "gemini" in metric_name
        or "agno" in metric_name
        or "temporal" in metric_name
        or "llamaindex" in metric_name
    ):
        return "adoption"
    if "hiring" in metric_name or "job" in metric_name or "lead" in metric_name:
        return "hiring"
    if "proof_point" in segment or "case_study" in segment or "story_anchor" in metric_name:
        return "marketing"
    if "theme" in metric_name or "language" in metric_name or "experience" in metric_name:
        return "market_research"

    return None


def _claim_score(section: str, row: dict[str, str]) -> tuple[int, float, float, float]:
    metric_name = row.get("metric_name", "")
    metric_name_l = metric_name.lower()
    segment_l = (row.get("segment") or "").lower()
    priority = 2 if metric_name in SECTION_PRIORITY_METRICS.get(section, set()) else 0
    if section == "adoption" and (
        "model_id" in metric_name_l
        or metric_name_l in {"lyria", "veo", "gemini-3-flash", "gemini-2.5-flash", "gemini-3.1-pro"}
    ):
        priority += 1
    if section == "adoption" and metric_name in {
        "q1_submitter_gemini_adoption",
        "q2_submitter_antigravity_adoption",
        "q3_submitter_llamaindex_adoption",
        "q4_submitter_temporal_adoption",
        "q5_submitter_agno_adoption",
    }:
        priority += 3
    if section == "adoption" and metric_name in {
        "q7_top_pair_gemini_antigravity_team_frequency",
        "q8_teams_using_multiple_tools_2plus",
        "q6_placed_gemini_antigravity_adoption",
        "q9_projects_with_sdk_package_evidence",
        "q11_projects_with_model_ids",
    }:
        priority += 2
    if section == "market_research" and (
        segment_l == "framework" or segment_l == "language" or "theme_placement_rate" in segment_l
    ):
        priority += 2
    if section == "sales_bd" and metric_name in {"checked_in_count", "submitters_count", "placed_from_submitters"}:
        priority += 2
    confidence = (row.get("confidence") or "").lower()
    conf_score = {"high": 3.0, "medium": 2.0, "low": 1.0}.get(confidence, 0.0)
    numerator = _parse_float(row.get("numerator", ""))
    magnitude = abs(_parse_float(row.get("value", "")))
    return (priority, conf_score, numerator, magnitude)


def _make_claim_text(row: dict[str, str], section: str) -> str:
    metric = row.get("metric_name", "metric")
    seg = row.get("segment", "all")
    numerator = row.get("numerator", "")
    denominator = row.get("denominator", "")
    value = row.get("value", "")
    if numerator and denominator:
        return f"{section}.{metric}[{seg}] = {numerator}/{denominator} ({_format_pct(value)})"
    return f"{section}.{metric}[{seg}] = {value}"


def _is_ratio_style_row(row: dict[str, str]) -> bool:
    raw_value = (row.get("value") or "").strip()
    if "%" in raw_value:
        return True
    val = _parse_float(raw_value)
    return 0.0 <= val <= 1.0


def _is_priority_claim(section: str, row: dict[str, str]) -> bool:
    return row.get("metric_name", "") in SECTION_PRIORITY_METRICS.get(section, set())


def _is_noise_row(row: dict[str, str]) -> bool:
    metric_name = (row.get("metric_name") or "").strip().lower()
    segment = (row.get("segment") or "").strip().lower()
    combined = f"{metric_name} {segment}".strip()
    section = (row.get("section") or "").strip().lower()
    source = (row.get("source_table_or_file") or "").strip().lower()

    if not metric_name:
        return True
    if section.startswith("i") and section not in {"sectioni_exec"}:
        return True
    if metric_name.startswith("rank_") or metric_name.startswith("master|"):
        return True
    if "consistency_exception" in segment:
        return True
    if "project:" in metric_name and not any(
        token in metric_name for token in {"model_id", "sdk", "placed_flag", "tool_combo_size"}
    ):
        return True
    if "lineage" in metric_name or "taxonomy" in metric_name or "inventory_count" in metric_name:
        return True
    if metric_name in {
        "q10_projects_with_external_base_urls",
        "projects_with_external_base_urls",
    }:
        return True
    numerator = _parse_float(row.get("numerator", ""))
    denominator = _parse_float(row.get("denominator", ""))
    if metric_name in {
        "q8_teams_using_multiple_tools_2plus",
        "q13_partner_story_anchor",
        "q13_gemini_plus_any_partner_story_anchor",
    }:
        if denominator > 0 and numerator <= 0:
            return True
    if metric_name == "q14_high_confidence_claim_share":
        return True
    if "critical_ranking_blocker" in combined:
        return True
    if "missing_" in metric_name and section not in {"sectioni_exec"}:
        return True
    if "data_gap" in combined and section not in {"sectioni_exec"}:
        return True
    if "@" in metric_name or "@" in segment:
        return True
    if "internal-only" in source or "restricted" in source:
        return True
    return False


def _row_key(row: dict[str, str], section: str) -> tuple[str, str, str]:
    return (
        section,
        row.get("metric_name", ""),
        row.get("segment", "all"),
    )


def _row_to_claim(row: dict[str, str], section: str, claim_idx: int) -> Claim:
    return Claim(
        claim_id=f"claim_{claim_idx:03d}",
        section=section,
        metric_name=row.get("metric_name", ""),
        segment=row.get("segment", "all"),
        value=row.get("value", ""),
        numerator=row.get("numerator", ""),
        denominator=row.get("denominator", ""),
        denominator_cohort=row.get("denominator_cohort", ""),
        coverage_pct=row.get("coverage_pct", ""),
        confidence=row.get("confidence", ""),
        source_table_or_file=row.get("source_table_or_file", ""),
        claim_label=row.get("claim_label", ""),
        notes=row.get("calculation_note", ""),
        claim_text=_make_claim_text(row, section),
    )


def _generate_claims(metrics_csv_path: Path, limit_per_section: int = 8) -> list[Claim]:
    rows: list[dict[str, str]] = []
    with metrics_csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            normalized = {k: (v or "").strip() for k, v in row.items()}
            rows.append(normalized)

    if not rows:
        raise ValueError(f"No rows found in metrics CSV: {metrics_csv_path}")

    grouped: dict[str, list[dict[str, str]]] = {key: [] for key in PACKET_SECTION_ORDER if key != "executive"}

    for row in rows:
        if _is_noise_row(row):
            continue
        section = _derive_narrative_section(row)
        if section is None or section == "executive":
            continue

        cohort = row.get("denominator_cohort", "")
        if cohort and cohort not in ALLOWED_DENOMINATOR_COHORTS:
            continue

        denominator = _parse_float(row.get("denominator", ""))
        numerator = _parse_float(row.get("numerator", ""))
        if denominator < 0 or numerator < 0:
            continue
        metric_name = row.get("metric_name", "")
        is_priority = _is_priority_claim(section, row)
        if denominator and denominator < 30 and not is_priority:
            continue

        if not is_priority and not _is_ratio_style_row(row):
            if not (row.get("section", "").lower().endswith("_exec") or denominator >= 100):
                continue

        if metric_name == "q12_projects_with_app_endpoint_signals" and numerator <= 0:
            continue

        grouped.setdefault(section, []).append(row)

        raw_section = (row.get("section") or "").strip().lower()
        metric_lower = (row.get("metric_name") or "").strip().lower()
        segment_lower = (row.get("segment") or "").strip().lower()
        if raw_section == "q2" and metric_lower in {"founder", "decision_maker"}:
            grouped.setdefault("sales_bd", []).append(row)
        if raw_section == "q3_segment" and metric_lower in {"checked_in_count", "submitters_count", "placed_from_submitters"}:
            if segment_lower in {"founders", "decision_makers"}:
                grouped.setdefault("sales_bd", []).append(row)
            grouped.setdefault("hiring", []).append(row)
        if raw_section == "sectione_exec" and metric_lower in {
            "q9_projects_with_sdk_package_evidence",
            "q11_projects_with_model_ids",
            "q8_teams_using_multiple_tools_2plus",
        }:
            grouped.setdefault("marketing", []).append(row)

    selected: list[Claim] = []
    seen: set[tuple[str, str, str]] = set()
    section_counts: dict[str, int] = {section: 0 for section in SECTION_LIMITS}
    claim_idx = 1
    for section in ("funnel", "hiring", "sales_bd", "market_research", "marketing", "adoption"):
        candidates = grouped.get(section, [])
        required_metrics = SECTION_PRIORITY_METRICS.get(section, set())
        required_rows = sorted(
            [r for r in candidates if r.get("metric_name", "") in required_metrics],
            key=lambda r: _claim_score(section, r),
            reverse=True,
        )
        ranked = sorted(candidates, key=lambda r: _claim_score(section, r), reverse=True)

        section_limit = max(limit_per_section, SECTION_LIMITS.get(section, limit_per_section))
        ordered_rows = required_rows + [r for r in ranked if r not in required_rows]

        for row in ordered_rows:
            if section_counts.get(section, 0) >= section_limit:
                break
            key = _row_key(row, section)
            if key in seen:
                continue
            seen.add(key)
            selected.append(_row_to_claim(row, section, claim_idx))
            section_counts[section] = section_counts.get(section, 0) + 1
            claim_idx += 1

    executive_candidates = []
    for c in selected:
        metric_l = (c.metric_name or "").lower()
        if c.metric_name in {
            "applied_to_approved",
            "approved_to_checked_in",
            "checked_in_to_submitted",
            "submitted_to_placed",
            "q1_submitter_gemini_adoption",
            "q2_submitter_antigravity_adoption",
            "q13_gemini_plus_any_partner_story_anchor",
            "q1_hiring_ready_checked_in",
            "q9_projects_with_sdk_package_evidence",
            "q11_projects_with_model_ids",
            "q8_teams_using_multiple_tools_2plus",
            "q14_project_inventory_submissions",
            "q13_partner_story_anchor",
        }:
            executive_candidates.append(c)
            continue
        if metric_l.startswith("submitter_tool_adoption_") or metric_l.startswith("team_tool_adoption_"):
            executive_candidates.append(c)
    if len(executive_candidates) < 6:
        remaining = [c for c in selected if c not in executive_candidates]
        executive_candidates.extend(remaining[: max(0, 6 - len(executive_candidates))])

    for c in executive_candidates[:8]:
        selected.append(
            Claim(
                claim_id=f"claim_{claim_idx:03d}",
                section="executive",
                metric_name=c.metric_name,
                segment=c.segment,
                value=c.value,
                numerator=c.numerator,
                denominator=c.denominator,
                denominator_cohort=c.denominator_cohort,
                coverage_pct=c.coverage_pct,
                confidence=c.confidence,
                source_table_or_file=c.source_table_or_file,
                claim_label=c.claim_label,
                notes=c.notes,
                claim_text=c.claim_text,
            )
        )
        claim_idx += 1

    if not selected:
        raise ValueError("No valid claims selected from sponsor_metrics_long.csv")

    return _drop_ratio_mismatch_claims(selected)


def _run_policy_filter(claims: list[Claim]) -> tuple[dict[str, Any], list[Claim]]:
    claim_dicts = [c.__dict__ for c in claims]
    policy_report = filter_allowed_claims(claim_dicts)

    blocked_ids = set(policy_report.get("blocked_claim_ids", []))
    allowed_claims = [c for c in claims if c.claim_id not in blocked_ids]
    if not allowed_claims:
        raise ValueError("Policy filter blocked all claims. Cannot generate narrative.")
    return policy_report, allowed_claims


def _claim_reviewer_block_reasons(claim: Claim) -> list[str]:
    metric = (claim.metric_name or "").strip().lower()
    segment = (claim.segment or "").strip().lower()
    combined = f"{metric} {segment}".strip()
    section = (claim.section or "").strip().lower()
    cohort = (claim.denominator_cohort or "").strip().lower()
    source = (claim.source_table_or_file or "").strip().lower()
    numerator = _parse_float(claim.numerator)
    denominator = _parse_float(claim.denominator)

    reasons: list[str] = []
    deficit_keywords = (
        "critical_ranking_blocker",
        "missing_",
        "data_gap",
        "sparse",
        "unreliable",
        "noisy",
    )

    # Drop misleading cross-cohort ratios (the exact issue user called out).
    if cohort == "all_applicants":
        if "submitter" in combined and "from_applied" not in combined and "placed_from_submitters" not in combined:
            reasons.append("cross_cohort_submitter_over_all_applicants")
        if section in {"marketing", "adoption"} and "cohort_size" in metric:
            reasons.append("non_actionable_cohort_size_for_section")

    # Drop synthetic helper rows that don't carry sponsor-facing signal.
    if metric in {
        "proof_point_submitter_cohort_size",
        "top50_share_of_all_applicants",
        "top_technical_performer_cohort_share_of_all_applicants",
    }:
        reasons.append("helper_metric_not_sponsor_facing")
    if "proof_point_submitter_cohort_size" in combined:
        reasons.append("helper_metric_not_sponsor_facing")

    # Drop tautology/placeholder rows that do not carry analytical signal.
    if metric in {
        "q10_projects_with_external_base_urls",
        "projects_with_external_base_urls",
    }:
        reasons.append("tautology_metric_external_urls")

    if metric in {
        "q8_teams_using_multiple_tools_2plus",
        "q13_partner_story_anchor",
        "q13_gemini_plus_any_partner_story_anchor",
    } and denominator > 0 and numerator <= 0:
        reasons.append("zero_signal_story_metric")

    # Avoid fragile high-level percentages with tiny denominators in sponsor-facing narrative.
    # Funnel stage metrics are exempt because they define core event flow.
    if denominator > 0 and denominator < 30 and section in {
        "executive",
        "hiring",
        "sales_bd",
        "marketing",
        "market_research",
        "adoption",
    }:
        reasons.append("tiny_denominator_for_business_narrative")

    # Remove ambiguous confidence-share meta rows from sponsor narrative.
    if "confidence_claim_share" in metric or metric == "q14_high_confidence_claim_share":
        reasons.append("meta_confidence_metric")

    # Avoid internal consistency lineage rows.
    if "consistency_exception" in segment or "consistency_exception" in source:
        reasons.append("internal_consistency_row")

    # Drop numerically-trivial 1:1 cohort-size framing in marketing/sales.
    if denominator > 0 and numerator == denominator and "cohort_size" in metric and section in {"marketing", "sales_bd"}:
        reasons.append("trivial_identity_ratio")

    # Keep sponsor-facing narrative opportunity-led; suppress internal deficit metrics.
    if section in {"executive", "hiring", "sales_bd", "marketing"}:
        if any(token in combined for token in deficit_keywords):
            reasons.append("deficit_metric_not_sponsor_facing")

    # Remove operational coverage/readiness rows that read as internal QA, not sponsor analysis.
    if metric in {
        "q2_hiring_ready_with_gh_li",
        "q3_language_signal_coverage_checked_in",
        "q4_github_quality_coverage_checked_in",
        "q5_judging_score_coverage_submitters",
        "q6_language_signal_coverage_all_applicants",
    }:
        reasons.append("coverage_metric_not_sponsor_narrative")

    # Drop "top list as % of all applicants" framing; keep list data in CSV only.
    if metric in {
        "q17_top25_hiring_leads_with_reason_codes",
        "q18_top50_hiring_leads_with_reason_codes",
    }:
        reasons.append("lead_list_share_not_narrative")

    # Hide low-density technical evidence rates from narrative when too low to market externally.
    if metric in {
        "q9_projects_with_sdk_package_evidence",
        "q11_projects_with_model_ids",
        "projects_with_any_sdk_package_evidence",
        "projects_with_any_model_id",
    }:
        if denominator > 0 and (numerator / denominator) < 0.40:
            reasons.append("low_signal_technical_evidence_rate")

    # Keep only one reason set per claim; duplicates removed.
    return sorted(set(reasons))


def _run_claim_reviewer(claims: list[Claim], *, mode: str = "strict") -> tuple[dict[str, Any], list[Claim]]:
    normalized_mode = (mode or "strict").strip().lower()
    if normalized_mode not in {"off", "strict"}:
        raise ValueError(f"Unsupported reviewer mode: {mode!r}. Use 'off' or 'strict'.")

    if normalized_mode == "off":
        return (
            {
                "mode": "off",
                "input_claims": len(claims),
                "blocked_count": 0,
                "allowed_count": len(claims),
                "blocked": [],
            },
            claims,
        )

    blocked: list[dict[str, Any]] = []
    kept: list[Claim] = []
    for claim in claims:
        reasons = _claim_reviewer_block_reasons(claim)
        if reasons:
            blocked.append(
                {
                    "claim_id": claim.claim_id,
                    "section": claim.section,
                    "metric_name": claim.metric_name,
                    "denominator_cohort": claim.denominator_cohort,
                    "reasons": reasons,
                }
            )
            continue
        kept.append(claim)

    if not kept:
        raise ValueError("Claim reviewer blocked all claims. Relax rules or use reviewer_mode=off.")

    report = {
        "mode": normalized_mode,
        "input_claims": len(claims),
        "blocked_count": len(blocked),
        "allowed_count": len(kept),
        "blocked": blocked,
    }
    return report, kept


def _ordered_models(primary: str | None, defaults: list[str]) -> list[str]:
    out: list[str] = []
    if primary and primary.strip():
        out.append(primary.strip())
    for model in defaults:
        if model not in out:
            out.append(model)
    return out


def _generate_narrative(
    claims: list[Claim],
    event_name: str | None,
    run_id: str,
    openai_model: str | None,
    openai_editor_model: str | None,
) -> str:
    primary_generation_model = openai_model or os.getenv("OPENAI_MODEL") or "gpt-5.2"
    primary_editor_model = openai_editor_model or os.getenv("OPENAI_EDITOR_MODEL") or "gpt-5.4"
    generation_models = _ordered_models(primary_generation_model, ["gpt-5.2", "gpt-5", "gpt-4.1-mini"])
    editor_models = _ordered_models(primary_editor_model, ["gpt-5.4", "gpt-5.2", "gpt-5", "gpt-4.1-mini"])
    claims_payload = [c.__dict__ for c in claims]

    cache_prompt = (
        "sponsor_narrative::"
        f"gen={generation_models[0]}::edit={editor_models[0]}::v6::{'|'.join(PACKET_SECTION_ORDER)}"
    )
    _, cached = read_cache(prompt=cache_prompt, claims=claims_payload)
    if isinstance(cached, dict):
        cached_text = cached.get("narrative_markdown")
        if isinstance(cached_text, str) and cached_text.strip():
            return cached_text.strip() + "\n"

    if not os.getenv("OPENAI_API_KEY"):
        narrative = _fallback_narrative(claims, event_name=event_name, run_id=run_id, model_used="fallback:no_api_key")
        write_cache(
            prompt=cache_prompt,
            claims=claims_payload,
            payload={
                "narrative_markdown": narrative,
                "generation_model": generation_models[0],
                "editor_model": editor_models[0],
            },
        )
        return narrative

    last_error: Exception | None = None
    client = None
    try:
        from openai import OpenAI

        from cv_rank.sponsor_automation.narrative import normalize_section_name

        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=90.0)
        claims_by_section: dict[str, list[dict[str, Any]]] = {s: [] for s in NARRATIVE_SECTION_ORDER}
        for claim in claims:
            canonical = normalize_section_name(claim.section)
            claims_by_section.setdefault(canonical, []).append(claim.__dict__)

        narrative = ""
        model_used = ""
        for generation_model in generation_models:
            section_rendered = False
            for editor_model in editor_models:
                try:
                    generated_sections = generate_narrative_sections(
                        client,
                        claims_by_section,
                        sections=PACKET_SECTION_ORDER,
                        model=generation_model,
                        run_editor_pass=True,
                        editor_model=editor_model,
                    )
                    narrative = render_report_markdown(generated_sections, section_order=PACKET_SECTION_ORDER)
                    narrative = synthesize_final_document(
                        client,
                        draft_markdown=narrative,
                        claims=claims_payload,
                        event_name=event_name,
                        model=editor_model,
                        max_words=1400,
                    )
                    narrative = _sanitize_narrative(narrative)
                    model_used = f"gen:{generation_model}|edit:{editor_model}"
                    section_rendered = True
                    break
                except Exception as exc:
                    last_error = exc
            if section_rendered:
                break

        if not narrative:
            raise RuntimeError(f"OpenAI narrative generation failed: {last_error}") from last_error
    except Exception:
        narrative = _fallback_narrative(claims, event_name=event_name, run_id=run_id, model_used="fallback:openai_error")
        model_used = "fallback:openai_error"
    finally:
        if client is not None:
            client.close()

    write_cache(
        prompt=cache_prompt,
        claims=claims_payload,
        payload={
            "narrative_markdown": narrative,
            "generation_model": generation_models[0],
            "editor_model": editor_models[0],
            "model_used": model_used,
        },
    )
    return narrative


def _generate_dense_packet_markdown(
    *,
    repo_root: Path,
    event_name: str | None,
    run_id: str | None,
    enriched_json: str | None,
    metrics_csv: Path,
    metrics_full_csv: Path,
    evidence_csv: Path,
    fallback_markdown: str,
) -> str:
    try:
        from cv_rank.sponsor_automation.dense_packet import build_dense_packet_markdown

        return build_dense_packet_markdown(
            repo_root=repo_root,
            event_name=event_name,
            run_id=run_id,
            enriched_json=enriched_json,
            metrics_csv=metrics_csv,
            metrics_full_csv=metrics_full_csv,
            evidence_csv=evidence_csv,
        )
    except Exception:
        return fallback_markdown


def _sanitize_narrative(markdown_text: str) -> str:
    out_lines: list[str] = []
    for line in markdown_text.splitlines():
        if re.match(r"^\s+-\s+", line):
            line = re.sub(r"^\s+-\s+", "- ", line)
        cleaned = re.sub(r"\s*\(?confidence\s*:\s*(high|medium|low)\)?", "", line, flags=re.I)
        cleaned = re.sub(r"\s*\|\s*source\s*:\s*.*$", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*\|\s*confidence\s*:\s*.*$", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).rstrip()
        if re.search(r"\bsponsor implication\b", cleaned, re.I):
            continue
        if re.search(r"\b(similar across both groups|nearly the same rate)\b", cleaned, re.I):
            continue
        if re.search(r"\bslightly (higher|lower|better|worse|stronger|weaker)\b", cleaned, re.I):
            continue
        if re.search(r"\b(source|claim_label|self_reported|observed_in_code)\b", cleaned, re.I):
            continue
        out_lines.append(cleaned)

    text = "\n".join(out_lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def _run_qa(
    *,
    claims: list[Claim],
    policy_report: dict[str, Any],
    reviewer_report: dict[str, Any],
    narrative: str,
    required_csv_paths: list[Path],
) -> QAReport:
    qa_raw = run_qa_checks(
        [c.__dict__ for c in claims],
        allowed_denominator_cohorts=ALLOWED_DENOMINATOR_COHORTS,
        tiny_sample_denominator_threshold=30,
    )

    errors: list[str] = []
    warnings: list[str] = []

    if not qa_raw.get("ok", False):
        for finding in qa_raw.get("findings", []):
            if finding.get("severity") == "error":
                errors.append(f"QA {finding.get('check')}: {finding.get('message')}")
    for finding in qa_raw.get("findings", []):
        if finding.get("severity") == "warning":
            warnings.append(f"QA {finding.get('check')}: {finding.get('message')}")

    blocked = policy_report.get("blocked", [])
    if blocked:
        warnings.append(f"Policy blocked {len(blocked)} claim(s) before narrative generation.")
    reviewer_blocked = int(reviewer_report.get("blocked_count") or 0)
    reviewer_mode = str(reviewer_report.get("mode") or "strict")
    if reviewer_mode != "off":
        warnings.append(
            f"Claim reviewer ({reviewer_mode}) blocked {reviewer_blocked} claim(s) before narrative generation."
        )

    if not narrative or len(narrative.strip()) < 300:
        errors.append("Narrative content is too short (<300 chars).")

    if re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", narrative, flags=re.I):
        errors.append("Narrative appears to contain email-like tokens.")

    if re.search(
        r"\b(source_table_or_file|claim_label|self_reported|observed_in_code|calculation_note|denominator_cohort)\b",
        narrative,
        flags=re.I,
    ):
        warnings.append("Narrative still includes internal metadata language; check prompt/output quality.")

    for csv_path in required_csv_paths:
        if not csv_path.exists():
            errors.append(f"Required CSV is missing: {csv_path}")
        elif csv_path.stat().st_size <= 16:
            errors.append(f"Required CSV appears empty: {csv_path}")

    return QAReport(passed=len(errors) == 0, errors=errors, warnings=warnings)


def _fallback_narrative(
    claims: list[Claim],
    *,
    event_name: str | None,
    run_id: str,
    model_used: str,
) -> str:
    title = event_name or "Sponsor Event"
    by_section: dict[str, list[Claim]] = {s: [] for s in PACKET_SECTION_ORDER}
    for claim in claims:
        by_section.setdefault(claim.section, []).append(claim)

    heading_map = {
        "executive": "Executive Summary",
        "adoption": "Sponsor Tech Adoption",
        "marketing": "Marketing Insights",
        "hiring": "Hiring Insights",
        "sales_bd": "Sales/BD Insights",
        "funnel": "Funnel",
        "market_research": "Market Research Insights",
    }

    lines = [f"# {title} Sponsor Packet", "", f"_Generated via {model_used}; run_id={run_id}_", ""]

    for section in PACKET_SECTION_ORDER:
        lines.append(f"## {heading_map[section]}")
        section_claims = by_section.get(section, [])[:6]
        if not section_claims:
            lines.append("- No validated claims available for this section.")
        else:
            for claim in section_claims:
                if claim.numerator and claim.denominator:
                    lines.append(
                        f"- {claim.numerator} of {claim.denominator} in `{claim.metric_name}` ({_format_pct(claim.value)})."
                    )
                else:
                    lines.append(f"- `{claim.metric_name}` value was {claim.value}.")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def _drop_ratio_mismatch_claims(claims: list[Claim]) -> list[Claim]:
    if not claims:
        return claims
    qa_raw = run_qa_checks(
        [c.__dict__ for c in claims],
        allowed_denominator_cohorts=ALLOWED_DENOMINATOR_COHORTS,
        tiny_sample_denominator_threshold=30,
    )
    bad_rows: set[int] = set()
    for finding in qa_raw.get("findings", []):
        if finding.get("check") != "ratio_recomputation":
            continue
        row_idx = finding.get("row_index")
        if isinstance(row_idx, int):
            bad_rows.add(row_idx)
    if not bad_rows:
        return claims
    filtered = [claim for idx, claim in enumerate(claims) if idx not in bad_rows]
    return filtered or claims


def _load_publish_module():
    try:
        from cv_rank.sponsor_automation import publish

        return publish
    except Exception:
        import importlib.util

        module_path = Path(__file__).with_name("publish.py")
        spec = importlib.util.spec_from_file_location("cv_rank.sponsor_automation.publish", module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load publish module from {module_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def _derive_event_slug(event_name: str | None, run_id: str | None) -> str:
    raw = (event_name or "").strip() or (run_id or "").strip() or "event"
    return _slugify(raw)


def _normalize_run_id(run_id: str | None) -> str:
    if run_id and run_id.strip():
        return run_id.strip().split("/")[-1]
    return "run_unknown"


def _maybe_pause(config: PipelineConfig, message: str) -> None:
    if not sys.stdin or not sys.stdin.isatty():
        return
    answer = input(f"{message} Continue? [y/N]: ").strip().lower()
    if answer not in {"y", "yes"}:
        raise RuntimeError("Pipeline paused by operator.")


def _slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "event"


def _format_pct(value: str) -> str:
    num = _parse_float(value)
    if 0 <= num <= 1.0:
        return f"{num * 100:.1f}%"
    return f"{num:.2f}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
