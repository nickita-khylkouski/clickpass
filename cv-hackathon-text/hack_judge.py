"""
hack_judge.py — Hackathon presentation judging from transcripts.

Adapted from cv-rank pipeline. Text-only, deep-thinking mode for small team counts.

Usage:
    # Score all teams
    python hack_judge.py

    # Score a single team (add incrementally)
    python hack_judge.py --team "Team Alpha"

    # Re-rank only (skip scoring)
    python hack_judge.py --rank-only

Setup:
    1. Put transcripts in teams/ folder: teams/team_name.txt
    2. Set OPENAI_API_KEY in .env
    3. Run: python hack_judge.py
"""

import asyncio
import itertools
import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("hack_judge")

# --- config ---

MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1")
MAX_TOKENS = 4096
RESULTS_DIR = Path("results")
TEAMS_DIR = Path("teams")

# hackathon judging criteria (from Browser Use hackathon)
CRITERIA = {
    "impact_potential": {
        "weight": 0.40,
        "description": "How well does the product deliver for its intended audience? Does it solve a real problem? Would people actually use this? Impact doesn't require something traditionally 'useful' — a delightful toy counts if it nails its audience.",
    },
    "creativity": {
        "weight": 0.20,
        "description": "How original and inventive is the idea? Does it combine concepts in a novel way? Is there a surprising insight or approach? Avoid rewarding complexity for its own sake.",
    },
    "technical_difficulty": {
        "weight": 0.20,
        "description": "How technically challenging is the implementation? Consider: browser automation complexity, model orchestration, data pipeline sophistication, integration depth, reliability of the agent. This is a web agents hackathon — good use of browser-use, MCP, agent SDKs counts.",
    },
    "demo_presentation": {
        "weight": 0.20,
        "description": "How clear, polished, and compelling is the demo? Can you understand what it does in 30 seconds? Does it work live? Is the presenter confident and concise? 4-minute format: ~3 min demo + ~1 min Q&A.",
    },
}

HACKATHON_CONTEXT = """This is the Browser Use Web Agents Hackathon at Y Combinator (Feb 28 - Mar 1, 2026).
Teams had ~20 hours to build web agent projects using browser-use (browser automation), LLMs, and sponsor tools.

Sponsors include: Browser Use, Google DeepMind, Anthropic, OpenAI, Convex, VibeFlow, HUD, Superset, Laminar, Vercel, Supermemory, MongoDB, Dedalus Labs, Daytona, AgentMail, Cubic, Minimax, An.

Prize tracks: Top 3 Overall, Founders Prize, Most Hardcore Infra, Best Devtool, Best Design, Best Use of Real-Time Data, Most Viral.

Key context: "The browser is where most digital interactions occur. Browsers can now get past anti-bot, agents can adjust to page changes, and models developed for browsing are cheaper, faster, and more accurate."

Suggested project types: Research Agents, Monitoring Systems, Legacy Industry Automation, Personal Productivity Agents, Outbound Engines."""


# --- LLM helpers (adapted from cv-rank/utils.py) ---

def get_client():
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ValueError("Set OPENAI_API_KEY in .env")
    return AsyncOpenAI(api_key=key, timeout=120.0)


def extract_json(text: str) -> dict:
    if text is None:
        raise ValueError("Model returned null content")
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", text.strip())
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", cleaned, re.DOTALL)
    if match:
        return json.loads(match.group())
    raise json.JSONDecodeError("No JSON object found", cleaned, 0)


async def llm_call(client, *, messages, label, max_tokens=MAX_TOKENS, temperature=0.2):
    """single LLM call with retry logic."""
    for attempt in range(5):
        try:
            req = {
                "model": MODEL,
                "max_completion_tokens": max_tokens,
                "messages": messages,
                "temperature": temperature,
                "response_format": {"type": "json_object"},
            }
            resp = await client.chat.completions.create(**req)
            content = resp.choices[0].message.content
            if not content or not content.strip():
                log.warning("%s: empty response (attempt %d)", label, attempt + 1)
                await asyncio.sleep(2)
                continue
            tokens = resp.usage.total_tokens if resp.usage else 0
            return content, tokens
        except Exception as e:
            err = str(e)
            if "429" in err or "rate" in err.lower():
                wait = min(60, 2 ** (attempt + 1))
                log.warning("%s: rate limited, waiting %ds", label, wait)
                await asyncio.sleep(wait)
            elif "402" in err:
                log.error("API quota exhausted!")
                raise
            else:
                log.warning("%s: error (attempt %d): %s", label, attempt + 1, err[:200])
                await asyncio.sleep(2)
    raise RuntimeError(f"{label}: all retries exhausted")


