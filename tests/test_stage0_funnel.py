from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from cv_rank.waves.stage0_funnel import (
    build_live_signup_row,
    merge_signup_and_funnel_examples,
    predict_hybrid_signup_totals,
)


class FakeModel:
    def __init__(self, values: list[float]) -> None:
        self.values = np.array(values, dtype=float)

    def predict(self, x):  # type: ignore[no-untyped-def]
        return self.values[: len(x)]


def test_build_live_signup_row_contains_application_and_approval_features() -> None:
    event = {
        "id": "ev-1",
        "title": "OpenAI Codex Hackathon",
        "description": "Build AI agents and win cash prizes.",
        "descriptionSummary": "AI builder weekend",
        "details": {"tracks": ["agents"]},
        "city": "San Francisco, CA, USA",
        "startDateTime": pd.Timestamp("2026-03-10T17:00:00Z"),
        "isPlatformHackathon": True,
        "approvalRequired": True,
        "capacity": 250,
        "horizon_days": 6.8,
        "applied_count": 300,
        "applied_ago_14d": 100,
        "applied_ago_11d": 120,
        "applied_ago_7d": 180,
        "applied_ago_6d": 190,
        "applied_ago_4d": 220,
        "applied_ago_2d": 260,
        "approved_count": 120,
        "approved_ago_14d": 10,
        "approved_ago_11d": 20,
        "approved_ago_7d": 60,
        "approved_ago_6d": 70,
        "approved_ago_4d": 90,
        "approved_ago_2d": 110,
    }

    row = build_live_signup_row(event)

    assert row["horizon_days"] == 7.0
    assert row["current_approved"] == 120.0
    assert row["current_applied"] == 300.0
    assert row["current_approved_proxy"] == 120.0
    assert row["current_approval_rate_proxy"] == 0.4
    assert "velocity_recent_applied" in row
    assert "velocity_recent" in row
    assert row["event_mentions_prizes"] == 1.0
    assert row["event_approval_required"] == 1.0


@pytest.mark.parametrize(
    ("horizon_days", "expected_bucket", "expected_previous_key"),
    [
        (12.0, 14.0, "approved_ago_7d"),
        (6.8, 7.0, "approved_ago_7d"),
        (3.1, 3.0, "approved_ago_4d"),
        (0.9, 1.0, "approved_ago_2d"),
    ],
)
def test_build_live_signup_row_uses_expected_bucket_windows(
    horizon_days: float,
    expected_bucket: float,
    expected_previous_key: str,
) -> None:
    event = {
        "id": "ev-branch",
        "title": "Test Hackathon",
        "description": "Build together",
        "descriptionSummary": "Builders",
        "details": {"tracks": ["ai"]},
        "city": "San Francisco, CA, USA",
        "startDateTime": pd.Timestamp("2026-03-10T17:00:00Z"),
        "isPlatformHackathon": True,
        "approvalRequired": False,
        "capacity": 100,
        "horizon_days": horizon_days,
        "applied_count": 300,
        "applied_ago_14d": 100,
        "applied_ago_11d": 120,
        "applied_ago_7d": 180,
        "applied_ago_6d": 190,
        "applied_ago_4d": 220,
        "applied_ago_2d": 260,
        "approved_count": 120,
        "approved_ago_14d": 10,
        "approved_ago_11d": 20,
        "approved_ago_7d": 60,
        "approved_ago_6d": 70,
        "approved_ago_4d": 90,
        "approved_ago_2d": 110,
    }

    row = build_live_signup_row(event)

    assert row["horizon_days"] == expected_bucket
    assert row["previous_approved"] == float(event[expected_previous_key])
    assert row["current_applied"] == 300.0


