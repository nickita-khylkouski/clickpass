from __future__ import annotations

import csv
from argparse import Namespace
from pathlib import Path

from cv_rank_v2.cli.main import _cmd_run, _cmd_validate
from cv_rank_v2.pipeline import run_csv_pipeline, run_event_pipeline
from cv_rank_v2.runtime.config import ResolvedConfig


def _write_csv(path: Path) -> None:
    path.write_text(
        "Name,Email,Company,Role,LinkedIn,GitHub\n"
        "Alice Chen,alice@example.com,Anthropic,Engineer,linkedin.com/in/alice,github.com/alice\n"
        "Bob Smith,bob@example.com,OpenAI,Researcher,linkedin.com/in/bob,github.com/bob\n"
    )


def _write_csv_four(path: Path) -> None:
    path.write_text(
        "Name,Email,Company,Role,LinkedIn,GitHub\n"
        "Alice Chen,alice@example.com,Anthropic,Engineer,linkedin.com/in/alice,github.com/alice\n"
        "Bob Smith,bob@example.com,OpenAI,Researcher,linkedin.com/in/bob,github.com/bob\n"
        "Cara Singh,cara@example.com,Scale,Engineer,linkedin.com/in/cara,github.com/cara\n"
        "Dan Park,dan@example.com,Meta,Engineer,linkedin.com/in/dan,github.com/dan\n"
    )


def test_run_csv_pipeline_with_mocked_legacy_stages(tmp_path, monkeypatch) -> None:
    csv_path = tmp_path / "applicants.csv"
    _write_csv(csv_path)

    async def fake_score_all(people, criteria, model, accept_count, config, run_dir, format_profile_fn):
        assert run_dir.is_dir()
        return [
            {
                "name": people[0]["name"],
                "score": 92.0,
                "confidence": "high",
                "strongest_signal": "OSS",
                "concerns": "",
                "why": "Strong builder",
            },
            {
                "name": people[1]["name"],
                "score": 63.0,
                "confidence": "medium",
                "strongest_signal": "research",
                "concerns": "thinner profile",
                "why": "Solid but less proven",
            },
        ]

    async def fake_run_swiss(people, criteria, model, rounds, accept_count, config, run_dir, format_profile_fn, pointwise_scores=None):
        assert run_dir.is_dir()
        return (
            {
                people[0]["name"]: {"wins": 5, "losses": 1, "byes": 0},
                people[1]["name"]: {"wins": 1, "losses": 5, "byes": 0},
            },
            {
                people[0]["name"]: 0.88,
                people[1]["name"]: 0.34,
            },
            [{"name_a": people[0]["name"], "name_b": people[1]["name"], "winner": people[0]["name"]}],
        )

    async def fake_borderline(*args, **kwargs):
        return {}, {}, []

    async def fake_quality(people, rankings, config, run_dir, format_profile_fn):
        assert run_dir.is_dir()
        (run_dir / "quality_progress.json").write_text("[]")
        return [
            {
                "name": rankings[0]["name"],
                "verdict": "YES",
                "specific_why": "Built real things",
            },
            {
                "name": rankings[1]["name"],
                "verdict": "BORDERLINE",
                "specific_why": "Needs more evidence",
            },
        ]

    monkeypatch.setattr("cv_rank_v2.pipeline.legacy_score_all", fake_score_all)
    monkeypatch.setattr("cv_rank_v2.pipeline.legacy_run_swiss", fake_run_swiss)
    monkeypatch.setattr("cv_rank_v2.pipeline.legacy_run_borderline_reeval", fake_borderline)
    monkeypatch.setattr("cv_rank_v2.pipeline.legacy_run_quality_check", fake_quality)
    monkeypatch.setattr("cv_rank_v2.pipeline.run_legacy_enrichment", lambda people, config: people)

    run = run_csv_pipeline(
        csv_path=csv_path,
        accept_count=1,
        output_dir=tmp_path / "results",
        event_name="Test Event",
    )

    assert run.run_dir.exists()
    assert len(run.applicants) == 2
    assert run.combined_rankings[0]["display_name"] == "Alice Chen"
    assert run.artifact_paths["ranked_csv"].exists()
    assert run.artifact_paths["needs_review"].exists()
    assert run.artifact_paths["failed_pointwise"].exists()

    rows = list(csv.DictReader(run.artifact_paths["ranked_csv"].open()))
    assert rows[0]["Status"] == "ACCEPT"
    assert rows[1]["Verdict"] == "BORDERLINE"


