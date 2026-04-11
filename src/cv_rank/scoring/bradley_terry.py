"""
Bradley-Terry strength computation from pairwise comparison results.

Uses the choix library (optional dependency) for maximum-likelihood
estimation via the Iterative Luce Spectral Ranking (ILSR) algorithm.
Falls back to an empty dict when choix is not installed.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("cv_rank.scoring.bradley_terry")


def compute_bt_strengths(
    names: list[str],
    comparisons: list[tuple[int, int]],
    *,
    pointwise_prior: dict[str, float] | None = None,
    prior_strength: int = 2,
) -> dict[str, float]:
    """Compute Bradley-Terry strengths from pairwise comparisons.

    Parameters
    ----------
    names:
        Ordered list of participant names.  Indices into this list are
        used in *comparisons*.
    comparisons:
        List of ``(winner_idx, loser_idx)`` tuples referencing positions
        in *names*.
    pointwise_prior:
        Optional mapping of name -> pointwise score (0-100).  When provided,
        virtual comparisons against a reference player are injected so that
        BT has a prior that aligns with pointwise evidence.  Helps candidates
        with few real matches get more reliable BT estimates.
    prior_strength:
        How many virtual comparisons to inject per candidate (default 2).
        Higher = stronger prior (pointwise has more influence).
        Set to 0 to disable.

    Returns
    -------
    dict[str, float]
        Mapping of name -> normalised strength in [0, 1].
        Empty dict when choix is unavailable or there are no comparisons.
    """
    if not comparisons and not pointwise_prior:
        return {}

    try:
        import choix
    except ImportError:
        return {}

    n_items = len(names)
    augmented = list(comparisons)

    # Inject virtual comparisons from pointwise scores.
    # Creates a virtual "reference" player.  For each candidate, adds
    # K virtual matches where the win/loss ratio reflects their
    # pointwise score (e.g. score=80/100 → ~80% virtual wins).
    virtual_idx: int | None = None
    if pointwise_prior and prior_strength > 0:
        virtual_idx = n_items  # index for the virtual reference player
        n_items += 1
        injected = 0
        for i, name in enumerate(names):
            score = pointwise_prior.get(name)
            if score is None:
                continue
            # Clamp to [5, 95] to avoid degenerate all-win or all-loss
            score = max(5.0, min(95.0, score))
            # Convert score to virtual wins out of prior_strength matches
            # against the reference player (who sits at "average" strength)
            virtual_wins = round(score / 100.0 * prior_strength)
            virtual_losses = prior_strength - virtual_wins
            for _ in range(virtual_wins):
                augmented.append((i, virtual_idx))
            for _ in range(virtual_losses):
                augmented.append((virtual_idx, i))
            injected += 1

        logger.info(
            "BT pointwise prior: %d candidates, %d strength, %d virtual comparisons",
            injected, prior_strength, len(augmented) - len(comparisons),
        )

    if not augmented:
        return {}

    strengths = choix.ilsr_pairwise(n_items, augmented, alpha=0.01)

    # Remove virtual player from results
    real_strengths = strengths[:len(names)]

    # Min-max normalise to [0, 1]
    mn, mx = min(real_strengths), max(real_strengths)
    rng = mx - mn if mx != mn else 1.0
    return {names[i]: (real_strengths[i] - mn) / rng for i in range(len(names))}
