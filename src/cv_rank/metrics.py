"""
Metrics for measuring ranking quality, especially at the accept/reject boundary.

Provides tools for:
- Measuring rank stability near the cutline
- Detecting signal disagreement (hidden gem candidates)
- Measuring length-bias correlation
- Comparing pipeline runs A/B
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger("cv_rank.metrics")


def _kendall_tau(a: list[str], b: list[str]) -> float:
    """Compute Kendall tau-b correlation between two orderings of the same names.

    Returns a value in [-1, 1] where 1 = identical ordering, -1 = reversed.
    """
    common = set(a) & set(b)
    if len(common) < 2:
        return 1.0

    names = sorted(common)
    # Re-index to contiguous ranks over the common subset only.
    # Using indices from the full lists breaks when lists have partial overlap.
    common_a = [x for x in a if x in common]
    common_b = [x for x in b if x in common]
    rank_a = {name: i for i, name in enumerate(common_a)}
    rank_b = {name: i for i, name in enumerate(common_b)}

    concordant = 0
    discordant = 0
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            n1, n2 = names[i], names[j]
            diff_a = rank_a[n1] - rank_a[n2]
            diff_b = rank_b[n1] - rank_b[n2]
            if diff_a * diff_b > 0:
                concordant += 1
            elif diff_a * diff_b < 0:
                discordant += 1
            # ties are ignored

    n_pairs = concordant + discordant
    if n_pairs == 0:
        return 1.0
    return (concordant - discordant) / n_pairs


def _spearman_rho(a: list[str], b: list[str]) -> float:
    """Compute Spearman rank correlation between two orderings.

    Returns a value in [-1, 1] where 1 = identical ordering.
    """
    common = sorted(set(a) & set(b))
    if len(common) < 2:
        return 1.0

    # Re-index to contiguous ranks over the common subset only.
    common_a = [x for x in a if x in common]
    common_b = [x for x in b if x in common]
    rank_a = {name: i for i, name in enumerate(common_a)}
    rank_b = {name: i for i, name in enumerate(common_b)}

    n = len(common)
    d_sq_sum = sum((rank_a[name] - rank_b[name]) ** 2 for name in common)

    return 1.0 - (6 * d_sq_sum) / (n * (n * n - 1))


def boundary_accuracy(rankings: list[dict], cutline: int) -> dict[str, Any]:
    """Metrics for the accept/reject boundary zone.

    Parameters
    ----------
    rankings:
        List of ranking dicts sorted by rank, each with ``"name"`` and ``"rank"``.
    cutline:
        The rank number at which accept/reject is divided (e.g. 150 means
        ranks 1-150 are accepted).

    Returns
    -------
    dict with:
        - boundary_band: tuple (low, high) of the boundary zone
        - boundary_candidates: list of names in the boundary zone
        - boundary_count: number of candidates in the boundary zone
        - accept_count: number accepted
        - reject_count: number rejected
    """
    if not rankings:
        return {
            "boundary_band": (0, 0),
            "boundary_candidates": [],
            "boundary_count": 0,
            "accept_count": 0,
            "reject_count": 0,
        }

    n = len(rankings)
    band_size = max(1, int(n * 0.15))
    low = max(1, cutline - band_size)
    high = min(n, cutline + band_size)

    boundary_names = [
        r["name"] for r in rankings
        if low <= r.get("rank", 0) <= high
    ]

    logger.info(
        "Boundary accuracy: %d total, cutline=%d, band=%d-%d (%d candidates), accept=%d, reject=%d",
        n, cutline, low, high, len(boundary_names),
        sum(1 for r in rankings if r.get("rank", 0) <= cutline),
        sum(1 for r in rankings if r.get("rank", 0) > cutline),
    )

    return {
        "boundary_band": (low, high),
        "boundary_candidates": boundary_names,
        "boundary_count": len(boundary_names),
        "accept_count": sum(1 for r in rankings if r.get("rank", 0) <= cutline),
        "reject_count": sum(1 for r in rankings if r.get("rank", 0) > cutline),
    }


def signal_disagreement(
    pointwise_scores: list[dict],
    swiss_records: dict[str, dict],
    bt_strengths: dict[str, float] | None = None,
    threshold: float = 0.3,
) -> list[dict]:
    """Find candidates where pointwise and swiss signals disagree significantly.

    These are hidden gem candidates (strong in one signal, weak in the other)
    or false positives (one signal inflates them).

    Parameters
    ----------
    pointwise_scores:
        List of dicts with ``"name"`` and ``"score"`` keys.
    swiss_records:
        ``{name: {"wins": int, "losses": int}}`` from Swiss tournament.
    bt_strengths:
        Optional BT strengths. If provided, used instead of win rate.
    threshold:
        Minimum normalized disagreement to report (0.0-1.0).

    Returns
    -------
    list of dicts sorted by disagreement magnitude descending, each with:
        - name, pw_norm, swiss_norm, disagreement, direction
    """
    # Build pointwise normalized values
    pw_by_name = {s["name"]: s.get("score", 0) or 0 for s in pointwise_scores
                  if s.get("score") is not None}
    common = sorted(set(pw_by_name.keys()) & set(swiss_records.keys()))

    if len(common) < 2:
        return []

    # Get Swiss signal
    if bt_strengths:
        swiss_vals = {n: bt_strengths.get(n, 0.0) for n in common}
    else:
        swiss_vals = {}
        for n in common:
            rec = swiss_records[n]
            total = rec["wins"] + rec["losses"]
            swiss_vals[n] = rec["wins"] / max(total, 1)

    # Min-max normalize both signals
    pw_values = [pw_by_name[n] for n in common]
    sw_values = [swiss_vals[n] for n in common]

    pw_min, pw_max = min(pw_values), max(pw_values)
    sw_min, sw_max = min(sw_values), max(sw_values)

    pw_range = pw_max - pw_min if pw_max != pw_min else 1.0
    sw_range = sw_max - sw_min if sw_max != sw_min else 1.0

    results = []
    for name in common:
        pw_norm = (pw_by_name[name] - pw_min) / pw_range
        sw_norm = (swiss_vals[name] - sw_min) / sw_range
        disagreement = abs(pw_norm - sw_norm)

        if disagreement >= threshold:
            direction = "hidden_gem" if pw_norm < sw_norm else "false_positive"
            results.append({
                "name": name,
                "pw_norm": round(pw_norm, 4),
                "swiss_norm": round(sw_norm, 4),
                "disagreement": round(disagreement, 4),
                "direction": direction,
            })

    results.sort(key=lambda x: -x["disagreement"])

    hidden_gems = [r for r in results if r["direction"] == "hidden_gem"]
    false_positives = [r for r in results if r["direction"] == "false_positive"]
    logger.info(
        "Signal disagreement: %d common, threshold=%.2f → %d disagreements (%d hidden gems, %d false positives)",
        len(common), threshold, len(results), len(hidden_gems), len(false_positives),
    )
    for r in results[:5]:
        logger.info(
            "  %s: pw=%.2f, swiss=%.2f, disagreement=%.2f, %s",
            r["name"], r["pw_norm"], r["swiss_norm"], r["disagreement"], r["direction"],
        )

    return results


def length_bias_correlation(
    scores: list[dict],
    profile_lengths: dict[str, int],
) -> float:
    """Pearson r between profile token count and pointwise score.

    Closer to 0 = less bias. Pre-debiasing should show r > 0.3.

    Parameters
    ----------
    scores:
        List of dicts with ``"name"`` and ``"score"`` keys.
    profile_lengths:
        ``{name: approx_token_count}`` for each person.

    Returns
    -------
    float: Pearson correlation coefficient in [-1, 1].
    """
    pairs = []
    for s in scores:
        name = s.get("name")
        score = s.get("score")
        if name and score is not None and name in profile_lengths:
            pairs.append((profile_lengths[name], score))

    if len(pairs) < 3:
        logger.info("Length-bias correlation: too few pairs (%d), returning 0.0", len(pairs))
        return 0.0

    lengths = [p[0] for p in pairs]
    score_vals = [p[1] for p in pairs]

    mean_l = sum(lengths) / len(lengths)
    mean_s = sum(score_vals) / len(score_vals)

    cov = sum((l - mean_l) * (s - mean_s) for l, s in zip(lengths, score_vals))
    var_l = sum((l - mean_l) ** 2 for l in lengths)
    var_s = sum((s - mean_s) ** 2 for s in score_vals)

    denom = math.sqrt(var_l * var_s)
    if denom == 0:
        logger.info("Length-bias correlation: zero variance, returning 0.0")
        return 0.0

    r = cov / denom
    logger.info(
        "Length-bias correlation: r=%.4f (n=%d, mean_length=%.0f, mean_score=%.1f, min_len=%d, max_len=%d)",
        r, len(pairs), mean_l, mean_s, min(lengths), max(lengths),
    )
    if abs(r) > 0.3:
        logger.warning("Length-bias correlation r=%.4f exceeds 0.3 — significant length bias detected", r)

    return r


def compare_runs(
    rankings_a: list[dict],
    rankings_b: list[dict],
    cutline: int,
) -> dict[str, Any]:
    """Compare two pipeline runs.

    Parameters
    ----------
    rankings_a, rankings_b:
        Lists of ranking dicts, each with ``"name"`` and ``"rank"``.
    cutline:
        Accept/reject boundary rank.

    Returns
    -------
    dict with:
        - spearman_rho: rank correlation over all common names
        - boundary_band_tau: Kendall tau for boundary zone only
        - new_accepts: names accepted in B but rejected in A
        - new_rejects: names rejected in B but accepted in A
        - rank_changes: list of (name, rank_a, rank_b, delta) near cutline
        - total_common: number of people in both runs
    """
    if not rankings_a or not rankings_b:
        logger.info("compare_runs: one or both rankings empty, returning defaults")
        return {
            "spearman_rho": 1.0,
            "boundary_band_tau": 1.0,
            "new_accepts": [],
            "new_rejects": [],
            "rank_changes": [],
            "total_common": 0,
        }

    names_a = [r["name"] for r in rankings_a]
    names_b = [r["name"] for r in rankings_b]

    rho = _spearman_rho(names_a, names_b)

    # Build rank maps
    rank_a = {r["name"]: r["rank"] for r in rankings_a}
    rank_b = {r["name"]: r["rank"] for r in rankings_b}
    common = set(rank_a.keys()) & set(rank_b.keys())

    logger.info(
        "compare_runs: %d in A, %d in B, %d common, cutline=%d",
        len(rankings_a), len(rankings_b), len(common), cutline,
    )

    # Accepted/rejected sets
    accepted_a = {n for n in common if rank_a[n] <= cutline}
    accepted_b = {n for n in common if rank_b[n] <= cutline}

    new_accepts = sorted(accepted_b - accepted_a)
    new_rejects = sorted(accepted_a - accepted_b)

    # Boundary zone analysis
    n = max(len(rankings_a), len(rankings_b))
    band = max(1, int(n * 0.15))
    low = max(1, cutline - band)
    high = min(n, cutline + band)

    boundary_names_a = [r["name"] for r in rankings_a if low <= r["rank"] <= high]
    boundary_names_b = [r["name"] for r in rankings_b if low <= r["rank"] <= high]
    boundary_tau = _kendall_tau(boundary_names_a, boundary_names_b)

    # Rank changes near cutline
    rank_changes = []
    for name in common:
        ra = rank_a[name]
        rb = rank_b[name]
        if low <= ra <= high or low <= rb <= high:
            rank_changes.append({
                "name": name,
                "rank_a": ra,
                "rank_b": rb,
                "delta": rb - ra,
            })
    rank_changes.sort(key=lambda x: -abs(x["delta"]))

    logger.info(
        "compare_runs: spearman_rho=%.4f, boundary_tau=%.4f, band=%d-%d, "
        "new_accepts=%d, new_rejects=%d, rank_changes=%d",
        round(rho, 4), round(boundary_tau, 4), low, high,
        len(new_accepts), len(new_rejects), len(rank_changes),
    )
    if new_accepts:
        logger.info("  New accepts: %s", ", ".join(new_accepts[:10]))
    if new_rejects:
        logger.info("  New rejects: %s", ", ".join(new_rejects[:10]))
    for rc in rank_changes[:5]:
        logger.info(
            "  Rank change: %s moved %+d (A:#%d → B:#%d)",
            rc["name"], rc["delta"], rc["rank_a"], rc["rank_b"],
        )

    return {
        "spearman_rho": round(rho, 4),
        "boundary_band_tau": round(boundary_tau, 4),
        "new_accepts": new_accepts,
        "new_rejects": new_rejects,
        "rank_changes": rank_changes[:20],
        "total_common": len(common),
    }
