"""
Combine pointwise scores and Swiss tournament results into a final ranking.

Normalises both signals to [0, 1] via min-max and applies configurable
weights (default 50/50).  Supports Bradley-Terry strengths when available,
falling back to raw win counts.

Bug fix: uses ``is not None`` checks for weights instead of ``or`` to
avoid the falsy-zero bug where a weight of 0.0 would be replaced by the
default.
"""

from __future__ import annotations

import logging
import math
import statistics
from typing import Any

logger = logging.getLogger("cv_rank.scoring.combine")


def _extract_dimension_score(result: dict[str, Any], dim_name: str) -> float:
    """Best-effort extraction of a rubric dimension score for tie-breaking."""
    dims = result.get("dimensions", {})
    raw: Any = None
    if isinstance(dims, dict) and dim_name in dims:
        dim_data = dims[dim_name]
        if isinstance(dim_data, dict):
            raw = dim_data.get("score")
        else:
            raw = dim_data
    elif f"{dim_name}_score" in result:
        raw = result.get(f"{dim_name}_score")

    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _min_max_normalize(values: list[float]) -> list[float]:
    """Min-max normalise a list of floats to [0, 1].

    When all values are identical the result is a list of 0.0s (rather
    than dividing by zero).
    """
    if not values:
        return []
    mn = min(values)
    mx = max(values)
    rng = mx - mn if mx != mn else 1.0
    return [(v - mn) / rng for v in values]


def _entropy_weights(
    swiss_norm: list[float],
    pw_norm: list[float],
    min_weight: float = 0.20,
    max_weight: float = 0.80,
) -> tuple[float, float]:
    """Compute weights from binned Shannon entropy of each signal.

    Higher entropy = more spread = more information = higher weight.
    Clamped to [min_weight, max_weight] to prevent extreme weights.

    Parameters
    ----------
    swiss_norm:
        Normalized Swiss/BT values in [0, 1].
    pw_norm:
        Normalized pointwise values in [0, 1].
    min_weight:
        Minimum weight for either signal.
    max_weight:
        Maximum weight for either signal.

    Returns
    -------
    tuple of (swiss_weight, pointwise_weight) summing to 1.0.
    """
    def _binned_entropy(values: list[float], n_bins: int = 20) -> float:
        if not values:
            return 0.0
        # Build histogram
        counts = [0] * n_bins
        for v in values:
            bin_idx = min(int(v * n_bins), n_bins - 1)
            counts[bin_idx] += 1

        # Shannon entropy
        n = len(values)
        entropy = 0.0
        for c in counts:
            if c > 0:
                p = c / n
                entropy -= p * math.log2(p)
        return entropy

    h_swiss = _binned_entropy(swiss_norm)
    h_pw = _binned_entropy(pw_norm)

    total_h = h_swiss + h_pw
    if total_h == 0:
        return 0.5, 0.5

    # Weight proportional to entropy
    sw = h_swiss / total_h
    pw = h_pw / total_h

    # Clamp
    sw = max(min_weight, min(max_weight, sw))
    pw = 1.0 - sw

    return round(sw, 4), round(pw, 4)


