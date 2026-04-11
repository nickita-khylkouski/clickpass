from __future__ import annotations

import csv

from cv_rank_v2.export.csv_exports import (
    write_failures_csv,
    write_needs_review_csv,
    write_ranked_csv,
)
from cv_rank_v2.scoring.borderline import identify_borderline_applicants
from cv_rank_v2.scoring.combine import (
    _entropy_weights,
    _min_max_normalize,
    combine_rankings,
    combine_rankings_with_metadata,
)


def _applicant(applicant_id: str, name: str, email: str) -> dict:
    return {
        "applicant_id": applicant_id,
        "basic": {
            "display_name": name,
            "email": email,
        },
        "application": {
            "raw_csv": {
                "Name": name,
                "Email": email,
            }
        },
    }


def test_v2_min_max_normalize() -> None:
    assert _min_max_normalize([10.0, 20.0, 30.0]) == [0.0, 0.5, 1.0]
    assert _min_max_normalize([5.0, 5.0, 5.0]) == [0.0, 0.0, 0.0]


def test_v2_entropy_weights_sum_to_one() -> None:
    swiss_weight, pointwise_weight = _entropy_weights([0.0, 0.5, 1.0], [0.1, 0.2, 0.9])
    assert round(swiss_weight + pointwise_weight, 6) == 1.0
    assert 0.2 <= swiss_weight <= 0.8


def test_v2_combine_rankings_uses_applicant_id() -> None:
    pointwise = [
        {
            "applicant_id": "cand_a",
            "display_name": "Same Name",
            "score": 90.0,
            "confidence": "high",
        },
        {
            "applicant_id": "cand_b",
            "display_name": "Same Name",
            "score": 50.0,
            "confidence": "medium",
        },
    ]
    swiss = {
        "cand_a": {"wins": 10, "losses": 0, "byes": 0},
        "cand_b": {"wins": 0, "losses": 10, "byes": 0},
    }

    combined = combine_rankings(pointwise, swiss, {})

    assert [row["applicant_id"] for row in combined] == ["cand_a", "cand_b"]
    assert combined[0]["display_name"] == "Same Name"
    assert combined[0]["rank"] == 1


def test_v2_combine_rankings_can_prefer_swiss_tie_breaker() -> None:
    pointwise = [
        {"applicant_id": "cand_a", "display_name": "Alice", "score": 80.0},
        {"applicant_id": "cand_b", "display_name": "Bob", "score": 80.0},
    ]
    swiss = {
        "cand_a": {"wins": 7, "losses": 3, "byes": 0},
        "cand_b": {"wins": 9, "losses": 1, "byes": 0},
    }

    combined, metadata = combine_rankings_with_metadata(
        pointwise,
        swiss,
        {},
        swiss_weight=0.5,
        pointwise_weight=0.5,
        tie_breaker="swiss",
    )

    assert combined[0]["applicant_id"] == "cand_b"
    assert metadata["tie_breaker"] == "swiss"
    assert metadata["swiss_weight"] == 0.5


def test_v2_identify_borderline_applicants_adds_disagreements() -> None:
    rankings = [
        {"applicant_id": "cand_a", "rank": 1},
        {"applicant_id": "cand_b", "rank": 2},
        {"applicant_id": "cand_c", "rank": 3},
        {"applicant_id": "cand_d", "rank": 4},
        {"applicant_id": "cand_e", "rank": 5},
    ]
    pointwise = [
        {"applicant_id": "cand_a", "score": 10.0},
        {"applicant_id": "cand_b", "score": 60.0},
        {"applicant_id": "cand_c", "score": 61.0},
        {"applicant_id": "cand_d", "score": 62.0},
        {"applicant_id": "cand_e", "score": 63.0},
    ]
    swiss = {
        "cand_a": {"wins": 10, "losses": 0},
        "cand_b": {"wins": 5, "losses": 5},
        "cand_c": {"wins": 5, "losses": 5},
        "cand_d": {"wins": 5, "losses": 5},
        "cand_e": {"wins": 5, "losses": 5},
    }

    selected = identify_borderline_applicants(
        rankings,
        cutline=3,
        band_pct=0.10,
        pointwise_results=pointwise,
        swiss_records=swiss,
        disagreement_threshold=0.5,
    )

    assert "cand_a" in selected


