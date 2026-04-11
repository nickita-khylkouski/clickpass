from __future__ import annotations

from datetime import datetime, timezone

from cv_rank_v2.domain.models import (
    Applicant,
    CombinedRank,
    EvidenceItem,
    PairwiseMatch,
    PointwiseScore,
    QualityDecision,
    QualityVerdict,
    SourceReference,
    StageResult,
    StageStatus,
    applicant_ids,
    evidence_snapshot,
    profile_snapshot,
    sort_applicants_by_id,
)
from cv_rank_v2.runtime.checkpoints import (
    RunCheckpointManifest,
    StageCheckpoint,
    checkpoint_key,
    checkpoint_path,
    to_json_ready,
)
from cv_rank_v2.runtime.config import ResolvedConfig


def test_resolved_config_merges_v1_style_mapping() -> None:
    cfg = ResolvedConfig.from_mapping(
        {
            "event": {"name": "Test Event", "type": "community_meetup", "city": "San Francisco"},
            "models": {"scoring": "gpt-5.2", "borderline": "gpt-5.4"},
            "swiss": {
                "rounds": 9,
                "use_bradley_terry": False,
                "auto_rounds": {"enabled": False, "offset": 0, "min_rounds": 3, "max_guard_offset": 1},
                "early_stop": {"enabled": False, "min_rounds": 3, "boundary_width_pct": 0.2, "boundary_width_min": 5},
            },
            "weights": {"swiss": 0.7, "pointwise": 0.3, "auto": False},
            "runtime": {"scoring_concurrency": 12, "random_seed": 99},
            "checkpoint": {"every_n": 25},
            "quality": {"needs_review_enabled": True, "disagreement_threshold": 0.33},
            "custom": {"keep": True},
        }
    )

    assert cfg.event.name == "Test Event"
    assert cfg.models.scoring == "gpt-5.2"
    assert cfg.models.swiss == "gpt-5.2"
    assert cfg.swiss.rounds == 9
    assert cfg.swiss.use_bradley_terry is False
    assert cfg.ranking.policy == "swiss_first"
    assert cfg.ranking.tie_breaker == "swiss"
    assert cfg.ranking.swiss_weight == 0.7
    assert cfg.ranking.pointwise_weight == 0.3
    assert cfg.runtime.scoring_concurrency == 12
    assert cfg.runtime.random_seed == 99
    assert cfg.checkpoint.every_n == 25
    assert cfg.quality.disagreement_threshold == 0.33
    assert cfg.extras["custom"] == {"keep": True}
    assert cfg.validate() == []


def test_resolved_config_validation_flags_invalid_weights() -> None:
    cfg = ResolvedConfig.from_mapping({"weights": {"swiss": 0.8, "pointwise": 0.3, "auto": False}})
    errors = cfg.validate()
    assert any("must equal 1.0" in error for error in errors)


def test_resolved_config_applies_policy_preset_defaults() -> None:
    cfg = ResolvedConfig.from_mapping({"ranking": {"policy": "swiss_dominant"}})

    assert cfg.ranking.policy == "swiss_dominant"
    assert cfg.ranking.swiss_weight == 0.8
    assert cfg.ranking.pointwise_weight == 0.2
    assert cfg.ranking.auto_weights is False
    assert cfg.ranking.tie_breaker == "swiss"


def test_applicant_and_stage_models_round_trip() -> None:
    applicant = Applicant(
        applicant_id="cand_123",
        display_name="Alice Chen",
        email="alice@example.com",
        links={"github": "https://github.com/alice"},
        profile={"company": "Anthropic"},
        tags=("builder",),
    )
    applicant = applicant.add_evidence(
        EvidenceItem(kind="github", summary="340 stars", source="github", confidence=0.9),
    )
    applicant = applicant.add_source_refs(
        SourceReference(source="csv", record_id="row-1"),
    )

    assert applicant.evidence_count == 1
    assert applicant_ids([applicant]) == ("cand_123",)
    assert profile_snapshot(applicant)["display_name"] == "Alice Chen"
    assert evidence_snapshot(applicant.evidence)[0]["kind"] == "github"

    stage_result = StageResult.success(
        "pointwise",
        [PointwiseScore(applicant_id="cand_123", score=91.5, confidence="high")],
        input_count=1,
        metadata={"model": "gpt-5.2"},
    )
    assert stage_result.succeeded is True
    assert stage_result.output_count == 1
    assert stage_result.items[0].applicant_id == "cand_123"
    assert stage_result.metadata["model"] == "gpt-5.2"
    assert stage_result.elapsed_ms is not None

    failed = StageResult.failure("swiss", "timeout", input_count=2)
    assert failed.status == StageStatus.FAILED
    assert failed.error == "timeout"


def test_sort_and_decision_models() -> None:
    applicants = [
        Applicant(applicant_id="cand_b", display_name="B"),
        Applicant(applicant_id="cand_a", display_name="A"),
    ]
    assert [a.applicant_id for a in sort_applicants_by_id(applicants)] == ["cand_a", "cand_b"]

    pairwise = PairwiseMatch("cand_a", "cand_b", "cand_a", swapped=True, model="gpt-5.2")
    assert pairwise.is_valid is True

    combined = CombinedRank("cand_a", pointwise_score=88.0, pairwise_strength=0.7, combined_score=0.82)
    assert combined.applicant_id == "cand_a"

    decision = QualityDecision("cand_a", verdict=QualityVerdict.YES, summary="Strong fit")
    assert decision.verdict == QualityVerdict.YES


def test_checkpoint_helpers_are_json_safe_and_round_trip() -> None:
    checkpoint = StageCheckpoint(
        run_id="run_20260403_0001",
        stage="pointwise",
        schema_version=2,
        status=StageStatus.RUNNING,
        completed_ids=("cand_1", "cand_2"),
        cursor="page_3",
        payload={"model": "gpt-5.2"},
        created_at=datetime(2026, 4, 3, 12, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 4, 3, 12, 1, tzinfo=timezone.utc),
    )

    raw = checkpoint.to_dict()
    assert raw["status"] == "running"
    assert raw["completed_ids"] == ["cand_1", "cand_2"]
    assert raw["created_at"] == "2026-04-03T12:00:00+00:00"
    assert checkpoint_key("run_20260403_0001", "pointwise", schema_version=2) == "run_20260403_0001:pointwise:v2"
    assert checkpoint_path("run_20260403_0001", "pointwise", schema_version=2) == "run_20260403_0001.pointwise.v2.json"

    restored = StageCheckpoint.from_dict(raw)
    assert restored == checkpoint

    manifest = RunCheckpointManifest(run_id="run_20260403_0001", schema_version=2, stages=(checkpoint,))
    manifest_raw = manifest.to_dict()
    assert manifest_raw["stages"][0]["stage"] == "pointwise"
    assert manifest.stage("pointwise") == checkpoint


def test_to_json_ready_handles_nested_dataclasses() -> None:
    value = to_json_ready(
        {
            "applicant": Applicant(applicant_id="cand_1", display_name="A"),
            "stage": StageStatus.COMPLETED,
            "when": datetime(2026, 4, 3, 12, 0, tzinfo=timezone.utc),
        }
    )
    assert value["applicant"]["applicant_id"] == "cand_1"
    assert value["stage"] == "completed"
    assert value["when"] == "2026-04-03T12:00:00+00:00"
