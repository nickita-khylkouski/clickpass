"""Tests for cv_rank.scoring.bradley_terry — BT strength computation."""

from cv_rank.scoring.bradley_terry import compute_bt_strengths


class TestComputeBtStrengths:
    def test_empty_comparisons(self):
        result = compute_bt_strengths(["Alice", "Bob"], [])
        assert result == {}

    def test_simple_two_player(self):
        # Alice always beats Bob
        names = ["Alice", "Bob"]
        comparisons = [(0, 1), (0, 1), (0, 1)]
        result = compute_bt_strengths(names, comparisons)

        assert "Alice" in result
        assert "Bob" in result
        # Alice should be stronger
        assert result["Alice"] > result["Bob"]
        # Values should be in [0, 1]
        assert 0.0 <= result["Alice"] <= 1.0
        assert 0.0 <= result["Bob"] <= 1.0
        # Min-max normalised: one should be 0, other should be 1
        assert result["Alice"] == 1.0
        assert result["Bob"] == 0.0

    def test_three_player_transitive(self):
        # A > B > C
        names = ["A", "B", "C"]
        comparisons = [
            (0, 1),  # A beats B
            (0, 2),  # A beats C
            (1, 2),  # B beats C
        ]
        result = compute_bt_strengths(names, comparisons)

        assert result["A"] > result["B"]
        assert result["B"] > result["C"]
        assert result["A"] == 1.0
        assert result["C"] == 0.0

    def test_symmetric_comparisons(self):
        # A beats B once, B beats A once → similar strengths
        names = ["A", "B"]
        comparisons = [(0, 1), (1, 0)]
        result = compute_bt_strengths(names, comparisons)

        # With symmetric wins, strengths should be very close
        diff = abs(result["A"] - result["B"])
        assert diff < 0.1  # approximately equal

    def test_all_values_in_unit_range(self):
        names = ["A", "B", "C", "D"]
        comparisons = [
            (0, 1), (0, 2), (0, 3),
            (1, 2), (1, 3),
            (2, 3),
        ]
        result = compute_bt_strengths(names, comparisons)

        for name in names:
            assert 0.0 <= result[name] <= 1.0

    def test_choix_unavailable_returns_empty(self):
        """When choix is not installed, should return empty dict."""
        import sys
        from unittest import mock

        # Setting sys.modules["choix"] = None makes `import choix` raise
        # ImportError. The import happens inside the function body on each
        # call, so no module reload is needed.
        with mock.patch.dict(sys.modules, {"choix": None}):
            result = compute_bt_strengths(["A", "B"], [(0, 1)])
        assert result == {}, f"Expected empty dict when choix unavailable, got {result}"