# --- Phase 1: Deep pointwise scoring ---

POINTWISE_SYSTEM = f"""You are an expert hackathon judge at a top-tier Y Combinator hackathon focused on web agents and browser automation.

{HACKATHON_CONTEXT}

You will receive a transcript of a team's presentation/demo. Analyze it deeply and score them.

SCORING CRITERIA (weights for final ranking):
{chr(10).join(f'- {name} ({v["weight"]:.0%}): {v["description"]}' for name, v in CRITERIA.items())}

INSTRUCTIONS:
1. First, think step-by-step about what the team built, how it works, and what's impressive or lacking.
2. Score each criterion from 1-10 with detailed justification.
3. Provide an overall assessment.
4. Consider: Is this a real product someone would use? Is the tech genuinely hard? Did they actually demo it working?
5. Be calibrated: a 7 is good, 8 is great, 9 is exceptional, 10 is "this could be a company."
6. Don't inflate scores. Most hackathon projects should land 5-7. Only truly exceptional work gets 8+.

Respond with JSON:
{{
    "team_name": "the team name",
    "what_they_built": "1-2 sentence summary of the project",
    "analysis": {{
        "impact_potential": {{
            "score": <1-10>,
            "reasoning": "detailed reasoning"
        }},
        "creativity": {{
            "score": <1-10>,
            "reasoning": "detailed reasoning"
        }},
        "technical_difficulty": {{
            "score": <1-10>,
            "reasoning": "detailed reasoning"
        }},
        "demo_presentation": {{
            "score": <1-10>,
            "reasoning": "detailed reasoning"
        }}
    }},
    "weighted_score": <float, computed from criteria weights>,
    "strengths": ["top 2-3 strengths"],
    "weaknesses": ["top 2-3 weaknesses"],
    "prize_tracks": ["which prize tracks this could win, if any"],
    "verdict": "STRONG YES | YES | MAYBE | NO — would this make finals?"
}}"""


async def score_team(client, team_name: str, transcript: str) -> dict:
    """deep score a single team's presentation."""
    log.info("Scoring: %s (%d chars)", team_name, len(transcript))
    messages = [
        {"role": "system", "content": POINTWISE_SYSTEM},
        {"role": "user", "content": f"Team: {team_name}\n\nTranscript of their presentation:\n\n{transcript}"},
    ]
    content, tokens = await llm_call(client, messages=messages, label=f"score:{team_name}")
    result = extract_json(content)
    # compute weighted score if not provided
    if "weighted_score" not in result or not result["weighted_score"]:
        ws = 0
        for crit, meta in CRITERIA.items():
            s = result.get("analysis", {}).get(crit, {}).get("score", 5)
            ws += s * meta["weight"]
        result["weighted_score"] = round(ws, 2)
    log.info("  %s → %.1f (tokens: %d)", team_name, result["weighted_score"], tokens)
    return result


# --- Phase 2: Pairwise comparisons (all-pairs for ≤12 teams) ---

PAIRWISE_SYSTEM = f"""You are comparing two hackathon presentations head-to-head.

{HACKATHON_CONTEXT}

CRITERIA:
{chr(10).join(f'- {name} ({v["weight"]:.0%}): {v["description"]}' for name, v in CRITERIA.items())}

You will see two team transcripts. Compare them directly.

Think deeply about which team built a more impressive project overall. Consider all four criteria but weigh Impact Potential most heavily (40%).

You MUST pick a winner. No ties.

Respond with JSON:
{{
    "winner": "ALPHA" or "BETA",
    "confidence": "high" | "medium" | "low",
    "reasoning": "2-3 sentence comparison explaining why the winner is better"
}}"""


