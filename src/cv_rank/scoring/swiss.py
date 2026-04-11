"""
Swiss-system tournament with head-to-head LLM comparisons.

Runs N rounds of pairwise comparisons where people with similar win
records are matched against each other (Swiss pairing).  Produces
win/loss records and optionally Bradley-Terry strengths.

Per-round checkpointing allows resuming interrupted tournaments.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from pathlib import Path
from typing import Any, Callable

from cv_rank.scoring.bradley_terry import compute_bt_strengths
from cv_rank.utils import extract_json as _extract_json

logger = logging.getLogger("cv_rank.scoring.swiss")


def _rank_snapshot(
    records: dict[str, dict],
    accept_count: int,
    boundary_width: int,
) -> dict[str, object]:
    """Return ranking snapshot used for adaptive early-stop checks."""
    ranked = sorted(records.items(), key=lambda x: (-x[1]["wins"], x[1]["losses"]))
    names = [name for name, _ in ranked]
    rank_by_name = {name: idx + 1 for idx, name in enumerate(names)}
    top_accept = set(names[:max(accept_count, 0)])

    n = len(names)
    low = max(1, accept_count - boundary_width)
    high = min(n, accept_count + boundary_width)
    boundary = {name for name, rank in rank_by_name.items() if low <= rank <= high}
    return {
        "ranked": ranked,
        "top_accept": top_accept,
        "boundary": boundary,
        "rank_by_name": rank_by_name,
    }


def _accept_set_jaccard(a: set[str], b: set[str]) -> float:
    """Compute Jaccard similarity for two accept sets."""
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def _snapshot_to_checkpoint(snapshot: dict[str, object] | None) -> dict[str, object] | None:
    """Serialize adaptive stability snapshot into JSON-safe checkpoint data."""
    if not snapshot:
        return None
    rank_by_name_raw = snapshot.get("rank_by_name", {})
    rank_by_name: dict[str, int] = {}
    if isinstance(rank_by_name_raw, dict):
        for k, v in rank_by_name_raw.items():
            try:
                rank_by_name[str(k)] = int(v)
            except (TypeError, ValueError):
                continue
    return {
        "top_accept": sorted(str(x) for x in snapshot.get("top_accept", set())),
        "boundary": sorted(str(x) for x in snapshot.get("boundary", set())),
        "rank_by_name": rank_by_name,
    }


def _snapshot_from_checkpoint(data: object) -> dict[str, object] | None:
    """Restore adaptive stability snapshot from checkpoint data."""
    if not isinstance(data, dict):
        return None
    rank_raw = data.get("rank_by_name", {})
    if not isinstance(rank_raw, dict):
        return None

    rank_by_name: dict[str, int] = {}
    for k, v in rank_raw.items():
        try:
            rank_by_name[str(k)] = int(v)
        except (TypeError, ValueError):
            continue

    return {
        "top_accept": set(str(x) for x in data.get("top_accept", [])),
        "boundary": set(str(x) for x in data.get("boundary", [])),
        "rank_by_name": rank_by_name,
    }


def _max_boundary_rank_shift(
    prev_boundary: set[str],
    prev_rank: dict[str, int],
    cur_boundary: set[str],
    cur_rank: dict[str, int],
) -> int:
    """Maximum absolute rank shift across all boundary-window participants.

    Uses the union of previous/current boundary windows so a full turnover
    is treated as unstable (instead of looking stable due to empty
    intersection).
    """
    observed = prev_boundary | cur_boundary
    if not observed:
        return 0
    common_ranked = [name for name in observed if name in prev_rank and name in cur_rank]
    if not common_ranked:
        return 0
    return max(abs(prev_rank[name] - cur_rank[name]) for name in common_ranked)


# ---------------------------------------------------------------------------
# Swiss pairing
# ---------------------------------------------------------------------------

def swiss_pair(
    records: dict[str, dict],
    past_matchups: set[tuple[str, str]],
) -> tuple[list[tuple[str, str]], list[str]]:
    """Pair people with similar W-L records, avoiding repeat matchups.

    Parameters
    ----------
    records:
        ``{name: {"wins": int, "losses": int, "byes": int}}`` for every
        participant.
    past_matchups:
        Set of ``(name_a, name_b)`` tuples (sorted) of past pairings.

    Returns
    -------
    tuple[list[tuple[str, str]], list[str]]
        ``(matches, byes)`` where *matches* is a list of ``(name_a, name_b)``
        pairs and *byes* is the list of unpaired names (they get a free win).
    """
    players = sorted(
        records.keys(),
        key=lambda n: (-records[n]["wins"], records[n]["losses"]),
    )

    paired: set[str] = set()
    matches: list[tuple[str, str]] = []

    for i, player in enumerate(players):
        if player in paired:
            continue

        best: str | None = None
        best_repeat: str | None = None

        for j in range(i + 1, len(players)):
            opponent = players[j]
            if opponent in paired:
                continue
            key = tuple(sorted([player, opponent]))
            if key not in past_matchups:
                best = opponent
                break
            elif best_repeat is None:
                best_repeat = opponent

        opponent = best or best_repeat
        if opponent is not None:
            paired.add(player)
            paired.add(opponent)
            matches.append((player, opponent))

    byes = [p for p in players if p not in paired]
    return matches, byes


# ---------------------------------------------------------------------------
# Comparison prompt
# ---------------------------------------------------------------------------

def _build_comparison_prompt(accept_count: int) -> str:
    """Build the system prompt for pairwise comparison."""
    return f"""You are comparing two candidates for a builder-first AI hackathon / AI community event. \
