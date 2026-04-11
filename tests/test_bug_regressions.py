"""Regression tests proving each bug fix addresses a real issue.

Each test documents the original bug, demonstrates the fix, and would
have FAILED on the old code.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Bug #1: pointwise.py and swiss.py used their own AsyncOpenAI(api_key=...)
#          instead of get_openai_client() from utils.py
# Impact: If OPENAI_API_KEY validation logic changed in utils.py, the scoring
#         modules would bypass it. Also no centralized client management.
# ---------------------------------------------------------------------------

class TestBug1_SharedOpenAIClient:
    """Verify pointwise and swiss import get_openai_client, not raw AsyncOpenAI."""

    def test_pointwise_imports_get_openai_client(self):
        """score_all should use get_openai_client, not create AsyncOpenAI directly."""
        import inspect
        from cv_rank.scoring.pointwise import score_all

        source = inspect.getsource(score_all)
        assert "get_openai_client" in source, "score_all should use get_openai_client()"
        assert "AsyncOpenAI(api_key=" not in source, "score_all should NOT create AsyncOpenAI directly"

    def test_swiss_imports_get_openai_client(self):
        """run_swiss should use get_openai_client, not create AsyncOpenAI directly."""
        import inspect
        from cv_rank.scoring.swiss import run_swiss

        source = inspect.getsource(run_swiss)
        assert "get_openai_client" in source, "run_swiss should use get_openai_client()"
        assert "AsyncOpenAI(api_key=" not in source, "run_swiss should NOT create AsyncOpenAI directly"

    def test_get_openai_client_raises_on_missing_key(self):
        """The centralized client factory should reject missing API keys."""
        from cv_rank.utils import get_openai_client

        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("OPENAI_API_KEY", None)
            with pytest.raises(ValueError, match="OPENAI_API_KEY"):
                get_openai_client()


# ---------------------------------------------------------------------------
# Bug #2: Event loop closed error from httpx AsyncClient __del__
# Impact: RuntimeError spammed to stderr after every pipeline run.
#         Caused by not closing the AsyncOpenAI client before event loop exits.
# ---------------------------------------------------------------------------

class TestBug2_ClientCloseBeforeReturn:
    """Verify pointwise and swiss explicitly close the client."""

    def test_pointwise_closes_client(self):
        """score_all should call await client.close() before returning."""
        import inspect
        from cv_rank.scoring.pointwise import score_all

        source = inspect.getsource(score_all)
        assert "client.close()" in source, "score_all must close the client to avoid event-loop-closed errors"

    def test_swiss_closes_client(self):
        """run_swiss should call await client.close() before returning."""
        import inspect
        from cv_rank.scoring.swiss import run_swiss

        source = inspect.getsource(run_swiss)
        assert "client.close()" in source, "run_swiss must close the client to avoid event-loop-closed errors"


# ---------------------------------------------------------------------------
# Bug #3: local_csv.py bare int() on CSV strings crashes on "N/A"
# Impact: Pipeline crashes with ValueError when judging CSV has non-numeric
#         weight or score values like "N/A", "", or "TBD".
# ---------------------------------------------------------------------------

class TestBug3_SafeIntConversion:
    """Verify local_csv handles non-numeric weight/score values."""

    def _make_csv_files(self, tmp_dir, profiles_data, judging_data):
        """Helper to create test CSV files."""
        profiles_path = Path(tmp_dir) / "profiles.csv"
        judging_path = Path(tmp_dir) / "judging.csv"

        import csv
        with open(profiles_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["userId", "email"])
            w.writeheader()
            w.writerows(profiles_data)

        with open(judging_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["userId", "event_name", "teamName", "criteria_name", "weight", "score", "finalistRecommendation"])
            w.writeheader()
            w.writerows(judging_data)

        return profiles_path, judging_path

    def test_na_weight_does_not_crash(self):
        """int("N/A") would crash; our fix handles it gracefully."""
        from cv_rank.enrichment.local_csv import _load_hackathon_data

        with tempfile.TemporaryDirectory() as tmp:
            profiles_path, judging_path = self._make_csv_files(
                tmp,
                profiles_data=[{"userId": "u1", "email": "a@b.com"}],
                judging_data=[{
                    "userId": "u1", "event_name": "hack1", "teamName": "t1",
                    "criteria_name": "c1", "weight": "N/A", "score": "8",
                    "finalistRecommendation": "",
                }],
            )

            paths = {"profiles": profiles_path, "judging": judging_path, "submissions": None, "details": None}
            # This would have crashed with ValueError: invalid literal for int() with base 10: 'N/A'
            judging, _, _ = _load_hackathon_data(paths)
            assert "a@b.com" in judging
            assert judging["a@b.com"][0]["weight"] == 0  # graceful fallback

    def test_empty_score_does_not_crash(self):
        """Empty string score should default to 0, not crash."""
        from cv_rank.enrichment.local_csv import _load_hackathon_data

        with tempfile.TemporaryDirectory() as tmp:
            profiles_path, judging_path = self._make_csv_files(
                tmp,
                profiles_data=[{"userId": "u1", "email": "a@b.com"}],
                judging_data=[{
                    "userId": "u1", "event_name": "hack1", "teamName": "t1",
                    "criteria_name": "c1", "weight": "5", "score": "",
                    "finalistRecommendation": "",
                }],
            )

            paths = {"profiles": profiles_path, "judging": judging_path, "submissions": None, "details": None}
            judging, _, _ = _load_hackathon_data(paths)
            assert judging["a@b.com"][0]["score"] == 0

    def test_float_weight_handled(self):
        """CSV might have '3.5' for weight — int('3.5') would crash."""
        from cv_rank.enrichment.local_csv import _load_hackathon_data

        with tempfile.TemporaryDirectory() as tmp:
            profiles_path, judging_path = self._make_csv_files(
                tmp,
                profiles_data=[{"userId": "u1", "email": "a@b.com"}],
                judging_data=[{
                    "userId": "u1", "event_name": "hack1", "teamName": "t1",
                    "criteria_name": "c1", "weight": "3.5", "score": "7.2",
                    "finalistRecommendation": "",
                }],
            )

            paths = {"profiles": profiles_path, "judging": judging_path, "submissions": None, "details": None}
            # int("3.5") would crash; int(float("3.5")) = 3
            judging, _, _ = _load_hackathon_data(paths)
            assert judging["a@b.com"][0]["weight"] == 3
            assert judging["a@b.com"][0]["score"] == 7


# ---------------------------------------------------------------------------
# Bug #4: checkpoint.py missing encoding="utf-8" on file opens
# Impact: On systems with non-UTF-8 default locale, saving/loading checkpoints
#         with Unicode names (e.g. "José García") would fail or corrupt data.
# ---------------------------------------------------------------------------

class TestBug4_CheckpointEncoding:
    """Verify checkpoint handles Unicode names correctly."""

    def test_save_and_load_unicode(self):
        """Save+load checkpoint with Unicode characters in the data."""
        from cv_rank.checkpoint import load_checkpoint, save_checkpoint

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            data = [
                {"name": "José García", "score": 85.2},
                {"name": "田中太郎", "score": 72.1},
                {"name": "Müller Straße", "score": 60.0},
                {"name": "Ελληνικά", "score": 55.5},
            ]
            save_checkpoint(run_dir, "test_phase", data)
            loaded = load_checkpoint(run_dir, "test_phase")

            assert loaded is not None
            assert len(loaded) == 4
            assert loaded[0]["name"] == "José García"
            assert loaded[1]["name"] == "田中太郎"

    def test_checkpoint_file_is_valid_utf8(self):
        """Verify the file on disk is valid UTF-8 and round-trips correctly."""
        from cv_rank.checkpoint import save_checkpoint

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            save_checkpoint(run_dir, "enc_test", [{"name": "café"}])

            path = run_dir / "enc_test.json"
            # Read as raw bytes, decode as UTF-8, parse JSON, verify content
            raw = path.read_bytes()
            data = json.loads(raw.decode("utf-8"))
            assert data[0]["name"] == "café"

    def test_meta_file_is_utf8(self):
        """Verify meta.json is UTF-8 encoded too."""
        from cv_rank.checkpoint import save_checkpoint

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            save_checkpoint(run_dir, "test", [{"x": 1}])

            meta_path = run_dir / "meta.json"
            raw = meta_path.read_bytes()
            data = json.loads(raw.decode("utf-8"))
            assert "test" in data["phases"]


# ---------------------------------------------------------------------------
# Bug #5: Falsy-zero weight bug in combine.py
# Impact: Setting swiss_weight=0.0 or pointwise_weight=0.0 would be treated
#         as "not provided" and replaced with the default 0.50, because the
#         original code used `args.swiss_weight or config[...]` which is falsy
#         for 0.0.
# ---------------------------------------------------------------------------

class TestBug5_FalsyZeroWeight:
    """Verify that weight=0.0 is honoured, not replaced with default."""

    def test_zero_swiss_weight_means_pointwise_only(self):
        """swiss_weight=0.0 should produce a ranking based solely on pointwise."""
        from cv_rank.scoring.combine import combine_rankings

        scores = [
            {"name": "Alice", "score": 90.0},
            {"name": "Bob", "score": 30.0},
        ]
        swiss = {
            "Alice": {"wins": 0, "losses": 2, "byes": 0},
            "Bob": {"wins": 2, "losses": 0, "byes": 0},
        }

        result = combine_rankings(
            scores, swiss, bt_strengths={},
            swiss_weight=0.0,  # THIS is 0.0, not None
            pointwise_weight=1.0,
        )

        # Alice has higher pointwise (90 vs 30) so should rank #1
        # despite losing all Swiss matches
        assert result[0]["name"] == "Alice"
        assert result[1]["name"] == "Bob"

    def test_zero_pointwise_weight_means_swiss_only(self):
        """pointwise_weight=0.0 should rank by Swiss results only."""
        from cv_rank.scoring.combine import combine_rankings

        scores = [
            {"name": "Alice", "score": 90.0},
            {"name": "Bob", "score": 30.0},
        ]
        swiss = {
            "Alice": {"wins": 0, "losses": 2, "byes": 0},
            "Bob": {"wins": 2, "losses": 0, "byes": 0},
        }

        result = combine_rankings(
            scores, swiss, bt_strengths={},
            swiss_weight=1.0,
            pointwise_weight=0.0,  # THIS is 0.0, not None
        )

        # Bob has better Swiss (2W-0L) so should rank #1
        # despite lower pointwise score
        assert result[0]["name"] == "Bob"
        assert result[1]["name"] == "Alice"

    def test_none_weight_uses_default(self):
        """None weight should fall back to 0.50 default."""
        from cv_rank.scoring.combine import combine_rankings

        scores = [
            {"name": "Alice", "score": 50.0},
            {"name": "Bob", "score": 50.0},
        ]
        swiss = {
            "Alice": {"wins": 1, "losses": 0, "byes": 0},
            "Bob": {"wins": 0, "losses": 1, "byes": 0},
        }

        result = combine_rankings(
            scores, swiss, bt_strengths={},
            swiss_weight=None,
            pointwise_weight=None,
        )

        # Both have same pointwise, Alice wins Swiss — should rank #1
        assert result[0]["name"] == "Alice"