def combine_rankings(
    scores: list[dict],
    swiss_records: dict[str, dict],
    bt_strengths: dict[str, float],
    swiss_weight: float | None = None,
    pointwise_weight: float | None = None,
    auto_weight: bool = False,
) -> list[dict]:
    """Combine pointwise scores with Swiss/BT results into a final ranking.

    Parameters
    ----------
    scores:
        List of pointwise scoring dicts.  Each must have ``"name"`` and
        ``"score"`` keys; may also have ``"strongest_signal"``,
        ``"concerns"``, ``"confidence"``.
    swiss_records:
        ``{name: {"wins": int, "losses": int, "byes": int}}`` from the
        Swiss tournament.
    bt_strengths:
        ``{name: float}`` Bradley-Terry strengths (empty dict if choix
        was unavailable).
    swiss_weight:
        Weight for the Swiss/BT signal.  Defaults to ``0.50``.
    pointwise_weight:
        Weight for the pointwise signal.  Defaults to ``0.50``.

    Returns
    -------
    list[dict]
        Combined ranking dicts sorted by ``final_score`` descending, each
        containing::

            {
                "name": str,
                "rank": int,
                "final_score": float,
                "pointwise_score": float,
                "swiss_wins": int,
                "swiss_losses": int,
                "bt_strength": float | None,
                "strongest_signal": str,
                "concerns": str,
                "confidence": str,
            }
    """
    # Fix falsy-zero bug: use ``is not None`` so a weight of 0.0 is honoured.
    sw = swiss_weight if swiss_weight is not None else 0.50
    pw = pointwise_weight if pointwise_weight is not None else 0.50

    if auto_weight:
        logger.info("Auto-weight enabled — manual weights (swiss=%.4f, pointwise=%.4f) will be overridden", sw, pw)
    else:
        logger.info("Combine weights: swiss_weight=%.4f, pointwise_weight=%.4f", sw, pw)

    # Exclude error results (score=None) — otherwise they'd rank last with
    # score=0, indistinguishable from a genuinely terrible candidate.
    pw_errors = [r for r in scores if r.get("score") is None or r.get("error")]
    if pw_errors:
        logger.warning(
            "%d people had scoring errors and are EXCLUDED from rankings: %s",
            len(pw_errors),
            ", ".join(f"{r.get('name', '?')} ({r.get('error', '?')})" for r in pw_errors[:10]),
        )
    pw_by_name: dict[str, dict] = {
        r["name"]: r for r in scores
        if r.get("score") is not None and not r.get("error")
    }

    # Detect name collisions: if two people share a name, dict comprehension
    # silently drops the first one.  Log it so we know rankings are wrong.
    if len(pw_by_name) < len([r for r in scores if r.get("score") is not None and not r.get("error")]):
        dupes = len(scores) - len(pw_errors) - len(pw_by_name)
        logger.warning(
            "NAME COLLISION: %d people lost due to duplicate names in pointwise scores. "
            "The pipeline uses name as a unique key — duplicate names cause data loss.",
            dupes,
        )

    # Only rank people present in both signals
    common_names = sorted(set(swiss_records.keys()) & set(pw_by_name.keys()))

    if not common_names:
        logger.warning("No common names between pointwise and Swiss -- returning empty ranking")
        return []

    only_swiss = set(swiss_records.keys()) - set(pw_by_name.keys())
    only_pw = set(pw_by_name.keys()) - set(swiss_records.keys())
    logger.info(
        "People counts: common=%d, only_swiss=%d, only_pointwise=%d",
        len(common_names), len(only_swiss), len(only_pw),
    )
    if only_swiss:
        logger.warning(
            "%d people in Swiss but not pointwise: %s",
            len(only_swiss),
            ", ".join(sorted(only_swiss)[:10]) + ("..." if len(only_swiss) > 10 else ""),
        )
    if only_pw:
        logger.warning(
            "%d people in pointwise but not Swiss: %s",
            len(only_pw),
            ", ".join(sorted(only_pw)[:10]) + ("..." if len(only_pw) > 10 else ""),
        )

    # Raw values for normalisation
    use_bt = bool(bt_strengths)
    if use_bt:
        logger.info("Using Bradley-Terry strengths for Swiss signal")
        swiss_raw = [
            bt_strengths.get(n, 0.0) if bt_strengths.get(n) is not None
            else swiss_records[n]["wins"] / max(swiss_records[n]["wins"] + swiss_records[n]["losses"], 1)
            for n in common_names
        ]
    else:
        logger.info("Falling back to win rate for Swiss signal (BT strengths unavailable)")
        swiss_raw = [
            swiss_records[n]["wins"] / max(swiss_records[n]["wins"] + swiss_records[n]["losses"], 1)
            for n in common_names
        ]
    pw_raw = [pw_by_name[n].get("score", 0) for n in common_names]

    # Log raw values for first 3 people
    for i in range(min(3, len(common_names))):
        logger.debug(
            "Raw values [%s]: swiss_raw=%.6f, pointwise_raw=%.4f",
            common_names[i], swiss_raw[i], pw_raw[i],
        )

    # min-max normalize: identical values → all zeros → that signal is ignored.
    swiss_norm = _min_max_normalize(swiss_raw)
    pw_norm = _min_max_normalize(pw_raw)

    if swiss_raw and min(swiss_raw) == max(swiss_raw):
        logger.warning(
            "All Swiss scores identical (%.4f) — ranking uses pointwise only",
            swiss_raw[0],
        )
    if pw_raw and min(pw_raw) == max(pw_raw):
        logger.warning(
            "All pointwise scores identical (%.1f) — ranking uses Swiss only",
            pw_raw[0],
        )

    # Log normalized values for first 3 people
    for i in range(min(3, len(common_names))):
        logger.debug(
            "Normalized [%s]: swiss_norm=%.6f, pointwise_norm=%.6f",
            common_names[i], swiss_norm[i], pw_norm[i],
        )

    # Entropy auto-weighting: override sw/pw if enabled
    if auto_weight:
        sw, pw = _entropy_weights(swiss_norm, pw_norm)
        logger.info("Auto-weights (entropy): swiss=%.4f, pointwise=%.4f", sw, pw)

    # Weighted combination
    combined: list[dict] = []
    for i, name in enumerate(common_names):
        final = sw * swiss_norm[i] + pw * pw_norm[i]
        rec = swiss_records[name]

        entry: dict[str, Any] = {
            "name": name,
            "final_score": round(final, 4),
            "pointwise_score": pw_by_name[name].get("score", 0),
            "builder_signal_score": _extract_dimension_score(pw_by_name[name], "builder_signal"),
            "technical_depth_score": _extract_dimension_score(pw_by_name[name], "technical_depth"),
            "swiss_wins": rec["wins"],
            "swiss_losses": rec["losses"],
            "bt_strength": round(bt_strengths[name], 6) if name in bt_strengths else None,
            "strongest_signal": pw_by_name[name].get("strongest_signal", ""),
            "concerns": pw_by_name[name].get("concerns", ""),
            "confidence": pw_by_name[name].get("confidence", ""),
            "why": pw_by_name[name].get("why", ""),
        }
        combined.append(entry)

    # Sort descending by final_score with builder-first tie-breaks.
    combined.sort(
        key=lambda x: (
            -x["final_score"],
            -x.get("builder_signal_score", 0.0),
            -x.get("technical_depth_score", 0.0),
            -x["pointwise_score"],
            -x["swiss_wins"],
            x["name"].lower(),
        )
    )
    for i, c in enumerate(combined):
        c["rank"] = i + 1

    # Log final combined scores
    logger.info("=== FINAL COMBINED RANKINGS (%d people) ===", len(combined))
    for c in combined:
        line = (
            "  #%d %s: final=%.4f (pw_score=%.2f, swiss=%dW-%dL, bt=%s)"
            % (
                c["rank"],
                c["name"],
                c["final_score"],
                c["pointwise_score"],
                c["swiss_wins"],
                c["swiss_losses"],
                "%.6f" % c["bt_strength"] if c["bt_strength"] is not None else "N/A",
            )
        )
        if c["rank"] <= 20:
            logger.info(line)
        else:
            logger.debug(line)

    # Log score distribution stats
    all_scores = [c["final_score"] for c in combined]
    if all_scores:
        mean_s = statistics.mean(all_scores)
        min_s = min(all_scores)
        max_s = max(all_scores)
        std_s = statistics.stdev(all_scores) if len(all_scores) > 1 else 0.0
        logger.info(
            "Score distribution: mean=%.4f, min=%.4f, max=%.4f, std=%.4f",
            mean_s, min_s, max_s, std_s,
        )

    return combined


