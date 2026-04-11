"""
Borderline re-evaluation: focused Swiss tournament on candidates near the cutline.

Spends extra comparison budget where uncertainty is highest — the accept/reject
boundary — rather than re-confirming that #1 is #1.

Evidence: Borderline allocation (Feb 2026) + information-theoretic argument:
spend more comparisons where uncertainty is highest.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("cv_rank.scoring.borderline")


def identify_borderline_candidates(
    rankings: list[dict],
    cutline: int,
    band_pct: float = 0.15,
    pointwise_scores: list[dict] | None = None,
    swiss_records: dict[str, dict] | None = None,
    bt_strengths: dict[str, float] | None = None,
    disagreement_threshold: float = 0.3,
) -> list[str]:
    """Select candidates within band_pct of cutline.

    Also includes signal-disagreement candidates (pointwise vs swiss disagree
    by >disagreement_threshold in normalized scores).

    Parameters
    ----------
    rankings:
        Full rankings list sorted by rank.
    cutline:
        Accept/reject boundary rank.
    band_pct:
        Fraction of total population to include on each side of cutline.
    pointwise_scores:
        Optional pointwise scores for disagreement detection.
    swiss_records:
        Optional Swiss records for disagreement detection.
    bt_strengths:
        Optional BT strengths for disagreement detection.
    disagreement_threshold:
        Min normalized disagreement to include candidates from outside band.

    Returns
    -------
    list of candidate names in the borderline zone.
    """
    if not rankings:
        return []

    n = len(rankings)
    band_size = max(1, int(n * band_pct))
    low = max(1, cutline - band_size)
    high = min(n, cutline + band_size)

    # Core boundary band
    borderline = {
        r["name"] for r in rankings
        if low <= r.get("rank", 0) <= high
    }

    logger.info(
        "Borderline band: ranks %d-%d (%d candidates, cutline=%d, band_pct=%.2f)",
        low, high, len(borderline), cutline, band_pct,
    )

    # Add signal-disagreement candidates from outside the band
    if pointwise_scores and swiss_records:
        from cv_rank.metrics import signal_disagreement
        disagreements = signal_disagreement(
            pointwise_scores, swiss_records, bt_strengths,
            threshold=disagreement_threshold,
        )
        for d in disagreements:
            if d["name"] not in borderline:
                borderline.add(d["name"])
                logger.info(
                    "  Added disagreement candidate: %s (pw=%.2f, sw=%.2f, %s)",
                    d["name"], d["pw_norm"], d["swiss_norm"], d["direction"],
                )

    return sorted(borderline)


async def run_borderline_reeval(
    people: list[dict],
    rankings: list[dict],
    criteria: dict[str, float],
    config: dict,
    run_dir: Path,
    format_profile_fn: Callable[[dict], str],
    pointwise_scores: list[dict] | None = None,
    swiss_records: dict[str, dict] | None = None,
    bt_strengths: dict[str, float] | None = None,
) -> tuple[dict[str, dict], dict[str, float], list[dict]]:
    """Run a mini Swiss tournament restricted to borderline candidates.

    Parameters
    ----------
    people:
        Full list of person dicts.
    rankings:
        Current combined rankings.
    criteria:
        Evaluation criteria.
    config:
        Full configuration dict.
    run_dir:
        Run directory for checkpointing.
    format_profile_fn:
        Profile formatting function (should be anonymized if enabled).
    pointwise_scores:
        Pointwise scores for PRePair context injection + disagreement detection.
    swiss_records:
        Swiss records for disagreement detection.
    bt_strengths:
        BT strengths for disagreement detection.

    Returns
    -------
    tuple of (records, bt_strengths, matches) for the borderline group.
    Empty dicts/list if borderline evaluation was skipped.
    """
    borderline_config = config.get("borderline", {})
    cutline = config.get("event", {}).get("target_accepts", 150)
    band_pct = borderline_config.get("band_pct", 0.15)
    extra_rounds = borderline_config.get("extra_rounds", 5)

    # Checkpoint
    checkpoint_path = Path(run_dir) / "borderline_checkpoint.json"
    if checkpoint_path.exists():
        try:
            data = json.loads(checkpoint_path.read_text())
            logger.info("Borderline: loaded from checkpoint (%d records)", len(data.get("records", {})))
            return data["records"], data.get("bt_strengths", {}), data.get("matches", [])
        except (json.JSONDecodeError, KeyError):
            logger.warning("Corrupt borderline checkpoint — re-running")

    # Identify borderline candidates
    borderline_names = identify_borderline_candidates(
        rankings, cutline, band_pct,
        pointwise_scores=pointwise_scores,
        swiss_records=swiss_records,
        bt_strengths=bt_strengths,
    )

    if len(borderline_names) < 4:
        logger.info("Borderline: too few candidates (%d), skipping re-evaluation", len(borderline_names))
        return {}, {}, []

    # Filter people to borderline subset
    name_set = set(borderline_names)
    borderline_people = [p for p in people if p.get("name") in name_set]

    logger.info(
        "Borderline re-evaluation: %d candidates, %d extra rounds",
        len(borderline_people), extra_rounds,
    )

    # Run mini Swiss tournament on the subset
    from cv_rank.scoring.swiss import run_swiss

    # Borderline re-eval uses its own model key (defaults to swiss model).
    # This is the ONE phase where accuracy matters most — it decides
    # accept/reject for people right at the cutline.
    models = config.get("models", {})
    model = models.get("borderline", models.get("swiss", "gpt-5-mini"))

    # Scale accept_count proportionally for the subset — telling the LLM
    # "150 people will be invited" when only 45 are being compared is misleading.
    borderline_accept = max(1, int(cutline * len(borderline_people) / max(len(rankings), 1)))

    t0 = time.time()
    records, bt_str, matches = await run_swiss(
        borderline_people,
        criteria,
        model,
        extra_rounds,
        borderline_accept,
        config,
        run_dir / "borderline",
        format_profile_fn,
        pointwise_scores=pointwise_scores,
    )
    elapsed = time.time() - t0

    total_tokens = sum(m.get("tokens", 0) for m in matches)
    logger.info(
        "Borderline re-evaluation complete: %d matches in %.1fs (%d tokens)",
        len(matches), elapsed, total_tokens,
    )

    # Save checkpoint
    checkpoint_data = {
        "records": records,
        "bt_strengths": bt_str,
        "matches": matches,
        "total_tokens": total_tokens,
    }
    checkpoint_path.write_text(json.dumps(checkpoint_data, indent=2, default=str))

    return records, bt_str, matches
