from __future__ import annotations

import pandas as pd

from scripts.benchmark_waves_v2 import (
    APPLICATION_RICH_T7_THRESHOLD,
    HIGH_BACKLOG_PENDING_SHARE_T7_THRESHOLD,
    LOW_APPROVED_T7_THRESHOLD,
    build_blocked_future_splits,
    build_rolling_origin_splits,
    build_slice_summary,
    family_label,
)


def test_build_rolling_origin_splits_uses_expanding_train_and_non_overlapping_tests() -> None:
    event_ids = [f"e{i}" for i in range(30)]
    splits = build_rolling_origin_splits(
        event_ids,
        folds=3,
        calibration_events=4,
        test_events=4,
    )

    assert [item.fold for item in splits] == ["rolling_1", "rolling_2", "rolling_3"]
    assert len(splits[0].split.train_event_ids) == 14
    assert list(splits[0].split.calibration_event_ids) == ["e14", "e15", "e16", "e17"]
    assert list(splits[0].split.test_event_ids) == ["e18", "e19", "e20", "e21"]
    assert list(splits[2].split.test_event_ids) == ["e26", "e27", "e28", "e29"]


def test_build_blocked_future_splits_respects_block_size() -> None:
    event_ids = [f"e{i}" for i in range(30)]
    splits = build_blocked_future_splits(
        event_ids,
        block_size=3,
        calibration_events=4,
        max_folds=3,
    )

    assert [item.fold for item in splits] == ["k3_1", "k3_2", "k3_3"]
    assert list(splits[0].split.test_event_ids) == ["e21", "e22", "e23"]
    assert list(splits[1].split.test_event_ids) == ["e24", "e25", "e26"]
    assert list(splits[2].split.test_event_ids) == ["e27", "e28", "e29"]


def test_family_label_detects_known_event_families() -> None:
    assert family_label("Gemini 3 Singapore Hackathon") == "gemini"
    assert family_label("OpenEnv Hackathon SF") == "openenv"
    assert family_label("Cartesia Build Day") == "cartesia"
    assert family_label("Random Hackathon") == "other"


def test_build_slice_summary_includes_t7_low_approved_and_high_backlog_slices() -> None:
    event_df = pd.DataFrame(
        [
            {
                "event_id": "a",
                "protocol": "single_holdout",
                "horizon": 7,
                "low_approved_t7": 1,
                "zero_approved_t7": 1,
                "high_backlog_t7": 0,
                "family": "gemini",
                "total_error": 10.0,
                "signup_error": 20.0,
                "current_error": 3.0,
            },
            {
                "event_id": "b",
                "protocol": "single_holdout",
                "horizon": 7,
                "low_approved_t7": 0,
                "zero_approved_t7": 0,
                "high_backlog_t7": 1,
                "application_rich_approval_sparse_t7": 1,
                "family": "openenv",
                "total_error": -4.0,
                "signup_error": -7.0,
                "current_error": 1.0,
            },
        ]
    )

    summary = build_slice_summary(event_df)

    assert "all_events" in set(summary["slice"])
    assert "low_approved_t7" in set(summary["slice"])
    assert "zero_approved_t7" in set(summary["slice"])
    assert "high_backlog_t7" in set(summary["slice"])
    assert "application_rich_approval_sparse_t7" in set(summary["slice"])
    assert "family:gemini" in set(summary["slice"])
    assert "family:openenv" in set(summary["slice"])


def test_slice_threshold_constants_are_reasonable() -> None:
    assert LOW_APPROVED_T7_THRESHOLD == 10
    assert HIGH_BACKLOG_PENDING_SHARE_T7_THRESHOLD == 0.65
    assert APPLICATION_RICH_T7_THRESHOLD == 180
