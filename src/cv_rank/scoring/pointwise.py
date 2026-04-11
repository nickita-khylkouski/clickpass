"""
Pointwise scoring with chain-of-thought reasoning and baseline calibration.

Supports two scoring modes (configurable via ``pointwise.scoring_mode``):

**"single" (default):** Each person gets a single 1.0-100.0 score.
**"rubric":** Prometheus-style rubric scoring — 5 dimensions scored 1-5 each,
weighted to produce a composite 1-100 score.  Based on research from
MT-Bench, Prometheus (KAIST), and Microsoft LLM-Rubric (ACL 2024).

Output per person::

    {
        "name": str,
        "score": float,          # 1.0-100.0 (composite in rubric mode)
        "confidence": str,       # "high" | "medium" | "low"
        "strongest_signal": str,
        "concerns": str,
        "reasoning": str,        # chain-of-thought / overall assessment
        "scoring_mode": str,     # "single" or "rubric"
        "dimensions": dict,      # rubric mode only: per-dimension scores
        "dimension_scores": dict, # rubric mode only: {dim: score} summary
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from pathlib import Path
from typing import Any, Callable

from cv_rank.utils import extract_json as _extract_json

logger = logging.getLogger("cv_rank.scoring.pointwise")


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _build_system_prompt(
    criteria: dict[str, float],
    baselines_text: str,
    total: int,
    accept_count: int,
) -> str:
    """Build the system prompt with criteria weights and calibration baselines."""

    criteria_text = "\n".join(
        f"- {k.replace('_', ' ').title()}: {v * 100:.0f}% weight"
        for k, v in criteria.items()
    )

    return f"""You are evaluating applicants for a builder-first AI hackathon / AI community event in San Francisco. \
About {accept_count} of {total} applicants will be invited. Score each person 1.0-100.0.

EVALUATION CRITERIA (weighted):
{criteria_text}

WHAT MAKES SOMEONE VALUABLE:
- Builders who ship real things (GitHub repos with stars, products with users, open source)
- Strong hackathon builders who can prototype quickly and contribute technically in a room
- Founders with real startups (funded, have a product, not just a title)
- People with concrete technical ownership at strong companies, labs, or startups
- Strong technical depth (commits, multiple languages, complex projects)
- Community connectors (large followings, content creators, event organizers)
- Researchers with publications and real academic work
- Unique backgrounds that add diversity of thought

SCORE RANGES (be precise):
- 90-100: World-class. Turing Award tier, unicorn founder, 10K+ GitHub stars, top-3 venue papers.
- 75-89: Exceptional. Senior or staff engineer with clearly described shipped systems, founder with a live product and traction, prolific OSS maintainer with real adoption, or researcher with concrete technical output.
- 55-74: Strong. Working engineer at known company, real shipped projects, active GitHub (100+ commits/yr).
- 35-54: Average. Mid-level at unremarkable company, some projects but nothing notable, student with real work.
- 15-34: Weak. Junior with tutorial-level repos, "aspiring" titles with nothing shipped, vague LinkedIn.
- 1-14: Reject. Almost no concrete profile evidence of building, shipping, or technical contribution.

SCORING RULES:
- USE THE FULL RANGE 1.0-100.0 with DECIMAL precision (e.g., 67.3, not 67)
- Do NOT cluster around 50-55. Spread scores -- every 5-point gap should reflect a REAL difference.
- ASSUME THE PROFILE DATA PROVIDED TO YOU IS TRUE.
- Treat stated facts as true, but do not add unstated metrics, scope, ownership, or impact. A title is evidence of the title only; technical depth and product scale must come from work explicitly described in the profile.
- Missing LinkedIn or GitHub can lower CONFIDENCE, but should not erase strong stated evidence.
- GitHub is important but NOT the only signal. A senior engineer with clearly described shipped systems can beat a student with tutorial commits even without public GitHub.
- Hackathon fit matters: reward people with clear evidence they can build, ship, demo, and collaborate in a fast-moving technical environment.
- Concrete shipped work beats vague prestige. Real repos, deployments, submissions, products, and technical artifacts should dominate generic status signals.
- No LinkedIn + No GitHub is a negative signal when there is also no concrete stated evidence, but it is not an automatic low score by itself.
- Founders with REAL product/funding > "aspiring entrepreneurs" with just a title.
- Students with real projects can score 40-60. Students with nothing: 10-20.
- When data is sparse because someone is private (not weak), note low confidence.

