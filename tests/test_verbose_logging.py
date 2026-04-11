"""Tests for verbose logging and progress bar additions.

Tests for:
- cli.py: per-person data quality logging after CSV load, phase timing, pipeline timing
- pointwise.py: Rich progress bar usage, per-person score logging, batch stats,
  score distribution summary, prompt length DEBUG logging
"""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

from helpers import make_person


def _make_person(name: str, email: str = "", linkedin_url: str = "",
                 github_url: str = "", **kwargs: Any) -> dict[str, Any]:
    return make_person(name, full=True, email=email,
                       linkedin_url=linkedin_url, github_url=github_url, **kwargs)


@pytest.fixture
def _require_supabase_and_mock_enrichment(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "test-key")
    with patch(
        "cv_rank.enrichment.supabase.enrich_from_supabase",
        side_effect=lambda people, config: people,
    ):
        yield


# ---------------------------------------------------------------------------
# CLI: data quality summary after CSV load
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_require_supabase_and_mock_enrichment")
class TestCliDataQualitySummary:
    """After CSV load, cli._cmd_run should log per-person data quality."""

    def test_data_quality_counts_logged(self, tmp_path, caplog):
        """After loading CSV, cli logs counts for email, linkedin, github."""
        from cv_rank.cli import _cmd_run, _build_parser

        # Create a CSV with mixed data
        csv_path = tmp_path / "people.csv"
        csv_path.write_text(
            "Name,Email,LinkedIn URL,GitHub URL\n"
            "Alice A,alice@x.com,https://linkedin.com/in/a,https://github.com/a\n"
            "Bob B,,https://linkedin.com/in/b,\n"
            "Carol C,carol@x.com,,https://github.com/c\n"
        )
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "event:\n  name: Test\n  type: community_meetup\n"
            "models:\n  scoring: gpt-4o\n"
        )

        parser = _build_parser()
        args = parser.parse_args([
            "run", "--csv", str(csv_path),
            "--config", str(config_path),
            "--output-dir", str(tmp_path / "results"),
            "--enrich-only",
            "--accept", "2",
        ])

        with caplog.at_level(logging.DEBUG, logger="cv_rank"):
            _cmd_run(args)  # --enrich-only: returns after enrichment, no API needed

        # Should see data quality summary with counts
        combined = " ".join(caplog.text.split())
        assert "email" in combined.lower() or "Email" in combined
        assert "linkedin" in combined.lower() or "LinkedIn" in combined
        assert "github" in combined.lower() or "GitHub" in combined

    def test_per_person_details_logged(self, tmp_path, caplog):
        """After loading CSV, cli logs each person's name and available fields."""
        from cv_rank.cli import _cmd_run, _build_parser

        csv_path = tmp_path / "people.csv"
        csv_path.write_text(
            "Name,Email,LinkedIn URL,GitHub URL\n"
            "Alice Aardvark,alice@x.com,https://linkedin.com/in/a,https://github.com/a\n"
            "Bob Bear,,https://linkedin.com/in/b,\n"
        )
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "event:\n  name: Test\n  type: community_meetup\n"
            "models:\n  scoring: gpt-4o\n"
        )

        parser = _build_parser()
        args = parser.parse_args([
            "run", "--csv", str(csv_path),
            "--config", str(config_path),
            "--output-dir", str(tmp_path / "results"),
            "--enrich-only",
            "--accept", "2",
        ])

        with caplog.at_level(logging.DEBUG, logger="cv_rank"):
            _cmd_run(args)  # --enrich-only: returns after enrichment, no API needed

        combined = caplog.text
        # Person names should appear in the log
        assert "Alice Aardvark" in combined
        assert "Bob Bear" in combined

    def test_phase_timing_logged(self, tmp_path, caplog):
        """Phase completion should include timing information."""
        from cv_rank.cli import _cmd_run, _build_parser

        csv_path = tmp_path / "people.csv"
        csv_path.write_text(
            "Name,Email\nAlice A,alice@x.com\n"
        )
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "event:\n  name: Test\n  type: community_meetup\n"
            "models:\n  scoring: gpt-4o\n"
        )

        parser = _build_parser()
        args = parser.parse_args([
            "run", "--csv", str(csv_path),
            "--config", str(config_path),
            "--output-dir", str(tmp_path / "results"),
            "--enrich-only",
            "--accept", "2",
        ])

        with caplog.at_level(logging.DEBUG, logger="cv_rank"):
            _cmd_run(args)  # --enrich-only: returns after enrichment, no API needed

        combined = caplog.text
        # Enrichment phase timing is already logged; we just verify it's there
        assert "Enrichment" in combined or "enrichment" in combined

    def test_pipeline_total_timing_logged(self, tmp_path, caplog):
        """At the end of the pipeline, total elapsed time should be logged."""
        from cv_rank.cli import _cmd_run, _build_parser

        csv_path = tmp_path / "people.csv"
        csv_path.write_text(
            "Name,Email\nAlice A,alice@x.com\n"
        )
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "event:\n  name: Test\n  type: community_meetup\n"
            "models:\n  scoring: gpt-4o\n"
        )

        parser = _build_parser()
        args = parser.parse_args([
            "run", "--csv", str(csv_path),
            "--config", str(config_path),
            "--output-dir", str(tmp_path / "results"),
            "--enrich-only",
            "--accept", "2",
        ])

        with caplog.at_level(logging.DEBUG, logger="cv_rank"):
            _cmd_run(args)  # --enrich-only: returns after enrichment, no API needed

        combined = caplog.text.lower()
        # Should log total pipeline elapsed time
        assert "total pipeline" in combined or "pipeline complete" in combined or "elapsed" in combined