async def compare_pair(client, team_a: str, text_a: str, team_b: str, text_b: str) -> dict:
    """compare two teams head-to-head. randomly assigns ALPHA/BETA to reduce position bias."""
    # randomly swap to reduce position bias
    if random.random() < 0.5:
        alpha_name, alpha_text = team_a, text_a
        beta_name, beta_text = team_b, text_b
        swapped = False
    else:
        alpha_name, alpha_text = team_b, text_b
        beta_name, beta_text = team_a, text_a
        swapped = True

    user_msg = f"""=== TEAM ALPHA ===
{alpha_text[:8000]}

=== TEAM BETA ===
{beta_text[:8000]}"""

    messages = [
        {"role": "system", "content": PAIRWISE_SYSTEM},
        {"role": "user", "content": user_msg},
    ]
    label = f"vs:{team_a[:15]}|{team_b[:15]}"
    content, tokens = await llm_call(client, messages=messages, label=label)
    result = extract_json(content)

    # resolve ALPHA/BETA back to actual team names
    winner_label = result.get("winner", "ALPHA").upper()
    if winner_label == "ALPHA":
        actual_winner = alpha_name
    else:
        actual_winner = beta_name

    log.info("  %s vs %s → %s (%s confidence, %d tokens)",
             team_a, team_b, actual_winner, result.get("confidence", "?"), tokens)

    return {
        "team_a": team_a,
        "team_b": team_b,
        "winner": actual_winner,
        "loser": team_b if actual_winner == team_a else team_a,
        "confidence": result.get("confidence", "medium"),
        "reasoning": result.get("reasoning", ""),
    }


async def all_pairs_tournament(client, teams: dict[str, str]) -> list[dict]:
    """run all pairwise comparisons. for 8 teams = 28 comparisons."""
    names = list(teams.keys())
    pairs = list(itertools.combinations(names, 2))
    log.info("Running %d pairwise comparisons...", len(pairs))

    # run in batches of 4 to respect rate limits
    results = []
    batch_size = 4
    for i in range(0, len(pairs), batch_size):
        batch = pairs[i:i + batch_size]
        coros = [
            compare_pair(client, a, teams[a], b, teams[b])
            for a, b in batch
        ]
        batch_results = await asyncio.gather(*coros, return_exceptions=True)
        for r in batch_results:
            if isinstance(r, Exception):
                log.error("Pairwise error: %s", r)
            else:
                results.append(r)
        if i + batch_size < len(pairs):
            await asyncio.sleep(1)  # brief pause between batches

    return results


def compute_pairwise_rankings(comparisons: list[dict], team_names: list[str]) -> dict:
    """compute win rates and rankings from pairwise results."""
    wins = {t: 0 for t in team_names}
    losses = {t: 0 for t in team_names}
    total = {t: 0 for t in team_names}

    for c in comparisons:
        winner = c["winner"]
        loser = c["loser"]
        if winner in wins:
            wins[winner] += 1
            total[winner] += 1
        if loser in losses:
            losses[loser] += 1
            total[loser] += 1

    rankings = {}
    for t in team_names:
        n = total.get(t, 0)
        w = wins.get(t, 0)
        rankings[t] = {
            "wins": w,
            "losses": losses.get(t, 0),
            "win_rate": w / n if n > 0 else 0,
            "total_matches": n,
        }

    return rankings


# --- Phase 3: Combine and rank ---