LANGUAGE RULES:
- Take all applicant statements at face value. If someone says they did X, describe it as fact.
- Do NOT use hedging words: "claims", "alleges", "purports", "states they", "says they".
- Write "built X" not "claims to have built X". Write "raised $5M" not "claims to have raised $5M".
- If evidence is weak or unverifiable, note low confidence — but don't hedge the language.

WHAT MAKES SOMEONE WEAK (score 1-35):
- Almost no concrete profile evidence — empty LinkedIn, empty GitHub, and no described projects, roles, or shipped work
- "Aspiring" or "passionate about AI" with zero evidence of execution
- Tutorial repos only (forks, TODO apps, course projects with no originality)
- Buzzword-heavy self-description with nothing concrete to back it up
- Very junior (< 1 year) with no side projects or portfolio

EXPECTED DISTRIBUTION for {total} people:
- 80.0-100.0: ~{total * 0.07:.0f} people (exceptional)
- 60.0-79.9: ~{total * 0.18:.0f} people (strong)
- 40.0-59.9: ~{total * 0.30:.0f} people (decent)
- 20.0-39.9: ~{total * 0.25:.0f} people (weak)
- 1.0-19.9: ~{total * 0.10:.0f} people (reject)

CALIBRATION PROFILES -- anchor your scoring to these real examples:

{baselines_text}

Use these as anchors. Spread your scores across the full range."""


def _build_user_prompt(formatted_profile: str, chain_of_thought: bool = True) -> str:
    """Build the per-person user prompt."""

    if chain_of_thought:
        return f"""Score this person:

{formatted_profile}

