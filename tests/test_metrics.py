"""Tests for cv_rank.metrics — ranking quality metrics."""

from __future__ import annotations

import pytest

from cv_rank.metrics import (
    _kendall_tau,
    _spearman_rho,
    boundary_accuracy,
    compare_runs,
    length_bias_correlation,
    signal_disagreement,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ranking(names: list[str]) -> list[dict]:
    """Build a ranking list from ordered names."""
    return [{"name": n, "rank": i + 1} for i, n in enumerate(names)]


def _scores(data: dict[str, float]) -> list[dict]:
    return [{"name": n, "score": s} for n, s in data.items()]


def _records(*entries: tuple[str, int, int]) -> dict[str, dict]:
    return {n: {"wins": w, "losses": l} for n, w, l in entries}


# ---------------------------------------------------------------------------
# _kendall_tau
# ---------------------------------------------------------------------------

class TestKendallTau:
    def test_identical_ordering(self):
        assert _kendall_tau(["A", "B", "C"], ["A", "B", "C"]) == 1.0

    def test_reversed_ordering(self):
        assert _kendall_tau(["A", "B", "C"], ["C", "B", "A"]) == -1.0

    def test_single_element(self):
        assert _kendall_tau(["A"], ["A"]) == 1.0

    def test_partial_overlap(self):
        tau = _kendall_tau(["A", "B", "C", "D"], ["B", "A", "C", "D"])
        assert -1.0 <= tau <= 1.0
        assert tau < 1.0  # not identical

    def test_partial_overlap_different_sizes(self):
        """Lists with different sizes: only common elements are compared."""
        a = ["X", "A", "B", "C"]  # X is extra
        b = ["A", "B", "C"]
        tau = _kendall_tau(a, b)
        # A, B, C are in same order in both → perfect agreement
        assert tau == 1.0

    def test_partial_overlap_reversed_common(self):
        """Common elements reversed across lists of different sizes."""
        a = ["X", "C", "B", "A"]
        b = ["A", "B", "C", "Y"]
        tau = _kendall_tau(a, b)
        assert tau == -1.0


# ---------------------------------------------------------------------------
# _spearman_rho
# ---------------------------------------------------------------------------

class TestSpearmanRho:
    def test_identical_ordering(self):
        assert _spearman_rho(["A", "B", "C"], ["A", "B", "C"]) == 1.0

    def test_reversed_ordering(self):
        rho = _spearman_rho(["A", "B", "C"], ["C", "B", "A"])
        assert rho == pytest.approx(-1.0, abs=0.01)

    def test_single_element(self):
        assert _spearman_rho(["A"], ["A"]) == 1.0

    def test_partial_overlap_same_order(self):
        """Common elements in same order despite extra elements in each list."""
        a = ["X", "A", "B", "C"]
        b = ["A", "B", "C", "Y"]
        rho = _spearman_rho(a, b)
        assert rho == 1.0

    def test_partial_overlap_reversed(self):
        """Common elements reversed across differently-sized lists."""
        a = ["X", "C", "B", "A"]
        b = ["A", "B", "C", "Y"]
        rho = _spearman_rho(a, b)
        assert rho == pytest.approx(-1.0, abs=0.01)


# ---------------------------------------------------------------------------
# boundary_accuracy
# ---------------------------------------------------------------------------

class TestBoundaryAccuracy:
    def test_basic(self):
        rankings = _ranking(["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"])
        result = boundary_accuracy(rankings, cutline=5)

        assert result["accept_count"] == 5
        assert result["reject_count"] == 5
        assert result["boundary_count"] > 0
        assert result["boundary_band"][0] >= 1
        assert result["boundary_band"][1] <= 10

    def test_empty_rankings(self):
        result = boundary_accuracy([], cutline=5)
        assert result["boundary_count"] == 0
        assert result["accept_count"] == 0
        assert result["reject_count"] == 0

    def test_cutline_beyond_length(self):
        rankings = _ranking(["A", "B", "C"])
        result = boundary_accuracy(rankings, cutline=10)
        assert result["accept_count"] == 3
        assert result["reject_count"] == 0

    def test_boundary_candidates_are_near_cutline(self):
        names = [f"P{i}" for i in range(100)]
        rankings = _ranking(names)
        result = boundary_accuracy(rankings, cutline=50)

        low, high = result["boundary_band"]
        for name in result["boundary_candidates"]:
            rank = next(r["rank"] for r in rankings if r["name"] == name)
            assert low <= rank <= high


# ---------------------------------------------------------------------------
# signal_disagreement
# ---------------------------------------------------------------------------

class TestSignalDisagreement:
    def test_detects_hidden_gem(self):
        """Low pointwise but high swiss = hidden gem."""
        scores = _scores({"A": 20, "B": 80, "C": 50})
        records = _records(("A", 15, 0), ("B", 0, 15), ("C", 7, 8))

        result = signal_disagreement(scores, records, threshold=0.3)

        names = {r["name"] for r in result}
        assert "A" in names  # low pw, high swiss
        # A should be marked hidden_gem
        a_entry = next(r for r in result if r["name"] == "A")
        assert a_entry["direction"] == "hidden_gem"

    def test_detects_false_positive(self):
        """High pointwise but low swiss = false positive."""
        scores = _scores({"A": 90, "B": 50, "C": 50})
        records = _records(("A", 0, 15), ("B", 10, 5), ("C", 7, 8))

        result = signal_disagreement(scores, records, threshold=0.3)

        names = {r["name"] for r in result}
        assert "A" in names
        a_entry = next(r for r in result if r["name"] == "A")
        assert a_entry["direction"] == "false_positive"

    def test_no_disagreement_when_aligned(self):
        """When both signals agree, no disagreements reported."""
        scores = _scores({"A": 90, "B": 50, "C": 10})
        records = _records(("A", 15, 0), ("B", 7, 8), ("C", 0, 15))

        result = signal_disagreement(scores, records, threshold=0.3)
        assert len(result) == 0

    def test_sorted_by_disagreement(self):
        scores = _scores({"A": 10, "B": 50, "C": 90})
        records = _records(("A", 15, 0), ("B", 0, 15), ("C", 7, 8))

        result = signal_disagreement(scores, records, threshold=0.0)
        if len(result) >= 2:
            assert result[0]["disagreement"] >= result[1]["disagreement"]

    def test_uses_bt_strengths(self):
        scores = _scores({"A": 20, "B": 80})
        records = _records(("A", 5, 10), ("B", 10, 5))
        bt = {"A": 0.9, "B": 0.1}

        result = signal_disagreement(scores, records, bt_strengths=bt, threshold=0.3)
        # A: low pw, high bt → hidden gem
        assert any(r["name"] == "A" and r["direction"] == "hidden_gem" for r in result)

    def test_empty_inputs(self):
        assert signal_disagreement([], {}) == []


# ---------------------------------------------------------------------------
# length_bias_correlation
# ---------------------------------------------------------------------------

class TestLengthBiasCorrelation:
    def test_positive_correlation(self):
        """Longer profiles → higher scores → positive r."""
        scores = _scores({"A": 40, "B": 60, "C": 80, "D": 100})
        lengths = {"A": 100, "B": 200, "C": 300, "D": 400}

        r = length_bias_correlation(scores, lengths)
        assert r > 0.9

    def test_no_correlation(self):
        """When scores are constant, r is 0."""
        scores = _scores({"A": 50, "B": 50, "C": 50, "D": 50})
        lengths = {"A": 100, "B": 200, "C": 300, "D": 400}

        r = length_bias_correlation(scores, lengths)
        assert abs(r) < 0.01

    def test_too_few_pairs(self):
        scores = _scores({"A": 50})
        lengths = {"A": 100}

        r = length_bias_correlation(scores, lengths)
        assert r == 0.0

    def test_missing_names(self):
        """Names not in profile_lengths are ignored."""
        scores = _scores({"A": 50, "B": 80, "C": 60, "MISSING": 90})
        lengths = {"A": 100, "B": 200, "C": 300}

        r = length_bias_correlation(scores, lengths)
        assert isinstance(r, float)


# ---------------------------------------------------------------------------
# compare_runs
# ---------------------------------------------------------------------------

class TestCompareRuns:
    def test_identical_runs(self):
        rankings = _ranking(["A", "B", "C", "D", "E"])
        result = compare_runs(rankings, rankings, cutline=3)

        assert result["spearman_rho"] == 1.0
        assert result["new_accepts"] == []
        assert result["new_rejects"] == []
        assert result["total_common"] == 5

    def test_detects_boundary_changes(self):
        a = _ranking(["A", "B", "C", "D", "E"])
        # Swap C and D (boundary change at cutline=3)
        b = _ranking(["A", "B", "D", "C", "E"])

        result = compare_runs(a, b, cutline=3)

        assert "D" in result["new_accepts"]
        assert "C" in result["new_rejects"]

    def test_high_correlation_similar_runs(self):
        a = _ranking(["A", "B", "C", "D", "E"])
        b = _ranking(["A", "C", "B", "D", "E"])

        result = compare_runs(a, b, cutline=3)
        assert result["spearman_rho"] > 0.5

    def test_rank_changes_sorted_by_magnitude(self):
        names = [f"P{i}" for i in range(20)]
        a = _ranking(names)
        # Reverse a chunk near cutline
        b_names = names[:5] + list(reversed(names[5:15])) + names[15:]
        b = _ranking(b_names)

        result = compare_runs(a, b, cutline=10)

        changes = result["rank_changes"]
        if len(changes) >= 2:
            assert abs(changes[0]["delta"]) >= abs(changes[1]["delta"])

    def test_empty_rankings(self):
        result = compare_runs([], [], cutline=5)
        assert result["total_common"] == 0
        assert result["new_accepts"] == []
        assert result["new_rejects"] == []

    def test_one_empty_ranking(self):
        a = _ranking(["A", "B", "C"])
        result = compare_runs(a, [], cutline=2)
        assert result["total_common"] == 0
