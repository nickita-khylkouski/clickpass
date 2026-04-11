"""Tests for incremental Swiss helper functions."""
from cv_rank.scoring.incremental import (
    _adaptive_match_budget,
    _seed_position,
)


class TestSeedPosition:
    """_seed_position should insert by pointwise score regardless of input order."""

    def test_basic_insertion(self):
        ranking = [
            {"name": "A", "pointwise_score": 90},
            {"name": "B", "pointwise_score": 80},
            {"name": "C", "pointwise_score": 70},
        ]
        # 85 slots between A(90) and B(80) → index 1
        assert _seed_position(85, ranking) == 1

    def test_unsorted_input(self):
        """Input ordered by combined rank (not pointwise) still seeds correctly."""
        ranking = [
            {"name": "B", "pointwise_score": 80},
            {"name": "A", "pointwise_score": 90},
            {"name": "C", "pointwise_score": 70},
        ]
        # Should sort internally: A(90), B(80), C(70) → 85 goes at index 1
        assert _seed_position(85, ranking) == 1

    def test_top_insertion(self):
        ranking = [
            {"name": "A", "pointwise_score": 50},
            {"name": "B", "pointwise_score": 30},
        ]
        assert _seed_position(99, ranking) == 0

    def test_bottom_insertion(self):
        ranking = [
            {"name": "A", "pointwise_score": 90},
            {"name": "B", "pointwise_score": 80},
        ]
        assert _seed_position(10, ranking) == 2

    def test_empty_ranking(self):
        assert _seed_position(50, []) == 0

    def test_with_existing_pw_map(self):
        """When existing_pw_map is provided, uses those scores over dict field."""
        ranking = [
            {"name": "A", "pointwise_score": 50},
            {"name": "B", "pointwise_score": 90},
            {"name": "C", "pointwise_score": 70},
        ]
        # pw_map overrides: A=95, B=70, C=10
        pw_map = {"A": 95, "B": 70, "C": 10}
        # Sorted by pw_map: A(95), B(70), C(10) → 80 goes at index 1
        assert _seed_position(80, ranking, pw_map) == 1


class TestAdaptiveMatchBudget:
    """_adaptive_match_budget should give more matches near the cutline."""

    def test_at_cutline(self):
        # est_rank=199, accept_count=200 → cutline_idx=199, distance=0
        budget = _adaptive_match_budget(est_rank=199, accept_count=200)
        assert budget == 7  # max budget

    def test_near_cutline(self):
        # est_rank=195, accept_count=200 → cutline_idx=199, distance=4
        budget = _adaptive_match_budget(est_rank=195, accept_count=200)
        assert budget == 7

    def test_moderate_distance(self):
        # est_rank=180, accept_count=200 → cutline_idx=199, distance=19
        budget = _adaptive_match_budget(est_rank=180, accept_count=200)
        assert budget == 5  # base

    def test_far_from_cutline(self):
        # est_rank=260, accept_count=200 → cutline_idx=199, distance=61
        budget = _adaptive_match_budget(est_rank=260, accept_count=200)
        assert budget == 2  # minimum

    def test_somewhat_far(self):
        # est_rank=230, accept_count=200 → cutline_idx=199, distance=31
        budget = _adaptive_match_budget(est_rank=230, accept_count=200)
        assert budget == 3

    def test_accept_count_one(self):
        # accept_count=1 → cutline_idx=0
        budget = _adaptive_match_budget(est_rank=0, accept_count=1)
        assert budget == 7  # distance=0, at cutline
