"""Checkpoint schema helpers for the v2 ranking pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from cv_rank_v2.domain.models import StageStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_json_ready(value: Any) -> Any:
    """Convert dataclasses and rich Python values into JSON-safe values."""

    if is_dataclass(value):
        return {f.name: to_json_ready(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): to_json_ready(val) for key, val in value.items()}
    if isinstance(value, tuple):
        return [to_json_ready(item) for item in value]
    if isinstance(value, list):
        return [to_json_ready(item) for item in value]
    if isinstance(value, set):
        return [to_json_ready(item) for item in sorted(value, key=str)]
    return value


def checkpoint_key(run_id: str, stage: str, *, schema_version: int = 1) -> str:
    return f"{run_id}:{stage}:v{schema_version}"


def checkpoint_path(run_id: str, stage: str, *, schema_version: int = 1, suffix: str = "json") -> str:
    return f"{run_id}.{stage}.v{schema_version}.{suffix}"


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


@dataclass(slots=True, frozen=True)
class StageCheckpoint:
    """Serializable per-stage checkpoint payload."""

    run_id: str
    stage: str
    schema_version: int
    status: StageStatus
    completed_ids: tuple[str, ...] = ()
    cursor: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def to_dict(self) -> dict[str, Any]:
        return to_json_ready(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StageCheckpoint:
        return cls(
            run_id=str(data["run_id"]),
            stage=str(data["stage"]),
            schema_version=int(data.get("schema_version", 1)),
            status=StageStatus(str(data.get("status", StageStatus.PENDING.value))),
            completed_ids=tuple(str(item) for item in data.get("completed_ids", []) or []),
            cursor=None if data.get("cursor") in (None, "") else str(data.get("cursor")),
            payload=dict(data.get("payload", {}) or {}),
            created_at=_parse_datetime(data.get("created_at")) or _utcnow(),
            updated_at=_parse_datetime(data.get("updated_at")) or _utcnow(),
        )

    @property
    def key(self) -> str:
        return checkpoint_key(self.run_id, self.stage, schema_version=self.schema_version)

    def with_progress(
        self,
        *,
        completed_ids: tuple[str, ...] | None = None,
        cursor: str | None = None,
        status: StageStatus | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> StageCheckpoint:
        return StageCheckpoint(
            run_id=self.run_id,
            stage=self.stage,
            schema_version=self.schema_version,
            status=status or self.status,
            completed_ids=completed_ids if completed_ids is not None else self.completed_ids,
            cursor=cursor if cursor is not None else self.cursor,
            payload=dict(payload) if payload is not None else dict(self.payload),
            created_at=self.created_at,
            updated_at=_utcnow(),
        )


@dataclass(slots=True, frozen=True)
class RunCheckpointManifest:
    """Summary of checkpointed stages for a single run."""

    run_id: str
    schema_version: int
    stages: tuple[StageCheckpoint, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def to_dict(self) -> dict[str, Any]:
        return to_json_ready(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RunCheckpointManifest:
        stages = tuple(StageCheckpoint.from_dict(item) for item in data.get("stages", []) or [])
        return cls(
            run_id=str(data["run_id"]),
            schema_version=int(data.get("schema_version", 1)),
            stages=stages,
            metadata=dict(data.get("metadata", {}) or {}),
            created_at=_parse_datetime(data.get("created_at")) or _utcnow(),
            updated_at=_parse_datetime(data.get("updated_at")) or _utcnow(),
        )

    def stage(self, name: str) -> StageCheckpoint | None:
        for stage in self.stages:
            if stage.stage == name:
                return stage
        return None
