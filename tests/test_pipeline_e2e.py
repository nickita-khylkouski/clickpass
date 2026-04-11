"""
End-to-end pipeline benchmark: run scoring on REAL profiles from the
ai_builders_gc.csv dataset with manually-assigned tier expectations,
then measure how well the model ranked them.

Profiles were hand-picked to span 4 tiers:
  Tier 1: Major exits, PhDs at top labs, well-known products
  Tier 2: YC founders, senior engineers at big tech
  Tier 3: Early-stage founders, domain specialists
  Tier 4: Non-technical, vague descriptions, minimal experience

Requires OPENAI_API_KEY in environment. Skipped unless:
  pytest -m e2e                       (marker-based)
  E2E_TESTS=1 pytest -m e2e          (env-based)

Cost: ~$0.03-0.08 per run (21 people, 5 swiss rounds).
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import statistics
import sys
import time
from pathlib import Path

import pytest

from cv_rank.config import DEFAULT_CONFIG

pytestmark = pytest.mark.e2e

_BENCHMARK_CSV = Path("/tmp/ai_builders_gc.csv")
_skip_reason = "E2E tests require OPENAI_API_KEY, benchmark CSV, and explicit opt-in (pytest -m e2e)"

# CSV row indices (0-based) of the 21 hand-picked profiles and their tiers.
# Tiers assigned based on self-descriptions, company backgrounds, and projects.
SELECTED_INDICES: dict[int, int] = {
    # Tier 1: 3x founder w/ exits, ex-FAANG directors, PhDs at top labs, Forbes 30U30
    93: 1,   # Gabor Cselle — 3x founder (T2, Namo Media, reMail), ex-Director Google, PM Twitter
    39: 1,   # Brooke Hopkins — Founder of Coval, ex-Waymo & Google TL/SWE
    47: 1,   # Cameron Pfiffer — Statistician, PhD economist, SWE at Stanford
    184: 1,  # Nalin Gupta — YC alum, exit in self-driving cars, Forbes 30U30

    # Tier 2: YC founders, senior ICs at big tech, shipped AI products
    108: 2,  # Ihsaan Patel — YC Founder, automated data cleaning tools
    138: 2,  # Kashish — YC founder, CyberSecurity expert, ex-Brex/Microsoft/Duo
    3: 2,    # Aiswarya Sankar — Founder entelligence.AI, prev senior ML at Uber
    28: 2,   # Baris Ozmen — Founder Peruser, prev AI systems at Meta/LinkedIn
    183: 2,  # Zubin Pahuja — Founder Freeform AI, ex-Uber/Microsoft/Citadel

    # Tier 3: Founders with products but less pedigree, domain specialists
    45: 3,   # Caleb J — CEO Pongo, production retrieval systems
    64: 3,   # David Correa — SWE turned CEO of Versive
    18: 3,   # Arul Gupta — Entrepreneur, AI in health/wellness
    171: 3,  # Michael Gold — Entrepreneur/educator, pipeline tools for 3D + AI
    153: 3,  # Lencol Metayer — Founder in Gen AI
    82: 3,   # Evelyn — Co-founder/CEO AI digital healthcare

    # Tier 4: Non-technical, vague, VC/investor, networking-focused
    130: 4,  # Joe Wang — Fund-of-funds LP looking to allocate
    201: 4,  # Paulina — VC
    86: 4,   # Francesco Cutrone — Corporate finance analyst
    205: 4,  # Rachel — Product & Design
    116: 4,  # Justin Rich — SWE looking to network in the AI space
    200: 4,  # paul@crinquand.com — CS junior, moved to SF
}


def _should_skip() -> bool:
    if not os.environ.get("OPENAI_API_KEY"):
        return True
    if not _BENCHMARK_CSV.exists():
        return True
    if os.environ.get("E2E_TESTS") == "1":
        return False
    return True


def _load_benchmark_profiles() -> tuple[list[dict], dict[str, int]]:
    """Load the 21 selected profiles from the CSV and return (people, tier_map).

    Returns
    -------
    people : list[dict]
        Profile dicts as loaded by load_csv (via csv.DictReader).
    tier_map : dict[str, int]
        name -> expected tier (1-4).
    """
    from cv_rank.csv_io import load_csv

    all_people = load_csv(_BENCHMARK_CSV)
    selected = []
    tier_map = {}
    for idx, tier in SELECTED_INDICES.items():
        if idx < len(all_people):
            p = all_people[idx]
            selected.append(p)
            tier_map[p["name"]] = tier
    return selected, tier_map


# ---------------------------------------------------------------------------
# Correlation metrics
# ---------------------------------------------------------------------------

def spearman_rho(expected: list[str], actual: list[str]) -> float:
    """Spearman rank correlation. Returns [-1, 1], 1.0 = perfect agreement."""
    n = len(expected)
    rank_expected = {name: i + 1 for i, name in enumerate(expected)}
    rank_actual = {name: i + 1 for i, name in enumerate(actual)}
    d_sq_sum = sum(
        (rank_expected[name] - rank_actual.get(name, n)) ** 2
        for name in expected
    )
    return 1 - (6 * d_sq_sum) / (n * (n**2 - 1))


def tier_accuracy(actual_order: list[str], expected_tiers: dict[str, int],
                  tier_sizes: dict[int, int]) -> float:
    """Fraction of people ranked within their expected tier band.

    Tier bands are variable-sized (tier 1 has 4 people, tier 2 has 5, etc.).
    """
    # Build actual tier assignment based on position
    cumulative = 0
    position_to_tier: dict[int, int] = {}
    for tier in sorted(tier_sizes.keys()):
        for pos in range(cumulative, cumulative + tier_sizes[tier]):
            position_to_tier[pos] = tier
        cumulative += tier_sizes[tier]

    correct = 0
    for rank_idx, name in enumerate(actual_order):
        if rank_idx in position_to_tier:
            actual_tier = position_to_tier[rank_idx]
            expected_tier = expected_tiers.get(name, 99)
            if actual_tier == expected_tier:
                correct += 1
    return correct / len(actual_order)


def top_k_precision(expected_tiers: dict[str, int], actual: list[str], k: int,
                    target_tier: int = 1) -> float:
    """What fraction of actual top-k are from the target tier?"""
    actual_top = actual[:k]
    from_tier = sum(1 for name in actual_top if expected_tiers.get(name) == target_tier)
    return from_tier / k


def mean_tier_distance(actual_order: list[str], expected_tiers: dict[str, int],
                       tier_sizes: dict[int, int]) -> float:
    """Average absolute tier distance. 0 = perfect."""
    cumulative = 0
    position_to_tier: dict[int, int] = {}
    for tier in sorted(tier_sizes.keys()):
        for pos in range(cumulative, cumulative + tier_sizes[tier]):
            position_to_tier[pos] = tier
        cumulative += tier_sizes[tier]

    total = 0
    for rank_idx, name in enumerate(actual_order):
        actual_tier = position_to_tier.get(rank_idx, 4)
        expected_tier = expected_tiers.get(name, 4)
        total += abs(actual_tier - expected_tier)
    return total / len(actual_order)


# ---------------------------------------------------------------------------
# Fixtures (module-scoped — run pipeline once, share across tests)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def e2e_config():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["event"]["name"] = "E2E Benchmark"
    cfg["event"]["type"] = "community_meetup"
    cfg["event"]["target_accepts"] = 9  # top ~40%
    cfg["swiss"]["rounds"] = 5
    cfg["swiss"]["use_bradley_terry"] = True
    cfg["weights"]["swiss"] = 0.50
    cfg["weights"]["pointwise"] = 0.50
    cfg["concurrency"]["scoring"] = 3
    cfg["concurrency"]["swiss"] = 3
    cfg["max_retries"] = 3
    cfg["save_every"] = 50
    cfg["baselines"] = []
    cfg["pointwise"]["temperature"] = 0
    cfg["pointwise"]["chain_of_thought"] = True
    # Skip enrichment — profiles come pre-loaded from CSV
    cfg["enrichment"]["supabase"]["enabled"] = False
    cfg["enrichment"]["github"]["enabled"] = False
    return cfg


@pytest.fixture(scope="module")
def e2e_results(e2e_config, tmp_path_factory):
    """Run the full scoring pipeline once on real profiles."""
    from dotenv import load_dotenv
    load_dotenv()

    if _should_skip():
        pytest.skip(_skip_reason)

    from cv_rank.profile import format_profile
    from cv_rank.scoring.pointwise import score_all
    from cv_rank.scoring.swiss import run_swiss
    from cv_rank.scoring.combine import combine_rankings

    people, tier_map = _load_benchmark_profiles()
    assert len(people) >= 15, f"Expected 15+ benchmark profiles, got {len(people)}"

    run_dir = tmp_path_factory.mktemp("e2e_benchmark")
    criteria = {
        "technical_depth": 0.30,
        "industry_experience": 0.25,
        "open_source": 0.20,
        "network_engagement": 0.15,
        "publications": 0.10,
    }
    model = e2e_config["models"]["scoring"]

    # Tier sizes for metrics (how many people per tier)
    tier_sizes: dict[int, int] = {}
    for tier in tier_map.values():
        tier_sizes[tier] = tier_sizes.get(tier, 0) + 1

    t0 = time.time()

    # Phase 1: Pointwise scoring
    scores = asyncio.run(score_all(
        people, criteria, model,
        e2e_config["event"]["target_accepts"],
        e2e_config, run_dir, format_profile,
    ))

    # Phase 2: Swiss tournament
    swiss_records, bt_strengths, all_matches = asyncio.run(run_swiss(
        people, criteria, model,
        e2e_config["swiss"]["rounds"],
        e2e_config["event"]["target_accepts"],
        e2e_config, run_dir, format_profile,
    ))

    # Phase 3: Combine
    rankings = combine_rankings(
        scores, swiss_records, bt_strengths,
        swiss_weight=e2e_config["weights"]["swiss"],
        pointwise_weight=e2e_config["weights"]["pointwise"],
    )

    elapsed = time.time() - t0
    actual_order = [r["name"] for r in rankings]

    # Expected order: tier 1 people first (sorted by tier, then by name within tier)
    expected_order = sorted(tier_map.keys(), key=lambda n: (tier_map[n], n))

    # Save detailed results for debugging
    results_path = run_dir / "benchmark_results.json"
    with open(results_path, "w") as f:
        json.dump({
            "elapsed_seconds": elapsed,
            "model": model,
            "actual_order": actual_order,
            "expected_order": expected_order,
            "tier_map": tier_map,
            "rankings": rankings,
            "scores": scores,
            "swiss_records": swiss_records,
        }, f, indent=2, default=str)

    print(f"\n  Benchmark results saved to: {results_path}", file=sys.stderr)

    return {
        "rankings": rankings,
        "scores": scores,
        "swiss_records": swiss_records,
        "bt_strengths": bt_strengths,
        "actual_order": actual_order,
        "expected_order": expected_order,
        "tier_map": tier_map,
        "tier_sizes": tier_sizes,
        "elapsed": elapsed,
        "model": model,
        "run_dir": run_dir,
        "people_count": len(people),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRankingQuality:
    """Verify the pipeline produces rankings that correlate with expected tiers."""

    def test_spearman_correlation_positive(self, e2e_results):
        """Rank correlation with expected tier order should be positive (> 0.3)."""
        rho = spearman_rho(e2e_results["expected_order"], e2e_results["actual_order"])
        actual = e2e_results["actual_order"]
        tier_map = e2e_results["tier_map"]

        print(f"\n  Spearman rho = {rho:.3f} (threshold: 0.3)", file=sys.stderr)
        print("  Actual ranking:", file=sys.stderr)
        for i, name in enumerate(actual):
            tier = tier_map.get(name, "?")
            print(f"    {i+1:2d}. [T{tier}] {name}", file=sys.stderr)

        assert rho > 0.3, f"Spearman rho = {rho:.3f}, expected > 0.3"

    def test_tier_accuracy_above_threshold(self, e2e_results):
        """At least 35% of people should be in their expected tier band."""
        actual = e2e_results["actual_order"]
        tier_map = e2e_results["tier_map"]
        tier_sizes = e2e_results["tier_sizes"]

        acc = tier_accuracy(actual, tier_map, tier_sizes)
        print(f"\n  Tier accuracy = {acc:.0%} (threshold: 35%)", file=sys.stderr)
        assert acc >= 0.35, f"Tier accuracy = {acc:.0%}, expected >= 35%"

    def test_tier1_mostly_in_top_half(self, e2e_results):
        """At least 3 of 4 tier-1 people should appear in the top half."""
        actual = e2e_results["actual_order"]
        tier_map = e2e_results["tier_map"]
        n = len(actual)
        top_half = set(actual[:n // 2])
        tier1 = [name for name, t in tier_map.items() if t == 1]
        in_top = sum(1 for name in tier1 if name in top_half)

        print(f"\n  Tier 1 in top half: {in_top}/{len(tier1)}", file=sys.stderr)
        for name in tier1:
            rank = actual.index(name) + 1 if name in actual else "?"
            in_half = "top" if name in top_half else "BOTTOM"
            print(f"    {name}: rank {rank} ({in_half})", file=sys.stderr)

        assert in_top >= 3, f"Only {in_top}/4 tier-1 people in top half"

    def test_tier4_mostly_in_bottom_half(self, e2e_results):
        """At least 4 of 6 tier-4 people should appear in the bottom half."""
        actual = e2e_results["actual_order"]
        tier_map = e2e_results["tier_map"]
        n = len(actual)
        bottom_half = set(actual[n // 2:])
        tier4 = [name for name, t in tier_map.items() if t == 4]
        in_bottom = sum(1 for name in tier4 if name in bottom_half)

        print(f"\n  Tier 4 in bottom half: {in_bottom}/{len(tier4)}", file=sys.stderr)
        for name in tier4:
            rank = actual.index(name) + 1 if name in actual else "?"
            in_half = "BOTTOM" if name in bottom_half else "top"
            print(f"    {name}: rank {rank} ({in_half})", file=sys.stderr)

        assert in_bottom >= 4, f"Only {in_bottom}/6 tier-4 people in bottom half"

    def test_tier1_above_tier4_on_average(self, e2e_results):
        """Average rank of tier-1 people should be better than tier-4."""
        actual = e2e_results["actual_order"]
        tier_map = e2e_results["tier_map"]
        rank_of = {name: i for i, name in enumerate(actual)}

        tier1_ranks = [rank_of[n] for n in tier_map if tier_map[n] == 1 and n in rank_of]
        tier4_ranks = [rank_of[n] for n in tier_map if tier_map[n] == 4 and n in rank_of]

        avg_t1 = statistics.mean(tier1_ranks) if tier1_ranks else 999
        avg_t4 = statistics.mean(tier4_ranks) if tier4_ranks else 0

        print(f"\n  Avg rank tier 1: {avg_t1:.1f}, tier 4: {avg_t4:.1f}", file=sys.stderr)
        assert avg_t1 < avg_t4, f"Tier 1 avg rank ({avg_t1:.1f}) should be < tier 4 ({avg_t4:.1f})"

    def test_mean_tier_distance_below_threshold(self, e2e_results):
        """Average tier distance should be < 1.5 (most people within 1 tier of expected)."""
        actual = e2e_results["actual_order"]
        dist = mean_tier_distance(actual, e2e_results["tier_map"], e2e_results["tier_sizes"])
        print(f"\n  Mean tier distance = {dist:.2f} (threshold: 1.5)", file=sys.stderr)
        assert dist < 1.5, f"Mean tier distance = {dist:.2f}, expected < 1.5"


class TestScoreDistribution:
    """Verify scores aren't compressed into a narrow range."""

    def test_pointwise_score_spread(self, e2e_results):
        """Pointwise scores should span > 15 points."""
        scores = e2e_results["scores"]
        vals = sorted([s.get("score", 0) for s in scores if s.get("score")], reverse=True)
        if not vals:
            pytest.skip("No valid pointwise scores")
        spread = max(vals) - min(vals)
        stdev = statistics.stdev(vals) if len(vals) > 1 else 0

        print(f"\n  Pointwise range: {min(vals):.1f} - {max(vals):.1f} (spread: {spread:.1f})", file=sys.stderr)
        print(f"  Stdev: {stdev:.1f}", file=sys.stderr)
        print(f"  Scores: {[f'{v:.0f}' for v in vals]}", file=sys.stderr)
        assert spread > 15, f"Score spread = {spread:.1f}, expected > 15"

    def test_unique_combined_scores(self, e2e_results):
        """At least 70% of combined scores should be unique."""
        rankings = e2e_results["rankings"]
        n = len(rankings)
        final_scores = [r.get("final_score", 0) for r in rankings]
        unique = len(set(round(s, 4) for s in final_scores))
        pct = unique / n

        print(f"\n  Unique combined scores: {unique}/{n} ({pct:.0%})", file=sys.stderr)
        assert pct >= 0.70, f"Only {unique}/{n} unique scores ({pct:.0%})"

    def test_tier1_scores_above_tier4_scores(self, e2e_results):
        """Average pointwise score for tier 1 should exceed tier 4."""
        scores = e2e_results["scores"]
        tier_map = e2e_results["tier_map"]
        score_of = {s["name"]: s.get("score", 0) for s in scores}

        t1_scores = [score_of[n] for n in tier_map if tier_map[n] == 1 and n in score_of]
        t4_scores = [score_of[n] for n in tier_map if tier_map[n] == 4 and n in score_of]

        avg_t1 = statistics.mean(t1_scores) if t1_scores else 0
        avg_t4 = statistics.mean(t4_scores) if t4_scores else 100

        print(f"\n  Avg pointwise — tier 1: {avg_t1:.1f}, tier 4: {avg_t4:.1f}", file=sys.stderr)
        assert avg_t1 > avg_t4, f"Tier 1 avg ({avg_t1:.1f}) should exceed tier 4 ({avg_t4:.1f})"


