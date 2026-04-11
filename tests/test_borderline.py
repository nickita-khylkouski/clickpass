"""Tests for cv_rank.scoring.borderline — borderline re-evaluation."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from cv_rank.scoring.borderline import identify_borderline_candidates


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ranking(names: list[str]) -> list[dict]:
    return [{"name": n, "rank": i + 1} for i, n in enumerate(names)]


def _scores(data: dict[str, float]) -> list[dict]:
    return [{"name": n, "score": s} for n, s in data.items()]


def _records(*entries: tuple[str, int, int]) -> dict[str, dict]:
    return {n: {"wins": w, "losses": l} for n, w, l in entries}


# ---------------------------------------------------------------------------
# identify_borderline_candidates
# ---------------------------------------------------------------------------

class TestIdentifyBorderlineCandidates:
    def test_correct_band_selection(self):
        """Selects candidates within band_pct of cutline."""
        names = [f"P{i:02d}" for i in range(100)]
        rankings = _ranking(names)

        result = identify_borderline_candidates(rankings, cutline=50, band_pct=0.15)

        # With 100 people and 15% band, band_size=15
        # low=35, high=65 → ~31 candidates
        assert len(result) > 20
        assert len(result) < 50

        # All returned names should be near cutline
        rank_map = {r["name"]: r["rank"] for r in rankings}
        for name in result:
            rank = rank_map[name]
            assert 30 <= rank <= 70  # generous bounds

    def test_empty_rankings(self):
        assert identify_borderline_candidates([], cutline=50) == []

    def test_small_group(self):
        rankings = _ranking(["A", "B", "C"])
        result = identify_borderline_candidates(rankings, cutline=2, band_pct=0.15)
        assert len(result) > 0

    def test_includes_disagreement_candidates(self):
        """Candidates with signal disagreement outside the band are added."""
        names = [f"P{i:02d}" for i in range(20)]
        rankings = _ranking(names)

        # P00 is rank 1 (well above cutline=10), but has huge disagreement
        pw_scores = _scores({
            **{f"P{i:02d}": 50.0 for i in range(20)},
            "P00": 10.0,  # Very low pointwise
        })
        swiss_rec = _records(
            *[(f"P{i:02d}", 5, 5) for i in range(20)],
        )
        # Override P00 to have high wins
        swiss_rec["P00"] = {"wins": 15, "losses": 0}

        result = identify_borderline_candidates(
            rankings, cutline=10, band_pct=0.15,
            pointwise_scores=pw_scores,
            swiss_records=swiss_rec,
        )

        # P00 should be included due to disagreement even though rank 1
        assert "P00" in result

    def test_band_pct_controls_size(self):
        names = [f"P{i:02d}" for i in range(100)]
        rankings = _ranking(names)

        small = identify_borderline_candidates(rankings, cutline=50, band_pct=0.05)
        large = identify_borderline_candidates(rankings, cutline=50, band_pct=0.25)

        assert len(small) < len(large)

    def test_cutline_at_edge(self):
        """Cutline at the start or end doesn't crash."""
        names = [f"P{i}" for i in range(10)]
        rankings = _ranking(names)

        result_low = identify_borderline_candidates(rankings, cutline=1, band_pct=0.15)
        result_high = identify_borderline_candidates(rankings, cutline=10, band_pct=0.15)

        assert isinstance(result_low, list)
        assert isinstance(result_high, list)

    def test_returns_sorted_names(self):
        names = ["Zara", "Alice", "Bob", "Charlie", "David"]
        rankings = _ranking(names)

        result = identify_borderline_candidates(rankings, cutline=3, band_pct=0.5)
        assert result == sorted(result)


# ---------------------------------------------------------------------------
# run_borderline_reeval
# ---------------------------------------------------------------------------

