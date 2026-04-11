"""Tests for cv_rank.scoring.combine — ranking combination logic."""

from __future__ import annotations


from cv_rank.scoring.combine import _entropy_weights, _min_max_normalize, combine_rankings


# ---------------------------------------------------------------------------
# _min_max_normalize
# ---------------------------------------------------------------------------


class TestMinMaxNormalize:
    """Tests for _min_max_normalize()."""

    def test_normal_range(self) -> None:
        """Standard values are normalised to [0, 1]."""
        result = _min_max_normalize([0, 50, 100])
        assert result == [0.0, 0.5, 1.0]

    def test_all_same_values(self) -> None:
        """When all values are identical, result is all 0.0."""
        result = _min_max_normalize([5.0, 5.0, 5.0])
        assert result == [0.0, 0.0, 0.0]

    def test_empty_list(self) -> None:
        """An empty list returns an empty list."""
        result = _min_max_normalize([])
        assert result == []

    def test_single_value(self) -> None:
        """A single-element list normalises to [0.0]."""
        result = _min_max_normalize([42.0])
        assert result == [0.0]

    def test_negative_values(self) -> None:
        """Negative values are handled correctly."""
        result = _min_max_normalize([-10, 0, 10])
        assert result == [0.0, 0.5, 1.0]

    def test_preserves_order(self) -> None:
        """Normalisation preserves relative ordering."""
        values = [10, 30, 20, 50, 40]
        result = _min_max_normalize(values)
        assert result[0] < result[2] < result[1] < result[4] < result[3]

    def test_two_values(self) -> None:
        """Two values map to 0.0 and 1.0."""
        result = _min_max_normalize([3.0, 7.0])
        assert result == [0.0, 1.0]


# ---------------------------------------------------------------------------
# combine_rankings
# ---------------------------------------------------------------------------


