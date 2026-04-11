"""Tests for adaptive Swiss controls and BT prior wiring."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from cv_rank.scoring.swiss import run_swiss


class _DummyClient:
    async def close(self):
        return None


def _people(n: int) -> list[dict]:
    return [{"name": f"P{i:02d}", "email": f"p{i}@test.com"} for i in range(n)]


@pytest.mark.asyncio
async def test_run_swiss_early_stop_writes_checkpoint(tmp_path: Path):
    people = _people(8)
    config = {
        "event": {"target_accepts": 4},
        "concurrency": {"swiss": 2},
        "max_retries": 1,
        "swiss": {
            "inject_pointwise": False,
            "use_bradley_terry": False,
            "early_stop": {
                "enabled": True,
                "min_rounds": 2,
                "jaccard_accept_set_min": 0.98,
                "max_boundary_rank_shift": 1,
                "consecutive_rounds": 1,
                "boundary_width_pct": 0.20,
                "boundary_width_min": 2,
            },
        },
    }

    async def fake_compare_one(*args, **kwargs):
        # deterministic winner for stable rankings
        p1 = args[1]
        p2 = args[2]
        winner = min(p1["name"], p2["name"])
        return {
            "name_a": p1["name"],
            "name_b": p2["name"],
            "winner": winner,
            "why": "deterministic",
            "tokens": 10,
        }

    with (
        patch("cv_rank.utils.get_openai_client", return_value=_DummyClient()),
        patch("cv_rank.scoring.swiss.compare_one", side_effect=fake_compare_one),
        patch("cv_rank.scoring.swiss._accept_set_jaccard", return_value=1.0),
        patch("cv_rank.scoring.swiss._max_boundary_rank_shift", return_value=0),
    ):
        records, bt, matches = await run_swiss(
            people=people,
            criteria={"technical_depth": 1.0},
            model="gpt-test",
            rounds=9,
            accept_count=4,
            config=config,
            run_dir=tmp_path,
            format_profile_fn=lambda p: f"Name: {p['name']}",
            pointwise_scores=None,
        )

    assert records
    assert bt == {}
    assert matches

    cp = tmp_path / "swiss_checkpoint.json"
    assert cp.exists()
    checkpoint = json.loads(cp.read_text())
    assert checkpoint["early_stopped"] is True
    assert checkpoint["completed_rounds"] < 9


@pytest.mark.asyncio
async def test_run_swiss_passes_pointwise_prior_to_bt(tmp_path: Path):
    people = _people(6)
    pointwise_scores = [{"name": p["name"], "score": float(50 + i)} for i, p in enumerate(people)]
    config = {
        "event": {"target_accepts": 3},
        "concurrency": {"swiss": 2},
        "max_retries": 1,
        "swiss": {
            "inject_pointwise": False,
            "use_bradley_terry": True,
            "bt_pointwise_prior": True,
            "bt_prior_strength": 3,
            "early_stop": {"enabled": False},
        },
    }

    async def fake_compare_one(*args, **kwargs):
        p1 = args[1]
        p2 = args[2]
        return {
            "name_a": p1["name"],
            "name_b": p2["name"],
            "winner": p1["name"],
            "why": "deterministic",
            "tokens": 5,
        }

    captured: dict = {}

    def fake_bt(names, comparisons, pointwise_prior=None, prior_strength=2):
        captured["names"] = names
        captured["comparisons"] = comparisons
        captured["pointwise_prior"] = pointwise_prior
        captured["prior_strength"] = prior_strength
        return {n: i / max(len(names) - 1, 1) for i, n in enumerate(names)}

    with (
        patch("cv_rank.utils.get_openai_client", return_value=_DummyClient()),
        patch("cv_rank.scoring.swiss.compare_one", side_effect=fake_compare_one),
        patch("cv_rank.scoring.swiss.compute_bt_strengths", side_effect=fake_bt),
    ):
        records, bt, matches = await run_swiss(
            people=people,
            criteria={"technical_depth": 1.0},
            model="gpt-test",
            rounds=2,
            accept_count=3,
            config=config,
            run_dir=tmp_path,
            format_profile_fn=lambda p: f"Name: {p['name']}",
            pointwise_scores=pointwise_scores,
        )

    assert records
    assert bt
    assert matches
    assert captured["pointwise_prior"] is not None
    assert captured["prior_strength"] == 3


@pytest.mark.asyncio
async def test_run_swiss_resume_restores_early_stop_state(tmp_path: Path):
    people = _people(8)
    config = {
        "event": {"target_accepts": 4},
        "concurrency": {"swiss": 2},
        "max_retries": 1,
        "swiss": {
            "inject_pointwise": False,
            "use_bradley_terry": False,
            "early_stop": {
                "enabled": True,
                "min_rounds": 2,
                "jaccard_accept_set_min": 0.98,
                "max_boundary_rank_shift": 1,
                "consecutive_rounds": 2,
                "boundary_width_pct": 0.20,
                "boundary_width_min": 2,
            },
        },
    }

    async def fake_compare_one(*args, **kwargs):
        p1 = args[1]
        p2 = args[2]
        winner = min(p1["name"], p2["name"])
        return {
            "name_a": p1["name"],
            "name_b": p2["name"],
            "winner": winner,
            "why": "deterministic",
            "tokens": 10,
        }

    with (
        patch("cv_rank.utils.get_openai_client", return_value=_DummyClient()),
        patch("cv_rank.scoring.swiss.compare_one", side_effect=fake_compare_one),
        patch("cv_rank.scoring.swiss._accept_set_jaccard", return_value=1.0),
        patch("cv_rank.scoring.swiss._max_boundary_rank_shift", return_value=0),
    ):
        # First run stops at round 2 naturally and checkpoints one stable hit.
        await run_swiss(
            people=people,
            criteria={"technical_depth": 1.0},
            model="gpt-test",
            rounds=2,
            accept_count=4,
            config=config,
            run_dir=tmp_path,
            format_profile_fn=lambda p: f"Name: {p['name']}",
            pointwise_scores=None,
        )

        first_cp = json.loads((tmp_path / "swiss_checkpoint.json").read_text())
        assert first_cp["early_stopped"] is False
        assert first_cp["completed_rounds"] == 2
        assert first_cp["stability_hits"] == 1
        assert first_cp["prev_snapshot"] is not None

        # Resume should preserve state and early-stop at round 3 (not round 5).
        await run_swiss(
            people=people,
            criteria={"technical_depth": 1.0},
            model="gpt-test",
            rounds=6,
            accept_count=4,
            config=config,
            run_dir=tmp_path,
            format_profile_fn=lambda p: f"Name: {p['name']}",
            pointwise_scores=None,
        )

    resumed_cp = json.loads((tmp_path / "swiss_checkpoint.json").read_text())
    assert resumed_cp["early_stopped"] is True
    assert resumed_cp["completed_rounds"] == 3


@pytest.mark.asyncio
async def test_run_swiss_no_early_stop_when_round_has_errors(tmp_path: Path):
    people = _people(8)
    config = {
        "event": {"target_accepts": 4},
        "concurrency": {"swiss": 2},
        "max_retries": 1,
        "swiss": {
            "inject_pointwise": False,
            "use_bradley_terry": False,
            "early_stop": {
                "enabled": True,
                "min_rounds": 2,
                "jaccard_accept_set_min": 0.98,
                "max_boundary_rank_shift": 1,
                "consecutive_rounds": 1,
                "boundary_width_pct": 0.20,
                "boundary_width_min": 2,
            },
        },
    }

    async def always_error_compare(*args, **kwargs):
        p1 = args[1]
        p2 = args[2]
        return {
            "name_a": p1["name"],
            "name_b": p2["name"],
            "winner": None,
            "error": "rate_limit",
            "tokens": 5,
        }

    with (
        patch("cv_rank.utils.get_openai_client", return_value=_DummyClient()),
        patch("cv_rank.scoring.swiss.compare_one", side_effect=always_error_compare),
        patch("cv_rank.scoring.swiss._accept_set_jaccard", return_value=1.0),
        patch("cv_rank.scoring.swiss._max_boundary_rank_shift", return_value=0),
    ):
        await run_swiss(
            people=people,
            criteria={"technical_depth": 1.0},
            model="gpt-test",
            rounds=4,
            accept_count=4,
            config=config,
            run_dir=tmp_path,
            format_profile_fn=lambda p: f"Name: {p['name']}",
            pointwise_scores=None,
        )

    checkpoint = json.loads((tmp_path / "swiss_checkpoint.json").read_text())
    assert checkpoint["early_stopped"] is False
    assert checkpoint["completed_rounds"] == 4