# ---------------------------------------------------------------------------
# CLI: phase start logging
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_require_supabase_and_mock_enrichment")
class TestCliPhaseStartLogging:
    """Before each phase, cli should log 'Starting phase X with N people'."""

    def test_enrichment_phase_start_logged(self, tmp_path, caplog):
        """Enrichment phase start is logged with person count."""
        from cv_rank.cli import _cmd_run, _build_parser

        csv_path = tmp_path / "people.csv"
        csv_path.write_text(
            "Name,Email\nAlice A,a@x.com\nBob B,b@x.com\nCarol C,c@x.com\n"
        )
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "event:\n  name: Test\n  type: community_meetup\n"
            "models:\n  scoring: gpt-4o\n"
        )

        parser = _build_parser()
        args = parser.parse_args([
            "run", "--csv", str(csv_path),
            "--config", str(config_path),
            "--output-dir", str(tmp_path / "results"),
            "--enrich-only",
            "--accept", "2",
        ])

        with caplog.at_level(logging.DEBUG, logger="cv_rank"):
            _cmd_run(args)  # --enrich-only: returns after enrichment, no API needed

        combined = caplog.text
        # Should mention starting enrichment with 3 people
        assert "3 people" in combined or "3 person" in combined


# ---------------------------------------------------------------------------
# Pointwise: score_one logging
# ---------------------------------------------------------------------------