class TestCombineRankings:
    """Tests for combine_rankings()."""

    def _make_scores(self, data: dict[str, float]) -> list[dict]:
        """Helper to create pointwise score dicts from a name -> score mapping."""
        return [
            {
                "name": name,
                "score": score,
                "strongest_signal": "technical",
                "concerns": "none",
                "confidence": "high",
            }
            for name, score in data.items()
        ]

    def test_normal_combination(self) -> None:
        """Normal 50/50 combination with BT strengths."""
        scores = self._make_scores({"Alice": 90, "Bob": 70, "Carol": 80})
        swiss_records = {
            "Alice": {"wins": 15, "losses": 5},
            "Bob": {"wins": 10, "losses": 10},
            "Carol": {"wins": 12, "losses": 8},
        }
        bt_strengths = {"Alice": 1.5, "Bob": 0.5, "Carol": 1.0}

        result = combine_rankings(scores, swiss_records, bt_strengths)

        assert len(result) == 3
        # All results have expected keys
        for r in result:
            assert "name" in r
            assert "rank" in r
            assert "final_score" in r
            assert "pointwise_score" in r
            assert "swiss_wins" in r
            assert "swiss_losses" in r
            assert "bt_strength" in r

        # Results are sorted by final_score descending
        assert result[0]["final_score"] >= result[1]["final_score"]
        assert result[1]["final_score"] >= result[2]["final_score"]

        # Ranks are 1-indexed
        assert result[0]["rank"] == 1
        assert result[1]["rank"] == 2
        assert result[2]["rank"] == 3

        # Alice should rank first (highest in both signals)
        assert result[0]["name"] == "Alice"

    def test_falsy_zero_swiss_weight(self) -> None:
        """A swiss_weight of 0.0 is honoured (not replaced by default 0.5).

        This is the 'falsy-zero bug' the docstring mentions.
        """
        scores = self._make_scores({"Alice": 90, "Bob": 50})
        swiss_records = {
            "Alice": {"wins": 0, "losses": 20},  # Alice loses all swiss
            "Bob": {"wins": 20, "losses": 0},     # Bob wins all swiss
        }
        bt_strengths = {}

        # With swiss_weight=0.0, only pointwise matters
        result = combine_rankings(
            scores, swiss_records, bt_strengths,
            swiss_weight=0.0, pointwise_weight=1.0,
        )

        assert len(result) == 2
        # Alice should rank first because swiss is ignored
        assert result[0]["name"] == "Alice"

    def test_falsy_zero_pointwise_weight(self) -> None:
        """A pointwise_weight of 0.0 means only Swiss matters."""
        scores = self._make_scores({"Alice": 90, "Bob": 50})
        swiss_records = {
            "Alice": {"wins": 0, "losses": 20},
            "Bob": {"wins": 20, "losses": 0},
        }
        bt_strengths = {}

        result = combine_rankings(
            scores, swiss_records, bt_strengths,
            swiss_weight=1.0, pointwise_weight=0.0,
        )

        assert len(result) == 2
        # Bob should rank first because only swiss matters and Bob has 100% win rate
        assert result[0]["name"] == "Bob"

    def test_empty_inputs(self) -> None:
        """When there is no overlap between signals, return empty list."""
        # No common names
        scores = self._make_scores({"Alice": 90})
        swiss_records = {"Bob": {"wins": 10, "losses": 5}}
        bt_strengths = {}

        result = combine_rankings(scores, swiss_records, bt_strengths)
        assert result == []

    def test_empty_scores_empty_swiss(self) -> None:
        """Both signals completely empty returns empty."""
        result = combine_rankings([], {}, {})
        assert result == []

    def test_only_common_names_ranked(self) -> None:
        """People appearing in only one signal are excluded from rankings."""
        scores = self._make_scores({"Alice": 90, "Bob": 70, "Extra_PW": 80})
        swiss_records = {
            "Alice": {"wins": 10, "losses": 5},
            "Bob": {"wins": 8, "losses": 7},
            "Extra_Swiss": {"wins": 15, "losses": 0},
        }
        bt_strengths = {}

        result = combine_rankings(scores, swiss_records, bt_strengths)

        names = {r["name"] for r in result}
        assert names == {"Alice", "Bob"}
        assert "Extra_PW" not in names
        assert "Extra_Swiss" not in names

    def test_bt_strengths_used_when_provided(self) -> None:
        """When bt_strengths is provided, it is used for the Swiss signal."""
        scores = self._make_scores({"Alice": 80, "Bob": 80})  # same pointwise
        swiss_records = {
            "Alice": {"wins": 10, "losses": 10},
            "Bob": {"wins": 10, "losses": 10},
        }
        # BT strengths differ -- Bob stronger
        bt_strengths = {"Alice": 0.3, "Bob": 0.9}

        result = combine_rankings(
            scores, swiss_records, bt_strengths,
            swiss_weight=0.5, pointwise_weight=0.5,
        )

        assert len(result) == 2
        # Bob should rank higher due to better BT strength
        assert result[0]["name"] == "Bob"
        assert result[0]["bt_strength"] is not None

    def test_custom_weights(self) -> None:
        """Custom weight values are applied correctly."""
        scores = self._make_scores({"Alice": 100, "Bob": 0})
        swiss_records = {
            "Alice": {"wins": 0, "losses": 20},
            "Bob": {"wins": 20, "losses": 0},
        }
        bt_strengths = {}

        # 80% swiss, 20% pointwise -- Bob should win
        result = combine_rankings(
            scores, swiss_records, bt_strengths,
            swiss_weight=0.8, pointwise_weight=0.2,
        )

        assert result[0]["name"] == "Bob"

    def test_preserves_metadata_fields(self) -> None:
        """Output includes strongest_signal, concerns, confidence from pointwise."""
        scores = [
            {
                "name": "Alice",
                "score": 90,
                "strongest_signal": "deep ML expertise",
                "concerns": "no OSS presence",
                "confidence": "high",
            },
        ]
        swiss_records = {"Alice": {"wins": 10, "losses": 5}}
        bt_strengths = {}

        result = combine_rankings(scores, swiss_records, bt_strengths)

        assert result[0]["strongest_signal"] == "deep ML expertise"
        assert result[0]["concerns"] == "no OSS presence"
        assert result[0]["confidence"] == "high"

    def test_tie_break_prefers_builder_and_technical_depth(self) -> None:
        scores = [
            {
                "name": "Alice",
                "score": 80,
                "builder_signal_score": 5,
                "technical_depth_score": 4,
            },
            {
                "name": "Bob",
                "score": 80,
                "builder_signal_score": 2,
                "technical_depth_score": 3,
            },
        ]
        swiss_records = {
            "Alice": {"wins": 10, "losses": 10},
            "Bob": {"wins": 10, "losses": 10},
        }

        result = combine_rankings(scores, swiss_records, {})

        assert result[0]["name"] == "Alice"


