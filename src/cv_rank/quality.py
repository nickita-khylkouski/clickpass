"""
Quality check -- LLM-based final review of ranked candidates.

Merges the verdict system from pipeline_v2/06_quality_check.py with the
specific-why / best-number prompt from scripts/opus/better_whys.py.

Produces per-person verdicts:
    STRONG YES | YES | BORDERLINE | NO
along with a numbers-rich ``specific_why``, ``best_number``, ``company``,
``role``, ``standout_achievements``, and ``bs_check``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from cv_rank.utils import LLMRetryExhausted, extract_json, get_openai_client, llm_request

logger = logging.getLogger("cv_rank")


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def _build_system_prompt(config: dict) -> str:
    # Safe access: a KeyError here propagates through asyncio.gather uncaught
    # (the except block only catches JSONDecodeError and LLMRetryExhausted),
    # which would crash the entire quality phase.
    target = config.get("event", {}).get("target_accepts", 50)
    return f"""You are doing the final quality review for a builder-first AI hackathon / AI community event.
About {target} applicants will be invited.

For each person, provide a JSON verdict with these fields:

1. **verdict**: "STRONG YES", "YES", "BORDERLINE", or "NO"
   - STRONG YES: Top ~5%. Exceptional builder or technical operator. Major founder with live product/traction, prolific OSS builder, senior FAANG/unicorn leader with concrete shipped systems, major patents/publications, or repeated hackathon/build wins.
   - YES: Would clearly add value. Working engineer with real shipped projects, founder with a shipped product, hackathon builder with strong technical artifacts, active GitHub with non-trivial code, or meaningful production ownership.
   - NO: Would not add value. No real builder evidence, mostly vague ideas, empty profiles, tutorial-level work only, or generic prestige with nothing concrete shipped.
   - BORDERLINE: Use ONLY when genuinely torn. There is some real signal, but it is thin or ambiguous. BORDERLINE should be RARE (~15% of decisions). If you can make a case for YES or NO, DO IT.

   DECISION RULES:
   - Ranking data is provided as context, not a command. Use it as a weak prior, not as a substitute for judgment.
   - People ranked in the top {target} usually have meaningful evidence already, but you should still evaluate the actual profile.
   - People ranked below {target} can still be YES if the concrete evidence is strong.
   - This event is BUILDER-FIRST. Shipped work, technical artifacts, hackathon execution, OSS adoption, production ownership, and hard technical metrics matter more than titles or audience size.
   - TAKE ALL STATEMENTS AT FACE VALUE. If the profile says they built or shipped something, evaluate it as real.
   - Treat stated facts as true, but do not add unstated metrics, scope, ownership, or impact. A title is evidence of the title only.
   - Missing LinkedIn or GitHub should lower confidence, not automatically force a NO, if the stated work is otherwise strong.
   - Has a real technical job plus concrete shipped work or real technical artifacts → usually YES.
   - Real hackathon submissions, live products, high-signal repos, patents, papers, or meaningful production metrics are strong reasons to say YES.
   - Meaningful LinkedIn presence alone is NOT enough for YES if builder evidence is weak.
   - Only has a name, vague interest, or generic title with nothing concrete shipped → NO.
   - "Some coding experience" or "interested in AI" with nothing to show → NO.
   - FORCE YOURSELF TO DECIDE. Ask: "Based on the profile, is this person likely to contribute meaningfully in a fast-moving builder room?" If yes → YES. If no → NO. Use BORDERLINE only when the evidence is genuinely mixed.

2. **specific_why**: A SPECIFIC, numbers-rich one-liner (max 30 words).

3. **best_number**: The single most impressive number (e.g. "130K GitHub stars", "$5M raised"). Use "N/A" if nothing quantifiable.

4. **company**: Their current company (or "Unknown").

5. **role**: Their current role (or "Unknown").

6. **standout_achievements**: List of 1-2 real achievements (max 15 words each).

7. **bs_check**: "legit", "overclaimed", or "unclear"
   - Use "overclaimed" ONLY when the profile is internally contradictory or obviously self-inconsistent.
   - Do NOT use "overclaimed" merely because links are missing or public evidence is sparse.
   - When in doubt, use "legit" or "unclear", while still taking the stated data at face value.

RULES for specific_why:
1. MUST include SPECIFIC NUMBERS when available (revenue, stars, users, funding raised, team size)
2. MUST include SPECIFIC COMPANY/PROJECT NAMES (not "a startup" but "Taxo AI (YC S24)")
3. MUST include their ACTUAL ROLE (not "founder" but "Co-founder & CTO of Taxo AI")
4. PRIORITIZE BUILDER NUMBERS over vanity numbers. Prefer users, revenue, stars, commits, submissions, deployments, latency/accuracy gains, patents, throughput, or funds raised.
5. If they have GitHub stars, a strong shipped metric, or a notable hackathon/product metric, mention that before followers or years of experience.
6. Follower count or years of experience should be the best number ONLY if there is no stronger builder/product/technical metric.
7. MAX 30 words. Every word must earn its place.
8. If there's genuinely nothing impressive, say so honestly -- don't inflate.
9. Do NOT mention rank numbers or cutline position. Focus only on the person's qualifications and evidence.
10. Take all statements at face value. Write "built X" not "claims to have built X". Never use "claims", "alleges", or "purports".