def combine_rankings(pointwise: dict, pairwise: dict, team_names: list[str]) -> list[dict]:
    """combine pointwise scores + pairwise win rates into final ranking."""
    # normalize pointwise scores to 0-1
    pw_scores = {t: pointwise[t]["weighted_score"] for t in team_names}
    pw_min = min(pw_scores.values()) if pw_scores else 0
    pw_max = max(pw_scores.values()) if pw_scores else 1
    pw_range = pw_max - pw_min if pw_max != pw_min else 1

    results = []
    for t in team_names:
        pw_norm = (pw_scores[t] - pw_min) / pw_range
        pair_wr = pairwise.get(t, {}).get("win_rate", 0.5)

        # weighted combine: 50% pointwise, 50% pairwise
        final = 0.5 * pw_norm + 0.5 * pair_wr
        results.append({
            "team": t,
            "final_score": round(final, 4),
            "pointwise_score": pw_scores[t],
            "pointwise_norm": round(pw_norm, 4),
            "pairwise_win_rate": round(pair_wr, 4),
            "wins": pairwise.get(t, {}).get("wins", 0),
            "losses": pairwise.get(t, {}).get("losses", 0),
            "what_they_built": pointwise[t].get("what_they_built", ""),
            "verdict": pointwise[t].get("verdict", ""),
            "strengths": pointwise[t].get("strengths", []),
            "weaknesses": pointwise[t].get("weaknesses", []),
            "prize_tracks": pointwise[t].get("prize_tracks", []),
            "analysis": pointwise[t].get("analysis", {}),
        })

    results.sort(key=lambda x: x["final_score"], reverse=True)

    # assign ranks
    for i, r in enumerate(results):
        r["rank"] = i + 1

    return results


# --- Main ---

