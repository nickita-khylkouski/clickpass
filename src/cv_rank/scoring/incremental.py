"""
Incremental Swiss integration for new candidates (v3: Research-Informed).

Optimized for what matters: the accept/reject boundary.  We don't need
precise ordering everywhere — rank #2 vs #100 matters less than #200 vs #201.

Improvements over v2 (informed by Chatbot Arena, TrueSkill, and BT research):
  - Phase A→B transition uses Phase A BT to identify borderline candidates
    (not just pointwise), catching candidates that pointwise misclassifies
  - Adaptive match budget: more cross-matches for candidates nearer the cutline
  - Information-maximizing opponent selection: pick opponents with similar
    estimated BT strength (Fisher info maximized when P(win) ≈ 0.5)
  - Pointwise-prior virtual matches in BT: gives candidates with few real
    matches a prior that aligns with their pointwise evidence

Three phases:
  Phase A — Mini Swiss among new candidates only (4 rounds)
    Establishes relative ordering among newcomers.

  Phase B — BT-informed borderline cross-matching
    Use Phase A BT (not just pointwise) to estimate where new candidates
    land in the combined ranking.  Only cross-match candidates near the
    accept/reject cutline.  Adaptive budget: 2-7 matches per candidate
    based on distance from cutline.

  Phase C — BT refit with pointwise prior
    Recompute Bradley-Terry on ALL comparisons with virtual matches from
    pointwise scores as a Bayesian-style prior.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable

from cv_rank.scoring.bradley_terry import compute_bt_strengths
from cv_rank.scoring.swiss import compare_one, run_swiss

logger = logging.getLogger("cv_rank.scoring.incremental")


def _seed_position(
    new_pw_score: float,
    existing_ranking: list[dict],
    existing_pw_map: dict[str, float] | None = None,
) -> int:
    """Find where a new candidate would slot in by pointwise score.

    Sorts existing_ranking by pointwise score (descending) before searching,
    since existing_ranking may be ordered by final combined rank instead.
    When existing_pw_map is provided, uses those scores (more authoritative)
    instead of the ranking dict's pointwise_score field.

    Returns an index into the sorted view (0 = top).
    """
    def _get_pw(r: dict) -> float:
        if existing_pw_map:
            return existing_pw_map.get(r.get("name", ""), r.get("pointwise_score", 0))
        return r.get("pointwise_score", 0)

    sorted_by_pw = sorted(existing_ranking, key=lambda r: -_get_pw(r))
    for i, r in enumerate(sorted_by_pw):
        if new_pw_score >= _get_pw(r):
            return i
    return len(sorted_by_pw)


def _pick_anchor(
    ranking: list[dict],
    target_rank: int,
    already_played: set[str],
    new_name: str,
) -> str | None:
    """Pick an anchor candidate near target_rank, avoiding repeats."""
    n = len(ranking)
    if n == 0:
        return None

    # Clamp
    target_rank = max(0, min(target_rank, n - 1))

    # Search outward from target
    for offset in range(n):
        for idx in (target_rank + offset, target_rank - offset):
            if 0 <= idx < n:
                name = ranking[idx]["name"]
                if name != new_name and name not in already_played:
                    return name
    return None


def _pick_anchor_by_strength(
    ranking: list[dict],
    existing_bt: dict[str, float],
    target_strength: float,
    already_played: set[str],
    new_name: str,
) -> str | None:
    """Pick an anchor whose BT strength is closest to target_strength.

    Fisher information is maximized when P(A beats B) ≈ 0.5,
    which happens when both have similar BT strength.
    """
    candidates = []
    for r in ranking:
        name = r["name"]
        if name == new_name or name in already_played:
            continue
        bt = existing_bt.get(name)
        if bt is not None:
            candidates.append((abs(bt - target_strength), name))

    if not candidates:
        return None

    # Sort by distance to target strength, pick closest
    candidates.sort()
    return candidates[0][1]


def _adaptive_match_budget(
    est_rank: int,
    accept_count: int,
    base_matches: int = 5,
) -> int:
    """Compute per-candidate cross-match budget based on distance from cutline.

    Candidates right at the cutline get more matches (up to 7).
    Candidates further away get fewer (down to 2).
    accept_count uses top-N semantics, so the 0-based cutline index is
    accept_count - 1.
    """
    cutline_idx = max(0, accept_count - 1)
    distance = abs(est_rank - cutline_idx)
    if distance < 10:
        return min(base_matches + 2, 7)
    elif distance < 25:
        return base_matches
    elif distance < 40:
        return max(base_matches - 2, 3)
    else:
        return 2


async def run_incremental_swiss(
    new_people: list[dict],
    existing_people: list[dict],
    existing_ranking: list[dict],
    existing_matches: list[dict],
    criteria: dict[str, float],
    model: str,
    config: dict,
    run_dir: Path,
    format_profile_fn: Callable[[dict], str],
    new_pointwise_scores: list[dict] | None = None,
    existing_pointwise_scores: list[dict] | None = None,
) -> tuple[dict[str, dict], dict[str, float], list[dict]]:
    """Run Hybrid Swiss-Merge to integrate new candidates (v3).

    Three phases:
      A) Mini Swiss among new candidates only
      B) BT-informed borderline cross-matching with adaptive budget
      C) BT refit with pointwise prior on all comparisons

    Parameters
    ----------
    new_people : list[dict]
        Person dicts for NEW candidates only.
    existing_people : list[dict]
        Person dicts for candidates from the previous run.
    existing_ranking : list[dict]
        Previous combined ranking (sorted by rank).
    existing_matches : list[dict]
        All match results from the previous Swiss run.
    criteria : dict
        Criteria weights.
    model : str
        LLM model for comparisons.
    config : dict
        Full config.
    run_dir : Path
        Output directory.
    format_profile_fn : callable
        Profile formatter.
    new_pointwise_scores : list[dict] | None
        Pointwise scores for new candidates.
    existing_pointwise_scores : list[dict] | None
        Pointwise scores for existing candidates (for PRePair context).

    Returns
    -------
    tuple[dict, dict, list]
        (records, bt_strengths, all_matches) covering ALL candidates.
    """
    from cv_rank.utils import get_openai_client

    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    all_people = {p.get("name", "?"): p for p in existing_people}
    all_people.update({p.get("name", "?"): p for p in new_people})

    new_names = {p.get("name", "?") for p in new_people}
    existing_names = {p.get("name", "?") for p in existing_people}

    # Build pointwise context for PRePair
    pw_context: dict[str, dict] | None = None
    all_pw = list(existing_pointwise_scores or []) + list(new_pointwise_scores or [])
    if all_pw:
        pw_context = {}
        for s in all_pw:
            name = s.get("name")
            if name and s.get("score") is not None:
                pw_context[name] = {
                    "score": s["score"],
                    "strongest_signal": s.get("strongest_signal", ""),
                    "concerns": s.get("concerns", ""),
                }

    # Build pointwise score lookups
    new_pw_map: dict[str, float] = {}
    if new_pointwise_scores:
        for s in new_pointwise_scores:
            if s.get("score") is not None:
                new_pw_map[s["name"]] = s["score"]

    existing_pw_map: dict[str, float] = {}
    if existing_pointwise_scores:
        for s in existing_pointwise_scores:
            if s.get("score") is not None:
                existing_pw_map[s["name"]] = s["score"]
    for r in existing_ranking:
        if r["name"] not in existing_pw_map:
            existing_pw_map[r["name"]] = r.get("pointwise_score", 0)

    # All pointwise scores for BT prior
    all_pw_scores: dict[str, float] = {}
    all_pw_scores.update(existing_pw_map)
    all_pw_scores.update(new_pw_map)

    # Sort existing ranking by rank
    existing_ranking = sorted(existing_ranking, key=lambda r: r.get("rank", 999))

    accept_count = config.get("event", {}).get("target_accepts", 200)
    n_total = len(existing_names) + len(new_names)

    # Config for phases
    inc_config = config.get("incremental", {})
    mini_swiss_rounds = inc_config.get("mini_swiss_rounds", 4)
    borderline_band_pct = inc_config.get("borderline_band_pct", 0.15)
    cross_matches_base = inc_config.get("cross_matches_base", 5)
    pw_prior_strength = inc_config.get("pointwise_prior_strength", 2)
    use_bt_borderline = inc_config.get("use_phase_a_bt_for_borderline", True)

    logger.info(
        "Incremental Swiss v3 (Research-Informed): "
        "%d new, %d existing, accept=%d, band=%.0f%%",
        len(new_names), len(existing_names),
        accept_count, borderline_band_pct * 100,
    )

    t_start = time.time()

    # ══════════════════════════════════════════════════════════════════
    # PHASE A: Mini Swiss among new candidates only
    # ══════════════════════════════════════════════════════════════════
    logger.info(
        "═══ Phase A: Mini Swiss among %d new candidates (%d rounds) ═══",
        len(new_people), mini_swiss_rounds,
    )

    t_phase_a = time.time()

    phase_a_records, phase_a_bt, phase_a_matches = await run_swiss(
        people=new_people,
        criteria=criteria,
        model=model,
        rounds=mini_swiss_rounds,
        accept_count=accept_count,
        config=config,
        run_dir=run_dir / "phase_a",
        format_profile_fn=format_profile_fn,
        pointwise_scores=new_pointwise_scores,
    )

    elapsed_a = time.time() - t_phase_a
    logger.info(
        "Phase A done: %d matches in %.0fs",
        len(phase_a_matches), elapsed_a,
    )

    # ══════════════════════════════════════════════════════════════════
    # PHASE B: BT-informed borderline cross-matching
    # ══════════════════════════════════════════════════════════════════
    # v3 improvement: use Phase A BT strengths (not just pointwise) to
    # estimate where new candidates land in the combined ranking.  This
    # catches candidates that pointwise misclassifies.

    n_existing = len(existing_ranking)
    band_size = max(10, int(n_total * borderline_band_pct))
    cutline_lo = max(0, accept_count - band_size)
    cutline_hi = min(n_total - 1, accept_count + band_size)

    logger.info(
        "═══ Phase B: BT-informed borderline cross-matching ═══\n"
        "  Cutline: rank %d | Band: ranks %d–%d (±%d)",
        accept_count, cutline_lo + 1, cutline_hi, band_size,
    )

    # Build existing BT strength lookup from previous ranking
    existing_bt: dict[str, float] = {}
    for r in existing_ranking:
        bt = r.get("bt_strength")
        if bt is not None:
            existing_bt[r["name"]] = bt

    # ── Estimate combined positions using BT (v3 improvement) ──
    # Merge new candidates (Phase A BT) and existing (previous BT) into
    # a unified estimated strength for better borderline detection.
    if use_bt_borderline and phase_a_bt and existing_bt:
        logger.info("  Using Phase A BT + existing BT for borderline detection")

        # Normalize Phase A BT to same scale as existing BT
        # Phase A BT is [0,1] within new candidates only.
        # Existing BT is [0,1] within existing candidates only.
        # Use pointwise scores as a bridge to align scales:
        # - Find the median pointwise of each group's BT range
        # - Scale Phase A BT so that similar pointwise → similar BT

        # Simpler approach: interleave by BT strength, treating both as
        # [0,1] normalized.  This isn't perfect but is much better than
        # pointwise alone since BT captures actual comparison outcomes.
        all_bt_entries = []
        for name in existing_names:
            bt = existing_bt.get(name, 0.5)
            all_bt_entries.append((name, bt, False))
        for name in new_names:
            bt = phase_a_bt.get(name, 0.5)
            all_bt_entries.append((name, bt, True))
        all_bt_entries.sort(key=lambda x: -x[1])

        est_rank: dict[str, int] = {}
        for rank_idx, (name, _bt, _is_new) in enumerate(all_bt_entries):
            est_rank[name] = rank_idx

        pw_borderline_count = 0  # for comparison logging
        # Count how many would be borderline with pointwise-only (for comparison)
        pw_entries = []
        for name in existing_names:
            pw_entries.append((name, existing_pw_map.get(name, 0)))
        for name in new_names:
            pw_entries.append((name, new_pw_map.get(name, 50.0)))
        pw_entries.sort(key=lambda x: -x[1])
        for rank_idx, (name, _) in enumerate(pw_entries):
            if name in new_names and cutline_lo <= rank_idx <= cutline_hi:
                pw_borderline_count += 1

    else:
        logger.info("  Falling back to pointwise-only borderline detection")
        # v2 fallback: pointwise-only estimation
        all_pw_entries = []
        for name in existing_names:
            all_pw_entries.append((name, existing_pw_map.get(name, 0), False))
        for name in new_names:
            all_pw_entries.append((name, new_pw_map.get(name, 50.0), True))
        all_pw_entries.sort(key=lambda x: -x[1])

        est_rank = {}
        for rank_idx, (name, _score, _is_new) in enumerate(all_pw_entries):
            est_rank[name] = rank_idx
        pw_borderline_count = None

    # Identify borderline new candidates
    borderline_new: list[str] = []
    non_borderline_new: list[str] = []
    for name in sorted(new_names):
        r = est_rank.get(name, n_total)
        if cutline_lo <= r <= cutline_hi:
            borderline_new.append(name)
        else:
            non_borderline_new.append(name)

    if pw_borderline_count is not None:
        logger.info(
            "  Borderline new (BT-informed): %d | Pointwise-only would be: %d | Clear: %d",
            len(borderline_new), pw_borderline_count, len(non_borderline_new),
        )
    else:
        logger.info(
            "  Borderline new: %d | Clear accept/reject: %d",
            len(borderline_new), len(non_borderline_new),
        )

    # Run targeted cross-matches for borderline candidates
    phase_b_matches: list[dict] = []

    if borderline_new and existing_ranking:
        client = get_openai_client()
        concurrency = config.get("concurrency", {}).get("swiss", 50)
        semaphore = asyncio.Semaphore(concurrency)

        async def _run_match(a: str, b: str) -> dict:
            async with semaphore:
                return await compare_one(
                    client, all_people[a], all_people[b],
                    criteria, model, format_profile_fn,
                    config=config,
                    pointwise_context=pw_context,
                )

        t_phase_b = time.time()

        # Plan cross-matches with adaptive budget and strength-based selection
        match_tasks: list[tuple[str, str]] = []
        budget_dist: dict[int, int] = {}  # budget → count

        # Map cutline band to existing-ranking space
        ratio = n_existing / max(n_total, 1)
        existing_cutline = int(accept_count * ratio)
        existing_band = max(5, int(band_size * ratio))
        elo = max(0, existing_cutline - existing_band)
        ehi = min(n_existing - 1, existing_cutline + existing_band)

        for new_name in borderline_new:
            played: set[str] = set()

            # v3: adaptive match budget based on distance from cutline
            candidate_est_rank = est_rank.get(new_name, n_total)
            budget = _adaptive_match_budget(
                candidate_est_rank, accept_count, cross_matches_base,
            )
            budget_dist[budget] = budget_dist.get(budget, 0) + 1

            # v3: information-maximizing opponent selection
            # Get candidate's estimated BT strength for proximity matching
            candidate_bt = phase_a_bt.get(new_name, 0.5)

            # Build target list using strength-based selection
            targets_added = 0

            # 1st: opponent with closest BT strength (max Fisher information)
            anchor = _pick_anchor_by_strength(
                existing_ranking, existing_bt, candidate_bt, played, new_name,
            )
            if anchor:
                played.add(anchor)
                match_tasks.append((new_name, anchor))
                targets_added += 1

            # 2nd: opponent at the cutline (calibration)
            if targets_added < budget:
                anchor = _pick_anchor(
                    existing_ranking, existing_cutline, played, new_name,
                )
                if anchor:
                    played.add(anchor)
                    match_tasks.append((new_name, anchor))
                    targets_added += 1

            # 3rd+: alternating above/below cutline, expanding outward
            spread = existing_band // (budget - 2) if budget > 2 else existing_band
            for k in range(1, budget):
                if targets_added >= budget:
                    break

                # Alternate above and below the candidate's estimated position
                seed_pos = _seed_position(
                    new_pw_map.get(new_name, 50.0), existing_ranking,
                    existing_pw_map,
                )
                if k % 2 == 1:
                    target = max(elo, seed_pos - k * spread)
                else:
                    target = min(ehi, seed_pos + k * spread)

                anchor = _pick_anchor(
                    existing_ranking, target, played, new_name,
                )
                if anchor:
                    played.add(anchor)
                    match_tasks.append((new_name, anchor))
                    targets_added += 1

        logger.info(
            "  Phase B: %d cross-matches planned (budget dist: %s)",
            len(match_tasks),
            ", ".join(f"{b}×{c}" for b, c in sorted(budget_dist.items())),
        )

        # Run all cross-matches in parallel
        if match_tasks:
            tasks = [_run_match(a, b) for a, b in match_tasks]
            results = await asyncio.gather(*tasks)
            phase_b_matches = list(results)

            total_tokens_b = sum(r.get("tokens", 0) for r in phase_b_matches)
            errors_b = sum(1 for r in phase_b_matches if r.get("winner") is None)
            elapsed_b = time.time() - t_phase_b

            logger.info(
                "Phase B done: %d matches in %.0fs (%d tokens, %d errors)",
                len(phase_b_matches), elapsed_b, total_tokens_b, errors_b,
            )

        await client.close()
    else:
        logger.info("  Phase B: skipped (no borderline candidates or no existing ranking)")

    # ══════════════════════════════════════════════════════════════════
    # PHASE C: BT refit with pointwise prior
    # ══════════════════════════════════════════════════════════════════
    logger.info("═══ Phase C: BT refit on all comparisons ═══")

    all_new_matches = phase_a_matches + phase_b_matches
    all_matches = list(existing_matches) + all_new_matches

    # Build unified records
    all_names = list(existing_names | new_names)
    records: dict[str, dict] = {
        name: {"wins": 0, "losses": 0, "byes": 0}
        for name in all_names
    }

    for m in all_matches:
        w = m.get("winner")
        if w is None:
            continue
        loser = m["name_b"] if w == m["name_a"] else m["name_a"]
        if w in records:
            records[w]["wins"] += 1
        if loser in records:
            records[loser]["losses"] += 1

    # Recompute Bradley-Terry with pointwise prior (v3 improvement)
    name_to_idx = {n: i for i, n in enumerate(all_names)}
    comparisons: list[tuple[int, int]] = []
    for m in all_matches:
        winner = m.get("winner")
        if winner is None:
            continue
        loser = m["name_b"] if winner == m["name_a"] else m["name_a"]
        w_idx = name_to_idx.get(winner)
        l_idx = name_to_idx.get(loser)
        if w_idx is not None and l_idx is not None:
            comparisons.append((w_idx, l_idx))

    logger.info(
        "BT refit: %d comparisons "
        "(%d old + %d phase_a + %d phase_b) over %d candidates",
        len(comparisons), len(existing_matches),
        len(phase_a_matches), len(phase_b_matches), len(all_names),
    )

    # v3: inject pointwise scores as BT prior via virtual matches
    pw_prior = all_pw_scores if pw_prior_strength > 0 else None

    bt_strengths = compute_bt_strengths(
        all_names, comparisons,
        pointwise_prior=pw_prior,
        prior_strength=pw_prior_strength,
    )

    if bt_strengths:
        logger.info(
            "BT: %d unique strength values (prior_strength=%d)",
            len(set(round(v, 6) for v in bt_strengths.values())),
            pw_prior_strength,
        )
    else:
        logger.warning("BT computation returned empty — falling back to win rates")

    elapsed_total = time.time() - t_start

    # ── Summary ───────────────────────────────────────────────────────
    logger.info("═══ INCREMENTAL v3 SUMMARY ═══")
    logger.info("  Phase A (mini Swiss):  %d matches", len(phase_a_matches))
    logger.info("  Phase B (cross-match): %d matches", len(phase_b_matches))
    logger.info("  Total new matches:     %d", len(all_new_matches))
    logger.info("  Borderline candidates: %d / %d new", len(borderline_new), len(new_names))
    logger.info("  Placed by score only:  %d", len(non_borderline_new))
    logger.info("  BT prior strength:     %d virtual matches", pw_prior_strength)
    logger.info("  Total elapsed:         %.0fs", elapsed_total)

    # Log new candidate results
    logger.info("=== NEW CANDIDATE RESULTS ===")
    for name in sorted(new_names):
        rec = records.get(name, {})
        bt = bt_strengths.get(name, 0.0)
        is_bl = "BL" if name in borderline_new else "  "
        total_matches = rec.get("wins", 0) + rec.get("losses", 0)
        logger.info(
            "  [%s] %s: %dW-%dL (%d matches, BT=%.6f)",
            is_bl, name,
            rec.get("wins", 0), rec.get("losses", 0),
            total_matches, bt,
        )

    return records, bt_strengths, all_matches
