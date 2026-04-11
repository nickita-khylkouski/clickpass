"""Cutline and disagreement selection keyed by stable applicant IDs."""

from __future__ import annotations

from typing import Any

from .combine import _min_max_normalize


def _pointwise_norm_map(pointwise_results: list[dict[str, Any]]) -> dict[str, float]:
    valid = [row for row in pointwise_results if row.get("score") is not None and row.get("applicant_id")]
    scores = [float(row["score"]) for row in valid]
    normalized = _min_max_normalize(scores)
    return {row["applicant_id"]: normalized[index] for index, row in enumerate(valid)}


def _swiss_norm_map(
    swiss_records: dict[str, dict[str, Any]],
    bt_strengths: dict[str, float] | None = None,
) -> dict[str, float]:
    ids = sorted(
        applicant_id
        for applicant_id, record in swiss_records.items()
        if (
            int(record.get("wins", 0)) + int(record.get("losses", 0)) + int(record.get("byes", 0)) > 0
            or (bt_strengths and applicant_id in bt_strengths and bt_strengths[applicant_id] is not None)
        )
    )
    if not ids:
        return {}
    values: list[float] = []
    for applicant_id in ids:
        if bt_strengths and applicant_id in bt_strengths and bt_strengths[applicant_id] is not None:
            values.append(float(bt_strengths[applicant_id]))
            continue
        wins = int(swiss_records[applicant_id].get("wins", 0))
        losses = int(swiss_records[applicant_id].get("losses", 0))
        total = wins + losses
        values.append(wins / total if total else 0.0)
    normalized = _min_max_normalize(values)
    return {applicant_id: normalized[index] for index, applicant_id in enumerate(ids)}


def identify_borderline_applicants(
    rankings: list[dict[str, Any]],
    *,
    cutline: int,
    band_pct: float = 0.15,
    pointwise_results: list[dict[str, Any]] | None = None,
    swiss_records: dict[str, dict[str, Any]] | None = None,
    bt_strengths: dict[str, float] | None = None,
    disagreement_threshold: float = 0.25,
) -> list[str]:
    """Return applicant IDs near the cutline or with signal disagreement."""
    if not rankings:
        return []

    total = len(rankings)
    band_size = max(1, int(total * band_pct))
    lower_rank = max(1, cutline - band_size)
    upper_rank = min(total, cutline + band_size)

    selected = {
        row["applicant_id"]
        for row in rankings
        if lower_rank <= int(row.get("rank", 0)) <= upper_rank and row.get("applicant_id")
    }

    if pointwise_results and swiss_records:
        pointwise_norm = _pointwise_norm_map(pointwise_results)
        swiss_norm = _swiss_norm_map(swiss_records, bt_strengths)
        for applicant_id in sorted(set(pointwise_norm) & set(swiss_norm)):
            if abs(pointwise_norm[applicant_id] - swiss_norm[applicant_id]) >= disagreement_threshold:
                selected.add(applicant_id)

    return sorted(selected)
