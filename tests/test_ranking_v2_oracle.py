"""Golden/oracle scaffold for the ranking-v2 rewrite.

These tests do not call live APIs. They verify that the synthetic oracle
fixture and comparison helpers are structured the way the future v2 pipeline
will need.
"""

from __future__ import annotations

from tests.ranking_v2_oracle import assert_ranked_order, assert_stage_matches, load_oracle


def test_oracle_fixture_has_expected_structure() -> None:
    oracle = load_oracle()

    assert oracle["schema_version"] == 1
    assert oracle["metadata"]["accept_count"] == 2
    assert len(oracle["applicants"]) == 3
    assert set(oracle["stages"]) == {
        "normalized",
        "pointwise",
        "pairwise",
        "combined",
        "borderline",
        "quality",
        "exports",
    }


def test_oracle_fixture_uses_stable_applicant_ids() -> None:
    oracle = load_oracle()
    applicant_ids = [row["applicant_id"] for row in oracle["applicants"]]

    assert len(applicant_ids) == len(set(applicant_ids))
    assert len({row["name"] for row in oracle["applicants"]}) == 2


def test_stage_comparison_is_keyed_by_applicant_id() -> None:
    oracle = load_oracle()

    actual_pointwise = [
        oracle["stages"]["pointwise"][2],
        oracle["stages"]["pointwise"][0],
        oracle["stages"]["pointwise"][1],
    ]

    assert_stage_matches(
        actual_pointwise,
        oracle["stages"]["pointwise"],
        keys=("score", "confidence", "strongest_signal", "concerns"),
    )


def test_ranked_stage_is_sorted_by_rank() -> None:
    oracle = load_oracle()
    assert_ranked_order(oracle["stages"]["combined"], key="rank")