def test_cli_validate_outputs_json(tmp_path, capsys) -> None:
    csv_path = tmp_path / "applicants.csv"
    _write_csv(csv_path)

    rc = _cmd_validate(Namespace(csv=str(csv_path)))
    captured = capsys.readouterr()

    assert rc == 0
    assert '"valid": true' in captured.out.lower()


def test_cli_run_uses_pipeline(monkeypatch, tmp_path, capsys) -> None:
    expected_run_dir = tmp_path / "results" / "run_v2_test"

    class DummyRun:
        run_dir = expected_run_dir
        config = ResolvedConfig.default()
        artifact_paths = {
            "ranked_csv": expected_run_dir / "RANKED_V2.csv",
            "needs_review": expected_run_dir / "NEEDS_REVIEW_V2.csv",
            "failed_pointwise": expected_run_dir / "FAILED_TO_RANK_V2.csv",
        }

    def fake_run_csv_pipeline(**kwargs):
        return DummyRun()

    monkeypatch.setattr("cv_rank_v2.cli.main.run_csv_pipeline", fake_run_csv_pipeline)

    rc = _cmd_run(
        Namespace(
            csv="applicants.csv",
            event=None,
            accept=50,
            config=None,
            criteria=None,
            model=None,
            backend=None,
            ranking_policy=None,
            swiss_weight=None,
            pointwise_weight=None,
            auto_weights=None,
            tie_breaker=None,
            shortlist_min_size=None,
            shortlist_multiplier=None,
            event_name=None,
            output_dir="results",
            sample_size=None,
            sample_seed=0,
        )
    )
    captured = capsys.readouterr()

    assert rc == 0
    assert "Ranked CSV" in captured.out


def test_cli_run_uses_event_pipeline(monkeypatch, tmp_path, capsys) -> None:
    expected_run_dir = tmp_path / "results" / "run_v2_test"

    class DummyRun:
        run_dir = expected_run_dir
        config = ResolvedConfig.default()
        artifact_paths = {
            "ranked_csv": expected_run_dir / "RANKED_V2.csv",
            "needs_review": expected_run_dir / "NEEDS_REVIEW_V2.csv",
            "failed_pointwise": expected_run_dir / "FAILED_TO_RANK_V2.csv",
        }

    def fake_run_event_pipeline(**kwargs):
        return DummyRun()

    monkeypatch.setattr("cv_rank_v2.cli.main.run_event_pipeline", fake_run_event_pipeline)

    rc = _cmd_run(
        Namespace(
            csv=None,
            event="Nebius.Build SF",
            accept=5,
            config=None,
            criteria=None,
            model=None,
            backend="legacy",
            ranking_policy=None,
            swiss_weight=None,
            pointwise_weight=None,
            auto_weights=None,
            tie_breaker=None,
            shortlist_min_size=None,
            shortlist_multiplier=None,
            event_name=None,
            output_dir="results",
            sample_size=20,
            sample_seed=7,
        )
    )
    captured = capsys.readouterr()

    assert rc == 0
    assert "Ranked CSV" in captured.out


