from __future__ import annotations

import csv
import importlib.util
import json
import math
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from cv_rank.sponsor_automation.io import load_enriched_json
from cv_rank.sponsor_automation.orchestrator import (
    _generate_claims,
    _run_claim_reviewer,
    _run_policy_filter,
)


RAW_BUNDLE_FILENAMES = (
    "sponsor_metrics_long.csv",
    "sponsor_metrics_long_full.csv",
    "sponsor_leads_internal.csv",
    "project_tech_evidence.csv",
    "SPONSOR_HEADLINE_BANK.md",
)

ROLE_SEGMENTS = (
    "all",
    "founders",
    "decision_makers",
    "students",
    "ics",
    "startup_employees",
    "big_tech",
    "mango",
    "yc",
    "us_based",
)

THEME_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("DevTools/Infra", ("developer tool", "devtool", "infra", "infrastructure", "observability", "deployment", "orchestration", "sdk", "api", "agent ops", "workflow engine")),
    ("Fintech/Payments", ("payment", "wallet", "finance", "fintech", "trading", "bank", "credit", "defi", "coinbase", "invoice")),
    ("Creative Media/Gaming", ("music", "video", "creator", "media", "art", "design", "gaming", "game", "avatar", "audio", "film")),
    ("Productivity/Collaboration", ("collaboration", "workspace", "meeting", "notes", "document", "knowledge base", "productivity", "task manager", "assistant for teams")),
    ("Security/Compliance", ("security", "compliance", "fraud", "auth", "identity", "risk", "governance")),
    ("Health/Bio", ("health", "medical", "patient", "therapy", "clinic", "biotech", "bio")),
    ("Real Estate/Location", ("real estate", "property", "housing", "rent", "apartment", "location", "map", "travel")),
    ("Education/Knowledge", ("education", "tutor", "learning", "study", "student", "course", "knowledge")),
    ("Enterprise Ops/Sales", ("sales", "crm", "support", "operations", "back office", "recruiting", "workflow automation")),
)

MANGO_KEYWORDS = (
    "google",
    "microsoft",
    "meta",
    "amazon",
    "aws",
    "apple",
    "netflix",
)

GENERIC_EVIDENCE_TOOL_NAMES = {
    "",
    "https",
    "http",
    "github.com",
    "youtu.be",
    "www.youtube.com",
    "youtube.com",
    "www.loom.com",
    "loom.com",
}

EVIDENCE_TOOL_ALIAS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bmongodb\b|\batlas\b", re.I), "MongoDB"),
    (re.compile(r"\bfireworks?\b", re.I), "Fireworks"),
    (re.compile(r"\bvoyage(?:\s*ai)?\b|\bvoyagerai\b|\bvoyageai\b", re.I), "Voyage AI"),
    (re.compile(r"\bcoinbase\b|\bcoin\s*base\b|\bcdp\b", re.I), "Coinbase"),
    (re.compile(r"\bvercel\b", re.I), "Vercel"),
    (re.compile(r"\bnvidia\b|\bnemo\b", re.I), "NVIDIA"),
    (re.compile(r"\bthesys\b", re.I), "Thesys"),
    (re.compile(r"\bgalileo\b", re.I), "Galileo"),
)


@dataclass(frozen=True, slots=True)
class AnalysisConfig:
    repo_root: Path
    output_root: Path
    event_name: str | None = None
    run_id: str | None = None
    enriched_json: str | None = None
    reviewer_mode: str = "strict"


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    event_name: str
    event_slug: str
    output_dir: Path
    summary_path: Path
    tasks_path: Path
    snapshot_path: Path
    claim_review_path: Path
    segment_funnel_path: Path
    tool_summary_path: Path
    theme_summary_path: Path
    audience_summary_path: Path
    tool_outcomes_path: Path
    tool_reconciliation_path: Path
    top_projects_path: Path
    big_numbers_path: Path
    raw_bundle_paths: tuple[Path, ...]


def run_analysis_helper(config: AnalysisConfig) -> AnalysisResult:
    repo_root = config.repo_root.resolve()
    output_root = config.output_root.resolve()
    load_dotenv(repo_root / ".env", override=False)

    event_name = _resolve_event_name(repo_root, config.run_id, config.event_name)
    event_slug = _slugify(event_name)
    output_dir = output_root / event_slug / "analysis_helper"
    output_dir.mkdir(parents=True, exist_ok=True)

    enriched_path = resolve_enriched_json(repo_root, config.run_id, config.enriched_json)
    bundle_module = _load_bundle_script_module(repo_root)

    profiles = load_enriched_json(enriched_path)
    snapshot = bundle_module.load_event_snapshot(event_name)
    if snapshot is None:
        raise RuntimeError(f"Could not load event snapshot for event={event_name!r}")
    bundle_module.hydrate_enriched_with_event_snapshot(profiles, snapshot)
    bundle_module.validate_snapshot_alignment(profiles, snapshot, allow_mismatch=False)

    _run_raw_bundle_builder(repo_root, config.run_id, event_name, enriched_path)
    raw_bundle_paths = _copy_raw_bundle_outputs(repo_root, output_dir)

    claims = _generate_claims(repo_root / "sponsor_metrics_long.csv")
    policy_report, policy_allowed = _run_policy_filter(claims)
    reviewer_report, reviewer_allowed = _run_claim_reviewer(policy_allowed, mode=config.reviewer_mode)

    segment_funnel_rows = _build_segment_funnel_rows(profiles)
    tool_summary_rows = _build_tool_summary_rows(raw_bundle_paths[1], raw_bundle_paths[3])
    theme_summary_rows = _build_theme_summary_rows(snapshot.get("submissions", []))
    audience_summary_rows = _build_audience_summary_rows(profiles)
    tool_outcome_rows = _build_tool_outcome_rows(snapshot, tool_summary_rows)
    tool_reconciliation_rows = _build_tool_reconciliation_rows(tool_summary_rows)
    top_project_rows = _build_top_project_rows(snapshot, raw_bundle_paths[3])
    big_number_rows = _build_big_number_rows(profiles)
    claim_review_rows = _build_claim_review_rows(claims, policy_report, reviewer_report)

    summary_path = output_dir / "ANALYSIS_SUMMARY.md"
    tasks_path = output_dir / "ANALYSIS_TASKS.md"
    snapshot_path = output_dir / "analysis_snapshot.json"
    claim_review_path = output_dir / "analysis_claim_review.csv"
    segment_funnel_path = output_dir / "analysis_segment_funnel.csv"
    tool_summary_path = output_dir / "analysis_tool_summary.csv"
    theme_summary_path = output_dir / "analysis_theme_summary.csv"
    audience_summary_path = output_dir / "analysis_audience_summary.csv"
    tool_outcomes_path = output_dir / "analysis_tool_outcomes.csv"
    tool_reconciliation_path = output_dir / "analysis_tool_reconciliation.csv"
    top_projects_path = output_dir / "analysis_top_projects.csv"
    big_numbers_path = output_dir / "analysis_big_numbers.csv"

    _write_csv(segment_funnel_path, segment_funnel_rows)
    _write_csv(tool_summary_path, tool_summary_rows)
    _write_csv(theme_summary_path, theme_summary_rows)
    _write_csv(audience_summary_path, audience_summary_rows)
    _write_csv(tool_outcomes_path, tool_outcome_rows)
    _write_csv(tool_reconciliation_path, tool_reconciliation_rows)
    _write_csv(top_projects_path, top_project_rows)
    _write_csv(big_numbers_path, big_number_rows)
    _write_csv(claim_review_path, claim_review_rows)

    analysis_snapshot = _build_snapshot_payload(
        event_name=event_name,
        enriched_path=enriched_path,
        profiles=profiles,
        snapshot=snapshot,
        segment_funnel_rows=segment_funnel_rows,
        tool_summary_rows=tool_summary_rows,
        theme_summary_rows=theme_summary_rows,
        audience_summary_rows=audience_summary_rows,
        top_project_rows=top_project_rows,
        big_number_rows=big_number_rows,
        claim_review_rows=claim_review_rows,
        reviewer_allowed_count=len(reviewer_allowed),
    )
    snapshot_path.write_text(json.dumps(analysis_snapshot, indent=2), encoding="utf-8")
    summary_path.write_text(
        _render_summary(
            event_name=event_name,
            analysis_snapshot=analysis_snapshot,
            segment_funnel_rows=segment_funnel_rows,
            tool_summary_rows=tool_summary_rows,
            theme_summary_rows=theme_summary_rows,
            audience_summary_rows=audience_summary_rows,
            top_project_rows=top_project_rows,
            big_number_rows=big_number_rows,
            claim_review_rows=claim_review_rows,
        ),
        encoding="utf-8",
    )
    tasks_path.write_text(
        _render_tasks(
            event_name=event_name,
            analysis_snapshot=analysis_snapshot,
            claim_review_rows=claim_review_rows,
        ),
        encoding="utf-8",
    )

    return AnalysisResult(
        event_name=event_name,
        event_slug=event_slug,
        output_dir=output_dir,
        summary_path=summary_path,
        tasks_path=tasks_path,
        snapshot_path=snapshot_path,
        claim_review_path=claim_review_path,
        segment_funnel_path=segment_funnel_path,
        tool_summary_path=tool_summary_path,
        theme_summary_path=theme_summary_path,
        audience_summary_path=audience_summary_path,
        tool_outcomes_path=tool_outcomes_path,
        tool_reconciliation_path=tool_reconciliation_path,
        top_projects_path=top_projects_path,
        big_numbers_path=big_numbers_path,
        raw_bundle_paths=tuple(raw_bundle_paths),
    )