GOOD specific_why examples:
- "Co-founder & CTO of Taxo AI (YC S24); raised $5M from General Catalyst and shipped a live AI tax workflow product used by enterprise customers."
- "Created Docusaurus and Tech Interview Handbook (130K+ GitHub stars); shipped developer tools used broadly across the ecosystem."
- "Built Zora marketplace to $39M GMV; now ships frontend platform at Perplexity; 2.3K GitHub stars across OSS work."

BAD specific_why examples (TOO VAGUE -- NEVER DO THIS):
- "Impressive builder with strong technical background" (says NOTHING)
- "Funded founder with deep AI expertise" (which company? how much?)
- "Experienced developer passionate about AI" (garbage)

IMPORTANT:
 - Large followings can help, but they are secondary to real builder evidence.
 - Senior FAANG engineers are STRONG even without GitHub when they describe concrete systems, ownership, or shipped impact.
 - Founders with real products/funding > founders with just a title.
 - Zero GitHub + Zero LinkedIn is a negative signal, but not an automatic NO if the stated builder evidence is otherwise strong.

Return ONLY valid JSON matching this schema:
{{"verdict": "...", "specific_why": "...", "best_number": "...", "company": "...", "role": "...", "standout_achievements": ["...", "..."], "bs_check": "legit|overclaimed|unclear"}}

CONTEXT: This person's ranking data is provided below their profile."""


def _build_user_prompt(
    person: dict,
    ranking: dict,
    config: dict,
    format_profile_fn: Callable[[dict], str],
) -> str:
    profile = format_profile_fn(person)
    final = ranking.get("final_score", "?")
    swiss_w = ranking.get("swiss_wins", "?")
    swiss_l = ranking.get("swiss_losses", "?")
    pw = ranking.get("pointwise_score", "?")
    why = ranking.get("why", "")
    concerns = ranking.get("concerns", "")

    return f"""{profile}

--- RANKING DATA ---
Final Score: {final}
Swiss: {swiss_w}W-{swiss_l}L
Pointwise Score: {pw}
Previous Assessment: {why}
Concerns: {concerns}

Evaluate this person. Return ONLY valid JSON:
{{"verdict": "STRONG YES"|"YES"|"BORDERLINE"|"NO", "specific_why": "<30 words max, include numbers and names when available>", "best_number": "<single most impressive number>", "company": "<current company>", "role": "<current role>", "standout_achievements": ["<achievement 1>", "<achievement 2>"], "bs_check": "legit|overclaimed|unclear"}}"""


# ---------------------------------------------------------------------------
# Single-person quality check
# ---------------------------------------------------------------------------