def test_v2_ranked_csv_export(tmp_path) -> None:
    applicants = [
        _applicant("cand_a", "Alice", "alice@example.com"),
        _applicant("cand_b", "Bob", "bob@example.com"),
    ]
    rankings = [
        {
            "applicant_id": "cand_a",
            "display_name": "Alice",
            "rank": 1,
            "final_score": 0.95,
            "pointwise_score": 91.0,
            "swiss_wins": 8,
            "swiss_losses": 1,
            "bt_strength": 0.88,
            "confidence": "high",
            "strongest_signal": "OSS",
            "concerns": "",
        },
        {
            "applicant_id": "cand_b",
            "display_name": "Bob",
            "rank": 2,
            "final_score": 0.55,
            "pointwise_score": 64.0,
            "swiss_wins": 4,
            "swiss_losses": 5,
            "bt_strength": 0.44,
            "confidence": "medium",
            "strongest_signal": "builder",
            "concerns": "thin profile",
        },
    ]
    quality = {
        "cand_a": {"verdict": "YES", "specific_why": "Built real things"},
        "cand_b": {"verdict": "BORDERLINE", "specific_why": "Sparse evidence"},
    }

    output = write_ranked_csv(applicants, rankings, quality, tmp_path / "ranked.csv", target_accepts=1)
    rows = list(csv.DictReader(output.open()))

    assert rows[0]["Applicant_ID"] == "cand_a"
    assert rows[0]["Status"] == "ACCEPT"
    assert rows[1]["Status"] == "WAITLIST"
    assert rows[1]["Verdict"] == "BORDERLINE"


def test_v2_failure_and_review_exports(tmp_path) -> None:
    applicants = [
        _applicant("cand_a", "Alice", "alice@example.com"),
        _applicant("cand_b", "Bob", "bob@example.com"),
    ]
    stage_results = [
        {"applicant_id": "cand_a", "error": "json_parse"},
        {"applicant_id": "cand_b"},
    ]
    failures_path = write_failures_csv(applicants, "pointwise", stage_results, tmp_path / "failed.csv")
    failure_rows = list(csv.DictReader(failures_path.open()))
    assert len(failure_rows) == 1
    assert failure_rows[0]["Applicant_ID"] == "cand_a"

    rankings = [
        {
            "applicant_id": "cand_a",
            "display_name": "Alice",
            "rank": 1,
            "final_score": 0.95,
            "pointwise_score": 95.0,
            "swiss_wins": 8,
            "swiss_losses": 1,
        },
        {
            "applicant_id": "cand_b",
            "display_name": "Bob",
            "rank": 2,
            "final_score": 0.10,
            "pointwise_score": 90.0,
            "swiss_wins": 0,
            "swiss_losses": 9,
        },
    ]
    review_path = write_needs_review_csv(
        applicants,
        rankings,
        tmp_path / "review.csv",
        target_accepts=1,
        boundary_width_pct=0.10,
        disagreement_threshold=0.25,
    )
    review_rows = list(csv.DictReader(review_path.open()))
    assert len(review_rows) == 2
    assert {row["Applicant_ID"] for row in review_rows} == {"cand_a", "cand_b"}


def test_v2_review_export_ignores_disagreement_without_swiss_eval(tmp_path) -> None:
    applicants = [_applicant("cand_a", "Alice", "alice@example.com")]
    rankings = [
        {
            "applicant_id": "cand_a",
            "display_name": "Alice",
            "rank": 10,
            "final_score": 0.10,
            "pointwise_score": 92.0,
            "swiss_wins": 0,
            "swiss_losses": 0,
            "swiss_evaluated": False,
        },
    ]

    review_path = write_needs_review_csv(
        applicants,
        rankings,
        tmp_path / "review.csv",
        target_accepts=1,
        boundary_width_pct=0.10,
        disagreement_threshold=0.25,
    )
    review_rows = list(csv.DictReader(review_path.open()))
    assert review_rows == []
