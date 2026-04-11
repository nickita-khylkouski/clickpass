"""Tests for cv_rank.cli — CLI argument parsing and subcommands."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from cv_rank import __version__
from cv_rank.cli import (
    _build_parser,
    _check_exclusion_guardrail,
    _cmd_init,
    _cmd_waves_predict,
    _cmd_waves_train,
    _cmd_run,
    _cmd_validate,
    _track_drops,
    _write_failed_to_rank_report,
    _write_needs_review_report,
)
from cv_rank.config import parse_criteria as _parse_criteria


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


class TestBuildParser:
    """Tests for _build_parser() and argument structure."""

    def test_version_flag(self, capsys) -> None:
        """--version prints version string and exits."""
        parser = _build_parser()

        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--version"])

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert __version__ in captured.out

    def test_run_subcommand_accepts_csv_or_event(self) -> None:
        """'run' subcommand accepts --csv or --event (neither required by argparse)."""
        parser = _build_parser()
        # Both are optional at the argparse level; _cmd_run validates one is provided
        args = parser.parse_args(["run"])
        assert args.csv is None
        assert args.event is None

        args_csv = parser.parse_args(["run", "--csv", "test.csv"])
        assert args_csv.csv == "test.csv"
        assert args_csv.event is None

        args_event = parser.parse_args(["run", "--event", "My Event"])
        assert args_event.csv is None
        assert args_event.event == "My Event"

    def test_run_subcommand_parses_all_args(self) -> None:
        """'run' subcommand parses all expected arguments."""
        parser = _build_parser()
        args = parser.parse_args([
            "run",
            "--csv", "applicants.csv",
            "--criteria", "technical_depth:0.3,creativity:0.2",
            "--accept", "100",
            "--model", "gpt-4o",
            "--rounds", "15",
            "--event-name", "Test Event",
            "--swiss-weight", "0.6",
            "--config", "myconfig.yaml",
            "--output-dir", "output",
            "--resume",
            "--enrich-only",
            "--no-supabase",
            "--no-github",
        ])

        assert args.command == "run"
        assert args.csv == "applicants.csv"
        assert args.criteria == "technical_depth:0.3,creativity:0.2"
        assert args.accept == 100
        assert args.model == "gpt-4o"
        assert args.rounds == 15
        assert args.event_name == "Test Event"
        assert args.swiss_weight == 0.6
        assert args.config == "myconfig.yaml"
        assert args.output_dir == "output"
        assert args.resume is True
        assert args.enrich_only is True
        assert args.no_supabase is True
        assert args.no_github is True

    def test_run_defaults(self) -> None:
        """'run' subcommand default values are correct."""
        parser = _build_parser()
        args = parser.parse_args(["run", "--csv", "test.csv"])

        assert args.command == "run"
        assert args.csv == "test.csv"
        assert args.criteria is None
        assert args.accept is None
        assert args.model is None
        assert args.rounds is None
        assert args.event_name is None
        assert args.swiss_weight is None
        assert args.config is None
        assert args.output_dir == "results"
        assert args.resume is False
        assert args.enrich_only is False

    def test_validate_subcommand(self) -> None:
        """'validate' subcommand parses --csv and --config."""
        parser = _build_parser()
        args = parser.parse_args(["validate", "--csv", "data.csv", "--config", "c.yaml"])

        assert args.command == "validate"
        assert args.csv == "data.csv"
        assert args.config == "c.yaml"

    def test_validate_requires_csv(self) -> None:
        """'validate' subcommand requires --csv."""
        parser = _build_parser()

        with pytest.raises(SystemExit):
            parser.parse_args(["validate"])

    def test_init_subcommand(self) -> None:
        """'init' subcommand parses --dir."""
        parser = _build_parser()
        args = parser.parse_args(["init", "--dir", "/tmp/project"])

        assert args.command == "init"
        assert args.dir == "/tmp/project"

    def test_init_defaults(self) -> None:
        """'init' with no args defaults dir to None."""
        parser = _build_parser()
        args = parser.parse_args(["init"])

        assert args.command == "init"
        assert args.dir is None

    def test_no_command_sets_none(self) -> None:
        """No subcommand results in command=None."""
        parser = _build_parser()
        args = parser.parse_args([])

        assert args.command is None

    def test_waves_train_subcommand(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "waves-train",
                "--output-dir",
                "results/waves_v2/test",
                "--calibration-events",
                "8",
                "--test-events",
                "6",
                "--horizons",
                "7,3,1",
                "--stage0-mode",
                "hybrid",
            ]
        )

        assert args.command == "waves-train"
        assert args.output_dir == "results/waves_v2/test"
        assert args.calibration_events == 8
        assert args.test_events == 6
        assert args.horizons == "7,3,1"
        assert args.stage0_mode == "hybrid"

    def test_waves_predict_subcommand(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "waves-predict",
                "--artifact-dir",
                "results/waves_v2/test",
                "--days-ahead",
                "21",
                "--output-csv",
                "results/preds.csv",
                "--include-non-hackathons",
            ]
        )

        assert args.command == "waves-predict"
        assert args.artifact_dir == "results/waves_v2/test"
        assert args.days_ahead == 21
        assert args.output_csv == "results/preds.csv"
        assert args.include_non_hackathons is True

    def test_sponsor_analyze_subcommand(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "sponsor-analyze",
                "--run-id",
                "run_20260307_023434",
                "--event",
                "Agentic Orchestration and Collaboration Hackathon",
                "--reviewer-mode",
                "strict",
                "--output-root",
                "outputs",
            ]
        )

        assert args.command == "sponsor-analyze"
        assert args.run_id == "run_20260307_023434"
        assert args.event == "Agentic Orchestration and Collaboration Hackathon"
        assert args.reviewer_mode == "strict"
        assert args.output_root == "outputs"


# ---------------------------------------------------------------------------
# Validate subcommand
# ---------------------------------------------------------------------------


class TestCmdValidate:
    """Tests for _cmd_validate()."""

    def test_validate_valid_csv(self, tmp_path: Path, monkeypatch) -> None:
        """validate subcommand returns 0 for a valid CSV."""
        csv_path = tmp_path / "valid.csv"
        csv_path.write_text(
            "Name,Email,Company\n"
            "Alice,alice@test.com,Anthropic\n"
        )
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_KEY", "test-key")

        # Create a valid config yaml
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "event:\n"
            "  name: Test Event\n"
            "  type: community_meetup\n"
            "models:\n"
            "  scoring: gpt-4o\n"
        )

        parser = _build_parser()
        args = parser.parse_args(["validate", "--csv", str(csv_path), "--config", str(config_path)])

        rc = _cmd_validate(args)
        assert rc == 0

    def test_validate_invalid_csv(self, tmp_path: Path, monkeypatch) -> None:
        """validate subcommand returns 1 for an invalid CSV."""
        csv_path = tmp_path / "bad.csv"
        csv_path.write_text(
            "Email,Company\n"  # no name column
            "alice@test.com,Anthropic\n"
        )

        monkeypatch.chdir(tmp_path)
        parser = _build_parser()
        args = parser.parse_args(["validate", "--csv", str(csv_path)])

        rc = _cmd_validate(args)
        assert rc == 1


class TestWavesCommands:
    def test_cmd_waves_train_invokes_script(self, monkeypatch, tmp_path: Path) -> None:
        captured: dict[str, object] = {}

        def fake_run(cmd, check):  # type: ignore[no-untyped-def]
            captured["cmd"] = cmd
            captured["check"] = check

            class Result:
                returncode = 0

            return Result()

        monkeypatch.setattr("cv_rank.cli.subprocess.run", fake_run)
        parser = _build_parser()
        args = parser.parse_args(
            [
                "waves-train",
                "--output-dir",
                str(tmp_path / "artifact"),
                "--stage0-mode",
                "hybrid",
            ]
        )

        rc = _cmd_waves_train(args)

        assert rc == 0
        cmd = captured["cmd"]
        assert isinstance(cmd, list)
        assert cmd[0]
        assert cmd[1].endswith("scripts/train_waves_v2.py")
        assert "--stage0-mode" in cmd
        assert "hybrid" in cmd

    def test_cmd_waves_predict_invokes_script(self, monkeypatch, tmp_path: Path) -> None:
        captured: dict[str, object] = {}

        def fake_run(cmd, check):  # type: ignore[no-untyped-def]
            captured["cmd"] = cmd
            captured["check"] = check

            class Result:
                returncode = 0

            return Result()

        monkeypatch.setattr("cv_rank.cli.subprocess.run", fake_run)
        parser = _build_parser()
        args = parser.parse_args(
            [
                "waves-predict",
                "--artifact-dir",
                str(tmp_path / "artifact"),
                "--output-csv",
                str(tmp_path / "preds.csv"),
            ]
        )

        rc = _cmd_waves_predict(args)

        assert rc == 0
        cmd = captured["cmd"]
        assert isinstance(cmd, list)
        assert cmd[1].endswith("scripts/predict_upcoming_v2.py")
        assert "--artifact-dir" in cmd
        assert str(tmp_path / "artifact") in cmd

    def test_cmd_waves_train_returns_subprocess_failure_code(self, monkeypatch, tmp_path: Path) -> None:
        def fake_run(cmd, check):  # type: ignore[no-untyped-def]
            class Result:
                returncode = 7

            return Result()

        monkeypatch.setattr("cv_rank.cli.subprocess.run", fake_run)
        parser = _build_parser()
        args = parser.parse_args(
            [
                "waves-train",
                "--output-dir",
                str(tmp_path / "artifact"),
            ]
        )

        rc = _cmd_waves_train(args)

        assert rc == 7

    def test_cmd_waves_predict_returns_subprocess_failure_code(self, monkeypatch, tmp_path: Path) -> None:
        def fake_run(cmd, check):  # type: ignore[no-untyped-def]
            class Result:
                returncode = 9

            return Result()

        monkeypatch.setattr("cv_rank.cli.subprocess.run", fake_run)
        parser = _build_parser()
        args = parser.parse_args(
            [
                "waves-predict",
                "--artifact-dir",
                str(tmp_path / "artifact"),
            ]
        )

        rc = _cmd_waves_predict(args)

        assert rc == 9


class TestCmdRunSupabaseRequired:
    """Supabase is mandatory for run/incremental commands."""

    def test_run_requires_supabase_credentials(self, tmp_path: Path, monkeypatch, capsys) -> None:
        csv_path = tmp_path / "people.csv"
        csv_path.write_text("Name,Email\nAlice,alice@test.com\n")
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "event:\n"
            "  name: Test Event\n"
            "  type: community_meetup\n"
            "models:\n"
            "  scoring: gpt-4o\n"
        )

        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_KEY", raising=False)

        parser = _build_parser()
        args = parser.parse_args(
            [
                "run",
                "--csv",
                str(csv_path),
                "--config",
                str(config_path),
                "--accept",
                "1",
                "--enrich-only",
                "--output-dir",
                str(tmp_path / "results"),
            ]
        )

        rc = _cmd_run(args)
        assert rc == 1
        assert "Supabase enrichment is required" in capsys.readouterr().err

    def test_run_rejects_no_supabase_flag(self, tmp_path: Path, monkeypatch, capsys) -> None:
        csv_path = tmp_path / "people.csv"
        csv_path.write_text("Name,Email\nAlice,alice@test.com\n")
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "event:\n"
            "  name: Test Event\n"
            "  type: community_meetup\n"
            "models:\n"
            "  scoring: gpt-4o\n"
        )

        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_KEY", "test-key")

        parser = _build_parser()
        args = parser.parse_args(
            [
                "run",
                "--csv",
                str(csv_path),
                "--config",
                str(config_path),
                "--accept",
                "1",
                "--enrich-only",
                "--no-supabase",
                "--output-dir",
                str(tmp_path / "results"),
            ]
        )

        rc = _cmd_run(args)
        assert rc == 1
        assert "Remove --no-supabase" in capsys.readouterr().err


class TestGuardrailsAndReports:
    """Unit tests for exclusion guardrails and report generation helpers."""

    def test_exclusion_guardrail_blocks_when_threshold_exceeded(self):
        people = [{"name": "A"}, {"name": "B"}, {"name": "C"}]
        rankings = [{"name": "A"}, {"name": "B"}]  # 1/3 excluded => 33%
        config = {"guardrails": {"max_exclusion_rate": 0.2}}
        logger = logging.getLogger("test.guardrails")

        assert _check_exclusion_guardrail(people, rankings, config, logger) is False

    def test_failed_to_rank_report_written(self, tmp_path: Path):
        people = [
            {"name": "A", "email": "a@test.com", "candidate_id": "cand_a"},
            {"name": "B", "email": "b@test.com", "candidate_id": "cand_b"},
        ]
        scores = [
            {"name": "A", "score": 80.0},
            {"name": "B", "score": None, "error": "json_parse"},
        ]
        swiss_records = {"A": {"wins": 1, "losses": 0, "byes": 0}}
        rankings = [{"name": "A", "rank": 1}]
        logger = logging.getLogger("test.reports.failed")
        _track_drops(people, scores, swiss_records, rankings, logger)

        path = _write_failed_to_rank_report(
            tmp_path, people, scores, swiss_records, rankings, logger
        )
        assert path is not None
        assert path.exists()
        text = path.read_text()
        assert "pointwise_error" in text
        assert "cand_b" in text

    def test_failed_to_rank_report_keeps_duplicate_names(self, tmp_path: Path):
        people = [
            {"name": "Same Name", "email": "a@test.com", "candidate_id": "cand_a"},
            {"name": "Same Name", "email": "b@test.com", "candidate_id": "cand_b"},
        ]
        scores = [
            {"name": "Same Name", "score": 80.0},
            {"name": "Same Name", "score": None, "error": "timeout"},
        ]
        swiss_records = {"Same Name": {"wins": 1, "losses": 0, "byes": 0}}
        rankings = [{"name": "Same Name", "rank": 1}]
        logger = logging.getLogger("test.reports.duplicate_names")
        _track_drops(people, scores, swiss_records, rankings, logger)

        path = _write_failed_to_rank_report(
            tmp_path, people, scores, swiss_records, rankings, logger
        )
        assert path is not None
        lines = path.read_text().strip().splitlines()
        # header + one dropped row for the second duplicate applicant
        assert len(lines) == 2
        assert "cand_b" not in lines[1]
        assert "pointwise_error" in lines[1]
        assert "ambiguous duplicate name" in lines[1]

    def test_needs_review_report_written(self, tmp_path: Path):
        people = [
            {"name": "A", "email": "a@test.com", "candidate_id": "cand_a"},
            {"name": "B", "email": "b@test.com", "candidate_id": "cand_b"},
            {"name": "C", "email": "c@test.com", "candidate_id": "cand_c"},
        ]
        rankings = [
            {"name": "A", "rank": 1, "final_score": 0.9, "swiss_wins": 3, "swiss_losses": 0},
            {"name": "B", "rank": 2, "final_score": 0.7, "swiss_wins": 2, "swiss_losses": 1},
            {"name": "C", "rank": 3, "final_score": 0.5, "swiss_wins": 1, "swiss_losses": 2},
        ]
        scores = [
            {"name": "A", "score": 95.0},
            {"name": "B", "score": 70.0},
            {"name": "C", "score": 10.0},
        ]
        swiss_records = {
            "A": {"wins": 3, "losses": 0},
            "B": {"wins": 2, "losses": 1},
            "C": {"wins": 3, "losses": 0},
        }
        bt_strengths = {"A": 1.0, "B": 0.5, "C": 0.8}
        config = {
            "guardrails": {
                "needs_review": {
                    "enabled": True,
                    "boundary_width_pct": 0.5,
                    "boundary_width_min": 1,
                    "disagreement_threshold": 0.25,
                }
            }
        }
        logger = logging.getLogger("test.reports.needs_review")

        path = _write_needs_review_report(
            tmp_path, people, rankings, scores, swiss_records, bt_strengths, 2, config, logger
        )
        assert path is not None
        assert path.exists()
        text = path.read_text()
        assert "boundary_band" in text
        assert "signal_disagreement" in text

# ---------------------------------------------------------------------------
# Init subcommand
# ---------------------------------------------------------------------------


class TestCmdInit:
    """Tests for _cmd_init()."""

    def test_init_creates_files_from_templates(self, tmp_path: Path) -> None:
        """init subcommand creates config.yaml (and .env if templates exist)."""
        target = tmp_path / "project"

        parser = _build_parser()
        args = parser.parse_args(["init", "--dir", str(target)])

        rc = _cmd_init(args)
        assert rc == 0
        assert target.exists()

        # The function tries to copy from project root templates.
        # Whether files are created depends on template existence,
        # but the command should not error out.

    def test_init_skips_existing_files(self, tmp_path: Path) -> None:
        """init does not overwrite existing config.yaml."""
        target = tmp_path / "project"
        target.mkdir()
        config_file = target / "config.yaml"
        config_file.write_text("existing: true\n")

        parser = _build_parser()
        args = parser.parse_args(["init", "--dir", str(target)])

        _cmd_init(args)

        # Original content preserved
        assert config_file.read_text() == "existing: true\n"


# ---------------------------------------------------------------------------
# _parse_criteria
# ---------------------------------------------------------------------------


class TestParseCriteria:
    """Tests for _parse_criteria()."""

    def test_explicit_weights_string(self) -> None:
        """Comma-separated 'key:weight' format is parsed correctly."""
        config = {"event": {"type": "community_meetup"}}
        result = _parse_criteria("technical_depth:0.3,creativity:0.2,oss:0.5", config)

        assert result == {
            "technical_depth": 0.3,
            "creativity": 0.2,
            "oss": 0.5,
        }

    def test_plain_description_uses_fallback(self) -> None:
        """A plain string without ':' falls back to default criteria."""
        config = {"event": {"type": "community_meetup"}}
        result = _parse_criteria("AI builders", config)

        # Should get the fallback defaults
        assert "technical_depth" in result
        assert "industry_experience" in result

    def test_none_criteria_uses_fallback(self) -> None:
        """None criteria uses the event type preset or fallback."""
        config = {"event": {"type": "community_meetup"}}
        result = _parse_criteria(None, config)

        assert isinstance(result, dict)
        assert len(result) > 0

    def test_config_preset_used(self) -> None:
        """When criteria config has a matching event type, use it."""
        config = {
            "event": {"type": "hackathon"},
            "criteria": {
                "hackathon": {
                    "shipping_ability": 0.4,
                    "technical_depth": 0.3,
                    "creativity": 0.3,
                },
            },
        }
        result = _parse_criteria("AI builders", config)

        assert result == {
            "shipping_ability": 0.4,
            "technical_depth": 0.3,
            "creativity": 0.3,
        }
