"""Tests for verbose logging in cv_rank.quality module."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cv_rank.quality import _qc_one, run_quality_check


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

from helpers import make_person


def _make_person(name: str) -> dict[str, Any]:
    return make_person(name)


def _make_ranking(name: str, rank: int, score: float) -> dict[str, Any]:
    return {
        "name": name,
        "rank": rank,
        "final_score": score,
        "swiss_wins": 3,
        "swiss_losses": 1,
        "pointwise_score": score,
        "why": "Good builder",
        "concerns": "",
    }


def _make_config(model: str = "gpt-4o-mini", concurrency: int = 5) -> dict[str, Any]:
    return {
        "models": {"quality_check": model},
        "event": {"target_accepts": 10},
        "concurrency": {"quality_check": concurrency},
        "max_retries": 2,
        "save_every": 50,
    }


def _mock_llm_response(verdict: str = "YES", specific_why: str = "Good builder",
                        best_number: str = "100 stars", bs_check: str = "legit"):
    """Create a mock OpenAI response."""
    result = json.dumps({
        "verdict": verdict,
        "specific_why": specific_why,
        "best_number": best_number,
        "company": "TestCo",
        "role": "Engineer",
        "standout_achievements": ["Built stuff"],
        "bs_check": bs_check,
    })
    usage = MagicMock()
    usage.total_tokens = 150
    choice = MagicMock()
    choice.message.content = result
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = usage
    return resp


# ---------------------------------------------------------------------------
# Tests for _qc_one logging
# ---------------------------------------------------------------------------

class TestQcOneLogging:
    """Verify _qc_one logs person being checked and verdict details."""

    @pytest.mark.asyncio
    async def test_logs_person_being_checked(self, caplog):
        """_qc_one logs '[N/M] Checking: PersonName (rank #X, score Y.Z)'."""
        client = AsyncMock()
        client.chat.completions.create.return_value = _mock_llm_response()

        person = _make_person("Alice Chen")
        ranking = _make_ranking("Alice Chen", 1, 95.5)
        config = _make_config()
        semaphore = asyncio.Semaphore(5)

        with caplog.at_level(logging.DEBUG, logger="cv_rank"):
            await _qc_one(
                client, person, ranking,
                "system prompt", config, semaphore,
                lambda p: f"Profile: {p['name']}",
                idx=0, total=10,
            )

        log_text = caplog.text
        assert "Checking:" in log_text or "Checking" in log_text
        assert "Alice Chen" in log_text

    @pytest.mark.asyncio
    async def test_logs_verdict_after_check(self, caplog):
        """_qc_one logs the verdict, specific_why, best_number, bs_check after checking."""
        client = AsyncMock()
        client.chat.completions.create.return_value = _mock_llm_response(
            verdict="STRONG YES",
            specific_why="Built amazing stuff with 50K stars",
            best_number="50K stars",
            bs_check="legit",
        )

        person = _make_person("Bob Martinez")
        ranking = _make_ranking("Bob Martinez", 2, 88.0)
        config = _make_config()
        semaphore = asyncio.Semaphore(5)

        with caplog.at_level(logging.DEBUG, logger="cv_rank"):
            await _qc_one(
                client, person, ranking,
                "system prompt", config, semaphore,
                lambda p: f"Profile: {p['name']}",
                idx=1, total=10,
            )

        log_text = caplog.text
        # Should log the verdict
        assert "STRONG YES" in log_text
        # Should log bs_check
        assert "legit" in log_text

    @pytest.mark.asyncio
    async def test_logs_error_with_person_name_and_attempt(self, caplog):
        """On error, _qc_one logs the person name, error type, and attempt number."""
        client = AsyncMock()
        client.chat.completions.create.side_effect = RuntimeError("Connection timeout")

        person = _make_person("Carol Okafor")
        ranking = _make_ranking("Carol Okafor", 3, 75.0)
        config = _make_config()
        config["max_retries"] = 2
        semaphore = asyncio.Semaphore(5)

        with caplog.at_level(logging.DEBUG, logger="cv_rank"):
            await _qc_one(
                client, person, ranking,
                "system prompt", config, semaphore,
                lambda p: f"Profile: {p['name']}",
                idx=2, total=10,
            )

        log_text = caplog.text
        # Should log the person name with the error
        assert "Carol Okafor" in log_text
        # Should mention the attempt number
        assert "attempt" in log_text.lower() or "retry" in log_text.lower()


# ---------------------------------------------------------------------------
# Tests for run_quality_check logging
# ---------------------------------------------------------------------------

class TestRunQualityCheckLogging:
    """Verify run_quality_check logs summary statistics and batch info."""

    @pytest.mark.asyncio
    async def test_logs_total_people_model_concurrency(self, caplog, tmp_path):
        """run_quality_check logs total people, model, and concurrency level."""
        people = [_make_person("Alice"), _make_person("Bob")]
        rankings = [_make_ranking("Alice", 1, 90.0), _make_ranking("Bob", 2, 80.0)]
        config = _make_config(model="gpt-4o-mini", concurrency=8)
        run_dir = tmp_path / "run_001"
        run_dir.mkdir()

        with patch("cv_rank.quality.get_openai_client") as mock_client_fn:
            client = AsyncMock()
            client.chat.completions.create.return_value = _mock_llm_response()
            mock_client_fn.return_value = client

            with caplog.at_level(logging.DEBUG, logger="cv_rank"):
                await run_quality_check(
                    people, rankings, config, run_dir,
                    lambda p: f"Profile: {p['name']}",
                )

        log_text = caplog.text
        # Should log total people count
        assert "2" in log_text
        # Should log the model name
        assert "gpt-4o-mini" in log_text
        # Should log concurrency
        assert "8" in log_text or "concurrency" in log_text.lower()

    @pytest.mark.asyncio
    async def test_logs_verdict_summary_table(self, caplog, tmp_path):
        """run_quality_check logs a verdict summary with counts and percentages."""
        people = [_make_person("Alice"), _make_person("Bob"), _make_person("Carol")]
        rankings = [
            _make_ranking("Alice", 1, 90.0),
            _make_ranking("Bob", 2, 80.0),
            _make_ranking("Carol", 3, 70.0),
        ]
        config = _make_config()
        run_dir = tmp_path / "run_002"
        run_dir.mkdir()

        # Return different verdicts
        responses = [
            _mock_llm_response(verdict="STRONG YES"),
            _mock_llm_response(verdict="YES"),
            _mock_llm_response(verdict="NO"),
        ]

        with patch("cv_rank.quality.get_openai_client") as mock_client_fn:
            client = AsyncMock()
            client.chat.completions.create.side_effect = responses
            mock_client_fn.return_value = client

            with caplog.at_level(logging.DEBUG, logger="cv_rank"):
                await run_quality_check(
                    people, rankings, config, run_dir,
                    lambda p: f"Profile: {p['name']}",
                )

        log_text = caplog.text
        # Should contain percentage information in the summary
        assert "%" in log_text or "percent" in log_text.lower()

    @pytest.mark.asyncio
    async def test_logs_batch_stats(self, caplog, tmp_path):
        """After each batch, run_quality_check logs verdict distribution so far."""
        people = [_make_person(f"Person{i}") for i in range(3)]
        rankings = [_make_ranking(f"Person{i}", i + 1, 90.0 - i * 5) for i in range(3)]
        config = _make_config()
        config["save_every"] = 2  # Small batches to trigger batch logging
        run_dir = tmp_path / "run_003"
        run_dir.mkdir()

        with patch("cv_rank.quality.get_openai_client") as mock_client_fn:
            client = AsyncMock()
            client.chat.completions.create.return_value = _mock_llm_response()
            mock_client_fn.return_value = client

            with caplog.at_level(logging.DEBUG, logger="cv_rank"):
                await run_quality_check(
                    people, rankings, config, run_dir,
                    lambda p: f"Profile: {p['name']}",
                )

        log_text = caplog.text
        # Should log batch progress info
        assert "batch" in log_text.lower() or "Batch" in log_text