async def _qc_one(
    client: Any,
    person: dict,
    ranking: dict,
    system_prompt: str,
    config: dict,
    semaphore: asyncio.Semaphore,
    format_profile_fn: Callable[[dict], str],
    idx: int,
    total: int,
) -> dict:
    """Quality check a single person via LLM."""
    name = person.get("name", "?")
    model = config["models"]["quality_check"]
    max_retries = config.get("max_retries", 3)

    user_prompt = _build_user_prompt(person, ranking, config, format_profile_fn)

    rank_num = ranking.get("rank", "?")
    score_num = ranking.get("final_score", "?")
    logger.info(
        "[%d/%d] Checking: %s (rank #%s, score %s)",
        idx + 1, total, name, rank_num,
        f"{score_num:.1f}" if isinstance(score_num, (int, float)) else score_num,
    )

    try:
        async with semaphore:
            content, tokens = await llm_request(
                client,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                model=model,
                max_tokens=500,
                label=f"[{idx+1}/{total}] {name}",
                max_retries=max_retries,
                trace_context={
                    "phase": "quality",
                    "candidate_name": name,
                    "rank": ranking.get("rank", 0),
                    "final_score": ranking.get("final_score", 0),
                    "index": idx + 1,
                    "total_candidates": total,
                },
            )

        result = extract_json(content)
        result["name"] = name
        result["rank"] = ranking.get("rank", 0)
        result["final_score"] = ranking.get("final_score", 0)
        result["tokens_used"] = tokens

        verdict = result.get("verdict", "?")
        specific_why = result.get("specific_why", "")
        best_number = result.get("best_number", "")
        bs_check = result.get("bs_check", "?")

        rank_str = str(result.get("rank", "?"))
        print(f"  [{idx+1}/{total}] {verdict:12s} | #{rank_str:>3s} | {name}")
        logger.info(
            "[%d/%d] %s: verdict=%s, bs_check=%s, best_number='%s'",
            idx + 1, total, name, verdict, bs_check, best_number,
        )
        logger.debug(
            "[%d/%d] %s: specific_why='%s'",
            idx + 1, total, name, specific_why,
        )
        return result

    except (json.JSONDecodeError, LLMRetryExhausted) as e:
        err = str(e) if str(e) else type(e).__name__
        logger.warning("[%d/%d] %s: failed: %s", idx + 1, total, name, err[:200])
        return {"name": name, "error": err[:200], "verdict": "?"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def run_quality_check(
    people: list[dict],
    rankings: list[dict],
    config: dict,
    run_dir: Path,
    format_profile_fn: Callable[[dict], str],
) -> list[dict]:
    """Run LLM quality review on all candidates.

    Parameters
    ----------
    people:
        List of enriched person dicts (must contain ``"name"``).
    rankings:
        List of ranking dicts (must contain ``"name"``).  Typically the
        output of the combined scoring phase.
    config:
        Full cv-rank config dict (needs ``models.quality_check``,
        ``event.target_accepts``, ``concurrency.quality_check``, etc.).
    run_dir:
        Run directory for checkpoints.  Quality results are saved
        incrementally as ``quality.json``.
    format_profile_fn:
        Callable that takes a person dict and returns a formatted text
        profile string for the LLM prompt.

    Returns
    -------
    list[dict]
        One result dict per person, sorted by rank.
    """
    profiles_by_name: dict[str, dict] = {p.get("name", "?"): p for p in people}

    system_prompt = _build_system_prompt(config)

    # Resume: load already-checked people from incremental progress file
    checked: dict[str, dict] = {}
    progress_path = run_dir / "quality_progress.json"
    if progress_path.exists():
        try:
            existing = json.loads(progress_path.read_text())
            if isinstance(existing, list):
                for r in existing:
                    checked[r["name"]] = r
                logger.info("Resuming quality check: %d already checked", len(checked))
        except (json.JSONDecodeError, KeyError):
            logger.warning("Corrupt quality progress file -- starting fresh")

    # Determine who still needs checking
    to_check: list[tuple[dict, dict]] = []
    for r in rankings:
        name = r["name"]
        if name in checked:
            continue
        if name not in profiles_by_name:
            continue
        to_check.append((profiles_by_name[name], r))

    if not to_check:
        logger.info("All %d people already quality-checked", len(checked))
        return sorted(checked.values(), key=lambda x: x.get("rank", 999))

    client = get_openai_client()
    concurrency = config.get("concurrency", {}).get("quality_check", 15)
    semaphore = asyncio.Semaphore(concurrency)
    save_every = config.get("save_every", 50)

    logger.info(
        "Quality checking %d people with model=%s, concurrency=%d, batch_size=%d",
        len(to_check),
        config["models"]["quality_check"],
        concurrency,
        save_every,
    )

    t_start = time.time()
    total_tokens = 0

    # Process in batches, checkpointing after each
    for batch_start in range(0, len(to_check), save_every):
        batch = to_check[batch_start : batch_start + save_every]
        batch_num = batch_start // save_every + 1
        total_batches = (len(to_check) + save_every - 1) // save_every
        logger.info(
            "Batch %d/%d: processing %d people (cumulative %d/%d)",
            batch_num, total_batches, len(batch),
            min(batch_start + len(batch), len(to_check)), len(to_check),
        )

        tasks = [
            _qc_one(
                client,
                person,
                ranking,
                system_prompt,
                config,
                semaphore,
                format_profile_fn,
                batch_start + i,
                len(to_check),
            )
            for i, (person, ranking) in enumerate(batch)
        ]
        results = await asyncio.gather(*tasks)

        for r in results:
            total_tokens += r.get("tokens_used", 0)
            checked[r["name"]] = r

        # Incremental progress (raw write — phase completion is handled by CLI)
        progress_path.write_text(json.dumps(list(checked.values()), indent=2, default=str))

        # Log batch stats: verdict distribution so far
        so_far = Counter(r.get("verdict", "?") for r in checked.values())
        dist_str = ", ".join(
            f"{v}={so_far.get(v, 0)}"
            for v in ("STRONG YES", "YES", "BORDERLINE", "NO", "?")
            if so_far.get(v, 0) > 0
        )
        logger.info(
            "Batch %d/%d complete: %d checked so far | verdicts: %s",
            batch_num, total_batches, len(checked), dist_str,
        )

    # Close client to avoid event-loop-closed errors from httpx AsyncClient
    await client.close()

    elapsed = time.time() - t_start
    all_results = sorted(checked.values(), key=lambda x: x.get("rank", 999))

    # Log verdict summary table with counts and percentages
    total_count = len(all_results)
    verdicts = Counter(r.get("verdict", "?") for r in all_results)
    logger.info(
        "Quality check complete: %d people in %.0fs | %s tokens",
        total_count,
        elapsed,
        f"{total_tokens:,}",
    )
    logger.info("--- Verdict Summary ---")
    for v in ("STRONG YES", "YES", "BORDERLINE", "NO", "?"):
        count = verdicts.get(v, 0)
        if count:
            pct = count * 100 / total_count if total_count > 0 else 0
            logger.info("  %-12s: %3d  (%5.1f%%)", v, count, pct)

    return all_results