About {accept_count} people will be invited.

Pick the BETTER candidate for this type of room.

ASSUME THE PROFILE DATA PROVIDED TO YOU IS TRUE.
- Do not hedge or discount claims just because links are missing.
- Missing LinkedIn or GitHub can lower confidence, but should not erase strong stated evidence.
- Treat stated facts as true, but do not add unstated metrics, scope, ownership, or impact. A title is evidence of the title only.

DECIDE IN THIS ORDER:
1. Builder signal: shipped products, hackathon submissions, deployed systems, original repos, concrete execution
2. Technical depth: hard engineering, research, systems, ML, infra, code quality, complexity
3. Hackathon fit: speed, hands-on building ability, prototype instinct, ability to contribute in a builder room
4. Product or research impact: users, revenue, stars, publications, patents, production metrics
5. Community value: network, content, speaking, audience -- only as a tiebreaker

IMPORTANT RULES:
- Concrete built artifacts beat vague prestige.
- Real hackathon or shipped-project evidence beats generic titles.
- A senior engineer or founder with concrete shipped impact can beat a candidate with more public GitHub activity.
- Large followings, polished bios, or prestige titles should NOT outrank clearly stronger builder evidence.
- Do NOT use vibe, charisma, dinner-table, or status heuristics.
- Prior assessments (if shown) are weak context only. Override them freely if the profiles point the other way.

You MUST pick one. No ties."""


# ---------------------------------------------------------------------------
# Single comparison
# ---------------------------------------------------------------------------

async def compare_one(
    client: Any,
    p1: dict,
    p2: dict,
    criteria: dict[str, float],
    model: str,
    format_profile_fn: Callable[[dict], str],
    *,
    config: dict | None = None,
    pointwise_context: dict[str, dict] | None = None,
) -> dict:
    """Run a single pairwise comparison between two people.

    Parameters
    ----------
    client:
        An ``openai.AsyncOpenAI`` instance.
    p1, p2:
        Person dicts.
    criteria:
        Criteria dict (reserved for future per-criteria weighting).
    model:
        Model identifier string.
    format_profile_fn:
        Callable that formats a person dict into a profile string.
    config:
        Optional full configuration dict.

    Returns
    -------
    dict
        ``{"name_a", "name_b", "winner", "why", "tokens", "swapped"}``.
        ``winner`` is ``None`` on error (with ``"error"`` key set).
    """
    if config is None:
        config = {}

    max_retries = config.get("max_retries", 5)
    accept_count = config.get("event", {}).get("target_accepts", 150)

    name_a = p1.get("name", "?")
    name_b = p2.get("name", "?")
    profile_a = format_profile_fn(p1)
    profile_b = format_profile_fn(p2)

    system_prompt = _build_comparison_prompt(accept_count)

    logger.debug("Matchup starting: %s vs %s", name_a, name_b)

    # Randomly assign positions to reduce position bias
    if random.random() < 0.5:
        first_name, second_name = name_a, name_b
        first_profile, second_profile = profile_a, profile_b
        swapped = False
        logger.debug("Position assignment: %s=ALPHA, %s=BETA (no swap)", name_a, name_b)
    else:
        first_name, second_name = name_b, name_a
        first_profile, second_profile = profile_b, profile_a
        swapped = True
        logger.debug("Position assignment: %s=ALPHA, %s=BETA (swapped)", name_b, name_a)

    # PRePair: inject pointwise context if available
    alpha_context = ""
    beta_context = ""
    if pointwise_context:
        pw_first = pointwise_context.get(first_name, {})
        if pw_first.get("score") is not None:
            alpha_context = (
                f"\nPRIOR ASSESSMENT (weak prior; override freely): "
                f"Summary: {pw_first.get('why', '?')}. "
                f"Strongest: {pw_first.get('strongest_signal', '?')}. "
                f"Concerns: {pw_first.get('concerns', 'none')}."
            )
        pw_second = pointwise_context.get(second_name, {})
        if pw_second.get("score") is not None:
            beta_context = (
                f"\nPRIOR ASSESSMENT (weak prior; override freely): "
                f"Summary: {pw_second.get('why', '?')}. "
                f"Strongest: {pw_second.get('strongest_signal', '?')}. "
                f"Concerns: {pw_second.get('concerns', 'none')}."
            )

    prompt = f"""=== CANDIDATE ALPHA ===
{first_profile}{alpha_context}

