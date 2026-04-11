"""Core ranking-domain models for the v2 rewrite.

These dataclasses are intentionally small and explicit so later scoring
and orchestration code can depend on stable shapes instead of raw CSV
dicts or ad hoc JSON blobs.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any, Generic, Mapping, Sequence, TypeVar


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StageStatus(StrEnum):
    """Lifecycle state for a ranking stage."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class QualityVerdict(StrEnum):
    """Final decision labels for review output."""

    STRONG_YES = "strong_yes"
    YES = "yes"
    BORDERLINE = "borderline"
    NO = "no"


@dataclass(slots=True, frozen=True)
class SourceReference:
    """Provenance for a raw source record."""

    source: str
    record_id: str
    url: str | None = None
    collected_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class EvidenceItem:
    """Structured evidence extracted from a source."""

    kind: str
    summary: str
    source: str
    confidence: float = 1.0
    verified: bool = False
    observed_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class Applicant:
    """Canonical applicant record used by every downstream stage."""

    applicant_id: str
    display_name: str
    email: str | None = None
    links: Mapping[str, str] = field(default_factory=dict)
    profile: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[EvidenceItem, ...] = ()
    source_refs: tuple[SourceReference, ...] = ()
    tags: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def with_profile(self, **updates: Any) -> Applicant:
        merged = dict(self.profile)
        merged.update(updates)
        return replace(self, profile=merged, updated_at=_utcnow())

    def add_evidence(self, *items: EvidenceItem) -> Applicant:
        return replace(self, evidence=self.evidence + tuple(items), updated_at=_utcnow())

    def add_source_refs(self, *refs: SourceReference) -> Applicant:
        return replace(self, source_refs=self.source_refs + tuple(refs), updated_at=_utcnow())

    @property
    def evidence_count(self) -> int:
        return len(self.evidence)


@dataclass(slots=True, frozen=True)
class PointwiseScore:
    """Output of the broad pointwise model pass."""

    applicant_id: str
    score: float
    confidence: str = "medium"
    strongest_signal: str = ""
    concerns: str = ""
    reasoning: str = ""
    model: str = ""
    prompt_version: str = ""
    dimensions: Mapping[str, float] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def clamped_score(self) -> float:
        return max(0.0, min(100.0, float(self.score)))


@dataclass(slots=True, frozen=True)
class PairwiseMatch:
    """One pairwise comparison result."""

    left_applicant_id: str
    right_applicant_id: str
    winner_applicant_id: str | None
    swapped: bool = False
    model: str = ""
    reasoning: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return self.winner_applicant_id in {
            self.left_applicant_id,
            self.right_applicant_id,
        }


@dataclass(slots=True, frozen=True)
class CombinedRank:
    """Final blended ranking signal before review/export."""

    applicant_id: str
    pointwise_score: float
    pairwise_strength: float | None = None
    combined_score: float = 0.0
    pointwise_weight: float = 0.5
    pairwise_weight: float = 0.5
    rank: int | None = None
    band: str = ""
    signal_notes: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class QualityDecision:
    """Final quality-review output."""

    applicant_id: str
    verdict: QualityVerdict
    summary: str
    details: str = ""
    needs_human_review: bool = False
    flags: tuple[str, ...] = ()
    model: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


T = TypeVar("T")


@dataclass(slots=True)
class StageResult(Generic[T]):
    """Typed stage envelope for progress, output, and failures."""

    stage: str
    status: StageStatus
    started_at: datetime
    finished_at: datetime | None = None
    input_count: int = 0
    output_count: int = 0
    items: tuple[T, ...] = ()
    error: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def pending(cls, stage: str, *, input_count: int = 0, metadata: Mapping[str, Any] | None = None) -> StageResult[Any]:
        now = _utcnow()
        return cls(stage=stage, status=StageStatus.PENDING, started_at=now, input_count=input_count, metadata=dict(metadata or {}))

    @classmethod
    def success(
        cls,
        stage: str,
        items: Sequence[T] = (),
        *,
        input_count: int = 0,
        metadata: Mapping[str, Any] | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> StageResult[T]:
        start = started_at or _utcnow()
        finish = finished_at or _utcnow()
        return cls(
            stage=stage,
            status=StageStatus.COMPLETED,
            started_at=start,
            finished_at=finish,
            input_count=input_count,
            output_count=len(items),
            items=tuple(items),
            metadata=dict(metadata or {}),
        )

    @classmethod
    def failure(
        cls,
        stage: str,
        error: str,
        *,
        input_count: int = 0,
        metadata: Mapping[str, Any] | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> StageResult[Any]:
        start = started_at or _utcnow()
        finish = finished_at or _utcnow()
        return cls(
            stage=stage,
            status=StageStatus.FAILED,
            started_at=start,
            finished_at=finish,
            input_count=input_count,
            output_count=0,
            error=error,
            metadata=dict(metadata or {}),
        )

    @property
    def succeeded(self) -> bool:
        return self.status == StageStatus.COMPLETED

    @property
    def elapsed_ms(self) -> int | None:
        if self.finished_at is None:
            return None
        delta = self.finished_at - self.started_at
        return max(0, int(delta.total_seconds() * 1000))

    def with_metadata(self, **updates: Any) -> StageResult[T]:
        merged = dict(self.metadata)
        merged.update(updates)
        return replace(self, metadata=merged)


def sort_applicants_by_id(applicants: Sequence[Applicant]) -> tuple[Applicant, ...]:
    """Return a stable applicant ordering for deterministic replay."""

    return tuple(sorted(applicants, key=lambda applicant: applicant.applicant_id))


def applicant_ids(applicants: Sequence[Applicant]) -> tuple[str, ...]:
    return tuple(applicant.applicant_id for applicant in applicants)


def profile_snapshot(applicant: Applicant) -> dict[str, Any]:
    """Compact JSON-ready applicant summary for later checkpoints."""

    return {
        "applicant_id": applicant.applicant_id,
        "display_name": applicant.display_name,
        "email": applicant.email,
        "links": dict(applicant.links),
        "profile": dict(applicant.profile),
        "tags": list(applicant.tags),
        "evidence_count": applicant.evidence_count,
    }


def evidence_snapshot(items: Sequence[EvidenceItem]) -> list[dict[str, Any]]:
    """Compact JSON-ready evidence view."""

    result: list[dict[str, Any]] = []
    for item in items:
        result.append(
            {
                "kind": item.kind,
                "summary": item.summary,
                "source": item.source,
                "confidence": item.confidence,
                "verified": item.verified,
                "observed_at": item.observed_at.isoformat() if item.observed_at else None,
                "metadata": dict(item.metadata),
            }
        )
    return result


def date_or_none(value: Any) -> date | None:
    """Small helper for typed parsing in later stages."""

    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value))


def datetime_or_none(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))