def test_predict_hybrid_signup_totals_uses_horizon_strategy_and_direct_fallback() -> None:
    frame = pd.DataFrame(
        [
            {
                "event_id": "ev-1",
                "horizon_days": 7.0,
                "current_approved": 100.0,
                "current_approved_proxy": 100.0,
                "current_applied": 200.0,
                "log_current_applied": np.log1p(200.0),
            },
            {
                "event_id": "ev-2",
                "horizon_days": 3.0,
                "current_approved": 80.0,
                "current_approved_proxy": 80.0,
                "current_applied": np.nan,
                "log_current_applied": np.nan,
            },
        ]
    )

    artifact = {
        "mode": "hybrid",
        "direct": {"unused": True},
        "application_model": {
            "model": FakeModel([50.0, 10.0]),
            "feature_cols": [],
            "bias_corrections": {},
            "target_col": "remaining_applied",
            "log_target": False,
        },
        "single_stage_model": {
            "model": FakeModel([130.0, 90.0]),
            "feature_cols": [],
            "bias_corrections": {},
            "target_col": "final_approved",
            "log_target": False,
        },
        "two_stage_model": {
            "model": FakeModel([140.0, 95.0]),
            "feature_cols": [],
            "bias_corrections": {},
            "target_col": "final_approved",
            "log_target": False,
        },
        "application_growth_caps": {"7": 1.5},
        "strategy_by_horizon": {"7": "two_stage", "3": "single_stage"},
    }

    preds = predict_hybrid_signup_totals(
        frame,
        artifact,
        direct_predict_fn=lambda f, a: np.array([110.0, 85.0]),
    )

    assert preds[0] == 140.0
    assert preds[1] == 85.0


def test_predict_hybrid_signup_totals_accepts_direct_artifact() -> None:
    frame = pd.DataFrame(
        [
            {
                "event_id": "ev-1",
                "horizon_days": 7.0,
                "current_approved": 90.0,
            }
        ]
    )

    preds = predict_hybrid_signup_totals(
        frame,
        {"feature_columns": [], "bias_corrections": {}},
        direct_predict_fn=lambda f, a: np.array([123.0]),
    )

    assert preds[0] == 123.0


def test_predict_hybrid_signup_totals_accepts_direct_only_wrapper() -> None:
    frame = pd.DataFrame(
        [
            {
                "event_id": "ev-1",
                "horizon_days": 7.0,
                "current_approved": 90.0,
            }
        ]
    )

    preds = predict_hybrid_signup_totals(
        frame,
        {
            "mode": "direct_only",
            "direct": {"feature_columns": [], "bias_corrections": {}},
        },
        direct_predict_fn=lambda f, a: np.array([123.0]),
    )

    assert preds[0] == 123.0


def test_merge_signup_and_funnel_examples_preserves_funnel_only_sparse_rows() -> None:
    signup_examples = pd.DataFrame(
        [
            {
                "event_id": "ev-direct",
                "horizon_days": 7.0,
                "event_start": pd.Timestamp("2026-03-10T17:00:00Z"),
                "title": "Direct Event",
                "current_approved": 12.0,
                "final_approved": 120.0,
            }
        ]
    )
    funnel_examples = pd.DataFrame(
        [
            {
                "event_id": "ev-direct",
                "horizon_days": 7.0,
                "current_applied": 100.0,
                "current_approved_proxy": 12.0,
            },
            {
                "event_id": "ev-sparse",
                "horizon_days": 14.0,
                "event_start": pd.Timestamp("2026-03-20T17:00:00Z"),
                "title": "Sparse Event",
                "current_applied": 90.0,
                "current_approved_proxy": 1.0,
                "previous_approved_proxy": 0.0,
                "previous2_approved_proxy": 0.0,
                "velocity_recent_approved_proxy": 0.1,
                "velocity_prev_approved_proxy": 0.0,
                "acceleration_approved_proxy": 0.1,
                "ratio_to_prev_approved_proxy": 1.0,
                "log_current_approved_proxy": math.log1p(1.0),
                "approved_proxy_x_horizon": 14.0,
                "approved_proxy_velocity_x_horizon": 1.4,
                "final_approved": 140.0,
            },
        ]
    )

    merged = merge_signup_and_funnel_examples(signup_examples, funnel_examples)

    assert set(merged["event_id"]) == {"ev-direct", "ev-sparse"}
    sparse = merged.loc[merged["event_id"] == "ev-sparse"].iloc[0]
    assert sparse["current_approved"] == 1.0
    assert sparse["current_applied"] == 90.0
    assert sparse["final_approved"] == 140.0