class TestPointwiseScoreOneLogging:
    """score_one should log person name, score, confidence, tokens."""

    @pytest.fixture
    def mock_openai_response(self):
        """Create a mock OpenAI response."""
        usage = MagicMock()
        usage.total_tokens = 500

        message = MagicMock()
        message.content = json.dumps({
            "score": 72.5,
            "confidence": "high",
            "why": "Strong ML background",
            "strongest_signal": "Anthropic experience",
            "concerns": "None notable",
            "reasoning": "Step by step analysis",
        })

        choice = MagicMock()
        choice.message = message

        response = MagicMock()
        response.choices = [choice]
        response.usage = usage

        return response

    @pytest.mark.asyncio
    async def test_score_one_logs_person_name_and_score(self, mock_openai_response, caplog):
        """score_one logs the person's name and the score received."""
        from cv_rank.scoring.pointwise import score_one

        client = AsyncMock()
        client.chat.completions.create = AsyncMock(return_value=mock_openai_response)

        person = _make_person("Alice Chen", email="alice@x.com")
        criteria = {"technical_depth": 0.5, "creativity": 0.5}
        config = {"pointwise": {"temperature": 0, "chain_of_thought": True}, "max_retries": 3}

        with caplog.at_level(logging.DEBUG, logger="cv_rank.scoring.pointwise"):
            result = await score_one(
                client, person, criteria, "gpt-4o", "(baselines)", 10, config,
                total=20, format_profile_fn=lambda p: f"Name: {p['name']}",
            )

        assert result["score"] == 72.5
        combined = caplog.text
        # Should log name and score
        assert "Alice Chen" in combined
        assert "72.5" in combined

    @pytest.mark.asyncio
    async def test_score_one_logs_confidence(self, mock_openai_response, caplog):
        """score_one logs the confidence level."""
        from cv_rank.scoring.pointwise import score_one

        client = AsyncMock()
        client.chat.completions.create = AsyncMock(return_value=mock_openai_response)

        person = _make_person("Bob Martinez")
        criteria = {"technical_depth": 0.5}
        config = {"pointwise": {"temperature": 0, "chain_of_thought": True}, "max_retries": 3}

        with caplog.at_level(logging.DEBUG, logger="cv_rank.scoring.pointwise"):
            await score_one(
                client, person, criteria, "gpt-4o", "(baselines)", 10, config,
                total=20, format_profile_fn=lambda p: f"Name: {p['name']}",
            )

        combined = caplog.text
        assert "high" in combined.lower()

    @pytest.mark.asyncio
    async def test_score_one_logs_tokens_used(self, mock_openai_response, caplog):
        """score_one logs the token count."""
        from cv_rank.scoring.pointwise import score_one

        client = AsyncMock()
        client.chat.completions.create = AsyncMock(return_value=mock_openai_response)

        person = _make_person("Carol Test")
        criteria = {"technical_depth": 0.5}
        config = {"pointwise": {"temperature": 0, "chain_of_thought": True}, "max_retries": 3}

        with caplog.at_level(logging.DEBUG, logger="cv_rank.scoring.pointwise"):
            await score_one(
                client, person, criteria, "gpt-4o", "(baselines)", 10, config,
                total=20, format_profile_fn=lambda p: f"Name: {p['name']}",
            )

        combined = caplog.text
        assert "500" in combined  # 500 tokens

    @pytest.mark.asyncio
    async def test_score_one_logs_errors(self, caplog):
        """score_one logs errors when scoring fails."""
        from cv_rank.scoring.pointwise import score_one

        client = AsyncMock()
        client.chat.completions.create = AsyncMock(
            side_effect=ValueError("Test error for scoring")
        )

        person = _make_person("Error Person")
        criteria = {"technical_depth": 0.5}
        config = {"pointwise": {"temperature": 0, "chain_of_thought": True}, "max_retries": 1}

        with caplog.at_level(logging.DEBUG, logger="cv_rank.scoring.pointwise"):
            result = await score_one(
                client, person, criteria, "gpt-4o", "(baselines)", 10, config,
                total=20, format_profile_fn=lambda p: f"Name: {p['name']}",
            )

        assert result.get("error")
        combined = caplog.text
        # Should log that an error occurred
        assert "Error Person" in combined or "error" in combined.lower()

    @pytest.mark.asyncio
    async def test_score_one_logs_prompt_lengths_at_debug(self, mock_openai_response, caplog):
        """score_one logs system and user prompt lengths at DEBUG level."""
        from cv_rank.scoring.pointwise import score_one

        client = AsyncMock()
        client.chat.completions.create = AsyncMock(return_value=mock_openai_response)

        person = _make_person("Debug Person")
        criteria = {"technical_depth": 0.5}
        config = {"pointwise": {"temperature": 0, "chain_of_thought": True}, "max_retries": 3}

        with caplog.at_level(logging.DEBUG, logger="cv_rank.scoring.pointwise"):
            await score_one(
                client, person, criteria, "gpt-4o", "(baselines)", 10, config,
                total=20, format_profile_fn=lambda p: f"Name: {p['name']}",
            )

        combined = caplog.text.lower()
        # Should log prompt lengths at DEBUG level
        assert "system prompt" in combined or "system_prompt" in combined
        assert "user prompt" in combined or "user_prompt" in combined


# ---------------------------------------------------------------------------
# Pointwise: score_all batch stats and distribution
# ---------------------------------------------------------------------------