def resolve_enriched_json(repo_root: Path, run_id: str | None, explicit_path: str | None) -> Path:
    if explicit_path:
        path = Path(explicit_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Explicit enriched JSON not found: {path}")
        return path

    if run_id:
        run_dir = repo_root / "results" / run_id
        if not run_dir.exists():
            raise FileNotFoundError(f"Run directory not found: {run_dir}")
        candidates = sorted(run_dir.glob("enriched_complete*.json"))
        if candidates:
            return max(candidates, key=lambda p: p.stat().st_mtime)
        fallback = run_dir / "enriched.json"
        if fallback.exists():
            return fallback.resolve()
        raise FileNotFoundError(
            f"No enriched_complete*.json or enriched.json found in run directory: {run_dir}"
        )

    latest_candidates = []
    for run_dir in (repo_root / "results").glob("run_*"):
        latest_candidates.extend(run_dir.glob("enriched_complete*.json"))
        fallback = run_dir / "enriched.json"
        if fallback.exists():
            latest_candidates.append(fallback)
    if not latest_candidates:
        raise FileNotFoundError(f"No enriched_complete*.json or enriched.json found under {repo_root / 'results'}")
    return max(latest_candidates, key=lambda p: p.stat().st_mtime)


def _load_bundle_script_module(repo_root: Path):
    script_path = repo_root / "scripts" / "build_sponsor_delivery_bundle.py"
    spec = importlib.util.spec_from_file_location("cv_rank.bundle_builder", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not import bundle builder from {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _resolve_event_name(repo_root: Path, run_id: str | None, explicit_event_name: str | None) -> str:
    if explicit_event_name and explicit_event_name.strip():
        return explicit_event_name.strip()
    if run_id:
        meta_path = repo_root / "results" / run_id / "meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            csv_path = _safe_str(meta.get("csv_path"))
            prefix = "Platform DB event: "
            if csv_path.startswith(prefix):
                return csv_path[len(prefix):].strip()
    raise ValueError("Event name could not be resolved. Provide --event or a run_id with Platform DB event metadata.")


def _run_raw_bundle_builder(repo_root: Path, run_id: str | None, event_name: str, enriched_path: Path) -> None:
    cmd = [
        sys.executable,
        str(repo_root / "scripts" / "build_sponsor_delivery_bundle.py"),
        "--base",
        str(repo_root),
        "--event-label",
        event_name,
        "--enriched-json",
        str(enriched_path),
    ]
    if run_id:
        cmd.extend(["--run-id", run_id])
    proc = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            "Raw sponsor bundle build failed "
            f"(exit={proc.returncode}). stderr={proc.stderr.strip()[:1000]}"
        )


def _copy_raw_bundle_outputs(repo_root: Path, output_dir: Path) -> list[Path]:
    copied: list[Path] = []
    for filename in RAW_BUNDLE_FILENAMES:
        src = repo_root / filename
        if not src.exists():
            raise FileNotFoundError(f"Expected raw bundle output missing: {src}")
        dest = output_dir / filename
        shutil.copy2(src, dest)
        copied.append(dest)
    return copied


def _build_segment_funnel_rows(profiles: list[dict[str, Any]]) -> list[dict[str, str]]:
    audience_context = _resolve_audience_context(profiles)
    audience_filter = audience_context["filter"]
    rows: list[dict[str, str]] = []
    for segment in ROLE_SEGMENTS:
        members = [p for p in profiles if _segment_match(p, segment)]
        applied = len(members)
        approved = sum(1 for p in members if _is_approved(p))
        checked_in = sum(1 for p in members if _is_checked_in(p))
        audience = sum(1 for p in members if audience_filter(p))
        submitters = sum(1 for p in members if _is_submitter(p))
        placed = sum(1 for p in members if _is_placed(p))
        rows.append(
            {
                "segment": segment,
                "applied": str(applied),
                "approved": str(approved),
                "checked_in": str(checked_in),
                "audience": str(audience),
                "audience_label": audience_context["label"],
                "submitters": str(submitters),
                "placed": str(placed),
                "applied_to_approved_pct": _pct_str(approved, applied),
                "approved_to_checked_in_pct": _pct_str(checked_in, approved),
                "approved_to_audience_pct": _pct_str(audience, approved),
                "checked_in_to_submitted_pct": _pct_str(submitters, checked_in),
                "audience_to_submitted_pct": _pct_str(submitters, audience),
                "submitted_to_placed_pct": _pct_str(placed, submitters),
            }
        )
    return rows


def _build_tool_summary_rows(metrics_full_csv: Path, evidence_csv: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    metric_rows = _load_csv_rows(metrics_full_csv)
    allowed_tools: set[str] = set()
    for row in metric_rows:
        raw_metric = _safe_str(row.get("raw_metric"))
        count = _safe_str(row.get("numerator"))
        denominator = _safe_str(row.get("denominator"))
        pct = _safe_str(row.get("coverage_pct"))
        if raw_metric.startswith("submitter_tool_adoption_"):
            tool_name = _humanize_metric_suffix(raw_metric.removeprefix("submitter_tool_adoption_"))
            allowed_tools.add(tool_name)
            rows.append(
                {
                    "group": "submitter_person",
                    "name": tool_name,
                    "count": count,
                    "denominator": denominator,
                    "pct": f"{pct}%",
                    "note": "self_reported_partner_tools",
                }
            )
        elif raw_metric.startswith("team_tool_adoption_"):
            tool_name = _humanize_metric_suffix(raw_metric.removeprefix("team_tool_adoption_"))
            allowed_tools.add(tool_name)
            rows.append(
                {
                    "group": "project_team",
                    "name": tool_name,
                    "count": count,
                    "denominator": denominator,
                    "pct": f"{pct}%",
                    "note": "self_reported_partner_tools",
                }
            )
        elif raw_metric == "q8_teams_using_multiple_tools_2plus":
            rows.append(
                {
                    "group": "project_team",
                    "name": "multi_tool_teams_2plus",
                    "count": count,
                    "denominator": denominator,
                    "pct": f"{pct}%",
                    "note": "self_reported_partner_tools",
                }
            )
        elif raw_metric.startswith("q7_top_pair_"):
            pair_name = raw_metric.removeprefix("q7_top_pair_").removesuffix("_team_frequency")
            rows.append(
                {
                    "group": "tool_pair",
                    "name": _humanize_metric_suffix(pair_name, pair=True),
                    "count": count,
                    "denominator": denominator,
                    "pct": f"{pct}%",
                    "note": "team_pair_frequency",
                }
            )

    evidence_rows = _load_csv_rows(evidence_csv)
    by_category = defaultdict(set)
    by_model_project: defaultdict[str, set[str]] = defaultdict(set)
    by_tool_project: defaultdict[str, set[str]] = defaultdict(set)
    for row in evidence_rows:
        project_id = _safe_str(row.get("project_id"))
        category = _safe_str(row.get("tool_category"))
        tool_name = _safe_str(row.get("tool_name"))
        if project_id and category:
            by_category[category].add(project_id)
        if category == "model" and tool_name:
            normalized_model = _normalize_model_name(tool_name)
            if normalized_model:
                by_model_project[normalized_model].add(project_id)
        if category in {"provider", "sdk", "infra"} and tool_name:
            normalized_tool = _normalize_evidence_tool_name(tool_name, allowed_tools)
            if normalized_tool and project_id:
                by_tool_project[normalized_tool].add(project_id)

    project_den = max(len({r.get("project_id") for r in evidence_rows if r.get("project_id")}), 1)
    for category, project_ids in sorted(by_category.items()):
        rows.append(
            {
                "group": "evidence_category",
                "name": category,
                "count": str(len(project_ids)),
                "denominator": str(project_den),
                "pct": _pct_str(len(project_ids), project_den),
                "note": "projects_with_category_evidence",
            }
        )
    for tool_name, project_ids in sorted(
        by_tool_project.items(),
        key=lambda item: (-len(item[1]), item[0]),
    )[:15]:
        count = len(project_ids)
        rows.append(
            {
                "group": "evidence_project",
                "name": tool_name,
                "count": str(count),
                "denominator": str(project_den),
                "pct": _pct_str(count, project_den),
                "note": "evidence_backed_project_coverage",
            }
        )
    for model_name, project_ids in sorted(
        by_model_project.items(),
        key=lambda item: (-len(item[1]), item[0]),
    )[:15]:
        count = len(project_ids)
        rows.append(
            {
                "group": "model_id",
                "name": model_name,
                "count": str(count),
                "denominator": str(project_den),
                "pct": _pct_str(count, project_den),
                "note": "projects_with_model_signal",
            }
        )
    rows.sort(key=lambda row: (row["group"], -_safe_float(row["count"]), row["name"]))
    return rows


def _build_theme_summary_rows(submissions: list[dict[str, Any]]) -> list[dict[str, str]]:
    return _theme_score_rows(submissions)


def _build_audience_summary_rows(profiles: list[dict[str, Any]]) -> list[dict[str, str]]:
    audience_context = _resolve_audience_context(profiles)
    audience_profiles = audience_context["profiles"]
    audience_label_slug = audience_context["label"].replace(" ", "_")
    submitters = [p for p in profiles if _is_submitter(p)]
    rows: list[dict[str, str]] = []

    segment_specs = (
        ("founders", "Founders"),
        ("decision_makers", "Decision Makers"),
        ("students", "Students"),
        ("ics", "ICs"),
        ("startup_employees", "Startup Background"),
        ("big_tech", "Big Tech Background"),
        ("mango", "MANGO Background"),
        ("yc", "YC Signal"),
        ("us_based", "US Based"),
    )
    for segment_key, label in segment_specs:
        applicant_count = sum(1 for p in profiles if _segment_match(p, segment_key))
        audience_count = sum(1 for p in audience_profiles if _segment_match(p, segment_key))
        submitter_count = sum(1 for p in submitters if _segment_match(p, segment_key))
        rows.extend(
            [
                {
                    "group": "role_segment_applicants",
                    "name": label,
                    "count": str(applicant_count),
                    "denominator": str(len(profiles)),
                    "pct": _pct_str(applicant_count, len(profiles)),
                    "note": "segment_share_of_applicants",
                },
                {
                    "group": "role_segment_audience",
                    "name": label,
                    "count": str(audience_count),
                    "denominator": str(len(audience_profiles)),
                    "pct": _pct_str(audience_count, len(audience_profiles)),
                    "note": f"segment_share_of_{audience_label_slug}",
                },
                {
                    "group": "role_segment_submitters",
                    "name": label,
                    "count": str(submitter_count),
                    "denominator": str(len(submitters)),
                    "pct": _pct_str(submitter_count, len(submitters)),
                    "note": "segment_share_of_submitters",
                },
            ]
        )

    company_counter = Counter(
        _clean_display_label(_safe_str(p.get("company")))
        for p in audience_profiles
        if _clean_display_label(_safe_str(p.get("company")))
    )
    for name, count in company_counter.most_common(15):
        rows.append(
            {
                "group": "company_audience",
                "name": name,
                "count": str(count),
                "denominator": str(len(audience_profiles)),
                "pct": _pct_str(count, len(audience_profiles)),
                "note": f"top_{audience_label_slug}_companies",
            }
        )

    school_counter = Counter(
        _clean_display_label(_extract_primary_school(p))
        for p in audience_profiles
        if _clean_display_label(_extract_primary_school(p))
    )
    for name, count in school_counter.most_common(15):
        rows.append(
            {
                "group": "school_audience",
                "name": name,
                "count": str(count),
                "denominator": str(len(audience_profiles)),
                "pct": _pct_str(count, len(audience_profiles)),
                "note": f"top_{audience_label_slug}_schools",
            }
        )

    country_counter = Counter(
        _clean_display_label(_safe_str(p.get("li_country")))
        for p in profiles
        if _clean_display_label(_safe_str(p.get("li_country")))
    )
    for name, count in country_counter.most_common(10):
        rows.append(
            {
                "group": "country_applicants",
                "name": name,
                "count": str(count),
                "denominator": str(len(profiles)),
                "pct": _pct_str(count, len(profiles)),
                "note": "top_applicant_countries",
            }
        )

    rows.sort(key=lambda row: (row["group"], -_safe_float(row["count"]), row["name"]))
    return rows


def _build_tool_outcome_rows(
    snapshot: dict[str, Any],
    tool_summary_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    allowed_tools = {
        row["name"]
        for row in tool_summary_rows
        if row["group"] == "project_team" and row["name"] != "multi_tool_teams_2plus"
    }
    evidence_counts = {
        row["name"]: int(row["count"])
        for row in tool_summary_rows
        if row["group"] == "evidence_project"
    }
    self_report_counts = {
        row["name"]: int(row["count"])
        for row in tool_summary_rows
        if row["group"] == "project_team" and row["name"] != "multi_tool_teams_2plus"
    }

    scores_by_tool: defaultdict[str, list[float]] = defaultdict(list)
    placed_by_tool = Counter()
    finalist_by_tool = Counter()
    for submission in snapshot.get("submissions", []):
        normalized_tools = {
            normalized
            for normalized in (
                _normalize_evidence_tool_name(_safe_str(tool), allowed_tools)
                for tool in (submission.get("parsed_partner_tools") or [])
            )
            if normalized
        }
        score = _safe_float(submission.get("current_event_judging_weighted_avg"))
        placement = _safe_str(submission.get("placement")).strip()
        for tool_name in normalized_tools:
            if score > 0:
                scores_by_tool[tool_name].append(score)
            if placement:
                placed_by_tool[tool_name] += 1
                finalist_by_tool[tool_name] += 1

    rows: list[dict[str, str]] = []
    for tool_name in sorted(allowed_tools, key=lambda name: (-self_report_counts.get(name, 0), name)):
        team_count = self_report_counts.get(tool_name, 0)
        scores = scores_by_tool.get(tool_name, [])
        avg_score = sum(scores) / len(scores) if scores else 0.0
        median_score = _safe_float(sorted(scores)[len(scores) // 2], 0.0) if scores else 0.0
        placed = placed_by_tool.get(tool_name, 0)
        finalists = finalist_by_tool.get(tool_name, 0)
        rows.append(
            {
                "tool_name": tool_name,
                "self_reported_teams": str(team_count),
                "evidence_backed_projects": str(evidence_counts.get(tool_name, 0)),
                "avg_judging_score": _format_float(avg_score, 2),
                "median_judging_score": _format_float(median_score, 2),
                "placed_teams": str(placed),
                "placed_rate": _pct_str(placed, team_count),
                "finalist_or_winner_teams": str(finalists),
                "note": "self_reported_usage_with_score_and_placement_overlay",
            }
        )
    return rows


def _build_tool_reconciliation_rows(tool_summary_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    self_report_submitter = {
        row["name"]: int(row["count"])
        for row in tool_summary_rows
        if row["group"] == "submitter_person"
    }
    self_report_team = {
        row["name"]: int(row["count"])
        for row in tool_summary_rows
        if row["group"] == "project_team" and row["name"] != "multi_tool_teams_2plus"
    }
    evidence_project = {
        row["name"]: int(row["count"])
        for row in tool_summary_rows
        if row["group"] == "evidence_project"
    }
    tool_names = sorted(
        set(self_report_submitter) | set(self_report_team) | set(evidence_project),
        key=lambda name: (-self_report_team.get(name, 0), name),
    )
    rows: list[dict[str, str]] = []
    for tool_name in tool_names:
        team_count = self_report_team.get(tool_name, 0)
        evidence_count = evidence_project.get(tool_name, 0)
        if team_count == 0 and evidence_count > 0:
            status = "evidence_only"
        elif team_count > 0 and evidence_count == 0:
            status = "self_report_only"
        elif evidence_count >= team_count:
            status = "aligned_or_stronger_evidence"
        else:
            status = "evidence_lags_self_report"
        rows.append(
            {
                "tool_name": tool_name,
                "submitter_people": str(self_report_submitter.get(tool_name, 0)),
                "self_reported_teams": str(team_count),
                "evidence_backed_projects": str(evidence_count),
                "delta_projects": str(evidence_count - team_count),
                "status": status,
            }
        )
    return rows


def _build_top_project_rows(snapshot: dict[str, Any], evidence_csv: Path) -> list[dict[str, str]]:
    evidence_rows = _load_csv_rows(evidence_csv)
    by_project: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    for row in evidence_rows:
        project_id = _safe_str(row.get("project_id"))
        if project_id:
            by_project[project_id].append(row)

    top_rows: list[dict[str, str]] = []
    submissions = []
    for sub in snapshot.get("submissions", []):
        score = _safe_float(sub.get("current_event_judging_weighted_avg"))
        submissions.append((score, sub))
    submissions.sort(key=lambda item: item[0], reverse=True)

    for idx, (score, sub) in enumerate(submissions[:20], start=1):
        submission_id = _safe_str(sub.get("submission_id"))
        description = _project_text(sub)
        normalized_tools = sorted(
            {
                normalized
                for normalized in (
                    _normalize_loose_tool_name(_safe_str(tool))
                    for tool in (sub.get("parsed_partner_tools") or [])
                )
                if normalized
            }
        )
        top_rows.append(
            {
                "rank": str(idx),
                "team_name": _safe_str(sub.get("team_name")),
                "submission_id": submission_id,
                "judging_weighted_avg": _format_float(score, 2),
                "placement": _safe_str(sub.get("placement")),
                "team_size": str(len(sub.get("member_emails", []))),
                "tools": ", ".join(normalized_tools),
                "theme": _classify_theme(description),
                "repo_url": _extract_repo_url(sub),
                "demo_url": _extract_demo_url(sub),
                "description_excerpt": _truncate(description, 180),
                "evidence_rows": str(len(by_project.get(submission_id, []))),
            }
        )
    return top_rows


def _build_big_number_rows(profiles: list[dict[str, Any]]) -> list[dict[str, str]]:
    audience_context = _resolve_audience_context(profiles)
    audience_profiles = audience_context["profiles"]
    audience_label_title = audience_context["label_title"]
    rows: list[dict[str, str]] = []
    aggregate_specs = (
        (
            "linkedin_followers",
            "li_follower_count",
            f"The {audience_label_title.lower()} carried {{value}} LinkedIn followers.",
            "Useful for sponsor distribution and post-event amplification.",
        ),
        (
            "linkedin_connections",
            "li_connection_count",
            f"The {audience_label_title.lower()} also carried {{value}} first-degree LinkedIn connections.",
            "Good proxy for direct network reach around launches and case studies.",
        ),
        (
            "github_private_contributions",
            "gh_api_private_contributions",
            f"Builders in the {audience_label_title.lower()} logged {{value}} private GitHub contributions.",
            "Strong signal of real build volume outside only public repos.",
        ),
        (
            "github_commits_year",
            "gh_api_commits_year",
            f"The {audience_label_title.lower()} represented {{value}} GitHub commits over the last year.",
            "Useful shorthand for engineering throughput in the audience.",
        ),
        (
            "github_repos",
            "gh_api_repos",
            f"{audience_label_title} collectively touched {{value}} GitHub repositories.",
            "Shows breadth of technical surface area across the room.",
        ),
        (
            "github_stars",
            "gh_api_stars",
            f"{audience_label_title} had {{value}} combined GitHub stars.",
            "A rough proxy for public technical credibility.",
        ),
        (
            "years_experience_sum",
            "years_experience",
            f"{audience_label_title} represented {{value}} combined years of experience.",
            "This was not just a beginner-heavy room.",
        ),
    )
    for metric_name, field_name, headline_template, interpretation in aggregate_specs:
        values = [_safe_float(p.get(field_name), math.nan) for p in audience_profiles]
        usable = [v for v in values if not math.isnan(v)]
        if not usable:
            continue
        total = sum(usable)
        rows.append(
            {
                "metric_name": metric_name,
                "value": _format_total(total),
                "coverage_count": str(len(usable)),
                "coverage_denominator": str(len(audience_profiles)),
                "headline": headline_template.format(value=_format_total(total)),
                "interpretation": interpretation,
            }
        )
    return rows


def _build_claim_review_rows(
    claims: list[Any],
    policy_report: dict[str, Any],
    reviewer_report: dict[str, Any],
) -> list[dict[str, str]]:
    blocked_policy = set(policy_report.get("blocked_claim_ids", []))
    blocked_reviewer = {
        item.get("claim_id"): ";".join(item.get("reasons", []))
        for item in reviewer_report.get("blocked", [])
    }
    rows: list[dict[str, str]] = []
    for claim in claims:
        status = "include"
        reason = ""
        if claim.claim_id in blocked_policy:
            status = "blocked_policy"
        if claim.claim_id in blocked_reviewer:
            status = "blocked_reviewer"
            reason = blocked_reviewer[claim.claim_id]
        rows.append(
            {
                "claim_id": claim.claim_id,
                "section": claim.section,
                "metric_name": claim.metric_name,
                "segment": claim.segment,
                "numerator": claim.numerator,
                "denominator": claim.denominator,
                "value": claim.value,
                "status": status,
                "reason": reason,
                "source_table_or_file": claim.source_table_or_file,
            }
        )
    return rows


def _build_snapshot_payload(
    *,
    event_name: str,
    enriched_path: Path,
    profiles: list[dict[str, Any]],
    snapshot: dict[str, Any],
    segment_funnel_rows: list[dict[str, str]],
    tool_summary_rows: list[dict[str, str]],
    theme_summary_rows: list[dict[str, str]],
    audience_summary_rows: list[dict[str, str]],
    top_project_rows: list[dict[str, str]],
    big_number_rows: list[dict[str, str]],
    claim_review_rows: list[dict[str, str]],
    reviewer_allowed_count: int,
) -> dict[str, Any]:
    audience_context = _resolve_audience_context(profiles)
    audience_profiles = audience_context["profiles"]
    submitters = [p for p in profiles if _is_submitter(p)]
    approved = [p for p in profiles if _is_approved(p)]
    placed = [p for p in profiles if _is_placed(p)]

    top_countries = Counter(_clean_display_label(_safe_str(p.get("li_country"))) for p in profiles if _clean_display_label(_safe_str(p.get("li_country")))).most_common(10)
    top_companies_audience = Counter(_clean_display_label(_safe_str(p.get("company"))) for p in audience_profiles if _clean_display_label(_safe_str(p.get("company")))).most_common(15)
    top_schools_audience = Counter(_clean_display_label(_extract_primary_school(p)) for p in audience_profiles if _clean_display_label(_extract_primary_school(p))).most_common(15)
    top_languages_audience = _language_counter(audience_profiles).most_common(12)
    top_languages_submitter = _language_counter(submitters).most_common(12)
    theme_counts = Counter(_classify_theme(_project_text(sub)) for sub in snapshot.get("submissions", []))
    theme_scores = theme_summary_rows

    return {
        "event_name": event_name,
        "enriched_path": str(enriched_path),
        "counts": {
            "applied": len(profiles),
            "approved": len(approved),
            "checked_in": sum(1 for p in profiles if _is_checked_in(p)),
            "audience": len(audience_profiles),
            "submitters": len(submitters),
            "placed": len(placed),
            "teams": len(snapshot.get("submissions", [])),
            "reviewer_allowed_claims": reviewer_allowed_count,
            "blocked_claims": sum(1 for row in claim_review_rows if row["status"] != "include"),
        },
        "attendance_suspect": audience_context["suspect"],
        "audience_label": audience_context["label"],
        "audience_label_title": audience_context["label_title"],
        "top_countries": [{"country": country, "count": count} for country, count in top_countries],
        "top_companies_audience": [{"company": company, "count": count} for company, count in top_companies_audience],
        "top_schools_audience": [{"school": school, "count": count} for school, count in top_schools_audience],
        "top_languages_audience": [{"language": lang, "count": count} for lang, count in top_languages_audience],
        "top_languages_submitters": [{"language": lang, "count": count} for lang, count in top_languages_submitter],
        "theme_counts": [{"theme": theme, "count": count} for theme, count in theme_counts.most_common(10)],
        "theme_scores": theme_scores,
        "segment_funnel": segment_funnel_rows,
        "tool_summary_top": tool_summary_rows[:40],
        "audience_summary_top": audience_summary_rows[:40],
        "top_projects": top_project_rows[:10],
        "big_numbers": big_number_rows,
        "claim_review_summary": dict(Counter(row["status"] for row in claim_review_rows)),
    }


def _render_summary(
    *,
    event_name: str,
    analysis_snapshot: dict[str, Any],
    segment_funnel_rows: list[dict[str, str]],
    tool_summary_rows: list[dict[str, str]],
    theme_summary_rows: list[dict[str, str]],
    audience_summary_rows: list[dict[str, str]],
    top_project_rows: list[dict[str, str]],
    big_number_rows: list[dict[str, str]],
    claim_review_rows: list[dict[str, str]],
) -> str:
    counts = analysis_snapshot["counts"]
    audience_label = analysis_snapshot["audience_label"]
    audience_label_title = analysis_snapshot["audience_label_title"]
    attendance_suspect = bool(analysis_snapshot["attendance_suspect"])
    founders = _find_segment_row(segment_funnel_rows, "founders")
    decisions = _find_segment_row(segment_funnel_rows, "decision_makers")
    students = _find_segment_row(segment_funnel_rows, "students")
    ics = _find_segment_row(segment_funnel_rows, "ics")
    startup = _find_segment_row(segment_funnel_rows, "startup_employees")
    mango = _find_segment_row(segment_funnel_rows, "mango")
    yc = _find_segment_row(segment_funnel_rows, "yc")
    us_based = _find_segment_row(segment_funnel_rows, "us_based")
    multi_tool = _find_tool_row(tool_summary_rows, "project_team", "multi_tool_teams_2plus")
    top_pair = next((row for row in tool_summary_rows if row["group"] == "tool_pair"), None)
    evidence_tools = [row for row in tool_summary_rows if row["group"] == "evidence_project"][:4]
    include_count = sum(1 for row in claim_review_rows if row["status"] == "include")
    blocked_count = len(claim_review_rows) - include_count
    top_themes = theme_summary_rows[:3]
    top_projects_preview = top_project_rows[:5]
    top_big_numbers = big_number_rows[:4]
    top_theme_text = ", ".join(f"{row['theme']} ({row['count']})" for row in top_themes) or "no dominant themes detected"
    top_companies = ", ".join(f"{row['company']} ({row['count']})" for row in analysis_snapshot["top_companies_audience"][:3]) or "no dominant company cluster"
    top_schools = ", ".join(f"{row['school']} ({row['count']})" for row in analysis_snapshot["top_schools_audience"][:3]) or "no dominant school cluster"
    top_languages = ", ".join(f"{row['language']} ({row['count']})" for row in analysis_snapshot["top_languages_audience"][:3]) or "no dominant language cluster"
    theme_score_candidates = [row for row in theme_summary_rows if int(row["count"]) >= 5]
    best_theme_row = max(theme_score_candidates, key=lambda row: _safe_float(row["avg_score"]), default=None)
    top_placement_theme = max(theme_score_candidates, key=lambda row: _safe_float(row["placed_pct"]), default=None)
    evidence_tool_text = ", ".join(f"{row['name']} ({row['count']}/{row['denominator']})" for row in evidence_tools) or "no strong evidence-backed tool coverage detected"
    top_segment_rows = [
        row for row in audience_summary_rows
        if row["group"] == "role_segment_audience" and row["name"] in {"Founders", "Decision Makers", "ICs", "Students"}
    ]
    top_segment_text = ", ".join(f"{row['name']} {row['count']}/{row['denominator']}" for row in top_segment_rows) or "no major audience segment split"
    top_pair_text = (
        f"{top_pair['name']} at {top_pair['count']} teams ({top_pair['pct']})"
        if top_pair else
        "no dominant self-reported pair"
    )

    lines = [
        f"# {event_name} - Analysis Helper",
        "",
        "## Executive View",
        "",
        f"- Core funnel: {counts['applied']} applied, {counts['approved']} approved, {counts['audience']} {audience_label}, {counts['submitters']} submitter participants, {counts['placed']} finalist/winner participants, and {counts['teams']} submitted teams.",
    ]
    if attendance_suspect:
        lines.append(
            f"- Attendance labels are incomplete in Platform DB for this event, so the helper uses the **approved cohort ({counts['approved']})** as the main audience base instead of the raw checked-in count of **{counts['checked_in']}**."
        )
        lines.append(
            f"- The most reliable conversion is approved to submission: {counts['submitters']} of {counts['approved']} approved participants submitted ({_pct_from_counts(counts['submitters'], counts['approved'])})."
        )
    else:
        lines.append(
            f"- The biggest attendance leak is still approval to check-in: {counts['checked_in']} of {counts['approved']} approved participants showed up ({_pct_from_counts(counts['checked_in'], counts['approved'])})."
        )
        lines.append(
            f"- Builder follow-through was solid once people arrived: {counts['submitters']} of {counts['checked_in']} checked-in participants submitted ({_pct_from_counts(counts['submitters'], counts['checked_in'])})."
        )
    lines.extend([
        f"- Claim triage status: {include_count} metrics survived strict include-review, while {blocked_count} were blocked for policy/reviewer reasons. Use `analysis_claim_review.csv` as the first filter before writing narrative.",
        "",
        "## Audience Analysis",
        "",
        f"- {audience_label_title} operator mix stayed broad: {top_segment_text}.",
        f"- Decision-makers were the largest sponsor-relevant operator segment in the audience base: {decisions['audience']} from {decisions['applied']} applicants ({decisions['applied_to_approved_pct']} applied-to-approved, {decisions['approved_to_audience_pct']} approved-to-audience).",
        f"- Founder volume was also meaningful: {founders['audience']} founders were in the audience base and {founders['submitters']} submitted, which keeps the event useful for startup-facing sponsor follow-up.",
        f"- ICs were the most execution-heavy technical cohort within the audience base: {ics['submitters']} of {ics['audience']} audience ICs submitted ({ics['audience_to_submitted_pct']}).",
        f"- Student representation was real but not dominant in this event: {students['applied']} student applicants versus {counts['applied']} total applicants.",
        f"- Sponsor-relevant background clusters were material: {startup['audience']} startup-background attendees were in the audience base, alongside {mango['audience']} MANGO-background attendees and {yc['audience']} YC-signal attendees.",
        f"- Geography remained mostly U.S.-based: {us_based['applied']} of {counts['applied']} applicants had U.S. LinkedIn country data.",
        f"- The biggest audience company clusters were {top_companies}, while the biggest school clusters were {top_schools}.",
        f"- Language depth was broad rather than single-stack: top audience language signals were {top_languages}.",
        "",
        "## Build Analysis",
        "",
        f"- Team formation was healthy: {counts['teams']} teams submitted, and {multi_tool['count']} of them used two or more sponsor tools ({multi_tool['pct']}).",
        f"- The dominant sponsor-tool pair was {top_pair_text} when self-reported tool data was present.",
        f"- The strongest project-theme pockets by raw volume were {top_theme_text}.",
        f"- Evidence-backed project coverage was strongest for {evidence_tool_text}.",
        "- The top-scoring projects were not all finalists, which means there is still strong sponsor follow-up value outside the winner list. Review `analysis_top_projects.csv` before limiting storytelling to placements only.",
        "",
        "## Big Numbers",
        "",
    ])
    for row in top_big_numbers:
        lines.append(f"- {row['headline']} Coverage: {row['coverage_count']}/{row['coverage_denominator']}.")

    if best_theme_row:
        lines.append(
            f"- Best average scoring theme with meaningful volume: {best_theme_row['theme']} at {best_theme_row['avg_score']} average judging across {best_theme_row['count']} projects."
        )
    if top_placement_theme:
        lines.append(
            f"- Highest placement-density theme with at least five projects: {top_placement_theme['theme']} at {top_placement_theme['placed']}/{top_placement_theme['count']} placed ({top_placement_theme['placed_pct']})."
        )

    lines.extend(
        [
            "",
            "## Top Projects",
            "",
        ]
    )
    for row in top_projects_preview:
        placement = row["placement"] or "not placed"
        lines.append(
            f"- {row['team_name']}: judging {row['judging_weighted_avg']}, {placement}, tools [{row['tools'] or 'none listed'}], theme {row['theme']}."
        )

    lines.extend(
        [
            "",
            "## File Guide",
            "",
            "- `analysis_claim_review.csv`: which metrics are safe to include, and which were blocked.",
            "- `analysis_segment_funnel.csv`: raw conversion table by audience segment.",
            "- `analysis_tool_summary.csv`: people/team/tool/model/evidence summaries.",
            "- `analysis_theme_summary.csv`: project theme counts, average score, and placement rate.",
            "- `analysis_audience_summary.csv`: top segment/company/school/country slices for attendance and submission.",
            "- `analysis_tool_outcomes.csv`: self-reported tool usage with judging and placement overlays.",
            "- `analysis_tool_reconciliation.csv`: self-report versus evidence-backed project counts by tool.",
            "- `analysis_top_projects.csv`: highest-scoring submitted teams with tool/theme context.",
            "- `analysis_big_numbers.csv`: reusable big-number headline inventory.",
            "- `analysis_snapshot.json`: machine-readable summary for later agent passes.",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def _render_tasks(
    *,
    event_name: str,
    analysis_snapshot: dict[str, Any],
    claim_review_rows: list[dict[str, str]],
) -> str:
    blocked = [row for row in claim_review_rows if row["status"] != "include"][:12]
    includes = [row for row in claim_review_rows if row["status"] == "include"][:12]
    lines = [
        f"# {event_name} - Agent Task List",
        "",
        "1. Start in `analysis_claim_review.csv` and shortlist only `include` rows for any sponsor-facing packet draft.",
        "2. Use `analysis_segment_funnel.csv` to compare founders, decision-makers, ICs, students, MANGO, and YC before writing any audience narrative.",
        "3. Use `analysis_tool_summary.csv` to separate self-reported tool adoption from evidence-backed project coverage.",
        "4. Use `analysis_theme_summary.csv` and `analysis_top_projects.csv` together before claiming what got built or which stacks/themes were strongest.",
        "5. Use `analysis_audience_summary.csv` when you need company, school, country, or segment concentration without recomputing it.",
        "6. Use `analysis_tool_outcomes.csv` and `analysis_tool_reconciliation.csv` before making claims about which sponsor tools actually drove stronger projects.",
        "7. Prefer the big-number rows that have strong coverage in `analysis_big_numbers.csv`; avoid small-coverage aggregates.",
        "8. If a metric is blocked but still looks tempting, recompute it manually from the raw tables before using it.",
        "",
        "## First Metrics To Review",
        "",
    ]
    for row in includes:
        lines.append(
            f"- Include candidate: {row['section']}.{row['metric_name']} ({row['numerator']}/{row['denominator']})."
        )
    if blocked:
        lines.extend(["", "## Metrics Blocked By The Helper", ""])
        for row in blocked:
            lines.append(
                f"- Review before using: {row['section']}.{row['metric_name']} ({row['numerator']}/{row['denominator']}) blocked because `{row['reason'] or row['status']}`."
            )
    return "\n".join(lines).strip() + "\n"


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _load_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _segment_match(profile: dict[str, Any], segment: str) -> bool:
    if segment == "all":
        return True
    if segment == "founders":
        return _as_bool(profile.get("is_founder"))
    if segment == "decision_makers":
        return _as_bool(profile.get("is_decision_maker"))
    if segment == "students":
        return _as_bool(profile.get("is_student"))
    if segment == "ics":
        return _is_ic(profile)
    if segment == "startup_employees":
        return _safe_str(profile.get("employment_category")).lower() == "startup"
    if segment == "big_tech":
        return _as_bool(profile.get("is_in_big_tech"))
    if segment == "mango":
        return _has_keyword(profile, MANGO_KEYWORDS)
    if segment == "yc":
        return _has_keyword(profile, ("y combinator", "yc ", "(yc", "yc-", "yc w", "yc s"))
    if segment == "us_based":
        return "united states" in _safe_str(profile.get("li_country")).lower()
    return False


def _has_keyword(profile: dict[str, Any], keywords: tuple[str, ...]) -> bool:
    haystacks = [
        _safe_str(profile.get("company")).lower(),
        _safe_str(profile.get("role")).lower(),
        _safe_str(profile.get("linkedin_headline")).lower(),
        _safe_str(profile.get("linkedin_bio")).lower(),
        _safe_str(profile.get("self_description")).lower(),
    ]
    for position in profile.get("positions_with_companies") or []:
        haystacks.append(_safe_str(position.get("company_name")).lower())
        haystacks.append(_safe_str(position.get("title")).lower())
    for school in profile.get("education_with_schools") or []:
        haystacks.append(_safe_str(school.get("school_name")).lower())
        haystacks.append(_safe_str(school.get("degree")).lower())
    corpus = " ".join(haystacks)
    return any(keyword in corpus for keyword in keywords)


def _is_ic(profile: dict[str, Any]) -> bool:
    if _as_bool(profile.get("is_founder")):
        return False
    role = _safe_str(profile.get("role")).lower()
    tokens = ("engineer", "developer", "scientist", "research")
    return any(token in role for token in tokens)


def _humanize_metric_suffix(value: str, *, pair: bool = False) -> str:
    if pair:
        parts = [part for part in value.split("_") if part]
        midpoint = len(parts) // 2
        if midpoint > 0:
            left = _clean_display_label(" ".join(parts[:midpoint]))
            right = _clean_display_label(" ".join(parts[midpoint:]))
            return f"{left} + {right}"
    special = {
        "mongodb": "MongoDB",
        "fireworks": "Fireworks",
        "vercel": "Vercel",
        "voyage_ai": "Voyage AI",
        "coinbase": "Coinbase",
        "nvidia": "NVIDIA",
    }
    if value in special:
        return special[value]
    return value.replace("_", " ").title()


def _clean_display_label(value: str) -> str:
    text = " ".join(_safe_str(value).split())
    if not text:
        return ""
    special = {
        "mongodb": "MongoDB",
        "mongo db": "MongoDB",
        "voyage ai": "Voyage AI",
        "voyagerai": "Voyage AI",
        "coinbase": "Coinbase",
        "fireworks": "Fireworks",
        "nvidia": "NVIDIA",
        "openai": "OpenAI",
    }
    lowered = text.lower()
    if lowered in special:
        return special[lowered]
    return text


def _normalize_evidence_tool_name(raw_name: str, allowed_tools: set[str]) -> str:
    text = _clean_display_label(raw_name)
    lowered = text.lower()
    if lowered in GENERIC_EVIDENCE_TOOL_NAMES:
        return ""
    for pattern, canonical in EVIDENCE_TOOL_ALIAS_PATTERNS:
        if pattern.search(text):
            if not allowed_tools or canonical in allowed_tools:
                return canonical
    if text in allowed_tools:
        return text
    return ""


def _normalize_model_name(raw_name: str) -> str:
    text = _clean_display_label(raw_name)
    lowered = text.lower()
    if not lowered or lowered in GENERIC_EVIDENCE_TOOL_NAMES:
        return ""
    if len(lowered) > 48:
        return ""
    return lowered


def _normalize_loose_tool_name(raw_name: str) -> str:
    text = _clean_display_label(raw_name)
    if not text:
        return ""
    canonical = _normalize_evidence_tool_name(text, set())
    return canonical or text


def _resolve_audience_context(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    approved = [p for p in profiles if _is_approved(p)]
    checked = [p for p in profiles if _is_checked_in(p)]
    submitters = [p for p in profiles if _is_submitter(p)]
    suspect = _has_suspect_attendance_labels(len(approved), len(checked), len(submitters))
    return {
        "profiles": approved if suspect else checked,
        "label": "approved participants" if suspect else "checked-in attendees",
        "label_title": "Approved participants" if suspect else "Checked-in attendees",
        "filter": _is_approved if suspect else _is_checked_in,
        "suspect": suspect,
    }


def _has_suspect_attendance_labels(approved_count: int, checked_count: int, submitter_count: int) -> bool:
    if approved_count >= 50 and checked_count <= 1:
        return True
    return checked_count > 0 and submitter_count > checked_count


def _is_approved(profile: dict[str, Any]) -> bool:
    return _safe_str(profile.get("current_event_status")).lower() in {"approved", "accepted"}


def _is_checked_in(profile: dict[str, Any]) -> bool:
    return _as_bool(profile.get("current_event_checked_in"))


def _is_submitter(profile: dict[str, Any]) -> bool:
    return bool(profile.get("current_event_submissions"))


def _is_placed(profile: dict[str, Any]) -> bool:
    for sub in profile.get("current_event_submissions") or []:
        if _safe_str(sub.get("placement")).strip():
            return True
    return False


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = _safe_str(value).lower()
    return text in {"1", "true", "yes", "y"}


def _safe_str(value: Any) -> str:
    return "" if value is None else str(value)


def _safe_float(value: Any, default: float = 0.0) -> float:
    text = _safe_str(value).strip().replace("%", "")
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def _pct_str(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "0.0%"
    return f"{(numerator / denominator) * 100:.1f}%"


def _pct_from_counts(numerator: int, denominator: int) -> str:
    return _pct_str(numerator, denominator)


def _format_float(value: float, decimals: int) -> str:
    return f"{value:.{decimals}f}"


def _format_total(value: float) -> str:
    if value >= 1000 or value.is_integer():
        return f"{int(round(value)):,}"
    return f"{value:,.1f}"


def _slugify(value: str) -> str:
    out = []
    for char in value.lower().strip():
        if char.isalnum():
            out.append(char)
        else:
            out.append("-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-") or "event"


def _find_segment_row(rows: list[dict[str, str]], segment: str) -> dict[str, str]:
    return next(row for row in rows if row["segment"] == segment)


def _find_tool_row(rows: list[dict[str, str]], group: str, name: str) -> dict[str, str]:
    return next(row for row in rows if row["group"] == group and row["name"] == name)


def _language_counter(profiles: list[dict[str, Any]]) -> Counter:
    counter = Counter()
    for profile in profiles:
        for language in profile.get("github_best_languages") or []:
            language_norm = _safe_str(language).strip()
            if language_norm:
                counter[language_norm] += 1
    return counter


def _extract_primary_school(profile: dict[str, Any]) -> str:
    schools = profile.get("education_with_schools") or []
    for school in schools:
        school_name = _safe_str(school.get("school_name")).strip()
        if school_name:
            return school_name
    return ""


def _project_text(submission: dict[str, Any]) -> str:
    parts = []
    for key, value in submission.items():
        key_norm = _safe_str(key).lower()
        if any(token in key_norm for token in ("description", "project", "idea", "summary")):
            parts.append(_safe_str(value))
    return " ".join(part for part in parts if part).strip()


def _classify_theme(text: str) -> str:
    lowered = _safe_str(text).lower()
    for theme, keywords in THEME_RULES:
        if any(keyword in lowered for keyword in keywords):
            return theme
    return "Other"


def _extract_repo_url(submission: dict[str, Any]) -> str:
    for key, value in submission.items():
        key_norm = _safe_str(key).lower()
        if "github" in key_norm or "repo" in key_norm:
            url = _safe_str(value).strip()
            if url.startswith("http"):
                return url
    return ""


def _extract_demo_url(submission: dict[str, Any]) -> str:
    for key, value in submission.items():
        key_norm = _safe_str(key).lower()
        if "demo" in key_norm or "video" in key_norm or "loom" in key_norm:
            url = _safe_str(value).strip()
            if url.startswith("http"):
                return url
    return ""


def _truncate(text: str, limit: int) -> str:
    clean = " ".join(_safe_str(text).split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def _theme_score_rows(submissions: list[dict[str, Any]]) -> list[dict[str, str]]:
    scores_by_theme: defaultdict[str, list[float]] = defaultdict(list)
    placement_counts = Counter()
    theme_counts = Counter()
    for sub in submissions:
        theme = _classify_theme(_project_text(sub))
        theme_counts[theme] += 1
        score = _safe_float(sub.get("current_event_judging_weighted_avg"))
        if score > 0:
            scores_by_theme[theme].append(score)
        if _safe_str(sub.get("placement")).strip():
            placement_counts[theme] += 1
    rows = []
    for theme, count in theme_counts.most_common():
        scores = scores_by_theme.get(theme, [])
        avg = sum(scores) / len(scores) if scores else 0.0
        rows.append(
            {
                "theme": theme,
                "count": str(count),
                "placed": str(placement_counts.get(theme, 0)),
                "placed_pct": _pct_str(placement_counts.get(theme, 0), count),
                "avg_score": _format_float(avg, 2),
            }
        )
    return rows