async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Hackathon presentation judge")
    parser.add_argument("--team", help="Score a single team only")
    parser.add_argument("--rank-only", action="store_true", help="Re-rank from existing scores")
    parser.add_argument("--skip-pairwise", action="store_true", help="Skip pairwise comparisons")
    args = parser.parse_args()

    # load transcripts
    if not TEAMS_DIR.exists():
        log.error("No teams/ directory. Create it and add transcript files.")
        sys.exit(1)

    teams = {}
    for f in sorted(TEAMS_DIR.glob("*.txt")):
        name = f.stem.replace("_", " ").replace("-", " ").title()
        text = f.read_text(encoding="utf-8").strip()
        if text:
            teams[name] = text
            log.info("Loaded: %s (%d chars)", name, len(text))

    if not teams:
        log.error("No .txt files found in teams/")
        sys.exit(1)

    log.info("Loaded %d teams", len(teams))
    team_names = list(teams.keys())

    RESULTS_DIR.mkdir(exist_ok=True)
    run_id = time.strftime("%Y%m%d_%H%M%S")
    run_dir = RESULTS_DIR / f"run_{run_id}"
    run_dir.mkdir(exist_ok=True)

    client = get_client()

    # --- Phase 1: Pointwise scoring ---
    if args.rank_only:
        # load existing scores
        latest = sorted(RESULTS_DIR.glob("run_*/pointwise.json"))
        if not latest:
            log.error("No existing scores found for --rank-only")
            sys.exit(1)
        pointwise = json.loads(latest[-1].read_text())
        log.info("Loaded existing pointwise scores")
    else:
        log.info("=" * 60)
        log.info("PHASE 1: Deep pointwise scoring (%d teams)", len(teams))
        log.info("=" * 60)

        if args.team:
            # single team mode
            if args.team not in teams:
                log.error("Team '%s' not found. Available: %s", args.team, list(teams.keys()))
                sys.exit(1)
            result = await score_team(client, args.team, teams[args.team])
            pointwise = {args.team: result}
        else:
            # score all teams (2 at a time for rate limits)
            pointwise = {}
            batch_size = 2
            names = list(teams.keys())
            for i in range(0, len(names), batch_size):
                batch = names[i:i + batch_size]
                coros = [score_team(client, n, teams[n]) for n in batch]
                results = await asyncio.gather(*coros, return_exceptions=True)
                for name, result in zip(batch, results):
                    if isinstance(result, Exception):
                        log.error("Failed to score %s: %s", name, result)
                    else:
                        pointwise[name] = result
                if i + batch_size < len(names):
                    await asyncio.sleep(1)

        # save pointwise
        (run_dir / "pointwise.json").write_text(json.dumps(pointwise, indent=2))
        log.info("Pointwise scores saved to %s", run_dir / "pointwise.json")

        # print intermediate results
        print("\n" + "=" * 60)
        print("POINTWISE SCORES")
        print("=" * 60)
        sorted_pw = sorted(pointwise.items(), key=lambda x: x[1].get("weighted_score", 0), reverse=True)
        for i, (name, data) in enumerate(sorted_pw, 1):
            ws = data.get("weighted_score", 0)
            verdict = data.get("verdict", "?")
            desc = data.get("what_they_built", "")[:80]
            print(f"  {i}. {name}: {ws:.1f}/10 [{verdict}]")
            print(f"     {desc}")
            analysis = data.get("analysis", {})
            for crit in CRITERIA:
                s = analysis.get(crit, {}).get("score", "?")
                print(f"       {crit}: {s}/10")
            print()

    # --- Phase 2: Pairwise tournament ---
    if not args.skip_pairwise and not args.team and len(pointwise) > 1:
        log.info("=" * 60)
        log.info("PHASE 2: All-pairs tournament (%d comparisons)", len(list(itertools.combinations(team_names, 2))))
        log.info("=" * 60)

        # only compare teams we have scores for
        scored_teams = {k: v for k, v in teams.items() if k in pointwise}
        comparisons = await all_pairs_tournament(client, scored_teams)

        (run_dir / "pairwise.json").write_text(json.dumps(comparisons, indent=2, default=str))
        pair_rankings = compute_pairwise_rankings(comparisons, list(scored_teams.keys()))

        print("\n" + "=" * 60)
        print("PAIRWISE WIN RATES")
        print("=" * 60)
        sorted_pair = sorted(pair_rankings.items(), key=lambda x: x[1]["win_rate"], reverse=True)
        for name, data in sorted_pair:
            print(f"  {name}: {data['wins']}W-{data['losses']}L ({data['win_rate']:.0%})")
    else:
        pair_rankings = {}
        comparisons = []

    # --- Phase 3: Combined ranking ---
    scored_names = [n for n in team_names if n in pointwise]
    if pair_rankings:
        final = combine_rankings(pointwise, pair_rankings, scored_names)
    else:
        # pointwise only
        final = []
        for i, name in enumerate(sorted(scored_names, key=lambda n: pointwise[n].get("weighted_score", 0), reverse=True)):
            final.append({
                "rank": i + 1,
                "team": name,
                "final_score": pointwise[name].get("weighted_score", 0),
                "pointwise_score": pointwise[name].get("weighted_score", 0),
                "what_they_built": pointwise[name].get("what_they_built", ""),
                "verdict": pointwise[name].get("verdict", ""),
                "strengths": pointwise[name].get("strengths", []),
                "weaknesses": pointwise[name].get("weaknesses", []),
                "prize_tracks": pointwise[name].get("prize_tracks", []),
            })

    # save final results
    (run_dir / "final_rankings.json").write_text(json.dumps(final, indent=2))

    # print final results
    print("\n" + "=" * 60)
    print("FINAL RANKINGS")
    print("=" * 60)
    for r in final:
        rank = r["rank"]
        name = r["team"]
        score = r["final_score"]
        desc = r.get("what_they_built", "")[:100]
        verdict = r.get("verdict", "")
        print(f"\n  #{rank} {name}")
        print(f"  Score: {score:.2f} | {verdict}")
        print(f"  Built: {desc}")
        if r.get("strengths"):
            print(f"  Strengths: {', '.join(r['strengths'][:3])}")
        if r.get("weaknesses"):
            print(f"  Weaknesses: {', '.join(r['weaknesses'][:3])}")
        if r.get("prize_tracks"):
            print(f"  Could win: {', '.join(r['prize_tracks'])}")
        if "pairwise_win_rate" in r:
            print(f"  Pointwise: {r['pointwise_score']:.1f}/10 | Pairwise: {r['wins']}W-{r['losses']}L ({r['pairwise_win_rate']:.0%})")

    print(f"\nResults saved to: {run_dir}/")
    print(f"  pointwise.json — detailed per-team analysis")
    print(f"  pairwise.json  — all head-to-head comparisons")
    print(f"  final_rankings.json — combined final rankings")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