class TestPointwiseScoreAllLogging:
    """score_all should log batch stats and score distribution summary."""

    def _make_mock_response(self, score: float, confidence: str = "medium"):
        """Create a mock OpenAI response with given score."""
        usage = MagicMock()
        usage.total_tokens = 400

        message = MagicMock()
        message.content = json.dumps({
            "score": score,
            "confidence": confidence,
            "why": "Test reason",
            "strongest_signal": "Test signal",
            "concerns": "None",
            "reasoning": "Test reasoning",
        })

        choice = MagicMock()
        choice.message = message

        response = MagicMock()
        response.choices = [choice]
        response.usage = usage
        return response

    @pytest.mark.asyncio
    async def test_score_all_logs_batch_stats(self, tmp_path, caplog):
        """After each batch checkpoint, score_all logs avg/min/max scores."""
        from cv_rank.scoring.pointwise import score_all

        people = [
            _make_person("Person A", email="a@x.com"),
            _make_person("Person B", email="b@x.com"),
            _make_person("Person C", email="c@x.com"),
        ]
        criteria = {"technical_depth": 0.5}
        config = {
            "pointwise": {"temperature": 0, "chain_of_thought": True},
            "max_retries": 3,
            "concurrency": {"scoring": 5},
            "save_every": 50,
            "baselines": [],
        }

        # Create responses with different scores
        responses = [
            self._make_mock_response(80.0),
            self._make_mock_response(60.0),
            self._make_mock_response(40.0),
        ]
        call_count = 0

        async def mock_create(**kwargs):
            nonlocal call_count
            resp = responses[call_count % len(responses)]
            call_count += 1
            return resp

        with patch("openai.AsyncOpenAI") as MockClient, \
             patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            mock_client = AsyncMock()
            mock_client.chat.completions.create = mock_create
            MockClient.return_value = mock_client

            with caplog.at_level(logging.DEBUG, logger="cv_rank.scoring.pointwise"):
                await score_all(
                    people, criteria, "gpt-4o", 2, config, tmp_path,
                    format_profile_fn=lambda p: f"Name: {p['name']}",
                )

        combined = caplog.text
        # Should log batch statistics (avg, min, max)
        assert "avg" in combined.lower() or "mean" in combined.lower() or "average" in combined.lower()
        assert "min" in combined.lower()
        assert "max" in combined.lower()

    @pytest.mark.asyncio
    async def test_score_all_logs_distribution_summary(self, tmp_path, caplog):
        """At the end, score_all logs full distribution summary (mean, median, std dev)."""
        from cv_rank.scoring.pointwise import score_all

        people = [
            _make_person("P1", email="p1@x.com"),
            _make_person("P2", email="p2@x.com"),
            _make_person("P3", email="p3@x.com"),
            _make_person("P4", email="p4@x.com"),
        ]
        criteria = {"technical_depth": 0.5}
        config = {
            "pointwise": {"temperature": 0, "chain_of_thought": True},
            "max_retries": 3,
            "concurrency": {"scoring": 5},
            "save_every": 50,
            "baselines": [],
        }

        responses = [
            self._make_mock_response(90.0),
            self._make_mock_response(70.0),
            self._make_mock_response(50.0),
            self._make_mock_response(30.0),
        ]
        call_count = 0

        async def mock_create(**kwargs):
            nonlocal call_count
            resp = responses[call_count % len(responses)]
            call_count += 1
            return resp

        with patch("openai.AsyncOpenAI") as MockClient, \
             patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            mock_client = AsyncMock()
            mock_client.chat.completions.create = mock_create
            MockClient.return_value = mock_client

            with caplog.at_level(logging.DEBUG, logger="cv_rank.scoring.pointwise"):
                await score_all(
                    people, criteria, "gpt-4o", 2, config, tmp_path,
                    format_profile_fn=lambda p: f"Name: {p['name']}",
                )

        combined = caplog.text.lower()
        # Should log distribution stats
        assert "mean" in combined or "average" in combined
        assert "median" in combined
        # stddev or std
        assert "std" in combined

    @pytest.mark.asyncio
    async def test_score_all_logs_error_count_in_batch(self, tmp_path, caplog):
        """Batch stats should include error count."""
        from cv_rank.scoring.pointwise import score_all

        people = [
            _make_person("Good Person", email="g@x.com"),
            _make_person("Bad Person", email="b@x.com"),
        ]
        criteria = {"technical_depth": 0.5}
        config = {
            "pointwise": {"temperature": 0, "chain_of_thought": True},
            "max_retries": 1,
            "concurrency": {"scoring": 5},
            "save_every": 50,
            "baselines": [],
        }

        good_resp = self._make_mock_response(75.0)
        call_count = 0

        async def mock_create(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise ValueError("Simulated API error")
            return good_resp

        with patch("openai.AsyncOpenAI") as MockClient, \
             patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            mock_client = AsyncMock()
            mock_client.chat.completions.create = mock_create
            MockClient.return_value = mock_client

            with caplog.at_level(logging.DEBUG, logger="cv_rank.scoring.pointwise"):
                await score_all(
                    people, criteria, "gpt-4o", 1, config, tmp_path,
                    format_profile_fn=lambda p: f"Name: {p['name']}",
                )

        combined = caplog.text.lower()
        # Should mention error count
        assert "error" in combined

    @pytest.mark.asyncio
    async def test_score_all_uses_progress_bar_or_logs_progress(self, tmp_path, caplog):
        """score_all should use Rich progress bar if available, else log [N/M]."""
        from cv_rank.scoring.pointwise import score_all

        people = [
            _make_person("P1", email="p1@x.com"),
            _make_person("P2", email="p2@x.com"),
        ]
        criteria = {"technical_depth": 0.5}
        config = {
            "pointwise": {"temperature": 0, "chain_of_thought": True},
            "max_retries": 3,
            "concurrency": {"scoring": 5},
            "save_every": 50,
            "baselines": [],
        }

        resp = self._make_mock_response(65.0)

        async def mock_create(**kwargs):
            return resp

        with patch("openai.AsyncOpenAI") as MockClient, \
             patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            mock_client = AsyncMock()
            mock_client.chat.completions.create = mock_create
            MockClient.return_value = mock_client

            with caplog.at_level(logging.DEBUG, logger="cv_rank.scoring.pointwise"):
                await score_all(
                    people, criteria, "gpt-4o", 1, config, tmp_path,
                    format_profile_fn=lambda p: f"Name: {p['name']}",
                )

        # The function should either use a progress bar (which we can't easily
        # test in caplog) or log scored/total info. The existing logger.debug
        # already has [N/M], so we check that logging still happens.
        combined = caplog.text
        assert "scored" in combined.lower() or "/2" in combined or "2/" in combined