def test_run_event_pipeline_with_mocked_legacy_stages(tmp_path, monkeypatch) -> None:
    def fake_load_event_applicants(event_name, **kwargs):
        fixture = tmp_path / "event_applicants.csv"
        _write_csv(fixture)
        from cv_rank_v2.ingest.csv_loader import load_csv

        return load_csv(fixture)

    async def fake_score_all(people, criteria, model, accept_count, config, run_dir, format_profile_fn):
        assert run_dir.is_dir()
        return [
            {
                "name": people[0]["name"],
                "score": 91.0,
                "confidence": "high",
                "strongest_signal": "shipping",
                "concerns": "",
                "why": "Strong execution",
            },
            {
                "name": people[1]["name"],
                "score": 60.0,
                "confidence": "medium",
                "strongest_signal": "experience",
                "concerns": "weaker evidence",
                "why": "Decent but less compelling",
            },
        ]

    async def fake_run_swiss(people, criteria, model, rounds, accept_count, config, run_dir, format_profile_fn, pointwise_scores=None):
        assert run_dir.is_dir()
        return (
            {
                people[0]["name"]: {"wins": 4, "losses": 1, "byes": 0},
                people[1]["name"]: {"wins": 1, "losses": 4, "byes": 0},
            },
            {
                people[0]["name"]: 0.81,
                people[1]["name"]: 0.25,
            },
            [],
        )

    async def fake_borderline(*args, **kwargs):
        return {}, {}, []

    async def fake_quality(people, rankings, config, run_dir, format_profile_fn):
        assert run_dir.is_dir()
        (run_dir / "quality_progress.json").write_text("[]")
        return [
            {"name": rankings[0]["name"], "verdict": "YES", "specific_why": "High signal"},
            {"name": rankings[1]["name"], "verdict": "BORDERLINE", "specific_why": "Lower signal"},
        ]

    monkeypatch.setattr("cv_rank_v2.pipeline.load_event_applicants", fake_load_event_applicants)
    monkeypatch.setattr("cv_rank_v2.pipeline.legacy_score_all", fake_score_all)
    monkeypatch.setattr("cv_rank_v2.pipeline.legacy_run_swiss", fake_run_swiss)
    monkeypatch.setattr("cv_rank_v2.pipeline.legacy_run_borderline_reeval", fake_borderline)
    monkeypatch.setattr("cv_rank_v2.pipeline.legacy_run_quality_check", fake_quality)
    monkeypatch.setattr("cv_rank_v2.pipeline.run_legacy_enrichment", lambda people, config: people)

    run = run_event_pipeline(
        event_name="Nebius.Build SF",
        accept_count=1,
        output_dir=tmp_path / "results",
        backend="legacy",
        sample_size=2,
        sample_seed=1,
    )

    assert len(run.applicants) == 2
    assert run.combined_rankings[0]["display_name"] == "Alice Chen"
    assert run.artifact_paths["ranked_csv"].exists()


