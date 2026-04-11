"""
Profile-length debiasing for pointwise scores.

Longer profiles score higher because more text = more evidence for the LLM.
This systematically buries "hidden gems" — private engineers at top companies,
researchers with sparse LinkedIn profiles.

Evidence: AlpacaEval showed 0.94→0.98 Spearman rho with GLM length correction.
"""

from __future__ import annotations

import logging
import math
from typing import Callable

logger = logging.getLogger("cv_rank.debiasing")


def compute_profile_lengths(
    people: list[dict],
    format_profile_fn: Callable[[dict], str],
) -> dict[str, int]:
    """Return approximate token count for each person's formatted profile.

    Uses a simple heuristic: token count ~ word count * 1.3.
    This avoids needing a tokenizer dependency.

    Parameters
    ----------
    people:
        List of person dicts.
    format_profile_fn:
        Function that formats a person dict into profile text.

    Returns
    -------
    dict mapping name → approximate token count.
    """
    lengths: dict[str, int] = {}
    for p in people:
        name = p.get("name", "?")
        text = format_profile_fn(p)
        # Approximate tokens: split on whitespace, multiply by 1.3
        word_count = len(text.split())
        lengths[name] = int(word_count * 1.3)
        logger.debug("  Profile length: %s → %d words → ~%d tokens", name, word_count, lengths[name])

    if lengths:
        vals = list(lengths.values())
        logger.info(
            "Profile lengths: %d people, min=%d, max=%d, mean=%.0f, median=%d tokens",
            len(vals), min(vals), max(vals),
            sum(vals) / len(vals),
            sorted(vals)[len(vals) // 2],
        )
        # Flag outliers (>2x median or <0.5x median)
        median = sorted(vals)[len(vals) // 2]
        short = [(n, l) for n, l in lengths.items() if l < median * 0.5]
        long = [(n, l) for n, l in lengths.items() if l > median * 2]
        if short:
            logger.info(
                "  Short profiles (%d, <%.0f tokens): %s",
                len(short), median * 0.5,
                ", ".join(f"{n}({l})" for n, l in sorted(short, key=lambda x: x[1])[:5]),
            )
        if long:
            logger.info(
                "  Long profiles (%d, >%.0f tokens): %s",
                len(long), median * 2,
                ", ".join(f"{n}({l})" for n, l in sorted(long, key=lambda x: -x[1])[:5]),
            )

    return lengths


def debias_scores(
    scores: list[dict],
    profile_lengths: dict[str, int],
    strength: float = 0.5,
) -> list[dict]:
    """Residualize pointwise scores against profile length via linear regression.

    1. Fit: score = slope * length + intercept
    2. Log correlation r (diagnostic)
    3. If |r| < 0.1: skip (no meaningful bias detected)
    4. Adjust: score -= strength * (predicted - mean_predicted)
    5. Preserve original as score_raw

    Parameters
    ----------
    scores:
        List of pointwise score dicts with ``"name"`` and ``"score"`` keys.
    profile_lengths:
        ``{name: approx_token_count}`` from compute_profile_lengths.
    strength:
        How aggressively to debias. 0.0 = no change, 1.0 = full residualization.
        Default 0.5 is conservative.

    Returns
    -------
    list of score dicts with ``score`` adjusted and ``score_raw`` preserving original.
    """
    # Clamp strength to valid range
    if not (0.0 <= strength <= 1.0):
        logger.warning("Debiasing: strength=%.2f outside [0,1], clamping", strength)
        strength = max(0.0, min(1.0, strength))

    # Build paired data
    pairs: list[tuple[float, float, int]] = []  # (length, score, index)
    for i, s in enumerate(scores):
        name = s.get("name")
        score = s.get("score")
        if name and score is not None and name in profile_lengths:
            pairs.append((float(profile_lengths[name]), float(score), i))

    if len(pairs) < 5:
        logger.info("Debiasing: too few valid pairs (%d), skipping", len(pairs))
        return scores

    # Compute Pearson r
    lengths_arr = [p[0] for p in pairs]
    scores_arr = [p[1] for p in pairs]
    n = len(pairs)

    mean_l = sum(lengths_arr) / n
    mean_s = sum(scores_arr) / n

    cov = sum((l - mean_l) * (s - mean_s) for l, s in zip(lengths_arr, scores_arr))
    var_l = sum((l - mean_l) ** 2 for l in lengths_arr)
    var_s = sum((s - mean_s) ** 2 for s in scores_arr)

    denom = math.sqrt(var_l * var_s)
    r = cov / denom if denom > 0 else 0.0

    logger.info("Debiasing: length-score correlation r=%.4f (n=%d)", r, n)

    if abs(r) < 0.1:
        logger.info("Debiasing: |r| < 0.1, no meaningful bias detected — skipping")
        return scores

    # Linear regression: score = slope * length + intercept
    slope = cov / var_l if var_l > 0 else 0.0
    intercept = mean_s - slope * mean_l

    # Compute predicted values and their mean
    predicted = [slope * l + intercept for l in lengths_arr]
    mean_predicted = sum(predicted) / len(predicted)

    logger.info(
        "Debiasing: slope=%.6f, intercept=%.2f, strength=%.2f",
        slope, intercept, strength,
    )

    # Apply adjustment
    result = []
    adjustment_map: dict[int, float] = {}
    for (length, score, idx), pred in zip(pairs, predicted):
        adjustment = strength * (pred - mean_predicted)
        adjustment_map[idx] = adjustment

    for i, s in enumerate(scores):
        new_s = dict(s)
        if i in adjustment_map and s.get("score") is not None:
            new_s["score_raw"] = s["score"]
            new_s["score"] = round(s["score"] - adjustment_map[i], 2)
        result.append(new_s)

    # Log adjustment stats
    adjustments = list(adjustment_map.values())
    if adjustments:
        logger.info(
            "Debiasing applied: min_adj=%.2f, max_adj=%.2f, mean_adj=%.2f",
            min(adjustments), max(adjustments), sum(adjustments) / len(adjustments),
        )

    return result
