from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import math
import re
from typing import Any, Iterable, Mapping, Sequence

DEFAULT_ALLOWED_DENOMINATOR_COHORTS: frozenset[str] = frozenset(
    {
        "all_applicants",
        "approved",
        "checked_in",
        "submitters",
        "placed",
        "top25",
        "top50",
        "top100",
        "internal_only",
    }
)

_RATIO_KEYWORDS = (
    "rate",
    "ratio",
    "share",
    "pct",
    "percent",
    "coverage",
    "fraction",
)

_HEADLINE_FIELDS = (
    "headline",
    "title",
    "claim",
    "claim_text",
    "text",
)


@dataclass(frozen=True)
class _Finding:
    check: str
    severity: str
    message: str
    row_index: int | None = None
    claim_index: int | None = None
    metric_name: str | None = None
    segment: str | None = None
    details: dict[str, Any] | None = None

    def as_dict(self, finding_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": finding_id,
            "check": self.check,
            "severity": self.severity,
            "message": self.message,
            "row_index": self.row_index,
            "claim_index": self.claim_index,
            "metric_name": self.metric_name,
            "segment": self.segment,
        }
        if self.details:
            payload["details"] = self.details
        return payload


def _coerce_mapping(item: Any) -> Mapping[str, Any]:
    if isinstance(item, Mapping):
        return item
    if hasattr(item, "__dict__"):
        return vars(item)
    return {}


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    s = s.replace(",", "")
    if s.endswith("%"):
        s = s[:-1].strip()
    try:
        out = float(s)
    except ValueError:
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _row_metric_name(row: Mapping[str, Any]) -> str:
    for k in ("metric_name", "metric", "name"):
        value = row.get(k)
        if value:
            return str(value)
    return "<unknown_metric>"


def _row_segment(row: Mapping[str, Any]) -> str:
    for k in ("segment", "cohort", "group"):
        value = row.get(k)
        if value:
            return str(value)
    return "all"


def _extract_value(row: Mapping[str, Any]) -> float | None:
    for k in ("value", "ratio", "share", "coverage_pct"):
        parsed = _safe_float(row.get(k))
        if parsed is not None:
            return parsed
    return None


def _extract_numerator_denominator(row: Mapping[str, Any]) -> tuple[float | None, float | None]:
    numerator = _safe_float(row.get("numerator"))
    denominator = _safe_float(row.get("denominator"))
    return numerator, denominator


def _is_ratio_like_metric(row: Mapping[str, Any], value: float | None) -> bool:
    metric_name = _row_metric_name(row).lower()
    if any(token in metric_name for token in _RATIO_KEYWORDS):
        return True

    raw_value = row.get("value")
    if isinstance(raw_value, str) and "%" in raw_value:
        return True

    # Conservative heuristic: only auto-treat decimal shares as ratios.
    if value is None:
        return False
    return 0.0 <= value <= 1.0


def _ratio_match(value: float, numerator: float, denominator: float, tolerance: float) -> tuple[bool, float, float]:
    ratio = numerator / denominator
    ratio_pct = ratio * 100.0
    as_percent = abs(value - ratio_pct)
    as_ratio = abs(value - ratio)
    best_delta = min(as_percent, as_ratio)
    return best_delta <= tolerance, ratio, ratio_pct


def _normalize_headline(text: str) -> str:
    lowered = text.strip().lower()
    lowered = re.sub(r"\s+", " ", lowered)
    lowered = re.sub(r"[^a-z0-9 ]+", "", lowered)
    return lowered.strip()


def _extract_headline(candidate: Mapping[str, Any] | str) -> str | None:
    if isinstance(candidate, str):
        out = candidate.strip()
        return out or None

    for key in _HEADLINE_FIELDS:
        value = candidate.get(key)
        if value and str(value).strip():
            return str(value).strip()
    return None


def _build_headline_duplicates(headlines: Sequence[Mapping[str, Any] | str]) -> dict[str, list[int]]:
    dupes: dict[str, list[int]] = defaultdict(list)
    for idx, raw in enumerate(headlines):
        text = _extract_headline(raw)
        if not text:
            continue
        normalized = _normalize_headline(text)
        if len(normalized) < 8:
            continue
        dupes[normalized].append(idx)
    return {k: v for k, v in dupes.items() if len(v) > 1}


