"""End-to-end ranking pipeline for cv-rank v2.

The v2 orchestration owns identity, ingest, config, staging, and exports.
Legacy scoring modules are used as temporary adapters so functionality
continues to work while the scoring kernels are ported.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from cv_rank.checkpoint import create_run_dir
from cv_rank.config import load_config as load_legacy_config
from cv_rank.config import parse_criteria
from cv_rank.profile import format_profile as legacy_format_profile
from cv_rank.quality import run_quality_check as legacy_run_quality_check
from cv_rank.scoring.borderline import run_borderline_reeval as legacy_run_borderline_reeval
from cv_rank.scoring.combine import recombine_with_borderline as legacy_recombine_with_borderline
from cv_rank.scoring.pointwise import score_all as legacy_score_all
from cv_rank.scoring.swiss import run_swiss as legacy_run_swiss
from cv_rank_v2.domain.models import PointwiseScore, StageResult
from cv_rank_v2.enrichment.legacy_bridge import run_legacy_enrichment
from cv_rank_v2.export.csv_exports import (
    write_failures_csv,
    write_needs_review_csv,
    write_ranked_csv,
)
from cv_rank_v2.ingest.csv_loader import load_csv as load_v2_csv
from cv_rank_v2.ingest.event_loader import load_event_applicants
from cv_rank_v2.ingest.models import Applicant
from cv_rank_v2.runtime.checkpoints import to_json_ready
from cv_rank_v2.runtime.config import ResolvedConfig
from cv_rank_v2.scoring.combine import combine_rankings_with_metadata
from cv_rank_v2.scoring.heuristic_backend import (
    native_merge_borderline,
    native_pointwise_scores,
    native_quality_review,
    native_run_borderline,
    native_run_swiss,
)


def _now_run_id() -> str:
    return f"run_v2_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def _load_repo_env() -> None:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)


def _hydrate_legacy_enrichment_config(config: dict[str, Any]) -> None:
    enrichment = config.setdefault("enrichment", {})

    supabase = enrichment.setdefault("supabase", {})
    supabase["url"] = supabase.get("url") or os.environ.get("SUPABASE_URL", "")
    supabase["key"] = supabase.get("key") or os.environ.get("SUPABASE_KEY", "")

    github = enrichment.setdefault("github", {})
    github["token"] = github.get("token") or os.environ.get("GITHUB_TOKEN", "")

    platform_db = enrichment.setdefault("platform_db", {})
    platform_db["dsn"] = platform_db.get("dsn") or os.environ.get("PLATFORM_DATABASE_URL", "")

    exa = enrichment.setdefault("exa", {})
    exa["api_key"] = exa.get("api_key") or os.environ.get("EXA_API_KEY", "")
    exa["gemini_api_key"] = exa.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY", "")


def _legacy_config_from_resolved(config: ResolvedConfig) -> dict[str, Any]:
    """Translate v2 config into the v1-style mapping expected by legacy stages."""
    payload = config.to_dict()
    payload.setdefault("event", {})
    payload["event"]["target_accepts"] = config.ranking.accept_count
    payload.setdefault("weights", {})
    payload["weights"]["pointwise"] = config.ranking.pointwise_weight
    payload["weights"]["swiss"] = config.ranking.swiss_weight
    payload["weights"]["auto"] = config.ranking.auto_weights
    payload.setdefault("borderline", {})
    payload["borderline"]["band_pct"] = config.ranking.borderline_band_pct
    payload["borderline"]["extra_rounds"] = config.ranking.borderline_extra_rounds
    payload.setdefault("concurrency", {})
    payload["concurrency"]["scoring"] = config.runtime.scoring_concurrency
    payload["concurrency"]["swiss"] = config.runtime.swiss_concurrency
    payload["concurrency"]["quality_check"] = config.runtime.quality_concurrency
    payload["max_retries"] = config.runtime.max_retries
    payload["save_every"] = config.checkpoint.every_n
    return payload


def _build_config(
    *,
    config_path: str | Path | None,
    accept_count: int,
    model: str | None,
    event_name: str | None,
    backend: str | None,
    ranking_policy: str | None = None,
    swiss_weight: float | None = None,
    pointwise_weight: float | None = None,
    auto_weights: bool | None = None,
    tie_breaker: str | None = None,
    shortlist_min_size: int | None = None,
    shortlist_multiplier: float | None = None,
) -> ResolvedConfig:
    _load_repo_env()
    legacy = load_legacy_config(config_path)
    if accept_count is not None:
        legacy.setdefault("ranking", {})
        legacy["ranking"]["accept_count"] = accept_count
        legacy.setdefault("event", {})
        legacy["event"]["target_accepts"] = accept_count
    if event_name:
        legacy.setdefault("event", {})
        legacy["event"]["name"] = event_name
    if model:
        legacy.setdefault("models", {})
        legacy["models"]["scoring"] = model
        legacy["models"]["swiss"] = model
        legacy["models"]["quality_check"] = model
    if backend:
        legacy.setdefault("runtime", {})
        legacy["runtime"]["backend"] = backend
    legacy.setdefault("ranking", {})
    if ranking_policy:
        legacy["ranking"]["policy"] = ranking_policy
    if swiss_weight is not None:
        legacy["ranking"]["swiss_weight"] = swiss_weight
        legacy.setdefault("weights", {})
        legacy["weights"]["swiss"] = swiss_weight
    if pointwise_weight is not None:
        legacy["ranking"]["pointwise_weight"] = pointwise_weight
        legacy.setdefault("weights", {})
        legacy["weights"]["pointwise"] = pointwise_weight
    if auto_weights is not None:
        legacy["ranking"]["auto_weights"] = auto_weights
        legacy.setdefault("weights", {})
        legacy["weights"]["auto"] = auto_weights
    if tie_breaker:
        legacy["ranking"]["tie_breaker"] = tie_breaker
    if shortlist_min_size is not None:
        legacy["ranking"]["shortlist_min_size"] = shortlist_min_size
    if shortlist_multiplier is not None:
        legacy["ranking"]["shortlist_multiplier"] = shortlist_multiplier
    _hydrate_legacy_enrichment_config(legacy)
    return ResolvedConfig.from_mapping(legacy)


def _legacy_person(applicant: Applicant) -> dict[str, Any]:
    """Convert a v2 applicant into the dict shape used by legacy scorers.

    The critical detail is that ``name`` becomes the stable applicant ID.
    Profiles still render the human-readable name in prompts.
    """
    raw = dict(applicant.source_row)
    return {
        "name": applicant.applicant_id,
        "display_name": applicant.name,
        "candidate_id": applicant.applicant_id,
        "email": applicant.email,
        "first_name": applicant.first_name,
        "last_name": applicant.last_name,
        "company": applicant.company,
        "role": applicant.role,
        "location": applicant.location,
        "linkedin_url": applicant.linkedin_url,
        "github_url": applicant.github_url,
        "x_handle": applicant.x_handle,
        "self_description": applicant.self_description,
        "ai_project": applicant.ai_project,
        "looking_for_job": applicant.looking_for_job,
        "years_experience": applicant.years_experience,
        "education_level": applicant.education_level,
        "employment_category": applicant.employment_category,
        "total_cv_events": applicant.total_cv_events,
        "hackathon_submissions": applicant.hackathon_submissions,
        "notable_achievements": list(applicant.notable_achievements),
        "positions_with_companies": [
            {
                "title": entry.title,
                "company_name": entry.company,
                "from_date": entry.start,
                "to_date": entry.end,
                "is_current": entry.current,
                "description": entry.description,
            }
            for entry in applicant.work_history
        ],
        "education_with_schools": [
            {
                "school_name": entry.school,
                "degree": entry.degree,
                "field_of_study": entry.field_of_study,
                "from_date": entry.start,
                "to_date": entry.end,
                "education_level": entry.level,
            }
            for entry in applicant.education_history
        ],
        "publications_detail": [
            {"name": entry.title, "publisher": entry.publisher}
            for entry in applicant.publications
        ],
        "certifications": [
            {"name": entry.name, "authority": entry.authority}
            for entry in applicant.certifications
        ],
        "event_history": [
            {"event_name": entry.name, "event_date": entry.date, "status": entry.status}
            for entry in applicant.event_history
        ],
        "_raw_csv": raw,
    }


def _legacy_people(applicants: list[Applicant]) -> tuple[list[dict[str, Any]], dict[str, Applicant]]:
    lookup = {applicant.applicant_id: applicant for applicant in applicants}
    return [_legacy_person(applicant) for applicant in applicants], lookup


def _format_profile_from_legacy(legacy_person: dict[str, Any], lookup: dict[str, Applicant]) -> str:
    applicant_id = legacy_person["name"]
    prompt_person = dict(legacy_person)
    prompt_person["name"] = lookup[applicant_id].name if applicant_id in lookup else prompt_person.get("display_name", applicant_id)
    return legacy_format_profile(prompt_person)


def _pointwise_stage_results(rows: list[dict[str, Any]], lookup: dict[str, Applicant]) -> StageResult[PointwiseScore]:
    items: list[PointwiseScore] = []
    for row in rows:
        applicant_id = row["name"]
        items.append(
            PointwiseScore(
                applicant_id=applicant_id,
                score=float(row["score"]) if row.get("score") is not None else 0.0,
                confidence=str(row.get("confidence", "medium")),
                strongest_signal=str(row.get("strongest_signal", "")),
                concerns=str(row.get("concerns", "")),
                reasoning=str(row.get("reasoning") or row.get("why") or ""),
                model="",
                prompt_version="legacy-bridge",
                metadata={
                    "display_name": lookup[applicant_id].name if applicant_id in lookup else applicant_id,
                    "error": row.get("error"),
                },
            )
        )
    return StageResult.success("pointwise", items, input_count=len(lookup))


def _combined_from_legacy(rows: list[dict[str, Any]], lookup: dict[str, Applicant]) -> list[dict[str, Any]]:
    combined: list[dict[str, Any]] = []
    for row in rows:
        applicant_id = row["name"]
        display_name = lookup[applicant_id].name if applicant_id in lookup else applicant_id
        swiss_wins = int(row.get("swiss_wins", 0))
        swiss_losses = int(row.get("swiss_losses", 0))
        swiss_byes = int(row.get("swiss_byes", 0))
        bt_strength = row.get("bt_strength")
        combined.append(
            {
                "applicant_id": applicant_id,
                "display_name": display_name,
                "rank": int(row["rank"]),
                "final_score": float(row["final_score"]),
                "pointwise_score": float(row["pointwise_score"]),
                "swiss_wins": swiss_wins,
                "swiss_losses": swiss_losses,
                "swiss_byes": swiss_byes,
                "bt_strength": bt_strength,
                "swiss_signal": (
                    float(bt_strength)
                    if bt_strength is not None
                    else (swiss_wins / max(1, swiss_wins + swiss_losses))
                ),
                "swiss_evaluated": (swiss_wins + swiss_losses + swiss_byes > 0 or bt_strength is not None),
                "confidence": row.get("confidence", ""),
                "strongest_signal": row.get("strongest_signal", ""),
                "concerns": row.get("concerns", ""),
                "reasoning": row.get("why", ""),
            }
        )
    return combined


def _sort_combined_rankings(rows: list[dict[str, Any]], tie_breaker: str) -> list[dict[str, Any]]:
    if tie_breaker == "swiss":
        rows.sort(
            key=lambda row: (
                -float(row["final_score"]),
                -float(row.get("swiss_signal", 0.0)),
                -int(row.get("swiss_wins", 0)),
                -float(row.get("pointwise_score", 0.0)),
                str(row.get("display_name", "")).lower(),
            )
        )
    else:
        rows.sort(
            key=lambda row: (
                -float(row["final_score"]),
                -float(row.get("pointwise_score", 0.0)),
                -float(row.get("swiss_signal", 0.0)),
                -int(row.get("swiss_wins", 0)),
                str(row.get("display_name", "")).lower(),
            )
        )
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


def _write_stage_json(run_dir: Path, name: str, payload: Any) -> Path:
    path = run_dir / f"{name}.json"
    path.write_text(json.dumps(to_json_ready(payload), indent=2))
    return path


def _sorted_pointwise_rows(pointwise_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        [row for row in pointwise_rows if row.get("name") and row.get("score") is not None and not row.get("error")],
        key=lambda row: (-float(row["score"]), row["name"]),
    )


def _shortlist_size(total_applicants: int, accept_count: int, resolved: ResolvedConfig) -> int:
    base = max(
        accept_count,
        resolved.ranking.shortlist_min_size,
        int(round(accept_count * resolved.ranking.shortlist_multiplier)),
    )
    return min(total_applicants, max(1, base))


def _shortlist_applicant_ids(
    pointwise_rows: list[dict[str, Any]],
    accept_count: int,
    resolved: ResolvedConfig,
) -> list[str]:
    ranked = _sorted_pointwise_rows(pointwise_rows)
    shortlist_size = _shortlist_size(len(ranked), accept_count, resolved)
    return [row["name"] for row in ranked[:shortlist_size]]


@dataclass(slots=True, frozen=True)
class PipelineRun:
    run_dir: Path
    config: ResolvedConfig
    applicants: tuple[Applicant, ...]
    criteria: dict[str, float]
    pointwise: StageResult[PointwiseScore]
    combined_rankings: tuple[dict[str, Any], ...]
    quality_results: dict[str, dict[str, Any]]
    artifact_paths: dict[str, Path]


def run_csv_pipeline(
    *,
    csv_path: str | Path,
    accept_count: int,
    output_dir: str | Path = "results",
    config_path: str | Path | None = None,
    criteria_text: str | None = None,
    model: str | None = None,
    event_name: str | None = None,
    backend: str | None = None,
    ranking_policy: str | None = None,
    swiss_weight: float | None = None,
    pointwise_weight: float | None = None,
    auto_weights: bool | None = None,
    tie_breaker: str | None = None,
    shortlist_min_size: int | None = None,
    shortlist_multiplier: float | None = None,
) -> PipelineRun:
    """Execute the ranking-only v2 pipeline on a CSV source."""
    resolved = _build_config(
        config_path=config_path,
        accept_count=accept_count,
        model=model,
        event_name=event_name,
        backend=backend,
        ranking_policy=ranking_policy,
        swiss_weight=swiss_weight,
        pointwise_weight=pointwise_weight,
        auto_weights=auto_weights,
        tie_breaker=tie_breaker,
        shortlist_min_size=shortlist_min_size,
        shortlist_multiplier=shortlist_multiplier,
    )
    return _run_pipeline(
        ingest_result=load_v2_csv(csv_path),
        resolved=resolved,
        output_dir=output_dir,
        criteria_text=criteria_text,
    )


def run_event_pipeline(
    *,
    event_name: str,
    accept_count: int,
    output_dir: str | Path = "results",
    config_path: str | Path | None = None,
    criteria_text: str | None = None,
    model: str | None = None,
    backend: str | None = None,
    sample_size: int | None = None,
    sample_seed: int = 0,
    ranking_policy: str | None = None,
    swiss_weight: float | None = None,
    pointwise_weight: float | None = None,
    auto_weights: bool | None = None,
    tie_breaker: str | None = None,
    shortlist_min_size: int | None = None,
    shortlist_multiplier: float | None = None,
) -> PipelineRun:
    """Execute the ranking-only v2 pipeline on a live event source."""
    resolved = _build_config(
        config_path=config_path,
        accept_count=accept_count,
        model=model,
        event_name=event_name,
        backend=backend,
        ranking_policy=ranking_policy,
        swiss_weight=swiss_weight,
        pointwise_weight=pointwise_weight,
        auto_weights=auto_weights,
        tie_breaker=tie_breaker,
        shortlist_min_size=shortlist_min_size,
        shortlist_multiplier=shortlist_multiplier,
    )
    enrichment = resolved.extras.get("enrichment", {}) if isinstance(resolved.extras, dict) else {}
    platform_db = enrichment.get("platform_db", {}) if isinstance(enrichment, dict) else {}
    supabase = enrichment.get("supabase", {}) if isinstance(enrichment, dict) else {}
    event_ingest = load_event_applicants(
        event_name,
        platform_dsn=str(platform_db.get("dsn", "")),
        supabase_url=str(supabase.get("url", "")),
        supabase_key=str(supabase.get("key", "")),
        sample_size=sample_size,
        sample_seed=sample_seed,
    )
    return _run_pipeline(
        ingest_result=event_ingest,
        resolved=resolved,
        output_dir=output_dir,
        criteria_text=criteria_text,
    )


def _run_pipeline(
    *,
    ingest_result: Any,
    resolved: ResolvedConfig,
    output_dir: str | Path,
    criteria_text: str | None,
) -> PipelineRun:
    errors = resolved.validate()
    if errors:
        raise ValueError("; ".join(errors))
    if not ingest_result.validation.valid:
        messages = "; ".join(message.message for message in ingest_result.validation.messages)
        raise ValueError(messages or "CSV validation failed")

    applicants = list(ingest_result.applicants)
    legacy_people, lookup = _legacy_people(applicants)
    if resolved.runtime.deterministic and resolved.runtime.random_seed is not None:
        random.seed(resolved.runtime.random_seed)

    legacy_config = _legacy_config_from_resolved(resolved)
    criteria = parse_criteria(criteria_text, legacy_config)
    run_dir = create_run_dir(Path(output_dir), _now_run_id())
    _write_stage_json(run_dir, "ingest_validation", ingest_result.validation)
    if resolved.runtime.backend == "legacy":
        legacy_people = run_legacy_enrichment(legacy_people, legacy_config)
    _write_stage_json(
        run_dir,
        "enrichment_summary",
        [
            {
                "applicant_id": person["name"],
                "display_name": person.get("display_name", ""),
                "has_linkedin": bool(person.get("linkedin_url")),
                "has_github": bool(person.get("github_url")),
                "total_cv_events": person.get("total_cv_events"),
                "github_stars": person.get("gh_api_stars") or person.get("github_stars"),
            }
            for person in legacy_people
        ],
    )

    def profile_fn(person: dict[str, Any]) -> str:
        return _format_profile_from_legacy(person, lookup)

    if resolved.runtime.backend == "heuristic":
        pointwise_rows = native_pointwise_scores(legacy_people, criteria)
    else:
        pointwise_dir = run_dir / "pointwise"
        pointwise_dir.mkdir(parents=True, exist_ok=True)
        pointwise_rows = asyncio.run(
            legacy_score_all(
                legacy_people,
                criteria,
                resolved.models.scoring,
                resolved.ranking.accept_count,
                legacy_config,
                pointwise_dir,
                profile_fn,
            )
        )
    pointwise_stage = _pointwise_stage_results(pointwise_rows, lookup)
    _write_stage_json(run_dir, "pointwise_stage", pointwise_stage)

    shortlist_ids = _shortlist_applicant_ids(
        pointwise_rows,
        resolved.ranking.accept_count,
        resolved,
    )
    shortlist_id_set = set(shortlist_ids)
    shortlisted_people = [person for person in legacy_people if person["name"] in shortlist_id_set]
    shortlisted_pointwise_rows = [row for row in pointwise_rows if row.get("name") in shortlist_id_set]
    _write_stage_json(
        run_dir,
        "shortlist_stage",
        {
            "policy": resolved.ranking.policy,
            "applicant_count": len(legacy_people),
            "shortlist_count": len(shortlist_ids),
            "accept_count": resolved.ranking.accept_count,
            "shortlist_ids": shortlist_ids,
        },
    )

    if resolved.runtime.backend == "heuristic":
        swiss_records, bt_strengths, swiss_matches = native_run_swiss(
            shortlisted_people,
            shortlisted_pointwise_rows,
            rounds=resolved.swiss.rounds,
        )
    else:
        swiss_dir = run_dir / "swiss"
        swiss_dir.mkdir(parents=True, exist_ok=True)
        swiss_records, bt_strengths, swiss_matches = asyncio.run(
            legacy_run_swiss(
                shortlisted_people,
                criteria,
                resolved.models.swiss,
                resolved.swiss.rounds,
                resolved.ranking.accept_count,
                legacy_config,
                swiss_dir,
                profile_fn,
                pointwise_scores=shortlisted_pointwise_rows,
            )
        )
    _write_stage_json(run_dir, "swiss_matches", swiss_matches)

    swiss_records_for_combine = {
        row["name"]: {"wins": 0, "losses": 0, "byes": 0}
        for row in pointwise_rows
        if row.get("name")
    }
    swiss_records_for_combine.update(swiss_records)

    combined_rankings, combine_metadata = combine_rankings_with_metadata(
        [
            {
                "applicant_id": row["name"],
                "display_name": lookup[row["name"]].name if row["name"] in lookup else row["name"],
                "score": row.get("score"),
                "confidence": row.get("confidence", ""),
                "strongest_signal": row.get("strongest_signal", ""),
                "concerns": row.get("concerns", ""),
                "reasoning": row.get("reasoning") or row.get("why") or "",
                "error": row.get("error"),
            }
            for row in pointwise_rows
        ],
        swiss_records_for_combine,
        bt_strengths,
        swiss_weight=resolved.ranking.swiss_weight,
        pointwise_weight=resolved.ranking.pointwise_weight,
        auto_weight=resolved.ranking.auto_weights,
        tie_breaker=resolved.ranking.tie_breaker,
    )

    legacy_combined_rows = [
        {
            "name": row["applicant_id"],
            "display_name": row["display_name"],
            "rank": row["rank"],
            "final_score": row["final_score"],
            "pointwise_score": row["pointwise_score"],
            "swiss_wins": row["swiss_wins"],
            "swiss_losses": row["swiss_losses"],
            "confidence": row.get("confidence", ""),
            "strongest_signal": row.get("strongest_signal", ""),
            "concerns": row.get("concerns", ""),
            "why": row.get("reasoning", ""),
        }
        for row in combined_rankings
    ]

    if resolved.ranking.borderline_extra_rounds > 0:
        pre_borderline_rankings = list(combined_rankings)
        if resolved.runtime.backend == "heuristic":
            bl_records, bl_bt, bl_matches = native_run_borderline(
                legacy_people,
                pointwise_rows,
                combined_rankings,
                swiss_records,
                bt_strengths,
                accept_count=resolved.ranking.accept_count,
                band_pct=resolved.ranking.borderline_band_pct,
                extra_rounds=resolved.ranking.borderline_extra_rounds,
                disagreement_threshold=resolved.quality.disagreement_threshold,
            )
        else:
            borderline_dir = run_dir / "borderline"
            borderline_dir.mkdir(parents=True, exist_ok=True)
            bl_records, bl_bt, bl_matches = asyncio.run(
                legacy_run_borderline_reeval(
                    legacy_people,
                    legacy_combined_rows,
                    criteria,
                    legacy_config,
                    borderline_dir,
                    profile_fn,
                    pointwise_scores=pointwise_rows,
                    swiss_records=swiss_records,
                    bt_strengths=bt_strengths,
                )
            )
        _write_stage_json(run_dir, "borderline_matches", bl_matches)
        if bl_records:
            if resolved.runtime.backend == "heuristic":
                combined_rankings = native_merge_borderline(
                    pointwise_rows,
                    swiss_records_for_combine,
                    bt_strengths,
                    bl_records,
                    bl_bt,
                    swiss_weight=resolved.ranking.swiss_weight,
                    pointwise_weight=resolved.ranking.pointwise_weight,
                    auto_weight=resolved.ranking.auto_weights,
                    tie_breaker=resolved.ranking.tie_breaker,
                )
            else:
                recombined = legacy_recombine_with_borderline(
                    pointwise_rows,
                    swiss_records,
                    bt_strengths,
                    bl_records,
                    bl_bt,
                    legacy_config,
                )
                recombined_rows = _combined_from_legacy(recombined, lookup)
                recombined_ids = {row["applicant_id"] for row in recombined_rows}
                combined_rankings = _sort_combined_rankings(
                    recombined_rows
                    + [row for row in pre_borderline_rankings if row["applicant_id"] not in recombined_ids],
                    resolved.ranking.tie_breaker,
                )

    pointwise_rank_by_id = {
        row["applicant_id"]: index
        for index, row in enumerate(
            sorted(
                [
                    {
                        "applicant_id": row["name"],
                        "score": row.get("score", 0.0),
                    }
                    for row in pointwise_rows
                    if row.get("name")
                ],
                key=lambda row: (-float(row["score"]), row["applicant_id"]),
            ),
            start=1,
        )
    }
    _write_stage_json(
        run_dir,
        "ranking_audit",
        {
            "policy": resolved.ranking.policy,
            "tie_breaker": resolved.ranking.tie_breaker,
            "combine": combine_metadata,
            "shortlist_count": len(shortlist_ids),
            "largest_rank_shifts": sorted(
                [
                    {
                        "applicant_id": row["applicant_id"],
                        "display_name": row["display_name"],
                        "pointwise_rank": pointwise_rank_by_id.get(row["applicant_id"]),
                        "final_rank": row["rank"],
                        "rank_delta": (
                            pointwise_rank_by_id.get(row["applicant_id"], row["rank"]) - row["rank"]
                        ),
                        "pointwise_score": row["pointwise_score"],
                        "swiss_wins": row["swiss_wins"],
                        "swiss_losses": row["swiss_losses"],
                        "bt_strength": row.get("bt_strength"),
                    }
                    for row in combined_rankings
                ],
                key=lambda row: abs(row["rank_delta"]),
                reverse=True,
            )[:10],
        },
    )

    if resolved.runtime.backend == "heuristic":
        quality_rows = native_quality_review(
            legacy_people,
            combined_rankings,
            pointwise_rows,
            accept_count=resolved.ranking.accept_count,
        )
    else:
        quality_dir = run_dir / "quality"
        quality_dir.mkdir(parents=True, exist_ok=True)
        quality_rows = asyncio.run(
            legacy_run_quality_check(
                legacy_people,
                [
                    {
                        "name": row["applicant_id"],
                        "rank": row["rank"],
                        "final_score": row["final_score"],
                        "pointwise_score": row["pointwise_score"],
                        "swiss_wins": row["swiss_wins"],
                        "swiss_losses": row["swiss_losses"],
                        "why": row.get("reasoning", ""),
                        "concerns": row.get("concerns", ""),
                    }
                    for row in combined_rankings
                ],
                legacy_config,
                quality_dir,
                profile_fn,
            )
        )
    quality_by_id = {row["name"]: row for row in quality_rows if row.get("name")}

    artifact_paths = {
        "ranked_csv": write_ranked_csv(
            applicants,
            combined_rankings,
            quality_by_id,
            run_dir / "RANKED_V2.csv",
            target_accepts=resolved.ranking.accept_count,
        ),
        "failed_pointwise": write_failures_csv(
            applicants,
            "pointwise",
            [
                {
                    "applicant_id": row["name"],
                    "display_name": lookup[row["name"]].name if row["name"] in lookup else row["name"],
                    "error": row.get("error"),
                }
                for row in pointwise_rows
            ],
            run_dir / "FAILED_TO_RANK_V2.csv",
        ),
        "needs_review": write_needs_review_csv(
            applicants,
            combined_rankings,
            run_dir / "NEEDS_REVIEW_V2.csv",
            target_accepts=resolved.ranking.accept_count,
            boundary_width_pct=resolved.quality.needs_review_band_pct,
            disagreement_threshold=resolved.quality.disagreement_threshold,
        ),
    }
    _write_stage_json(run_dir, "combined_rankings", combined_rankings)
    _write_stage_json(run_dir, "quality_results", quality_rows)

    return PipelineRun(
        run_dir=run_dir,
        config=resolved,
        applicants=tuple(applicants),
        criteria=criteria,
        pointwise=pointwise_stage,
        combined_rankings=tuple(combined_rankings),
        quality_results=quality_by_id,
        artifact_paths=artifact_paths,
    )