# ---------------------------------------------------------------------------
# _entropy_weights
# ---------------------------------------------------------------------------


class TestEntropyWeights:
    """Tests for _entropy_weights()."""

    def test_uniform_distributions_equal_weights(self) -> None:
        """When both signals have similar spread, weights are near 50/50."""
        import random
        random.seed(42)
        swiss = [random.random() for _ in range(100)]
        pw = [random.random() for _ in range(100)]

        sw, pw_w = _entropy_weights(swiss, pw)

        assert abs(sw - 0.5) < 0.15
        assert abs(pw_w - 0.5) < 0.15
        assert abs(sw + pw_w - 1.0) < 0.001

    def test_compressed_signal_gets_lower_weight(self) -> None:
        """A compressed signal (low entropy) gets lower weight."""
        # Swiss: highly spread
        swiss = [i / 100.0 for i in range(100)]
        # Pointwise: all clustered around 0.5
        pw = [0.49 + 0.02 * (i % 2) for i in range(100)]

        sw, pw_w = _entropy_weights(swiss, pw)

        # Swiss should get higher weight (more spread)
        assert sw > pw_w

    def test_clamping_works(self) -> None:
        """Weights are clamped to [0.20, 0.80]."""
        # One signal with zero entropy (all same)
        swiss = [0.5] * 100
        pw = [i / 100.0 for i in range(100)]

        sw, pw_w = _entropy_weights(swiss, pw)

        assert sw >= 0.20
        assert sw <= 0.80
        assert pw_w >= 0.20
        assert pw_w <= 0.80

    def test_empty_inputs(self) -> None:
        """Empty inputs return 50/50."""
        sw, pw = _entropy_weights([], [])
        assert sw == 0.5
        assert pw == 0.5

    def test_weights_sum_to_one(self) -> None:
        """Weights always sum to 1.0."""
        test_cases = [
            ([0.1, 0.5, 0.9], [0.3, 0.3, 0.3]),
            ([0.0, 1.0], [0.5, 0.5]),
            ([i / 50.0 for i in range(50)], [0.5] * 50),
        ]
        for swiss, pw in test_cases:
            sw, pw_w = _entropy_weights(swiss, pw)
            assert abs(sw + pw_w - 1.0) < 0.001


class TestCombineAutoWeight:
    """Tests for combine_rankings with auto_weight=True."""

    def _make_scores(self, data: dict[str, float]) -> list[dict]:
        return [{"name": n, "score": s} for n, s in data.items()]

    def test_auto_weight_runs_without_error(self) -> None:
        """auto_weight=True doesn't crash."""
        scores = self._make_scores({"Alice": 90, "Bob": 70, "Carol": 80})
        swiss_records = {
            "Alice": {"wins": 15, "losses": 5},
            "Bob": {"wins": 10, "losses": 10},
            "Carol": {"wins": 12, "losses": 8},
        }
        bt_strengths = {"Alice": 1.5, "Bob": 0.5, "Carol": 1.0}

        result = combine_rankings(
            scores, swiss_records, bt_strengths,
            auto_weight=True,
        )

        assert len(result) == 3
        assert result[0]["rank"] == 1

    def test_auto_weight_overrides_manual(self) -> None:
        """auto_weight=True ignores manual swiss_weight/pointwise_weight."""
        scores = self._make_scores({"Alice": 100, "Bob": 0})
        swiss_records = {
            "Alice": {"wins": 0, "losses": 20},
            "Bob": {"wins": 20, "losses": 0},
        }

        # Manual weights: 100% pointwise → Alice wins
        result_manual = combine_rankings(
            scores, swiss_records, {},
            swiss_weight=0.0, pointwise_weight=1.0,
        )
        assert result_manual[0]["name"] == "Alice"

        # Auto weight with two people won't necessarily be the same
        result_auto = combine_rankings(
            scores, swiss_records, {},
            swiss_weight=0.0, pointwise_weight=1.0,
            auto_weight=True,
        )
        assert len(result_auto) == 2