def run_qa_checks(
    metric_rows: Sequence[Mapping[str, Any] | Any],
    *,
    headlines: Sequence[Mapping[str, Any] | str] | None = None,
    allowed_denominator_cohorts: Iterable[str] = DEFAULT_ALLOWED_DENOMINATOR_COHORTS,
    tiny_sample_denominator_threshold: int = 30,
    ratio_tolerance: float = 0.02,
) -> dict[str, Any]:
    """
    Run deterministic QA checks for sponsor automation rows.

    Returns a machine-readable report object designed for orchestration systems.
    """

    allowed_set = {str(v) for v in allowed_denominator_cohorts}
    rows: list[Mapping[str, Any]] = [_coerce_mapping(r) for r in metric_rows]
    findings: list[_Finding] = []

    invalid_cohort_count = 0
    ratio_checked = 0
    ratio_mismatch_count = 0
    tiny_sample_count = 0

    for i, row in enumerate(rows):
        metric_name = _row_metric_name(row)
        segment = _row_segment(row)

        cohort = str(row.get("denominator_cohort") or "").strip()
        if cohort and cohort not in allowed_set:
            invalid_cohort_count += 1
            findings.append(
                _Finding(
                    check="denominator_cohort_validity",
                    severity="error",
                    message="Invalid denominator cohort enum value.",
                    row_index=i,
                    metric_name=metric_name,
                    segment=segment,
                    details={
                        "denominator_cohort": cohort,
                        "allowed_denominator_cohorts": sorted(allowed_set),
                    },
                )
            )

        numerator, denominator = _extract_numerator_denominator(row)
        value = _extract_value(row)

        if (
            numerator is not None
            and denominator is not None
            and denominator > 0
            and value is not None
            and _is_ratio_like_metric(row, value)
        ):
            ratio_checked += 1
            matched, ratio, ratio_pct = _ratio_match(
                value=value,
                numerator=numerator,
                denominator=denominator,
                tolerance=ratio_tolerance,
            )
            if not matched:
                ratio_mismatch_count += 1
                findings.append(
                    _Finding(
                        check="ratio_recomputation",
                        severity="error",
                        message="Value does not match recomputed numerator/denominator ratio.",
                        row_index=i,
                        metric_name=metric_name,
                        segment=segment,
                        details={
                            "value": value,
                            "numerator": numerator,
                            "denominator": denominator,
                            "recomputed_ratio": ratio,
                            "recomputed_percent": ratio_pct,
                            "tolerance": ratio_tolerance,
                        },
                    )
                )

        if denominator is not None and denominator > 0 and denominator < tiny_sample_denominator_threshold:
            tiny_sample_count += 1
            findings.append(
                _Finding(
                    check="tiny_sample_claim_flags",
                    severity="warning",
                    message="Tiny sample detected; downstream claims should include caveat.",
                    row_index=i,
                    metric_name=metric_name,
                    segment=segment,
                    details={
                        "numerator": numerator,
                        "denominator": denominator,
                        "threshold": tiny_sample_denominator_threshold,
                    },
                )
            )

    duplicate_groups = _build_headline_duplicates(list(headlines or []))
    for normalized, indexes in duplicate_groups.items():
        findings.append(
            _Finding(
                check="duplicate_headline_detection",
                severity="warning",
                message="Duplicate headline text detected after normalization.",
                claim_index=indexes[0],
                details={
                    "normalized_headline": normalized,
                    "duplicate_indexes": indexes,
                },
            )
        )

    finding_dicts = [f.as_dict(f"F{idx + 1:04d}") for idx, f in enumerate(findings)]
    errors = sum(1 for f in findings if f.severity == "error")
    warnings = sum(1 for f in findings if f.severity == "warning")

    check_status = {
        "denominator_cohort_validity": "pass" if invalid_cohort_count == 0 else "fail",
        "ratio_recomputation": "pass" if ratio_mismatch_count == 0 else "fail",
        "duplicate_headline_detection": "pass" if not duplicate_groups else "warn",
        "tiny_sample_claim_flags": "pass" if tiny_sample_count == 0 else "warn",
    }

    return {
        "ok": errors == 0,
        "status": "pass" if errors == 0 else "fail",
        "summary": {
            "rows_audited": len(rows),
            "headline_candidates_audited": len(headlines or []),
            "findings": len(findings),
            "errors": errors,
            "warnings": warnings,
            "checks_run": 4,
        },
        "checks": {
            "denominator_cohort_validity": {
                "status": check_status["denominator_cohort_validity"],
                "invalid_count": invalid_cohort_count,
            },
            "ratio_recomputation": {
                "status": check_status["ratio_recomputation"],
                "rows_checked": ratio_checked,
                "mismatch_count": ratio_mismatch_count,
                "tolerance": ratio_tolerance,
            },
            "duplicate_headline_detection": {
                "status": check_status["duplicate_headline_detection"],
                "duplicate_group_count": len(duplicate_groups),
            },
            "tiny_sample_claim_flags": {
                "status": check_status["tiny_sample_claim_flags"],
                "flagged_count": tiny_sample_count,
                "threshold": tiny_sample_denominator_threshold,
            },
        },
        "findings": finding_dicts,
        "metadata": {
            "allowed_denominator_cohorts": sorted(allowed_set),
        },
    }


def build_qa_report(
    metric_rows: Sequence[Mapping[str, Any] | Any],
    *,
    headlines: Sequence[Mapping[str, Any] | str] | None = None,
    allowed_denominator_cohorts: Iterable[str] = DEFAULT_ALLOWED_DENOMINATOR_COHORTS,
    tiny_sample_denominator_threshold: int = 30,
    ratio_tolerance: float = 0.02,
) -> dict[str, Any]:
    """Alias for orchestrators that expect a builder-style API."""
    return run_qa_checks(
        metric_rows,
        headlines=headlines,
        allowed_denominator_cohorts=allowed_denominator_cohorts,
        tiny_sample_denominator_threshold=tiny_sample_denominator_threshold,
        ratio_tolerance=ratio_tolerance,
    )


def qa_report(
    metric_rows: Sequence[Mapping[str, Any] | Any],
    *,
    headlines: Sequence[Mapping[str, Any] | str] | None = None,
) -> dict[str, Any]:
    """Thin alias for compatibility with simple orchestrator call sites."""
    return run_qa_checks(metric_rows, headlines=headlines)


__all__ = [
    "DEFAULT_ALLOWED_DENOMINATOR_COHORTS",
    "run_qa_checks",
    "build_qa_report",
    "qa_report",
]
