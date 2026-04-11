"""
Cost estimation and tracking for cv-rank pipeline runs.

Provides dry-run estimates and post-run actual cost summaries by reading
token counts from checkpoint files.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# Pricing per 1M tokens: (input, cached_input, output)
MODEL_PRICING: dict[str, tuple[float, float, float]] = {
    "gpt-5.2":      (1.75,  0.175, 14.00),
    "gpt-5-mini":   (0.25,  0.025,  2.00),
    "gpt-5-nano":   (0.05,  0.005,  0.40),
    "gpt-4o":       (2.50,  1.25,  10.00),
    "gpt-4o-mini":  (0.15,  0.075,  0.60),
    "o4-mini":      (1.10,  0.275,  4.40),
}
# Fallback for unknown models
_DEFAULT_PRICING = (1.00, 0.50, 5.00)


def estimate_cost(model: str, total_tokens: int, output_ratio: float = 0.15) -> float:
    """Estimate cost for a model given total tokens and output fraction."""
    inp, _, out = MODEL_PRICING.get(model, _DEFAULT_PRICING)
    input_tokens = total_tokens * (1 - output_ratio)
    output_tokens = total_tokens * output_ratio
    return input_tokens / 1_000_000 * inp + output_tokens / 1_000_000 * out


def print_dry_run(people: list, config: dict) -> None:
    """Estimate API costs and print summary without running any phases."""
    n = len(people)
    models = config["models"]
    rounds = config["swiss"]["rounds"]
    matches_per_round = n // 2
    bl_enabled = config.get("borderline", {}).get("enabled", True)
    bl_band = config.get("borderline", {}).get("band_pct", 0.15)
    bl_rounds = config.get("borderline", {}).get("extra_rounds", 5)

    # Token estimates calibrated from real 311-person enriched run
    pw_tokens_per_person = 2500   # enriched profiles are ~700 tokens + response
    swiss_tokens_per_match = 2500 # two profiles + system prompt + response
    qc_tokens_per_person = 2400   # profile + ranking context + response
    bl_candidates = int(n * bl_band * 2) if bl_enabled else 0
    bl_matches = bl_candidates // 2 * bl_rounds if bl_enabled else 0

    phases = [
        ("Pointwise",  models.get("scoring", "?"),    n * pw_tokens_per_person,          n, "people", 0.16),
        ("Swiss",      models.get("swiss", "?"),      rounds * matches_per_round * swiss_tokens_per_match, rounds * matches_per_round, "matches", 0.06),
        ("Borderline", models.get("borderline", models.get("swiss", "?")), bl_matches * swiss_tokens_per_match, bl_matches, "matches", 0.06),
        ("Quality",    models.get("quality_check", "?"), n * qc_tokens_per_person,       n, "people", 0.06),
    ]

    total_tokens = 0
    total_cost = 0.0
    print("", file=sys.stderr)
    print("  ┌─────────────┬──────────────┬────────────┬──────────┐", file=sys.stderr)
    print("  │ Phase       │ Model        │ Est.Tokens │ Est.Cost │", file=sys.stderr)
    print("  ├─────────────┼──────────────┼────────────┼──────────┤", file=sys.stderr)
    for phase_name, model, tokens, count, unit, out_ratio in phases:
        if tokens == 0:
            continue
        cost = estimate_cost(model, tokens, out_ratio)
        total_tokens += tokens
        total_cost += cost
        print(f"  │ {phase_name:<11} │ {model:<12} │ {tokens:>10,} │ ${cost:>6.2f} │", file=sys.stderr)
    print("  ├─────────────┼──────────────┼────────────┼──────────┤", file=sys.stderr)
    print(f"  │ {'TOTAL':<11} │ {'':12} │ {total_tokens:>10,} │ ${total_cost:>6.2f} │", file=sys.stderr)
    print("  └─────────────┴──────────────┴────────────┴──────────┘", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"  People: {n} | Swiss: {rounds} rounds × {matches_per_round} matches", file=sys.stderr)
    if bl_enabled:
        print(f"  Borderline: ~{bl_candidates} candidates × {bl_rounds} rounds = ~{bl_matches} matches", file=sys.stderr)
    supa_on = config.get("enrichment", {}).get("supabase", {}).get("enabled", False)
    gh_on = config.get("enrichment", {}).get("github", {}).get("enabled", False)
    print(f"  Enrichment: supabase={'ON' if supa_on else 'OFF'}, github={'ON' if gh_on else 'OFF'}", file=sys.stderr)


def print_cost_summary(run_dir: Path, config: dict, logger: logging.Logger) -> None:
    """Print actual cost summary by reading token counts from checkpoints."""
    models = config["models"]

    # Read actual token counts from checkpoint/output files
    phases: list[tuple[str, str, int, float]] = []  # (name, model, tokens, output_ratio)

    # Pointwise tokens
    pw_path = run_dir / "pointwise.json"
    if pw_path.exists():
        pw_data = json.loads(pw_path.read_text())
        if isinstance(pw_data, list):
            pw_tokens = sum(r.get("tokens_used", 0) for r in pw_data)
            phases.append(("Pointwise", models.get("scoring", "?"), pw_tokens, 0.16))

    # Swiss tokens (from checkpoint)
    sw_path = run_dir / "swiss_checkpoint.json"
    if sw_path.exists():
        sw_data = json.loads(sw_path.read_text())
        sw_tokens = sw_data.get("total_tokens", 0)
        phases.append(("Swiss", models.get("swiss", "?"), sw_tokens, 0.06))

    # Borderline tokens
    bl_path = run_dir / "borderline_checkpoint.json"
    if bl_path.exists():
        bl_data = json.loads(bl_path.read_text())
        bl_tokens = bl_data.get("total_tokens", 0)
        bl_model = models.get("borderline", models.get("swiss", "?"))
        phases.append(("Borderline", bl_model, bl_tokens, 0.06))

    # Quality tokens
    qc_path = run_dir / "quality.json"
    if qc_path.exists():
        qc_data = json.loads(qc_path.read_text())
        if isinstance(qc_data, list):
            qc_tokens = sum(r.get("tokens_used", 0) for r in qc_data)
            phases.append(("Quality", models.get("quality_check", "?"), qc_tokens, 0.06))

    if not phases:
        return

    total_tokens = 0
    total_cost = 0.0
    lines = []
    for phase_name, model, tokens, out_ratio in phases:
        cost = estimate_cost(model, tokens, out_ratio)
        total_tokens += tokens
        total_cost += cost
        lines.append(f"  {phase_name:<11} ({model:<12}): {tokens:>10,} tokens = ${cost:.2f}")

    logger.info("Cost summary:")
    for line in lines:
        logger.info(line)
    logger.info("  %-11s %14s  %10s tokens = $%.2f", "TOTAL", "", f"{total_tokens:,}", total_cost)