def recombine_with_borderline(
    scores: list[dict],
    swiss_records: dict[str, dict],
    bt_strengths: dict[str, float],
    bl_records: dict[str, dict],
    bl_bt: dict[str, float],
    config: dict,
) -> list[dict]:
    """Merge borderline re-eval results into main ranking.

    For borderline candidates: blend BT strengths (40% original + 60% focused).
    Merge win records additively. Re-run combine_rankings().
    """
    # Merge Swiss records: add borderline wins/losses to originals
    merged_records = dict(swiss_records)
    for name, bl_rec in bl_records.items():
        if name in merged_records:
            merged_records[name] = {
                "wins": merged_records[name]["wins"] + bl_rec["wins"],
                "losses": merged_records[name]["losses"] + bl_rec["losses"],
                "byes": merged_records[name].get("byes", 0) + bl_rec.get("byes", 0),
            }
        else:
            merged_records[name] = bl_rec

    # Merge BT strengths: blend original and borderline
    merged_bt = dict(bt_strengths)
    for name, bl_strength in bl_bt.items():
        if name in merged_bt:
            merged_bt[name] = 0.4 * merged_bt[name] + 0.6 * bl_strength
        else:
            merged_bt[name] = bl_strength

    logger.info(
        "Borderline merge: %d merged records, %d merged BT strengths",
        len(merged_records), len(merged_bt),
    )

    # Re-combine with merged data
    return combine_rankings(
        scores, merged_records, merged_bt,
        swiss_weight=config["weights"]["swiss"],
        pointwise_weight=config["weights"]["pointwise"],
        auto_weight=config.get("weights", {}).get("auto", False),
    )