def test_run_csv_pipeline_heuristic_e2e_fixture(tmp_path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "ranking_v2" / "e2e_applicants.csv"

    run = run_csv_pipeline(
        csv_path=fixture,
        accept_count=2,
        output_dir=tmp_path / "results",
        event_name="Heuristic E2E",
        backend="heuristic",
    )

    assert len(run.combined_rankings) == 4
    assert run.combined_rankings[0]["display_name"] == "Alice Chen"
    assert run.combined_rankings[-1]["display_name"] == "Dan Student"

    rows = list(csv.DictReader(run.artifact_paths["ranked_csv"].open()))
    assert rows[0]["Name"] == "Alice Chen"
    assert rows[0]["Status"] == "ACCEPT"
    assert rows[1]["Status"] == "ACCEPT"
    assert rows[-1]["Name"] == "Dan Student"
    assert rows[-1]["Status"] == "WAITLIST"


def test_run_csv_pipeline_runs_swiss_only_on_shortlist(tmp_path, monkeypatch) -> None:
    csv_path = tmp_path / "applicants.csv"
    _write_csv_four(csv_path)

    def fake_build_config(**kwargs):
        return ResolvedConfig.from_mapping(
            {
                "event": {"name": "Test Event"},
                "runtime": {"backend": "legacy"},
                "ranking": {
                    "accept_count": 1,
                    "shortlist_min_size": 2,
                    "shortlist_multiplier": 1.0,
                    "borderline_extra_rounds": 0,
                    "policy": "swiss_first",
                },
            }
        )

    async def fake_score_all(people, criteria, model, accept_count, config, run_dir, format_profile_fn):
        assert len(people) == 4
        return [
            {"name": people[0]["name"], "score": 95.0, "confidence": "high", "why": "top"},
            {"name": people[1]["name"], "score": 85.0, "confidence": "high", "why": "top2"},
            {"name": people[2]["name"], "score": 55.0, "confidence": "medium", "why": "mid"},
            {"name": people[3]["name"], "score": 35.0, "confidence": "medium", "why": "low"},
        ]

    async def fake_run_swiss(people, criteria, model, rounds, accept_count, config, run_dir, format_profile_fn, pointwise_scores=None):
        assert len(people) == 2
        assert len(pointwise_scores) == 2
        return (
            {
                people[0]["name"]: {"wins": 4, "losses": 0, "byes": 0},
                people[1]["name"]: {"wins": 0, "losses": 4, "byes": 0},
            },
            {
                people[0]["name"]: 0.9,
                people[1]["name"]: 0.2,
            },
            [],
        )

    async def fake_quality(people, rankings, config, run_dir, format_profile_fn):
        return [{"name": row["name"], "verdict": "YES", "specific_why": "ok"} for row in rankings]

    monkeypatch.setattr("cv_rank_v2.pipeline._build_config", fake_build_config)
    monkeypatch.setattr("cv_rank_v2.pipeline.legacy_score_all", fake_score_all)
    monkeypatch.setattr("cv_rank_v2.pipeline.legacy_run_swiss", fake_run_swiss)
    monkeypatch.setattr("cv_rank_v2.pipeline.legacy_run_quality_check", fake_quality)
    monkeypatch.setattr("cv_rank_v2.pipeline.run_legacy_enrichment", lambda people, config: people)

    run = run_csv_pipeline(
        csv_path=csv_path,
        accept_count=1,
        output_dir=tmp_path / "results",
        event_name="Test Event",
    )

    assert len(run.combined_rankings) == 4
    assert sum(1 for row in run.combined_rankings if row["swiss_evaluated"]) == 2


def test_cli_run_passes_ranking_policy_overrides(monkeypatch, tmp_path, capsys) -> None:
    expected_run_dir = tmp_path / "results" / "run_v2_test"
    seen: dict[str, object] = {}

    class DummyRun:
        run_dir = expected_run_dir
        config = ResolvedConfig.from_mapping(
            {
                "ranking": {
                    "policy": "swiss_dominant",
                    "swiss_weight": 0.8,
                    "pointwise_weight": 0.2,
                    "auto_weights": False,
                    "tie_breaker": "swiss",
                }
            }
        )
        artifact_paths = {
            "ranked_csv": expected_run_dir / "RANKED_V2.csv",
            "needs_review": expected_run_dir / "NEEDS_REVIEW_V2.csv",
            "failed_pointwise": expected_run_dir / "FAILED_TO_RANK_V2.csv",
        }

    def fake_run_event_pipeline(**kwargs):
        seen.update(kwargs)
        return DummyRun()

    monkeypatch.setattr("cv_rank_v2.cli.main.run_event_pipeline", fake_run_event_pipeline)

    rc = _cmd_run(
        Namespace(
            csv=None,
            event="Nebius.Build SF",
            accept=5,
            config=None,
            criteria=None,
            model=None,
            backend="legacy",
            ranking_policy="swiss_dominant",
            swiss_weight=0.8,
            pointwise_weight=0.2,
            auto_weights=False,
            tie_breaker="swiss",
            shortlist_min_size=25,
            shortlist_multiplier=5.0,
            event_name=None,
            output_dir="results",
            sample_size=20,
            sample_seed=7,
        )
    )
    captured = capsys.readouterr()

    assert rc == 0
    assert seen["ranking_policy"] == "swiss_dominant"
    assert seen["swiss_weight"] == 0.8
    assert seen["pointwise_weight"] == 0.2
    assert seen["auto_weights"] is False
    assert seen["tie_breaker"] == "swiss"
    assert seen["shortlist_min_size"] == 25
    assert seen["shortlist_multiplier"] == 5.0
    assert "Ranking policy: swiss_dominant" in captured.out
