"""Combine pointwise and Swiss signals using stable applicant IDs."""

from __future__ import annotations

import math
from typing import Any


def _min_max_normalize(values: list[float]) -> list[float]:
    if not values:
        return []
    minimum = min(values)
    maximum = max(values)
    spread = maximum - minimum or 1.0
    return [(value - minimum) / spread for value in values]


def _entropy_weights(
    swiss_norm: list[float],
    pointwise_norm: list[float],
    *,
    min_weight: float = 0.20,
    max_weight: float = 0.80,
    bins: int = 20,
) -> tuple[float, float]:
    """Compute entropy-based weights for the two normalized signals."""

    def _entropy(values: list[float]) -> float:
        if not values:
            return 0.0
        histogram = [0] * bins
        for value in values:
            index = min(int(value * bins), bins - 1)
            histogram[index] += 1
        total = len(values)
        entropy = 0.0
        for count in histogram:
            if count == 0:
                continue
            probability = count / total
            entropy -= probability * math.log2(probability)
        return entropy

    swiss_entropy = _entropy(swiss_norm)
    pointwise_entropy = _entropy(pointwise_norm)
    total_entropy = swiss_entropy + pointwise_entropy
    if total_entropy == 0:
        return 0.5, 0.5

    swiss_weight = swiss_entropy / total_entropy
    swiss_weight = max(min_weight, min(max_weight, swiss_weight))
    pointwise_weight = 1.0 - swiss_weight
    return round(swiss_weight, 4), round(pointwise_weight, 4)


def _signal_value(
    record: dict[str, Any],
    bt_strengths: dict[str, float],
    *,
    applicant_id: str | None = None,
) -> float:
    applicant_id = applicant_id or record.get("applicant_id")
    if applicant_id in bt_strengths and bt_strengths[applicant_id] is not None:
        return float(bt_strengths[applicant_id])
    wins = int(record.get("wins", 0))
    losses = int(record.get("losses", 0))
    total = wins + losses
    return wins / total if total else 0.0


def combine_rankings_with_metadata(
    pointwise_results: list[dict[str, Any]],
    swiss_records: dict[str, dict[str, Any]],
    bt_strengths: dict[str, float] | None = None,
    *,
    swiss_weight: float | None = None,
    pointwise_weight: float | None = None,
    auto_weight: bool = False,
    tie_breaker: str = "pointwise",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Combine ranking signals keyed by ``applicant_id`` and return metadata."""

    bt_strengths = bt_strengths or {}
    swiss_weight = 0.50 if swiss_weight is None else swiss_weight
    pointwise_weight = 0.50 if pointwise_weight is None else pointwise_weight

    pointwise_by_id = {
        row["applicant_id"]: row
        for row in pointwise_results
        if row.get("applicant_id") and row.get("score") is not None and not row.get("error")
    }

    common_ids = sorted(set(pointwise_by_id) & set(swiss_records))
    if not common_ids:
        return [], {
            "swiss_weight": swiss_weight,
            "pointwise_weight": pointwise_weight,
            "auto_weight": auto_weight,
            "tie_breaker": tie_breaker,
            "population": 0,
        }

    swiss_rows = [
        {
            "applicant_id": applicant_id,
            **swiss_records[applicant_id],
        }
        for applicant_id in common_ids
    ]
    swiss_raw = [
        _signal_value(row, bt_strengths, applicant_id=row["applicant_id"])
        for row in swiss_rows
    ]
    pointwise_raw = [float(pointwise_by_id[applicant_id]["score"]) for applicant_id in common_ids]

    swiss_norm = _min_max_normalize(swiss_raw)
    pointwise_norm = _min_max_normalize(pointwise_raw)
    if auto_weight:
        swiss_weight, pointwise_weight = _entropy_weights(swiss_norm, pointwise_norm)

    combined: list[dict[str, Any]] = []
    for index, applicant_id in enumerate(common_ids):
        pointwise = pointwise_by_id[applicant_id]
        swiss = swiss_records[applicant_id]
        swiss_signal = _signal_value(swiss, bt_strengths, applicant_id=applicant_id)
        swiss_evaluated = (
            int(swiss.get("wins", 0)) + int(swiss.get("losses", 0)) + int(swiss.get("byes", 0)) > 0
            or applicant_id in bt_strengths
        )
        final_score = swiss_weight * swiss_norm[index] + pointwise_weight * pointwise_norm[index]
        combined.append(
            {
                "applicant_id": applicant_id,
                "display_name": pointwise.get("display_name") or applicant_id,
                "rank": 0,
                "final_score": round(final_score, 6),
                "pointwise_score": float(pointwise["score"]),
                "swiss_wins": int(swiss.get("wins", 0)),
                "swiss_losses": int(swiss.get("losses", 0)),
                "swiss_byes": int(swiss.get("byes", 0)),
                "bt_strength": bt_strengths.get(applicant_id),
                "swiss_signal": round(swiss_signal, 6),
                "swiss_evaluated": swiss_evaluated,
                "confidence": pointwise.get("confidence", ""),
                "strongest_signal": pointwise.get("strongest_signal", ""),
                "concerns": pointwise.get("concerns", ""),
                "reasoning": pointwise.get("reasoning", ""),
            }
        )

    if tie_breaker == "swiss":
        combined.sort(
            key=lambda row: (
                -row["final_score"],
                -row["swiss_signal"],
                -row["swiss_wins"],
                -row["pointwise_score"],
                row["display_name"].lower(),
            )
        )
    else:
        combined.sort(
            key=lambda row: (
                -row["final_score"],
                -row["pointwise_score"],
                -row["swiss_signal"],
                -row["swiss_wins"],
                row["display_name"].lower(),
            )
        )
    for rank, row in enumerate(combined, start=1):
        row["rank"] = rank
    metadata = {
        "swiss_weight": swiss_weight,
        "pointwise_weight": pointwise_weight,
        "auto_weight": auto_weight,
        "tie_breaker": tie_breaker,
        "population": len(common_ids),
    }
    return combined, metadata


def combine_rankings(
    pointwise_results: list[dict[str, Any]],
    swiss_records: dict[str, dict[str, Any]],
    bt_strengths: dict[str, float] | None = None,
    *,
    swiss_weight: float | None = None,
    pointwise_weight: float | None = None,
    auto_weight: bool = False,
    tie_breaker: str = "pointwise",
) -> list[dict[str, Any]]:
    """Combine ranking signals keyed by ``applicant_id``."""

    combined, _ = combine_rankings_with_metadata(
        pointwise_results,
        swiss_records,
        bt_strengths,
        swiss_weight=swiss_weight,
        pointwise_weight=pointwise_weight,
        auto_weight=auto_weight,
        tie_breaker=tie_breaker,
    )
    return combined