THINK step by step:
1. What is their strongest signal? (specific evidence)
2. What concerns you? (what's missing or weak)
3. Which calibration profile are they closest to? Above or below?
4. What specific score (with decimal) do they deserve?

Return ONLY valid JSON:
{{"reasoning": "<your step-by-step analysis>", "score": <1.0-100.0 with decimal>, "confidence": "high"|"medium"|"low", "why": "<2-3 sentences with specific evidence>", "strongest_signal": "<single most impressive thing>", "concerns": "<what's weak or missing>"}}"""
    else:
        return f"""Score this person:

{formatted_profile}

Return ONLY valid JSON:
{{"score": <1.0-100.0 with decimal>, "confidence": "high"|"medium"|"low", "why": "<2-3 sentences with specific evidence>", "strongest_signal": "<single most impressive thing>", "concerns": "<what's weak or missing>"}}"""


# ---------------------------------------------------------------------------
# V2: Rubric-based scoring (Prometheus pattern)
# ---------------------------------------------------------------------------

# Default dimension weights (must sum to 1.0).  Overridable via config
# ``pointwise.rubric_weights``.
DEFAULT_RUBRIC_WEIGHTS: dict[str, float] = {
    "technical_depth": 0.30,
    "builder_signal": 0.35,
    "professional_standing": 0.15,
    "community_impact": 0.05,
    "uniqueness": 0.15,
}

# Prometheus-style rubric: concrete 1-5 level definitions per dimension.
RUBRIC_DEFINITIONS: dict[str, dict[str, str | dict[int, str]]] = {
    "technical_depth": {
        "question": "How strong is this person's technical ability based on the evidence presented?",
        "levels": {
            1: "No technical background. No GitHub, no technical role, no evidence of coding or engineering.",
            2: "Minimal technical signal. Tutorial-level projects only. Non-technical role or very junior with no demonstrated projects.",
            3: "Moderate technical ability. Some non-trivial code, or a technical role at a company, or a relevant degree with concrete project evidence.",
            4: "Strong technical skills. Multiple real projects, difficult systems work, senior engineering role, peer-reviewed publications, or deep domain expertise.",
            5: "Exceptional technical depth. Major open-source contributions, advanced systems/ML/robotics work, top-lab or staff+ engineering, or top-tier published research.",
        },
    },
    "builder_signal": {
        "question": "Has this person built and shipped real things that people use?",
        "levels": {
            1: "No evidence of building anything. No projects, no products, no repos with original code.",
            2: "Only ideas, hackathon concepts, tutorials, forks, or incomplete repos. Little evidence of real-world use.",
            3: "Has built and shipped at least one real thing with concrete evidence: deployed app, hackathon submission, production feature, or original library.",
            4: "Strong builder. Multiple shipped projects or one clearly strong shipped project with measurable impact, serious technical scope, or meaningful open-source adoption.",
            5: "Exceptional builder. Repeated evidence of shipping valuable products: funded startup with live product, widely-used OSS, major production ownership, or multiple strong shipped systems.",
        },
    },
    "professional_standing": {
        "question": "What is this person's career trajectory and professional credibility?",
        "levels": {
            1: "No professional experience. Student with no internships, or entirely unrelated field with no tech connection.",
            2: "Early career or tangentially related. Junior role, 0-2 years experience, or experience only in non-technical fields.",
            3: "Mid-career professional. 3-7 years in a relevant field, solid company on resume, or strong internship at a notable company.",
            4: "Senior professional. 7+ years experience, leadership or staff-level role, well-known company, or founder with real traction.",
            5: "Industry leader. VP/Staff+ at top company, well-known founder with traction or exits, or recognized expert with strong execution record.",
        },
    },
    "community_impact": {
        "question": "How visible and connected is this person in the tech/AI community?",
        "levels": {
            1: "No community presence described. No followers, no content, no community participation mentioned.",
            2: "Minimal community presence. Under 200 followers mentioned, or basic professional profile. Little content creation.",
            3: "Active community member. 200-1000 followers, some content creation or conference attendance, participates in events or meetups.",
            4: "Notable community presence. 1000-5000 followers, regular content creation, speaking engagements, or event organizer.",
            5: "Community leader. 5000+ followers, recognized thought leader, major conference speaker, open-source community founder, or significant media presence.",
        },
    },
    "uniqueness": {
        "question": "What makes this person distinctively valuable or interesting for an AI community event?",
        "levels": {
            1: "Nothing distinctive. Generic background, no unique angle, interchangeable with many other applicants.",
            2: "Slightly interesting but nothing remarkable. One minor differentiator that doesn't strongly stand out.",
            3: "One notable differentiator: unusual career path, interesting side-project, specific domain expertise, or a relevant award.",
            4: "Multiple differentiators. Unique combination of skills, notable achievements or awards, or a distinctive perspective that adds diversity.",
            5: "Truly exceptional uniqueness. Rare expertise, groundbreaking work, extraordinary achievement (patents, major awards, pioneering research), or a story that would energize a room.",
        },
    },
}


def _build_rubric_text(weights: dict[str, float] | None = None) -> str:
    """Format the rubric definitions into prompt text."""
    w = weights or DEFAULT_RUBRIC_WEIGHTS
    sections: list[str] = []
    for dim_key, dim_def in RUBRIC_DEFINITIONS.items():
        weight_pct = w.get(dim_key, 0) * 100
        lines = [
            f"### {dim_key.replace('_', ' ').title()} ({weight_pct:.0f}% weight)",
            f"Question: {dim_def['question']}",
        ]
        for level, desc in dim_def["levels"].items():
            lines.append(f"  Score {level}: {desc}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _build_system_prompt_v2(
    criteria: dict[str, float],
    baselines_text: str,
    total: int,
    accept_count: int,
    rubric_weights: dict[str, float] | None = None,
) -> str:
    """Build rubric-based system prompt (Prometheus + MT-Bench hybrid)."""

    rubric_text = _build_rubric_text(rubric_weights)

    return f"""You are a fair, impartial judge evaluating applicants for a builder-first AI hackathon / AI community event in San Francisco. \
About {accept_count} of {total} applicants will be invited.

Your evaluation MUST follow the rubric below. Score each dimension independently on a 1-5 scale \
by matching the candidate's evidence to the level descriptions. Do NOT evaluate in general — \
assess strictly based on the rubric criteria.

SCORING RUBRIC:

{rubric_text}

IMPORTANT RULES:
- Score each dimension INDEPENDENTLY. A person can be 5 on Builder Signal but 2 on Community Impact.
- TAKE ALL CLAIMS AT FACE VALUE. If someone says they work at Google Brain, score them as a Google Brain employee. If they say they published 12 NeurIPS papers, score them as having 12 NeurIPS papers. Do NOT penalize for missing LinkedIn/GitHub links — many strong candidates simply don't include them.
- Treat stated facts as true, but do not add unstated metrics, scope, ownership, or impact. A title is evidence of the title only; product scale and technical depth must come from work explicitly described in the profile.
- When LinkedIn or GitHub are missing, note it in concerns and lower CONFIDENCE (not score). A Staff ML Engineer at Google Brain is still a Staff ML Engineer even without a LinkedIn URL.
- GitHub stars and real users matter more than number of repos.
- This event is BUILDER-FIRST. Builder Signal and Technical Depth matter more than social reach or generic seniority.
- Reward strong hackathon fit: clear evidence the person can build fast, ship demos, unblock teammates, or turn ideas into working artifacts.
- Founders with REAL product/funding > title-only "founders" or "aspiring entrepreneurs."
- Senior engineers at top companies can score high on Professional Standing without GitHub, but Technical Depth should reach 4-5 only when the profile describes hard systems, research, infrastructure, or original technical work.
- A large following or impressive title should NOT rescue weak builder evidence. Community Impact is a tie-breaker, not a primary signal.
- Proposed future hackathon ideas count less than projects already built, shipped, submitted, or deployed.
- Be strict: a score of 3 means AVERAGE, not good. Use the FULL 1-5 range freely.
- Expect roughly: ~20% at 1, ~25% at 2, ~25% at 3, ~20% at 4, ~10% at 5. Use 1s generously for people with little evidence. Use 5s only for genuinely exceptional shipped work or technical output: widely used OSS, high-scale production ownership, strong live product traction, or major research execution.

CALIBRATION PROFILES — anchor your scoring to these real examples:

{baselines_text}

Use these anchors to maintain consistency across all evaluations."""


def _build_user_prompt_v2(
    formatted_profile: str,
    rubric_weights: dict[str, float] | None = None,
) -> str:
    """Build rubric-based per-person user prompt."""
    w = rubric_weights or DEFAULT_RUBRIC_WEIGHTS

    # Flat format (dim_score, dim_evidence) — smaller models produce this
    # more reliably than nested JSON.  _compute_composite_score() parses both.
    dim_lines = []
    for k in w:
        dim_lines.append(f'  "{k}_score": <integer 1-5>')
        dim_lines.append(f'  "{k}_evidence": "<specific facts>"')
    dim_text = ",\n".join(dim_lines)

    return f"""Evaluate this applicant:

{formatted_profile}

For each rubric dimension, find evidence in the profile and assign a score (1-5).
Assume the profile data provided is true. Focus on builder signal, technical depth, and hackathon usefulness over generic prestige.

Return ONLY valid JSON with these exact keys:
{{
{dim_text},
  "confidence": "high" or "medium" or "low",
  "why": "<2-3 sentences with concrete evidence>",
  "strongest_signal": "<single most impressive fact>",
  "concerns": "<what is weak or missing>",
  "reasoning": "<2-3 sentence assessment>"
}}"""


def _compute_composite_score(
    result: dict[str, Any],
    weights: dict[str, float] | None = None,
) -> float:
    """Convert per-dimension 1-5 scores to a composite 1.0-100.0 score.

    Accepts two formats:
    - **Nested:** ``{"dimensions": {"technical_depth": {"score": 4}, ...}}``
    - **Flat:** ``{"technical_depth_score": 4, ...}``

    Formula: weighted_avg(1-5) mapped to 1-100 via ``(avg - 1) * 24.75 + 1``.
    This maps 1→1.0, 3→50.5, 5→100.0.
    """
    w = weights or DEFAULT_RUBRIC_WEIGHTS
    total_weight = 0.0
    weighted_sum = 0.0

    # Check for nested format first
    dims = result.get("dimensions", {})

    for dim, weight in w.items():
        raw: Any = None
        # Try nested format: dimensions.dim.score
        if dim in dims:
            dim_data = dims[dim]
            if isinstance(dim_data, dict):
                raw = dim_data.get("score", 3)
            else:
                raw = dim_data
        # Try flat format: dim_score
        elif f"{dim}_score" in result:
            raw = result[f"{dim}_score"]
        else:
            # Missing dimension = LLM forgot it, not a zero score.
            # 3 is the 1-5 midpoint.  We warn because it inflates composites.
            raw = 3
            logger.warning(
                "Rubric dimension '%s' missing from LLM output, defaulting to 3/5",
                dim,
            )

        try:
            score = float(raw)
        except (TypeError, ValueError):
            score = 3.0
        score = max(1.0, min(5.0, score))
        weighted_sum += score * weight
        total_weight += weight

    if total_weight == 0:
        return 50.0

    avg = weighted_sum / total_weight  # 1.0-5.0
    # Map 1-5 → 1-100
    return round((avg - 1) * 24.75 + 1.0, 1)


# ---------------------------------------------------------------------------
# Baseline builder (bug #7 fix: match by NAME, not profile_idx)
# ---------------------------------------------------------------------------

def _build_baselines_text(
    baselines_config: list[dict],
    people: list[dict],
    format_profile_fn: Callable[[dict], str],
) -> str:
    """Build calibration baseline text by matching people by name.

    This fixes bug #7 from the original pipeline where baselines used
    ``profile_idx`` (positional indexing that broke when the data order
    changed).  We now match by name for robustness.
    """
    sections: list[str] = []

    for b in baselines_config:
        name = b.get("name", "")
        if not name:
            continue

        matched = next(
            (p for p in people if p.get("name", "").lower() == name.lower()),
            None,
        )
        if matched is None:
            logger.warning("Baseline '%s' not found in people list -- skipping", name)
            continue

        formatted = format_profile_fn(matched)
        sections.append(
            f'=== {b["score"]}/100: {b["label"]} ===\n'
            f"{formatted}\n\n"
            f'WHY {b["score"]}: {b.get("why", "See profile above")}'
        )

    if sections:
        return "\n\n---\n\n".join(sections)

    # Synthetic fallback anchors when no baselines are configured
    return """=== 10/100: REJECT ===
No LinkedIn, no GitHub, generic "aspiring AI entrepreneur" with no evidence of building anything. Self-description is buzzwords only.
WHY 10: Zero verifiable signal. No projects, no code, no professional history.

---

=== 35/100: WEAK ===
Junior developer, 1 year experience at a no-name startup. GitHub has 3 repos with < 10 commits each, all tutorial-level. LinkedIn exists but sparse.
WHY 35: Some technical signal but nothing impressive. Tutorial-level work only.

---

=== 55/100: AVERAGE ===
Mid-level software engineer at a mid-size company, 3 years experience. Has a few GitHub repos with some original code but < 50 stars total. Decent LinkedIn.
WHY 55: Real working engineer with some projects, but nothing that stands out.

---

=== 78/100: STRONG ===
Senior engineer at a well-known startup, 7 years experience. Active GitHub with 200+ stars across projects. Has shipped real products used by thousands.
WHY 78: Strong builder with real impact. Multiple shipped projects at scale.

---

=== 92/100: EXCEPTIONAL ===
Staff ML Engineer at Google Brain, 3 NeurIPS papers, 500+ GitHub stars on an ML framework. Previously co-founded a YC startup.
WHY 92: World-class technical depth plus real building track record."""


# ---------------------------------------------------------------------------
# Single-person scoring
# ---------------------------------------------------------------------------

async def score_one(
    client: Any,
    person: dict,
    criteria: dict[str, float],
    model: str,
    baselines_text: str,
    accept_count: int,
    config: dict,
    *,
    total: int = 0,
    format_profile_fn: Callable[[dict], str] | None = None,
) -> dict:
    """Score a single person with retries and backoff.

    Parameters
    ----------
    client:
        An ``openai.AsyncOpenAI`` instance.
    person:
        The person dict (must contain at least ``"name"``).
    criteria:
        Weighted criteria dict (e.g. ``{"technical_depth": 0.3, ...}``).
    model:
        Model identifier string (e.g. ``"gpt-4o"``).
    baselines_text:
        Pre-built calibration baseline text (from ``_build_baselines_text``).
    accept_count:
        How many people will be accepted.
    config:
        Full configuration dict.
    total:
        Total number of people being scored (for the system prompt distribution).
    format_profile_fn:
        Callable that turns a person dict into a formatted string.

    Returns
    -------
    dict
        Scoring result with keys: name, score, confidence, strongest_signal,
        concerns, reasoning (when CoT enabled).  On failure includes "error".
    """
    name = person.get("name", "?")
    pw_cfg = config.get("pointwise", {})
    temperature = pw_cfg.get("temperature", 0)
    cot = pw_cfg.get("chain_of_thought", False)
    max_retries = config.get("max_retries", 5)
    scoring_mode = pw_cfg.get("scoring_mode", "rubric")  # "single" or "rubric"
    rubric_weights = pw_cfg.get("rubric_weights") or DEFAULT_RUBRIC_WEIGHTS

    if format_profile_fn is None:
        formatted = json.dumps(person, indent=2)
    else:
        formatted = format_profile_fn(person)

    # Dispatch to v1 (single score) or v2 (rubric-based)
    if scoring_mode == "rubric":
        system_prompt = _build_system_prompt_v2(
            criteria, baselines_text, total, accept_count, rubric_weights,
        )
        user_prompt = _build_user_prompt_v2(formatted, rubric_weights)
        max_tokens = 1200
    else:
        system_prompt = _build_system_prompt(criteria, baselines_text, total, accept_count)
        user_prompt = _build_user_prompt(formatted, chain_of_thought=cot)
        max_tokens = 1400 if cot else 700

    logger.debug(
        "Scoring %s [mode=%s]: system_prompt=%d chars, user_prompt=%d chars",
        name, scoring_mode, len(system_prompt), len(user_prompt),
    )

    from cv_rank.utils import LLMRetryExhausted, llm_request

    t0 = time.time()
    try:
        content, tokens = await llm_request(
            client,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            model=model,
            max_tokens=max_tokens,
            label=name,
            temperature=temperature,
            max_retries=max_retries,
            trace_context={
                "phase": "pointwise",
                "candidate_name": name,
                "scoring_mode": scoring_mode,
                "accept_count": accept_count,
                "total_candidates": total,
            },
        )

        logger.debug("%s: raw response (first 500): %s", name, content[:500])
        result = _extract_json(content)
        result["name"] = name
        result["tokens_used"] = tokens

        # Compute score based on mode
        if scoring_mode == "rubric":
            has_rubric_fields = any(
                key in result or f"{key}_score" in result
                for key in rubric_weights
            ) or "dimensions" in result
            if has_rubric_fields:
                composite = _compute_composite_score(result, rubric_weights)
            else:
                raw_score = result.get("score", 50.0)
                try:
                    composite = round(float(raw_score), 1)
                except (TypeError, ValueError):
                    composite = 50.0
            result["score"] = composite
            result["scoring_mode"] = "rubric"
            dim_scores = {}
            dims = result.get("dimensions", {})
            for dk in rubric_weights:
                if dk in dims:
                    dd = dims[dk]
                    dim_scores[dk] = dd.get("score", "?") if isinstance(dd, dict) else dd
                elif f"{dk}_score" in result:
                    dim_scores[dk] = result[f"{dk}_score"]
                else:
                    dim_scores[dk] = "?"
            result["dimension_scores"] = dim_scores
            dim_summary = " ".join(f"{k[:4]}={v}" for k, v in dim_scores.items())
            logger.info(
                "Scored %s: %.1f [rubric: %s] (confidence=%s, tokens=%d)",
                name, composite, dim_summary,
                result.get("confidence", "?"), tokens,
            )
        else:
            score = result.get("score", 0)
            if isinstance(score, str):
                score = float(score)
            result["score"] = round(float(score), 1)
            result["scoring_mode"] = "single"
            elapsed_ms = (time.time() - t0) * 1000
            logger.info(
                "Scored %s: %.1f (confidence=%s, tokens=%d, %.0fms)",
                name, result["score"],
                result.get("confidence", "?"),
                tokens, elapsed_ms,
            )

        return result

    except json.JSONDecodeError:
        logger.error("%s: FAILED (json_parse)", name)
        # score=None → combine_rankings excludes them entirely.
        # score=0 would silently rank them last.
        return {"name": name, "error": "json_parse", "score": None}

    except LLMRetryExhausted as e:
        logger.error("%s: FAILED (%s)", name, e)
        return {"name": name, "error": str(e)[:200], "score": None}


# ---------------------------------------------------------------------------
# Batch scoring with checkpoints
# ---------------------------------------------------------------------------

async def score_all(
    people: list[dict],
    criteria: dict[str, float],
    model: str,
    accept_count: int,
    config: dict,
    run_dir: Path,
    format_profile_fn: Callable[[dict], str],
) -> list[dict]:
    """Score all people with concurrent LLM calls and periodic checkpoints.

    Parameters
    ----------
    people:
        List of person dicts to score.
    criteria:
        Weighted criteria dict.
    model:
        Model identifier string.
    accept_count:
        Target number of accepts.
    config:
        Full configuration dict.
    run_dir:
        Directory for checkpoint and output files.
    format_profile_fn:
        Callable that formats a person dict into a profile string for the LLM.

    Returns
    -------
    list[dict]
        All scoring results, sorted by score descending.
    """
    from cv_rank.utils import get_openai_client

    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    checkpoint_path = run_dir / "pointwise_checkpoint.json"
    output_path = run_dir / "pointwise_scores.json"

    # Build baselines text (bug #7 fix: match by name, not profile_idx)
    baselines_config = config.get("baselines", [])
    baselines_text = _build_baselines_text(baselines_config, people, format_profile_fn)

    # Resume from checkpoint
    scored: dict[str, dict] = {}
    if checkpoint_path.exists():
        try:
            existing = json.loads(checkpoint_path.read_text())
            for s in existing:
                scored[s["name"]] = s
            logger.info("Resuming: %d already scored", len(scored))
        except (json.JSONDecodeError, KeyError):
            logger.warning("Corrupt checkpoint -- starting fresh")

    # Filter to unscored
    to_score = [p for p in people if p.get("name", "?") not in scored]
    if not to_score:
        logger.info("All %d people already scored", len(people))
        return sorted(scored.values(), key=lambda x: -(x.get("score") or 0))

    logger.info("Scoring %d people with %s ...", len(to_score), model)

    # Client & concurrency — default to 5 to avoid overwhelming smaller models
    client = get_openai_client()
    concurrency = config.get("concurrency", {}).get("scoring", 5)
    semaphore = asyncio.Semaphore(concurrency)
    save_every = config.get("save_every", 50)
    total = len(people)

    t_start = time.time()
    total_tokens = 0
    errors = 0

    async def _guarded_score(person: dict, idx: int) -> dict:
        # Stagger requests to avoid thundering herd
        jitter = random.uniform(0, min(2.0, idx * 0.3))
        await asyncio.sleep(jitter)
        async with semaphore:
            return await score_one(
                client,
                person,
                criteria,
                model,
                baselines_text,
                accept_count,
                config,
                total=total,
                format_profile_fn=format_profile_fn,
            )

    for batch_start in range(0, len(to_score), save_every):
        batch = to_score[batch_start: batch_start + save_every]
        tasks = [
            _guarded_score(p, batch_start + i)
            for i, p in enumerate(batch)
        ]
        results = await asyncio.gather(*tasks)

        for r in results:
            total_tokens += r.get("tokens_used", 0)
            if r.get("error"):
                errors += 1
            scored[r["name"]] = r
            err_flag = " ERROR:" + r["error"] if r.get("error") else ""
            score_val = r.get("score")
            score_str = f"{score_val:.1f}" if score_val is not None else "ERR"
            logger.info(
                "[%d/%d] score=%s conf=%s | %s%s",
                len(scored),
                total,
                score_str,
                r.get("confidence", "?"),
                r.get("name", "?"),
                err_flag,
            )

        # Save checkpoint
        all_results = list(scored.values())
        checkpoint_path.write_text(json.dumps(all_results, indent=2))

        # Batch stats: avg, min, max scores for this batch
        batch_scores = [r["score"] for r in results if r.get("score") is not None and not r.get("error")]
        batch_errors = sum(1 for r in results if r.get("error"))
        if batch_scores:
            b_avg = sum(batch_scores) / len(batch_scores)
            b_min = min(batch_scores)
            b_max = max(batch_scores)
            logger.info(
                "Checkpoint: %d/%d scored (%d errors, %d tokens) | "
                "batch avg=%.1f, min=%.1f, max=%.1f, batch_errors=%d",
                len(scored), total, errors, total_tokens,
                b_avg, b_min, b_max, batch_errors,
            )
        else:
            logger.info(
                "Checkpoint: %d/%d scored (%d errors, %d tokens) | "
                "batch had no successful scores, batch_errors=%d",
                len(scored), total, errors, total_tokens, batch_errors,
            )

    # Explicitly close the client to avoid event-loop-closed errors on cleanup
    await client.close()

    elapsed = time.time() - t_start
    all_results = sorted(scored.values(), key=lambda x: -(x.get("score") or 0))

    # Save final output
    output_path.write_text(json.dumps(all_results, indent=2))
    logger.info(
        "Scoring complete: %d people in %.0fs (%d tokens, %d errors)",
        len(all_results), elapsed, total_tokens, errors,
    )

    # Surface error rate so it's impossible to miss
    error_pct = errors * 100 / len(all_results) if all_results else 0
    if error_pct > 10:
        logger.warning(
            "HIGH ERROR RATE: %d/%d (%.0f%%) scoring attempts failed. "
            "These people are EXCLUDED from final rankings. Check API key, "
            "rate limits, or model availability.",
            errors, len(all_results), error_pct,
        )
    elif errors > 0:
        logger.info("Errors: %d/%d (%.0f%%) -- excluded from rankings", errors, len(all_results), error_pct)

    # Score distribution summary — detect compression early
    valid_scores = [r["score"] for r in all_results if r.get("score") is not None and not r.get("error")]
    if valid_scores:
        import statistics
        s_mean = statistics.mean(valid_scores)
        s_median = statistics.median(valid_scores)
        s_stdev = statistics.stdev(valid_scores) if len(valid_scores) > 1 else 0.0
        s_min = min(valid_scores)
        s_max = max(valid_scores)
        sorted_s = sorted(valid_scores)
        n = len(sorted_s)
        q1 = sorted_s[n // 4] if n >= 4 else s_min
        q3 = sorted_s[(3 * n) // 4] if n >= 4 else s_max
        logger.info(
            "Score distribution: mean=%.1f, median=%.1f, std=%.1f, "
            "min=%.1f, max=%.1f, Q1=%.1f, Q3=%.1f",
            s_mean, s_median, s_stdev, s_min, s_max, q1, q3,
        )

        # LLMs tend to cluster scores mid-range.  Healthy stdev for 200+
        # people is >15.  Low stdev = rankings can't differentiate.
        if s_stdev < 12 and n > 30:
            logger.warning(
                "SCORE COMPRESSION: stdev=%.1f, scores clustered in %.0f-%.0f. "
                "Rankings may not differentiate candidates.",
                s_stdev, q1, q3,
            )
        # If nobody scored below 30, weak candidates aren't being penalized.
        if s_min > 30 and n > 30:
            logger.warning(
                "NO LOW SCORES: min=%.1f (expected some in 1-20 range)",
                s_min,
            )

    return all_results