=== CANDIDATE BETA ===
{second_profile}{beta_context}

Who is the better candidate? Return JSON:
{{"winner": "ALPHA" or "BETA", "why": "<1-2 sentences with specific evidence focused on builder signal, technical depth, and hackathon fit>"}}"""

    from cv_rank.utils import LLMRetryExhausted, llm_request

    label = f"{name_a} vs {name_b}"
    try:
        content, tokens = await llm_request(
            client,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            model=model,
            max_tokens=700,
            label=label,
            max_retries=max_retries,
            trace_context={
                "phase": "swiss",
                "candidate_a": name_a,
                "candidate_b": name_b,
                "swapped": swapped,
                "accept_count": accept_count,
                "has_pointwise_context": bool(pointwise_context),
            },
        )

        result = _extract_json(content)
        w = result.get("winner", "").strip().upper()

        if w in ("ALPHA", "A"):
            winner = first_name
        elif w in ("BETA", "B"):
            winner = second_name
        else:
            logger.error("Unrecognized winner value '%s' for %s", w, label)
            return {
                "name_a": name_a, "name_b": name_b,
                "winner": None, "error": f"bad_winner: {w}",
            }

        why_text = result.get("why", "")
        logger.debug(
            "Winner: %s (reason: %s) [%s, tokens=%d]",
            winner, why_text[:120], label, tokens,
        )
        return {
            "name_a": name_a, "name_b": name_b,
            "winner": winner, "why": why_text,
            "tokens": tokens, "swapped": swapped,
        }

    except json.JSONDecodeError:
        logger.error("JSON parse failed for %s", label)
        return {"name_a": name_a, "name_b": name_b, "winner": None, "error": "json"}

    except LLMRetryExhausted as e:
        logger.error("All retries exhausted for %s: %s", label, e)
        return {"name_a": name_a, "name_b": name_b, "winner": None, "error": str(e)[:100]}


# ---------------------------------------------------------------------------
# Full Swiss tournament
# ---------------------------------------------------------------------------

async def run_swiss(
    people: list[dict],
    criteria: dict[str, float],
    model: str,
    rounds: int,
    accept_count: int,
    config: dict,
    run_dir: Path,
    format_profile_fn: Callable[[dict], str],
    pointwise_scores: list[dict] | None = None,
) -> tuple[dict[str, dict], dict[str, float], list[dict]]:
    """Run a Swiss-system tournament.

    Parameters
    ----------
    people:
        List of person dicts.
    criteria:
        Criteria dict (for future per-criteria weighting).
    model:
        Model identifier string.
    rounds:
        Number of Swiss rounds.
    accept_count:
        Target number of accepts (for the comparison prompt).
    config:
        Full configuration dict.
    run_dir:
        Directory for checkpoint and output files.
    format_profile_fn:
        Callable that formats a person dict into a profile string.

    Returns
    -------
    tuple[dict, dict, list]
        ``(records, bt_strengths, all_matches)`` where:
        - *records* maps name -> ``{"wins", "losses", "byes"}``
        - *bt_strengths* maps name -> float in [0, 1] (empty if choix unavailable)
        - *all_matches* is the raw list of match result dicts
    """
    from cv_rank.utils import get_openai_client

    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    checkpoint_path = run_dir / "swiss_checkpoint.json"

    # Pre-format profiles into a lookup for fast access
    profiles: dict[str, dict] = {}
    for p in people:
        name = p.get("name", "?")
        profiles[name] = p

    names = list(profiles.keys())

    # Initialise or resume
    records: dict[str, dict] = {}
    all_matches: list[dict] = []
    past_matchups: set[tuple[str, str]] = set()
    start_round = 1
    total_tokens = 0
    resumed_stability_hits = 0
    resumed_prev_snapshot: dict[str, object] | None = None

    early_stopped = False

    if checkpoint_path.exists():
        try:
            prog = json.loads(checkpoint_path.read_text())
            records = prog["records"]
            all_matches = prog["matches"]
            start_round = prog["completed_rounds"] + 1
            total_tokens = prog.get("total_tokens", 0)
            early_stopped = bool(prog.get("early_stopped", False))
            resumed_stability_hits = int(prog.get("stability_hits", 0) or 0)
            resumed_prev_snapshot = _snapshot_from_checkpoint(prog.get("prev_snapshot"))
            for m in all_matches:
                past_matchups.add(tuple(sorted([m["name_a"], m["name_b"]])))
            logger.info("Resuming from round %d (%d matches)", start_round, len(all_matches))
            if early_stopped:
                logger.info("Checkpoint indicates Swiss previously early-stopped at round %d", prog["completed_rounds"])
                rounds = min(rounds, prog["completed_rounds"])
        except (json.JSONDecodeError, KeyError):
            logger.warning("Corrupt checkpoint -- starting fresh")

    if not records:
        for name in names:
            records[name] = {"wins": 0, "losses": 0, "byes": 0}

    # Build pointwise context dict for PRePair injection
    pw_context: dict[str, dict] | None = None
    inject_pw = config.get("swiss", {}).get("inject_pointwise", True)
    if inject_pw and pointwise_scores:
        pw_context = {}
        for s in pointwise_scores:
            name = s.get("name")
            if name and s.get("score") is not None:
                pw_context[name] = {
                    "score": s["score"],
                    "strongest_signal": s.get("strongest_signal", ""),
                    "concerns": s.get("concerns", ""),
                }
        logger.info("PRePair: injecting pointwise context for %d candidates", len(pw_context))
    elif inject_pw:
        logger.info("PRePair: no pointwise scores available, skipping injection")

    # Client & concurrency
    client = get_openai_client()
    concurrency = config.get("concurrency", {}).get("swiss", 5)
    semaphore = asyncio.Semaphore(concurrency)
    rate_limit_until = 0.0  # shared timestamp for global backoff

    early_cfg = config.get("swiss", {}).get("early_stop", {})
    early_enabled = bool(early_cfg.get("enabled", True))
    min_rounds = max(1, int(early_cfg.get("min_rounds", 5)))
    jaccard_min = float(early_cfg.get("jaccard_accept_set_min", 0.98))
    max_boundary_shift_allowed = max(0, int(early_cfg.get("max_boundary_rank_shift", 2)))
    required_stable_rounds = max(1, int(early_cfg.get("consecutive_rounds", 2)))
    boundary_width = max(
        int(early_cfg.get("boundary_width_min", 20)),
        int(len(names) * float(early_cfg.get("boundary_width_pct", 0.10))),
    )

    stability_hits = resumed_stability_hits
    prev_snapshot: dict[str, object] | None = resumed_prev_snapshot
    completed_rounds = start_round - 1

    t_start = time.time()

    for rnd in range(start_round, rounds + 1):
        t_rnd = time.time()
        matches, byes = swiss_pair(records, past_matchups)
        logger.info("Round %d/%d: %d matches, %d byes", rnd, rounds, len(matches), len(byes))

        # Log all matchups for this round
        for idx, (a, b) in enumerate(matches, 1):
            a_wins = records[a]["wins"]
            b_wins = records[b]["wins"]
            logger.debug(
                "  Round %d matchup %d: %s (%dW) vs %s (%dW)",
                rnd, idx, a, a_wins, b, b_wins,
            )

        # Byes get a free win
        for name in byes:
            records[name]["byes"] += 1
            records[name]["wins"] += 1
            logger.info("  Bye (free win): %s (now %dW)", name, records[name]["wins"])

        async def _guarded_compare(a: str, b: str, idx: int = 0) -> dict:
            nonlocal rate_limit_until
            # Stagger requests to avoid thundering herd
            jitter = random.uniform(0, min(2.0, idx * 0.4))
            await asyncio.sleep(jitter)
            # If a global backoff is active, wait for it
            now = time.time()
            if rate_limit_until > now:
                await asyncio.sleep(rate_limit_until - now)
            async with semaphore:
                return await compare_one(
                    client,
                    profiles[a],
                    profiles[b],
                    criteria,
                    model,
                    format_profile_fn,
                    config=config,
                    pointwise_context=pw_context,
                )

        tasks = [_guarded_compare(a, b, i) for i, (a, b) in enumerate(matches)]
        results = await asyncio.gather(*tasks)

        rnd_tokens = 0
        errors = 0
        for r in results:
            all_matches.append(r)
            rnd_tokens += r.get("tokens", 0)
            w = r.get("winner")
            if w is None:
                errors += 1
                logger.warning(
                    "  Error in round %d matchup %s vs %s: %s",
                    rnd, r.get("name_a", "?"), r.get("name_b", "?"), r.get("error", "unknown"),
                )
                continue
            loser = r["name_b"] if w == r["name_a"] else r["name_a"]
            records[w]["wins"] += 1
            records[loser]["losses"] += 1
            past_matchups.add(tuple(sorted([r["name_a"], r["name_b"]])))

        total_tokens += rnd_tokens
        elapsed = time.time() - t_rnd

        snapshot = _rank_snapshot(records, accept_count, boundary_width)
        round_has_errors = errors > 0
        ranked = snapshot["ranked"]
        top3 = " | ".join(f"{n[:18]}({r['wins']}W)" for n, r in ranked[:3])
        logger.info(
            "Round %d: %.0fs | %d tok | err=%d | Top: %s",
            rnd, elapsed, rnd_tokens, errors, top3,
        )

        # Log full standings after each round (top 10 at INFO, rest at DEBUG)
        logger.info("  Standings after round %d:", rnd)
        for pos, (n, rec) in enumerate(ranked, 1):
            line = "    #%d %s: %dW-%dL-%dB" % (
                pos, n, rec["wins"], rec["losses"], rec["byes"],
            )
            if pos <= 10:
                logger.info(line)
            else:
                logger.debug(line)

        # Adaptive early-stop: stop once the accept set and boundary ranks are stable.
        if early_enabled and round_has_errors:
            logger.info(
                "Early-stop check skipped: round %d had %d comparison errors; resetting stability streak",
                rnd, errors,
            )
            stability_hits = 0
        elif early_enabled and rnd >= min_rounds and prev_snapshot is not None:
            accept_jaccard = _accept_set_jaccard(
                prev_snapshot["top_accept"], snapshot["top_accept"],
            )
            boundary_shift = _max_boundary_rank_shift(
                prev_snapshot["boundary"], prev_snapshot["rank_by_name"],
                snapshot["boundary"], snapshot["rank_by_name"],
            )
            is_stable = accept_jaccard >= jaccard_min and boundary_shift <= max_boundary_shift_allowed
            if is_stable:
                stability_hits += 1
            else:
                stability_hits = 0

            logger.info(
                "Early-stop check: jaccard=%.4f (>=%.2f), boundary_shift=%d (<=%d), stable=%s (%d/%d)",
                accept_jaccard,
                jaccard_min,
                boundary_shift,
                max_boundary_shift_allowed,
                is_stable,
                stability_hits,
                required_stable_rounds,
            )

            if stability_hits >= required_stable_rounds:
                early_stopped = True
                completed_rounds = rnd
                logger.info(
                    "Early-stop triggered at round %d/%d after %d consecutive stable checks",
                    rnd, rounds, required_stable_rounds,
                )
                # Save a final checkpoint with the early_stop marker before breaking.
                checkpoint_data = {
                    "completed_rounds": rnd,
                    "total_rounds": rounds,
                    "total_tokens": total_tokens,
                    "records": records,
                    "matches": all_matches,
                    "early_stopped": True,
                    "stability_hits": stability_hits,
                    "prev_snapshot": _snapshot_to_checkpoint(snapshot),
                }
                checkpoint_path.write_text(json.dumps(checkpoint_data, indent=2))
                break

        if not round_has_errors:
            prev_snapshot = snapshot
        completed_rounds = rnd

        # Save checkpoint
        checkpoint_data = {
            "completed_rounds": rnd,
            "total_rounds": rounds,
            "total_tokens": total_tokens,
            "records": records,
            "matches": all_matches,
            "early_stopped": False,
            "stability_hits": stability_hits,
            "prev_snapshot": _snapshot_to_checkpoint(prev_snapshot),
        }
        checkpoint_path.write_text(json.dumps(checkpoint_data, indent=2))

    # Explicitly close the client to avoid event-loop-closed errors on cleanup
    await client.close()

    total_elapsed = time.time() - t_start
    logger.info(
        "Swiss tournament complete: %d/%d rounds, %d matches in %.0fs (%d tokens)%s",
        completed_rounds,
        rounds,
        len(all_matches),
        total_elapsed,
        total_tokens,
        " [early-stop]" if early_stopped else "",
    )

    # Log final standings sorted by wins
    final_ranked = sorted(records.items(), key=lambda x: (-x[1]["wins"], x[1]["losses"]))
    logger.info("=== FINAL SWISS STANDINGS ===")
    for pos, (n, rec) in enumerate(final_ranked, 1):
        logger.info(
            "  #%d %s: %dW-%dL-%dB (win rate %.1f%%)",
            pos, n, rec["wins"], rec["losses"], rec["byes"],
            100.0 * rec["wins"] / max(rec["wins"] + rec["losses"], 1),
        )

    # Byes count as wins, inflating win-rate.  BT is immune (uses real matches
    # only) but the win-rate fallback ranks bye-heavy people too high.
    bye_heavy = [
        (n, rec) for n, rec in records.items()
        if rec["byes"] > 0 and rec["byes"] >= rec["wins"] // 2
    ]
    if bye_heavy:
        logger.warning(
            "BYE INFLATION: %d people have byes >= half their wins. "
            "Their win-rate is inflated. If BT is unavailable, rankings "
            "near the cutline may be unreliable: %s",
            len(bye_heavy),
            ", ".join(f"{n} ({rec['byes']}B/{rec['wins']}W)" for n, rec in bye_heavy[:5]),
        )

    # Bradley-Terry strengths
    use_bt = config.get("swiss", {}).get("use_bradley_terry", True)
    bt_strengths: dict[str, float] = {}

    if use_bt:
        logger.info("Computing Bradley-Terry strengths ...")
        name_to_idx = {n: i for i, n in enumerate(names)}
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

        prior_map: dict[str, float] | None = None
        if config.get("swiss", {}).get("bt_pointwise_prior", True) and pointwise_scores:
            prior_map = {
                s["name"]: float(s["score"])
                for s in pointwise_scores
                if s.get("name") and s.get("score") is not None
            }
            if prior_map:
                logger.info("BT prior: using pointwise priors for %d candidates", len(prior_map))
        prior_strength = int(config.get("swiss", {}).get("bt_prior_strength", 2))

        bt_strengths = compute_bt_strengths(
            names,
            comparisons,
            pointwise_prior=prior_map,
            prior_strength=prior_strength,
        )
        if bt_strengths:
            unique_bt = len(set(round(v, 6) for v in bt_strengths.values()))
            logger.info("BT: %d unique strength values", unique_bt)

            # Log BT strengths sorted descending
            bt_sorted = sorted(bt_strengths.items(), key=lambda x: -x[1])
            logger.info("=== BRADLEY-TERRY STRENGTHS ===")
            for pos, (n, strength) in enumerate(bt_sorted, 1):
                logger.info("  #%d %s: %.6f", pos, n, strength)
        else:
            logger.warning("BT computation returned empty strengths")
    else:
        logger.info("Bradley-Terry disabled by config")

    return records, bt_strengths, all_matches