class TestRunBorderlineReeval:
    """Tests for the async run_borderline_reeval orchestrator."""

    @staticmethod
    def _config(target_accepts=10, band_pct=0.15, extra_rounds=3):
        return {
            "event": {"target_accepts": target_accepts},
            "borderline": {"enabled": True, "band_pct": band_pct, "extra_rounds": extra_rounds},
            "models": {"swiss": "gpt-test", "borderline": "gpt-test"},
            "concurrency": {"swiss": 5},
            "swiss": {"inject_pointwise": False},
            "max_retries": 1,
        }

    @staticmethod
    def _people(n=20):
        return [{"name": f"P{i:02d}", "email": f"p{i}@x.com"} for i in range(n)]

    @pytest.mark.asyncio
    async def test_checkpoint_resume(self, tmp_path):
        """Loads results from existing checkpoint file."""
        from cv_rank.scoring.borderline import run_borderline_reeval

        # Create a fake checkpoint
        checkpoint = {
            "records": {"P05": {"wins": 3, "losses": 1}},
            "bt_strengths": {"P05": 0.8},
            "matches": [{"winner": "P05", "loser": "P06"}],
        }
        (tmp_path / "borderline_checkpoint.json").write_text(
            __import__("json").dumps(checkpoint)
        )

        records, bt, matches = await run_borderline_reeval(
            self._people(), _ranking([f"P{i:02d}" for i in range(20)]),
            {"technical_depth": 0.5}, self._config(), tmp_path,
            format_profile_fn=lambda p: f"Name: {p['name']}",
        )

        assert records == {"P05": {"wins": 3, "losses": 1}}
        assert bt == {"P05": 0.8}
        assert len(matches) == 1

    @pytest.mark.asyncio
    async def test_corrupt_checkpoint_reruns(self, tmp_path):
        """Corrupt checkpoint triggers re-run instead of crashing."""
        from cv_rank.scoring.borderline import run_borderline_reeval

        (tmp_path / "borderline_checkpoint.json").write_text("{{bad json")

        # Mock run_swiss so we don't need real API
        mock_result = (
            {"P05": {"wins": 2, "losses": 1}},
            {"P05": 0.7},
            [{"winner": "P05", "loser": "P06"}],
        )
        with patch("cv_rank.scoring.swiss.run_swiss", new_callable=AsyncMock, return_value=mock_result):
            records, bt, matches = await run_borderline_reeval(
                self._people(), _ranking([f"P{i:02d}" for i in range(20)]),
                {"technical_depth": 0.5}, self._config(), tmp_path,
                format_profile_fn=lambda p: f"Name: {p['name']}",
            )

        assert records == {"P05": {"wins": 2, "losses": 1}}

    @pytest.mark.asyncio
    async def test_too_few_candidates_skips(self, tmp_path):
        """Fewer than 4 borderline candidates → skip with empty results."""
        from cv_rank.scoring.borderline import run_borderline_reeval

        # Only 3 people total — fewer than 4 borderline candidates possible
        people = [{"name": "A"}, {"name": "B"}, {"name": "C"}]
        rankings = _ranking(["A", "B", "C"])

        records, bt, matches = await run_borderline_reeval(
            people, rankings, {"technical_depth": 0.5},
            self._config(target_accepts=2), tmp_path,
            format_profile_fn=lambda p: f"Name: {p['name']}",
        )

        assert records == {}
        assert bt == {}
        assert matches == []

    @pytest.mark.asyncio
    async def test_proportional_accept_scaling(self, tmp_path):
        """borderline_accept is scaled proportionally to the subset size."""
        from cv_rank.scoring.borderline import run_borderline_reeval

        # 100 people, accept 50, 10% band → ~20 borderline candidates
        people = self._people(100)
        rankings = _ranking([f"P{i:02d}" for i in range(100)])
        config = self._config(target_accepts=50, band_pct=0.10)

        captured_accept = {}

        async def fake_run_swiss(people, criteria, model, rounds, accept_count,
                                 config, run_dir, format_profile_fn, **kwargs):
            captured_accept["value"] = accept_count
            return {}, {}, []

        with patch("cv_rank.scoring.swiss.run_swiss", side_effect=fake_run_swiss):
            await run_borderline_reeval(
                people, rankings, {"technical_depth": 0.5},
                config, tmp_path,
                format_profile_fn=lambda p: f"Name: {p['name']}",
            )

        # accept_count should be scaled: cutline * borderline_count / total
        # ~50 * 21/100 = ~10, much less than the full 50
        assert "value" in captured_accept
        assert captured_accept["value"] < 50  # scaled down from the full cutline

    @pytest.mark.asyncio
    async def test_saves_checkpoint_after_run(self, tmp_path):
        """Checkpoint file is written after successful run."""
        from cv_rank.scoring.borderline import run_borderline_reeval
        import json

        mock_result = (
            {"P05": {"wins": 2, "losses": 1}},
            {"P05": 0.7},
            [{"winner": "P05", "loser": "P06", "tokens": 100}],
        )
        with patch("cv_rank.scoring.swiss.run_swiss", new_callable=AsyncMock, return_value=mock_result):
            await run_borderline_reeval(
                self._people(), _ranking([f"P{i:02d}" for i in range(20)]),
                {"technical_depth": 0.5}, self._config(), tmp_path,
                format_profile_fn=lambda p: f"Name: {p['name']}",
            )

        cp_path = tmp_path / "borderline_checkpoint.json"
        assert cp_path.exists()
        data = json.loads(cp_path.read_text())
        assert "records" in data
        assert "bt_strengths" in data
        assert data["total_tokens"] == 100