class TestPipelineHealth:
    """Verify the pipeline runs without errors."""

    def test_no_scoring_errors(self, e2e_results):
        scores = e2e_results["scores"]
        errors = [s for s in scores if s.get("error")]
        if errors:
            for e in errors:
                print(f"  ERROR: {e['name']}: {e.get('error')}", file=sys.stderr)
        assert len(errors) == 0, f"{len(errors)} scoring errors"

    def test_all_people_ranked(self, e2e_results):
        n = e2e_results["people_count"]
        ranked = len(e2e_results["rankings"])
        assert ranked == n, f"Ranked {ranked}, expected {n}"

    def test_completes_within_timeout(self, e2e_results):
        elapsed = e2e_results["elapsed"]
        print(f"\n  Pipeline elapsed: {elapsed:.1f}s", file=sys.stderr)
        assert elapsed < 180, f"Pipeline took {elapsed:.1f}s, expected < 180s"


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    os.environ["E2E_TESTS"] = "1"
    from dotenv import load_dotenv
    load_dotenv()

    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: Set OPENAI_API_KEY to run the benchmark", file=sys.stderr)
        sys.exit(1)
    if not _BENCHMARK_CSV.exists():
        print(f"ERROR: Benchmark CSV not found at {_BENCHMARK_CSV}", file=sys.stderr)
        sys.exit(1)

    sys.exit(pytest.main([__file__, "-v", "-s", "-m", "e2e", "--tb=short"]))
