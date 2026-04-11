"""Resolved configuration for the v2 ranking pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping
import copy
import os
import re

RANKING_POLICY_PRESETS: dict[str, dict[str, Any]] = {
    "balanced": {
        "swiss_weight": 0.50,
        "pointwise_weight": 0.50,
        "auto_weights": True,
        "tie_breaker": "pointwise",
    },
    "swiss_first": {
        "swiss_weight": 0.65,
        "pointwise_weight": 0.35,
        "auto_weights": False,
        "tie_breaker": "swiss",
    },
    "swiss_dominant": {
        "swiss_weight": 0.80,
        "pointwise_weight": 0.20,
        "auto_weights": False,
        "tie_breaker": "swiss",
    },
    "pointwise_first": {
        "swiss_weight": 0.35,
        "pointwise_weight": 0.65,
        "auto_weights": False,
        "tie_breaker": "pointwise",
    },
}


def _utc_env_interpolate(value: Any, env: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        pattern = re.compile(r"\$\{(\w+)\}")

        def _sub(match: re.Match[str]) -> str:
            return env.get(match.group(1), match.group(0))

        return pattern.sub(_sub, value)
    if isinstance(value, list):
        return [_utc_env_interpolate(item, env) for item in value]
    if isinstance(value, dict):
        return {key: _utc_env_interpolate(val, env) for key, val in value.items()}
    return value


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(dict(base))
    for key, value in override.items():
        base_value = merged.get(key)
        if isinstance(base_value, dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(base_value, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _section(mapping: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = mapping.get(key, {})
    return dict(value) if isinstance(value, Mapping) else {}


def _overlay(mapping: Mapping[str, Any], section_name: str) -> dict[str, Any]:
    """Overlay a section on top of the full mapping for legacy compatibility."""

    combined = dict(mapping)
    combined.update(_section(mapping, section_name))
    return combined


@dataclass(slots=True, frozen=True)
class EventConfig:
    name: str = ""
    type: str = "community_meetup"
    city: str = ""
    date: str | None = None

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> EventConfig:
        return cls(
            name=str(mapping.get("name", "")),
            type=str(mapping.get("type", "community_meetup")),
            city=str(mapping.get("city", "")),
            date=None if mapping.get("date") in (None, "") else str(mapping.get("date")),
        )


@dataclass(slots=True, frozen=True)
class ModelConfig:
    scoring: str = "gpt-5-mini"
    swiss: str = "gpt-5-mini"
    quality_check: str = "gpt-5-mini"
    borderline: str = "gpt-5.2"

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> ModelConfig:
        return cls(
            scoring=str(mapping.get("scoring", "gpt-5-mini")),
            swiss=str(mapping.get("swiss", mapping.get("scoring", "gpt-5-mini"))),
            quality_check=str(mapping.get("quality_check", mapping.get("scoring", "gpt-5-mini"))),
            borderline=str(mapping.get("borderline", "gpt-5.2")),
        )


@dataclass(slots=True, frozen=True)
class SwissConfig:
    rounds: int = 20
    use_bradley_terry: bool = True
    inject_pointwise: bool = True
    bt_pointwise_prior: bool = True
    bt_prior_strength: int = 2
    auto_rounds_enabled: bool = True
    auto_rounds_offset: int = -1
    auto_rounds_min: int = 5
    auto_rounds_max_guard_offset: int = 2
    early_stop_enabled: bool = True
    early_stop_min_rounds: int = 5
    early_stop_jaccard_accept_set_min: float = 0.98
    early_stop_max_boundary_rank_shift: int = 2
    early_stop_consecutive_rounds: int = 2
    early_stop_boundary_width_pct: float = 0.10
    early_stop_boundary_width_min: int = 20

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> SwissConfig:
        auto = _section(mapping, "auto_rounds")
        early = _section(mapping, "early_stop")
        return cls(
            rounds=int(mapping.get("rounds", 20)),
            use_bradley_terry=bool(mapping.get("use_bradley_terry", True)),
            inject_pointwise=bool(mapping.get("inject_pointwise", True)),
            bt_pointwise_prior=bool(mapping.get("bt_pointwise_prior", True)),
            bt_prior_strength=int(mapping.get("bt_prior_strength", 2)),
            auto_rounds_enabled=bool(auto.get("enabled", True)),
            auto_rounds_offset=int(auto.get("offset", -1)),
            auto_rounds_min=int(auto.get("min_rounds", 5)),
            auto_rounds_max_guard_offset=int(auto.get("max_guard_offset", 2)),
            early_stop_enabled=bool(early.get("enabled", True)),
            early_stop_min_rounds=int(early.get("min_rounds", 5)),
            early_stop_jaccard_accept_set_min=float(early.get("jaccard_accept_set_min", 0.98)),
            early_stop_max_boundary_rank_shift=int(early.get("max_boundary_rank_shift", 2)),
            early_stop_consecutive_rounds=int(early.get("consecutive_rounds", 2)),
            early_stop_boundary_width_pct=float(early.get("boundary_width_pct", 0.10)),
            early_stop_boundary_width_min=int(early.get("boundary_width_min", 20)),
        )


@dataclass(slots=True, frozen=True)
class RankingConfig:
    accept_count: int = 0
    policy: str = "swiss_first"
    pointwise_weight: float = 0.35
    swiss_weight: float = 0.65
    auto_weights: bool = False
    tie_breaker: str = "swiss"
    shortlist_min_size: int = 100
    shortlist_multiplier: float = 3.0
    borderline_band_pct: float = 0.15
    borderline_extra_rounds: int = 5

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> RankingConfig:
        policy = str(mapping.get("policy", "swiss_first"))
        preset = RANKING_POLICY_PRESETS.get(policy, RANKING_POLICY_PRESETS["swiss_first"])
        weights = mapping.get("weights", {})
        return cls(
            accept_count=int(mapping.get("accept_count", 0)),
            policy=policy,
            pointwise_weight=float(mapping.get("pointwise_weight", weights.get("pointwise", preset["pointwise_weight"]))),
            swiss_weight=float(mapping.get("swiss_weight", weights.get("swiss", preset["swiss_weight"]))),
            auto_weights=bool(mapping.get("auto_weights", weights.get("auto", preset["auto_weights"]))),
            tie_breaker=str(mapping.get("tie_breaker", preset["tie_breaker"])),
            shortlist_min_size=int(mapping.get("shortlist_min_size", 100)),
            shortlist_multiplier=float(mapping.get("shortlist_multiplier", 3.0)),
            borderline_band_pct=float(mapping.get("borderline_band_pct", 0.15)),
            borderline_extra_rounds=int(mapping.get("borderline_extra_rounds", 5)),
        )


@dataclass(slots=True, frozen=True)
class RuntimeConfig:
    backend: str = "legacy"
    scoring_concurrency: int = 50
    swiss_concurrency: int = 50
    quality_concurrency: int = 50
    max_retries: int = 5
    deterministic: bool = False
    random_seed: int | None = None

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> RuntimeConfig:
        return cls(
            backend=str(mapping.get("backend", "legacy")),
            scoring_concurrency=int(mapping.get("scoring_concurrency", 50)),
            swiss_concurrency=int(mapping.get("swiss_concurrency", 50)),
            quality_concurrency=int(mapping.get("quality_concurrency", 50)),
            max_retries=int(mapping.get("max_retries", 5)),
            deterministic=bool(mapping.get("deterministic", False)),
            random_seed=None if mapping.get("random_seed") in (None, "") else int(mapping.get("random_seed")),
        )


@dataclass(slots=True, frozen=True)
class CheckpointConfig:
    every_n: int = 50
    schema_version: int = 1
    allow_resume: bool = True
    atomic_writes: bool = True

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> CheckpointConfig:
        return cls(
            every_n=int(mapping.get("every_n", mapping.get("save_every", 50))),
            schema_version=int(mapping.get("schema_version", 1)),
            allow_resume=bool(mapping.get("allow_resume", True)),
            atomic_writes=bool(mapping.get("atomic_writes", True)),
        )


@dataclass(slots=True, frozen=True)
class QualityConfig:
    needs_review_enabled: bool = True
    needs_review_band_pct: float = 0.10
    needs_review_band_min: int = 20
    disagreement_threshold: float = 0.25
    max_exclusion_rate: float = 0.02
    require_failed_report: bool = True

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> QualityConfig:
        return cls(
            needs_review_enabled=bool(mapping.get("needs_review_enabled", mapping.get("needs_review", {}).get("enabled", True))),
            needs_review_band_pct=float(mapping.get("needs_review_band_pct", mapping.get("needs_review", {}).get("boundary_width_pct", 0.10))),
            needs_review_band_min=int(mapping.get("needs_review_band_min", mapping.get("needs_review", {}).get("boundary_width_min", 20))),
            disagreement_threshold=float(mapping.get("disagreement_threshold", mapping.get("needs_review", {}).get("disagreement_threshold", 0.25))),
            max_exclusion_rate=float(mapping.get("max_exclusion_rate", 0.02)),
            require_failed_report=bool(mapping.get("require_failed_report", True)),
        )


@dataclass(slots=True, frozen=True)
class ResolvedConfig:
    event: EventConfig = field(default_factory=EventConfig)
    models: ModelConfig = field(default_factory=ModelConfig)
    swiss: SwissConfig = field(default_factory=SwissConfig)
    ranking: RankingConfig = field(default_factory=RankingConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    checkpoint: CheckpointConfig = field(default_factory=CheckpointConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    extras: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def default(cls) -> ResolvedConfig:
        return cls()

    @classmethod
    def from_mapping(
        cls,
        mapping: Mapping[str, Any] | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> ResolvedConfig:
        raw = dict(mapping or {})
        env_map = env or os.environ
        interpolated = _utc_env_interpolate(raw, env_map)
        raw_models = _section(interpolated, "models")
        raw_weights = _section(interpolated, "weights")
        raw_ranking = _section(interpolated, "ranking")
        merged = _deep_merge(cls.default().to_dict(), interpolated)

        known_keys = {"event", "models", "swiss", "ranking", "runtime", "checkpoint", "quality"}
        extras = {key: value for key, value in merged.items() if key not in known_keys}
        models = ModelConfig.from_mapping(_overlay(merged, "models"))
        if "swiss" not in raw_models:
            models = replace(models, swiss=models.scoring)
        if "quality_check" not in raw_models:
            models = replace(models, quality_check=models.scoring)
        ranking = RankingConfig.from_mapping(_overlay(merged, "ranking"))
        if raw_ranking.get("policy") and not raw_weights:
            explicit_ranking_keys = {
                "pointwise_weight",
                "swiss_weight",
                "auto_weights",
                "tie_breaker",
            }
            if explicit_ranking_keys.isdisjoint(raw_ranking):
                preset = RANKING_POLICY_PRESETS.get(
                    str(raw_ranking["policy"]),
                    RANKING_POLICY_PRESETS["swiss_first"],
                )
                ranking = replace(
                    ranking,
                    pointwise_weight=float(preset["pointwise_weight"]),
                    swiss_weight=float(preset["swiss_weight"]),
                    auto_weights=bool(preset["auto_weights"]),
                    tie_breaker=str(preset["tie_breaker"]),
                )
        if raw_weights:
            ranking = replace(
                ranking,
                pointwise_weight=float(raw_weights.get("pointwise", ranking.pointwise_weight)),
                swiss_weight=float(raw_weights.get("swiss", ranking.swiss_weight)),
                auto_weights=bool(raw_weights.get("auto", ranking.auto_weights)),
            )
        return cls(
            event=EventConfig.from_mapping(_overlay(merged, "event")),
            models=models,
            swiss=SwissConfig.from_mapping(_overlay(merged, "swiss")),
            ranking=ranking,
            runtime=RuntimeConfig.from_mapping(_overlay(merged, "runtime")),
            checkpoint=CheckpointConfig.from_mapping(_overlay(merged, "checkpoint")),
            quality=QualityConfig.from_mapping(_overlay(merged, "quality")),
            extras=extras,
        )

    def with_overrides(self, **updates: Any) -> ResolvedConfig:
        return replace(self, **updates)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "event": {
                "name": self.event.name,
                "type": self.event.type,
                "city": self.event.city,
                "date": self.event.date,
            },
            "models": {
                "scoring": self.models.scoring,
                "swiss": self.models.swiss,
                "quality_check": self.models.quality_check,
                "borderline": self.models.borderline,
            },
            "swiss": {
                "rounds": self.swiss.rounds,
                "use_bradley_terry": self.swiss.use_bradley_terry,
                "inject_pointwise": self.swiss.inject_pointwise,
                "bt_pointwise_prior": self.swiss.bt_pointwise_prior,
                "bt_prior_strength": self.swiss.bt_prior_strength,
                "auto_rounds": {
                    "enabled": self.swiss.auto_rounds_enabled,
                    "offset": self.swiss.auto_rounds_offset,
                    "min_rounds": self.swiss.auto_rounds_min,
                    "max_guard_offset": self.swiss.auto_rounds_max_guard_offset,
                },
                "early_stop": {
                    "enabled": self.swiss.early_stop_enabled,
                    "min_rounds": self.swiss.early_stop_min_rounds,
                    "jaccard_accept_set_min": self.swiss.early_stop_jaccard_accept_set_min,
                    "max_boundary_rank_shift": self.swiss.early_stop_max_boundary_rank_shift,
                    "consecutive_rounds": self.swiss.early_stop_consecutive_rounds,
                    "boundary_width_pct": self.swiss.early_stop_boundary_width_pct,
                    "boundary_width_min": self.swiss.early_stop_boundary_width_min,
                },
            },
            "ranking": {
                "accept_count": self.ranking.accept_count,
                "policy": self.ranking.policy,
                "pointwise_weight": self.ranking.pointwise_weight,
                "swiss_weight": self.ranking.swiss_weight,
                "auto_weights": self.ranking.auto_weights,
                "tie_breaker": self.ranking.tie_breaker,
                "shortlist_min_size": self.ranking.shortlist_min_size,
                "shortlist_multiplier": self.ranking.shortlist_multiplier,
                "borderline_band_pct": self.ranking.borderline_band_pct,
                "borderline_extra_rounds": self.ranking.borderline_extra_rounds,
            },
            "runtime": {
                "backend": self.runtime.backend,
                "scoring_concurrency": self.runtime.scoring_concurrency,
                "swiss_concurrency": self.runtime.swiss_concurrency,
                "quality_concurrency": self.runtime.quality_concurrency,
                "max_retries": self.runtime.max_retries,
                "deterministic": self.runtime.deterministic,
                "random_seed": self.runtime.random_seed,
            },
            "checkpoint": {
                "every_n": self.checkpoint.every_n,
                "schema_version": self.checkpoint.schema_version,
                "allow_resume": self.checkpoint.allow_resume,
                "atomic_writes": self.checkpoint.atomic_writes,
            },
            "quality": {
                "needs_review_enabled": self.quality.needs_review_enabled,
                "needs_review_band_pct": self.quality.needs_review_band_pct,
                "needs_review_band_min": self.quality.needs_review_band_min,
                "disagreement_threshold": self.quality.disagreement_threshold,
                "max_exclusion_rate": self.quality.max_exclusion_rate,
                "require_failed_report": self.quality.require_failed_report,
            },
        }
        data.update(copy.deepcopy(dict(self.extras)))
        return data

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.ranking.accept_count < 0:
            errors.append("ranking.accept_count must be >= 0")
        if self.runtime.scoring_concurrency <= 0:
            errors.append("runtime.scoring_concurrency must be > 0")
        if self.runtime.backend not in {"legacy", "heuristic"}:
            errors.append("runtime.backend must be 'legacy' or 'heuristic'")
        if self.runtime.swiss_concurrency <= 0:
            errors.append("runtime.swiss_concurrency must be > 0")
        if self.runtime.quality_concurrency <= 0:
            errors.append("runtime.quality_concurrency must be > 0")
        if self.runtime.max_retries < 0:
            errors.append("runtime.max_retries must be >= 0")
        if self.swiss.rounds <= 0:
            errors.append("swiss.rounds must be > 0")
        if self.ranking.policy not in RANKING_POLICY_PRESETS:
            errors.append(
                f"ranking.policy must be one of: {', '.join(sorted(RANKING_POLICY_PRESETS))}"
            )
        if not 0.0 <= self.ranking.pointwise_weight <= 1.0:
            errors.append("ranking.pointwise_weight must be between 0 and 1")
        if not 0.0 <= self.ranking.swiss_weight <= 1.0:
            errors.append("ranking.swiss_weight must be between 0 and 1")
        if self.ranking.tie_breaker not in {"pointwise", "swiss"}:
            errors.append("ranking.tie_breaker must be 'pointwise' or 'swiss'")
        if not 0.0 <= self.ranking.borderline_band_pct <= 1.0:
            errors.append("ranking.borderline_band_pct must be between 0 and 1")
        if not 0.0 <= self.quality.needs_review_band_pct <= 1.0:
            errors.append("quality.needs_review_band_pct must be between 0 and 1")
        if not 0.0 <= self.quality.disagreement_threshold <= 1.0:
            errors.append("quality.disagreement_threshold must be between 0 and 1")
        if not self.ranking.auto_weights:
            total = self.ranking.pointwise_weight + self.ranking.swiss_weight
            if abs(total - 1.0) > 1e-6:
                errors.append("ranking.pointwise_weight + ranking.swiss_weight must equal 1.0 when auto_weights is false")
        return errors

    def stage_models(self) -> dict[str, str]:
        return {
            "pointwise": self.models.scoring,
            "swiss": self.models.swiss,
            "quality": self.models.quality_check,
            "borderline": self.models.borderline,
        }
