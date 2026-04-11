from __future__ import annotations

import builtins
import sys

import numpy as np
import pandas as pd
import pytest

from scripts.train_waves_v2 import choose_split_sizes, parse_horizons
from cv_rank.waves import posthog_features
from cv_rank.waves.event_strength import compute_event_strength_at_cutoffs
import cv_rank.waves.stage0_funnel as waves_stage0_funnel
from cv_rank.waves.v2_pipeline import (
    accumulate_prior_history_before_snapshot,
    attendance_feature_columns,
    build_application_augmented_signup_examples,
    build_signup_examples,
    build_temporal_split,
    classify_modeling_events,
    derive_future_show_rates,
    event_strength_stage0_features,
    event_posthog_stage0_features,
    event_signup_metadata_features,
    fit_attendance_model,
    fit_signup_model,
    fit_signup_strategy_model,
    horizon_bucket_name,
    is_hackathon_event,
    is_hackathon_title,
    predict_attendance_probabilities,
    predict_signup_totals,
    suspect_attendance_event_ids,
    valid_attendance_event_ids,
)


def sample_signup_df() -> pd.DataFrame:
    rows = []
    for idx in range(25):
        rows.append(
            {
                "event_id": f"ev-{idx}",
                "event_start": pd.Timestamp("2026-01-01") + pd.Timedelta(days=idx),
                "title": f"Event {idx}",
                "city": "San Francisco, CA, USA",
                "approved_t21": 10 + idx,
                "approved_t14": 20 + idx,
                "approved_t7": 40 + idx,
                "approved_t3": 60 + idx,
                "approved_t1": 75 + idx,
                "approved_t0": 90 + idx,
                "actual_attended": 30 + idx,
                "is_platform_hackathon": True,
            }
        )
    return pd.DataFrame(rows)


def sample_attendance_df(size: int, *, horizon_days: int = 7) -> pd.DataFrame:
    rows = []
    log_horizon_days = float(np.log1p(horizon_days))
    for idx in range(size):
        days_before_event = float(horizon_days + 2 + (idx % 6))
        page_views = float(1 + (idx % 5))
        prior_events = float(idx % 4)
        prior_attended = float(min(prior_events, idx % 3))
        notification_read = bool(idx % 2)
        log_page_views = float(np.log1p(page_views))
        elapsed_days = max(days_before_event - horizon_days, 0.25)
        rows.append(
            {
                "days_before_event": days_before_event,
                "horizon_days": float(horizon_days),
                "log_horizon_days": log_horizon_days,
                "page_views": page_views,
                "log_page_views": log_page_views,
                "recent_view_count_7d": float(idx % 4),
                "log_recent_view_count_7d": float(np.log1p(idx % 4)),
                "views_after_approval": float(idx % 3),
                "log_views_after_approval": float(np.log1p(idx % 3)),
                "days_since_last_view_capped": float(min(idx % 9, 8)),
                "distinct_active_view_days": float(1 + (idx % 4)),
                "approval_to_first_view_hours_capped": float(min(idx * 3, 72)),
                "notification_read": notification_read,
                "notification_read_latency_hours": 12.0 if notification_read else 9999.0,
                "notification_latency_known": int(notification_read),
                "notification_read_bucket_same_day": int(notification_read),
                "notification_read_bucket_one_to_three_days": 0,
                "notification_read_bucket_after_three_days": 0,
                "ph_open_messages_count": float(idx % 3),
                "ph_open_messages_recent_7d": float(idx % 2),
                "ph_open_messages_after_approval": float(idx % 2),
                "log_ph_open_messages_count": float(np.log1p(idx % 3)),
                "ph_guest_list_count": float(idx % 2),
                "ph_guest_list_recent_7d": float(idx % 2),
                "log_ph_guest_list_count": float(np.log1p(idx % 2)),
                "ph_chat_send_count": float(idx % 2),
                "ph_chat_send_recent_7d": float(idx % 2),
                "ph_chat_send_after_approval": float(idx % 2),
                "log_ph_chat_send_count": float(np.log1p(idx % 2)),
                "ph_add_to_calendar_count": float(idx % 2),
                "ph_add_to_calendar_flag": float(idx % 2),
                "ph_open_messages_x_horizon": float(np.log1p(idx % 3)) * log_horizon_days,
                "ph_chat_send_x_horizon": float(np.log1p(idx % 2)) * log_horizon_days,
                "ph_calendar_x_horizon": float(idx % 2) * log_horizon_days,
                "ph_commitment_signal_count": float((idx % 3 > 0) + (idx % 2 > 0) + (idx % 2 > 0) + (idx % 2 > 0)),
                "tracked_application_flag": float(idx % 2),
                "tracked_x_horizon": float(idx % 2) * log_horizon_days,
                "invited_user_flag": float(idx % 3 == 0),
                "invited_x_horizon": float(idx % 3 == 0) * log_horizon_days,
                "repeat_builder_flag": float(idx % 4 == 0),
                "repeat_builder_age_days_capped": float(30 + idx),
                "repeat_builder_x_horizon": float(idx % 4 == 0) * log_horizon_days,
                "prior_events": prior_events,
                "prior_attended": prior_attended,
                "prior_rate": (prior_attended / prior_events) if prior_events else -1.0,
                "has_prior_history": int(prior_events > 0),
                "log_prior_events": float(np.log1p(prior_events)),
                "elapsed_days": elapsed_days,
                "page_views_per_elapsed_day": page_views / elapsed_days,
                "recent_views_per_elapsed_day": float(idx % 4) / elapsed_days,
                "page_views_x_horizon": log_page_views * log_horizon_days,
                "recent_views_x_horizon": float(np.log1p(idx % 4)) * log_horizon_days,
                "notif_x_horizon": int(notification_read) * log_horizon_days,
                "notif_x_views": int(notification_read) * log_page_views,
                "tz_offset_hrs": float((idx % 5) - 2),
                "abs_tz_offset_hrs": float(abs((idx % 5) - 2)),
                "recent_signup_flag": int(days_before_event <= 3),
                "late_signup_flag": int(days_before_event <= 7),
                "tz_local": float(idx % 3 == 0),
                "tz_long_haul": float(idx % 4 == 0),
                "showed_up": int((idx % 4) in {1, 2}),
            }
        )
    return pd.DataFrame(rows)


def block_xgboost_import(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__
    sys.modules.pop("xgboost", None)

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):  # type: ignore[no-untyped-def]
        if name == "xgboost":
            raise ImportError("xgboost disabled for fallback test")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)


def test_build_temporal_split_sizes() -> None:
    signup_df = sample_signup_df()
    split = build_temporal_split(signup_df, calibration_events=5, test_events=4)

    assert len(split.train_event_ids) == 16
    assert len(split.calibration_event_ids) == 5
    assert len(split.test_event_ids) == 4
    assert split.test_event_ids[0] == "ev-21"


def test_choose_split_sizes_relaxes_lower_bounds_to_preserve_min_train() -> None:
    calibration_events, test_events = choose_split_sizes(22, requested_calibration=12, requested_test=10)

    assert 22 - calibration_events - test_events >= 15
    assert calibration_events < 5 or test_events < 5


def test_parse_horizons_rejects_duplicates_after_normalization() -> None:
    assert parse_horizons("14, 3,1") == [14, 3, 1]
    with pytest.raises(ValueError, match="Duplicate horizons"):
        parse_horizons("14, 07, 7")


def test_suspect_attendance_filtering() -> None:
    signup_df = sample_signup_df()
    signup_df.loc[signup_df["event_id"] == "ev-2", "actual_attended"] = 1
    signup_df.loc[signup_df["event_id"] == "ev-4", ["approved_t0", "actual_attended"]] = [20, 30]
    suspects = suspect_attendance_event_ids(signup_df)

    assert "ev-2" in suspects
    assert "ev-4" in suspects
    filtered = valid_attendance_event_ids(["ev-1", "ev-2", "ev-3"], signup_df)
    assert filtered == ["ev-1", "ev-3"]


def test_build_signup_examples_has_multiple_horizons() -> None:
    examples = build_signup_examples(sample_signup_df())

    assert set(examples["horizon_days"]) == {14.0, 7.0, 3.0, 1.0}
    assert {"current_approved", "velocity_recent", "final_approved"} <= set(examples.columns)
    assert "event_log_text_chars" in examples.columns


def test_build_signup_examples_keeps_zero_to_positive_velocity_signal() -> None:
    signup_df = sample_signup_df()
    signup_df.loc[signup_df["event_id"] == "ev-0", "approved_t14"] = 0
    signup_df.loc[signup_df["event_id"] == "ev-0", "approved_t7"] = 6

    examples = build_signup_examples(signup_df)
    row = examples[(examples["event_id"] == "ev-0") & (examples["horizon_days"] == 7.0)].iloc[0]

    assert row["velocity_recent"] > 0
    assert row["previous_approved_zero_flag"] == 1.0
    assert row["current_approved_lt5_flag"] == 0.0


def test_event_signup_metadata_features_detect_key_hackathon_signals() -> None:
    features = event_signup_metadata_features(
        {
            "title": "Gemini 3 NYC Hackathon",
            "description": "Build AI agents for real users. Cash prizes and demo day.",
            "description_summary": "AI builders weekend",
            "details": {"tracks": ["agents", "multimodal"]},
            "is_platform_hackathon": True,
            "approval_required": True,
            "capacity": 400,
        }
    )

    assert features["event_is_platform_hackathon"] == 1.0
    assert features["event_approval_required"] == 1.0
    assert features["event_has_capacity_limit"] == 1.0
    assert features["event_mentions_prizes"] == 1.0
    assert features["event_mentions_ai"] == 1.0
    assert features["event_mentions_build"] == 1.0


def test_event_signup_metadata_features_are_disabled_outside_t7_and_t1() -> None:
    features = event_signup_metadata_features(
        {
            "title": "Gemini 3 NYC Hackathon",
            "description": "Build AI agents for real users. Cash prizes and demo day.",
            "is_platform_hackathon": True,
            "approval_required": True,
            "capacity": 400,
        },
        horizon_days=3.0,
    )

    assert set(features.values()) == {0.0}


def test_event_posthog_stage0_features_include_apply_signal() -> None:
    features = event_posthog_stage0_features(
        {
            "approved_t14": 0,
            "approved_t7": 0,
            "ph_pageview_t14": 50,
            "ph_pageview_t7": 150,
            "ph_apply_t14": 10,
            "ph_apply_t7": 80,
            "ph_register_click_t14": 5,
            "ph_register_click_t7": 20,
            "ph_registration_t14": 2,
            "ph_registration_t7": 7,
            "ph_guest_list_t14": 0,
            "ph_guest_list_t7": 3,
            "ph_add_to_calendar_t14": 0,
            "ph_add_to_calendar_t7": 2,
        },
        horizon=7,
    )

    assert features["ph_apply_current"] == 80.0
    assert features["ph_apply_velocity_recent"] == 10.0
    assert features["ph_apply_per_current_approved"] == 80.0
    assert features["ph_apply_per_pageview"] > 0.0


def test_event_strength_stage0_features_include_invite_and_history_signals() -> None:
    features = event_strength_stage0_features(
        {
            "invited_t14": 20,
            "invited_t7": 80,
            "linked_invited_t14": 10,
            "linked_invited_t7": 40,
            "linked_invited_applicant_t14": 4,
            "linked_invited_applicant_t7": 30,
            "tracked_applied_t14": 8,
            "tracked_applied_t7": 60,
            "repeat_builder_applied_t14": 3,
            "repeat_builder_applied_t7": 25,
        },
        horizon=7,
        current_applied=200.0,
    )

    assert features["invited_current"] == 80.0
    assert features["invited_velocity_recent"] > 0.0
    assert features["linked_invited_share_of_invited"] == 0.5
    assert features["tracked_applied_share"] == 0.3
    assert features["repeat_builder_applied_share"] == 0.125


def test_compute_event_strength_at_cutoffs_counts_invites_tracked_and_repeat_builders() -> None:
    first_submission_map = {
        "user-1": pd.Timestamp("2025-01-01T00:00:00Z"),
        "user-2": pd.Timestamp("2026-03-09T00:00:00Z"),
    }
    applicants = [
        {
            "user_id": "user-1",
            "applied_at": pd.Timestamp("2026-03-05T00:00:00Z"),
            "tracked_application": True,
        },
        {
            "user_id": "user-2",
            "applied_at": pd.Timestamp("2026-03-08T00:00:00Z"),
            "tracked_application": False,
        },
    ]
    invites = [
        {
            "user_id": "user-1",
            "invited_at": pd.Timestamp("2026-03-04T00:00:00Z"),
        },
        {
            "user_id": None,
            "invited_at": pd.Timestamp("2026-03-07T00:00:00Z"),
        },
    ]

    counts = compute_event_strength_at_cutoffs(
        applicants,
        invites,
        cutoffs={
            "now": pd.Timestamp("2026-03-09T00:00:00Z"),
            "ago_2d": pd.Timestamp("2026-03-07T00:00:00Z"),
        },
        first_submission_map=first_submission_map,
    )

    assert counts["invited_now"] == 2.0
    assert counts["linked_invited_now"] == 1.0
    assert counts["linked_invited_applicant_now"] == 1.0
    assert counts["tracked_applied_now"] == 1.0
    assert counts["repeat_builder_applied_now"] == 1.0
    assert counts["linked_invited_applicant_ago_2d"] == 1.0


def test_horizon_bucket_name() -> None:
    assert horizon_bucket_name(14) == "t14"
    assert horizon_bucket_name(7) == "t7"
    assert horizon_bucket_name(3) == "t3"
    assert horizon_bucket_name(1) == "t1"


def test_accumulate_prior_history_before_snapshot_excludes_future_from_snapshot() -> None:
    snapshot_time = pd.Timestamp("2026-03-01T00:00:00Z")
    user_history = [
        (pd.Timestamp("2026-02-01T00:00:00Z"), 1.0, 1.0),
        (pd.Timestamp("2026-02-20T00:00:00Z"), 2.0, 1.0),
        (pd.Timestamp("2026-03-05T00:00:00Z"), 3.0, 2.0),
    ]

    prior_events, prior_attended = accumulate_prior_history_before_snapshot(
        user_history,
        snapshot_time=snapshot_time,
    )

    assert prior_events == 3.0
    assert prior_attended == 2.0


def test_derive_future_show_rates_has_distinct_t14_bucket(tmp_path) -> None:
    engagement = pd.DataFrame(
        [
            {"event_id": "ev-1", "signup_bucket": "T-14 to T-7", "showed_up": 0},
            {"event_id": "ev-1", "signup_bucket": "T-7 to T-3", "showed_up": 1},
            {"event_id": "ev-1", "signup_bucket": "T-3 to T-1", "showed_up": 1},
            {"event_id": "ev-1", "signup_bucket": "T-1 to T-0", "showed_up": 1},
        ]
    )
    path = tmp_path / "engagement.csv"
    engagement.to_csv(path, index=False)

    rates = derive_future_show_rates(["ev-1"], engagement_csv_path=path)

    assert rates["t14"] == 0.75
    assert rates["t7"] == 1.0


def test_attendance_feature_columns_include_richer_posthog_features() -> None:
    frame = sample_attendance_df(4)

    cols = attendance_feature_columns(frame)

    assert "recent_view_count_7d" in cols
    assert "log_recent_view_count_7d" in cols
    assert "views_after_approval" in cols
    assert "notification_read_latency_hours" in cols
    assert "notification_latency_known" in cols
    assert "recent_views_x_horizon" in cols
    assert "ph_open_messages_count" in cols
    assert "ph_chat_send_count" in cols
    assert "ph_add_to_calendar_flag" in cols


def test_build_person_attendance_features_gracefully_handles_posthog_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = pd.DataFrame(
        [
            {
                "user_id": "user-1",
                "event_id": "event-1",
                "event_slug": "test-hackathon",
                "event_start": pd.Timestamp("2026-03-10T17:00:00Z"),
                "approved_at": pd.Timestamp("2026-03-01T10:00:00Z"),
            }
        ]
    )

    monkeypatch.setattr(
        posthog_features,
        "load_posthog_config",
        lambda: posthog_features.PostHogConfig(
            api_host="https://example.com",
            project_id="1",
            api_key="secret",
        ),
    )
    monkeypatch.setattr(
        posthog_features,
        "fetch_person_attendance_events",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("timeout")),
    )

    features = posthog_features.build_person_attendance_features(snapshot, horizon_days=7)

    assert features.loc[0, "ph_open_messages_count"] == 0.0
    assert features.loc[0, "ph_chat_send_count"] == 0.0
    assert features.loc[0, "ph_add_to_calendar_flag"] == 0.0


def test_fetch_hourly_stage0_counts_batches_by_signal_and_slug_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[str] = []

    monkeypatch.setattr(
        posthog_features,
        "load_posthog_config",
        lambda: posthog_features.PostHogConfig(
            api_host="https://example.com",
            project_id="1",
            api_key="secret",
        ),
    )

    def fake_run_hogql(query: str, *, name: str, config):  # type: ignore[no-untyped-def]
        queries.append(query)
        return {"columns": ["event_slug", "bucket_time", "count"], "results": []}

    monkeypatch.setattr(posthog_features, "_run_hogql", fake_run_hogql)

    frame = posthog_features.fetch_hourly_stage0_counts(
        event_slugs=["slug-a", "slug-b", "slug-c"],
        since=pd.Timestamp("2026-01-01T00:00:00Z").to_pydatetime(),
        event_map={
            "apply": "completion/apply",
            "registration": "event/registration",
        },
        slug_chunk_size=2,
    )

    assert frame.empty
    assert len(queries) == 4
    assert any("event = 'completion/apply'" in query for query in queries)
    assert any("event = 'event/registration'" in query for query in queries)


def test_build_event_velocity_features_supports_extended_event_maps() -> None:
    events_df = pd.DataFrame(
        [
            {
                "event_id": "event-1",
                "slug": "test-hackathon",
                "event_start": pd.Timestamp("2026-03-10T17:00:00Z"),
            }
        ]
    )
    hourly_counts = pd.DataFrame(
        [
            {
                "event_name": "completion/apply",
                "event_slug": "test-hackathon",
                "bucket_time": pd.Timestamp("2026-03-01T00:00:00Z"),
                "count": 5.0,
            },
            {
                "event_name": "event/open-messages",
                "event_slug": "test-hackathon",
                "bucket_time": pd.Timestamp("2026-03-09T00:00:00Z"),
                "count": 2.0,
            },
        ]
    )

    features = posthog_features.build_event_velocity_features(
        events_df,
        hourly_counts,
        event_map={
            "apply": "completion/apply",
            "open_messages": "event/open-messages",
        },
    )

    assert features.loc[0, "ph_apply_t7"] == 5.0
    assert features.loc[0, "ph_apply_t3"] == 5.0
    assert features.loc[0, "ph_open_messages_t1"] == 2.0


def test_build_application_augmented_signup_examples_includes_low_approved_high_applied_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signup_df = pd.DataFrame(
        [
            {
                "event_id": "ev-1",
                "event_start": pd.Timestamp("2026-01-01"),
                "title": "Gemini 3 Example Hackathon",
                "city": "San Francisco, CA, USA",
                "description": "",
                "description_summary": "",
                "details": "",
                "capacity": 300,
                "approval_required": True,
                "is_platform_hackathon": True,
                "approved_t21": 0,
                "approved_t14": 0,
                "approved_t7": 2,
                "approved_t3": 50,
                "approved_t1": 90,
                "approved_t0": 120,
                "actual_attended": 60,
                "ph_pageview_t21": 10,
                "ph_pageview_t14": 20,
                "ph_pageview_t7": 200,
                "ph_apply_t21": 0,
                "ph_apply_t14": 5,
                "ph_apply_t7": 80,
                "ph_register_click_t21": 0,
                "ph_register_click_t14": 2,
                "ph_register_click_t7": 40,
                "ph_registration_t21": 0,
                "ph_registration_t14": 1,
                "ph_registration_t7": 15,
                "ph_guest_list_t21": 0,
                "ph_guest_list_t14": 0,
                "ph_guest_list_t7": 3,
                "ph_add_to_calendar_t21": 0,
                "ph_add_to_calendar_t14": 0,
                "ph_add_to_calendar_t7": 1,
            }
        ]
    )
    application_df = pd.DataFrame(
        [
            {
                "event_id": "ev-1",
                "event_start": pd.Timestamp("2026-01-01"),
                "applied_t21": 0,
                "applied_t14": 40,
                "applied_t7": 200,
                "applied_t3": 260,
                "applied_t1": 290,
                "applied_t0": 300,
            }
        ]
    )

    monkeypatch.setattr(waves_stage0_funnel, "load_application_velocity_frame", lambda: application_df)

    examples = build_application_augmented_signup_examples(signup_df)
    row = examples[(examples["event_id"] == "ev-1") & (examples["horizon_days"] == 7.0)].iloc[0]

    assert row["current_approved"] == 2.0
    assert row["current_applied"] == 200.0
    assert row["current_pending_proxy"] == 198.0
    assert row["current_approval_rate_proxy"] == 0.01
    assert row["approval_sparse_flag"] == 1.0


def test_is_hackathon_title_avoids_shack_false_positive() -> None:
    assert is_hackathon_title("Gemini 3 NYC Hackathon")
    assert is_hackathon_title("Gemini 3 ハッカソン 東京")
    assert is_hackathon_title("Gemini 3 서울 해커톤")
    assert is_hackathon_title("Gemini 3 SuperHack")
    assert not is_hackathon_title("Shack15 x Cerebral Valley AI Happy Hour")
    assert not is_hackathon_title("AIE World's Fair Afterparty Hack Night @ AgentOps HQ")


def test_is_hackathon_event_uses_platform_flag_but_still_excludes_ancillary_titles() -> None:
    assert is_hackathon_event("Random title", is_platform_hackathon=True)
    assert not is_hackathon_event("Vercel x Equinox v0 Workshop", is_platform_hackathon=True)
    assert not is_hackathon_event("Claude Code Birthday Party & Showcase", is_platform_hackathon=True)
    assert not is_hackathon_event("Claude Code Birthday Party & Showcase", is_platform_hackathon=False)


def test_classify_modeling_events_separates_signup_and_attendance_eligibility() -> None:
    signup_df = pd.DataFrame(
        [
            {
                "event_id": "hack-good",
                "event_start": pd.Timestamp("2026-01-01"),
                "title": "OpenAI Codex Hackathon",
                "city": "San Francisco, CA, USA",
                "approved_t21": 10,
                "approved_t14": 20,
                "approved_t7": 40,
                "approved_t3": 60,
                "approved_t1": 75,
                "approved_t0": 120,
                "actual_attended": 55,
                "is_platform_hackathon": True,
            },
            {
                "event_id": "hack-bad-label",
                "event_start": pd.Timestamp("2026-01-02"),
                "title": "Autonomous Business Hackathon",
                "city": "San Francisco, CA, USA",
                "approved_t21": 10,
                "approved_t14": 20,
                "approved_t7": 40,
                "approved_t3": 60,
                "approved_t1": 75,
                "approved_t0": 120,
                "actual_attended": 0,
                "is_platform_hackathon": True,
            },
            {
                "event_id": "non-hack",
                "event_start": pd.Timestamp("2026-01-03"),
                "title": "Claude Code Birthday Party",
                "city": "San Francisco, CA, USA",
                "approved_t21": 10,
                "approved_t14": 20,
                "approved_t7": 40,
                "approved_t3": 60,
                "approved_t1": 75,
                "approved_t0": 120,
                "actual_attended": 60,
                "is_platform_hackathon": False,
            },
            {
                "event_id": "hack-ancillary",
                "event_start": pd.Timestamp("2026-01-04"),
                "title": "Hackathon Afterparty",
                "city": "San Francisco, CA, USA",
                "approved_t21": 10,
                "approved_t14": 20,
                "approved_t7": 40,
                "approved_t3": 60,
                "approved_t1": 75,
                "approved_t0": 120,
                "actual_attended": 60,
                "is_platform_hackathon": True,
            },
            {
                "event_id": "impossible-label",
                "event_start": pd.Timestamp("2026-01-05"),
                "title": "Gemini 3 NYC Hackathon",
                "city": "New York, NY, USA",
                "approved_t21": 10,
                "approved_t14": 20,
                "approved_t7": 40,
                "approved_t3": 60,
                "approved_t1": 75,
                "approved_t0": 20,
                "actual_attended": 169,
                "is_platform_hackathon": False,
            },
        ]
    )

    classified = classify_modeling_events(signup_df).set_index("event_id")

    assert bool(classified.loc["hack-good", "eligible_for_signup_model"])
    assert bool(classified.loc["hack-good", "eligible_for_attendance_model"])
    assert bool(classified.loc["hack-bad-label", "eligible_for_signup_model"])
    assert not bool(classified.loc["hack-bad-label", "eligible_for_attendance_model"])
    assert not bool(classified.loc["hack-ancillary", "eligible_for_signup_model"])
    assert not bool(classified.loc["hack-ancillary", "eligible_for_attendance_model"])
    assert not bool(classified.loc["non-hack", "eligible_for_signup_model"])
    assert bool(classified.loc["impossible-label", "suspect_attendance_label"])
    assert not bool(classified.loc["impossible-label", "eligible_for_attendance_model"])


def test_fit_models_fall_back_to_hist_gradient_boosting_without_xgboost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    block_xgboost_import(monkeypatch)

    attendance_train = sample_attendance_df(24)
    attendance_cal = sample_attendance_df(8)
    attendance_artifact = fit_attendance_model(attendance_train, attendance_cal)
    attendance_probs = predict_attendance_probabilities(attendance_cal, attendance_artifact)

    signup_examples = build_signup_examples(sample_signup_df())
    train_event_ids = {f"ev-{idx}" for idx in range(18)}
    cal_event_ids = {f"ev-{idx}" for idx in range(18, 23)}
    signup_train = signup_examples[signup_examples["event_id"].isin(train_event_ids)].copy()
    signup_cal = signup_examples[signup_examples["event_id"].isin(cal_event_ids)].copy()
    signup_artifact = fit_signup_model(signup_train, signup_cal)
    signup_preds = predict_signup_totals(signup_cal, signup_artifact)

    assert attendance_artifact["backend"] == "sklearn_hist_gradient_boosting"
    assert signup_artifact["backend"] == "sklearn_hist_gradient_boosting"
    assert attendance_artifact["model"].__class__.__module__.startswith("sklearn.")
    assert signup_artifact["model"].__class__.__module__.startswith("sklearn.")
    assert attendance_probs.shape == (len(attendance_cal),)
    assert np.all((attendance_probs >= 0.0) & (attendance_probs <= 1.0))
    assert signup_preds.shape == (len(signup_cal),)
    assert np.all(signup_preds >= signup_cal["current_approved"].to_numpy(dtype=float))


def test_fit_signup_model_shrinks_bias_corrections() -> None:
    signup_examples = build_signup_examples(sample_signup_df())
    train_event_ids = {f"ev-{idx}" for idx in range(18)}
    cal_event_ids = {f"ev-{idx}" for idx in range(18, 23)}
    signup_train = signup_examples[signup_examples["event_id"].isin(train_event_ids)].copy()
    signup_cal = signup_examples[signup_examples["event_id"].isin(cal_event_ids)].copy()

    artifact = fit_signup_model(signup_train, signup_cal, prior_strength=8.0)
    x_cal = signup_cal.reindex(columns=artifact["feature_columns"], fill_value=0.0).astype(float)
    raw_cal = artifact["model"].predict(x_cal)
    temp = signup_cal.copy()
    temp["residual"] = temp["final_approved"] - raw_cal

    for horizon_value, horizon_df in temp.groupby("horizon_days"):
        raw_mean = float(horizon_df["residual"].mean())
        shrunk = float(artifact["bias_corrections"][str(int(horizon_value))])
        assert abs(shrunk) <= abs(raw_mean) + 1e-9


def test_predict_signup_totals_strategy_uses_horizon_specific_model_choice() -> None:
    class FakeRegressor:
        def __init__(self, outputs: list[float]) -> None:
            self.outputs = np.array(outputs, dtype=float)

        def predict(self, x):  # type: ignore[no-untyped-def]
            return self.outputs[: len(x)]

    frame = pd.DataFrame(
        [
            {"horizon_days": 14.0, "current_approved": 20.0, "feature_a": 1.0},
            {"horizon_days": 7.0, "current_approved": 30.0, "feature_a": 2.0},
        ]
    )
    artifact = {
        "mode": "strategy",
        "baseline": {
            "mode": "single",
            "model": FakeRegressor([50.0, 60.0]),
            "feature_columns": ["feature_a"],
            "bias_corrections": {},
        },
        "posthog": {
            "mode": "single",
            "model": FakeRegressor([150.0, 160.0]),
            "feature_columns": ["feature_a"],
            "bias_corrections": {},
        },
        "strategy_by_horizon": {"7": "posthog", "14": "baseline"},
    }

    preds = predict_signup_totals(frame, artifact)

    assert preds.tolist() == [50.0, 160.0]


def test_predict_signup_totals_strategy_can_use_app_augmented_override() -> None:
    class FakeRegressor:
        def __init__(self, outputs: list[float]) -> None:
            self.outputs = np.array(outputs, dtype=float)

        def predict(self, x):  # type: ignore[no-untyped-def]
            return self.outputs[: len(x)]

    frame = pd.DataFrame(
        [
            {"horizon_days": 7.0, "current_approved": 3.0, "current_applied": 120.0, "feature_a": 1.0},
            {"horizon_days": 3.0, "current_approved": 30.0, "current_applied": np.nan, "feature_a": 2.0},
        ]
    )
    artifact = {
        "mode": "strategy",
        "baseline": {
            "mode": "single",
            "model": FakeRegressor([20.0, 60.0]),
            "feature_columns": ["feature_a"],
            "bias_corrections": {},
        },
        "posthog": {
            "mode": "single",
            "model": FakeRegressor([30.0, 70.0]),
            "feature_columns": ["feature_a"],
            "bias_corrections": {},
        },
        "app_augmented": {
            "mode": "single",
            "model": FakeRegressor([90.0, 99.0]),
            "feature_columns": ["feature_a"],
            "bias_corrections": {},
        },
        "base_strategy_by_horizon": {"7": "posthog", "3": "baseline"},
        "strategy_by_horizon": {"7": "app_augmented", "3": "baseline"},
    }

    preds = predict_signup_totals(frame, artifact)

    assert preds.tolist() == [90.0, 60.0]


def test_predict_signup_totals_strategy_can_use_applications_first_override() -> None:
    class FakeRegressor:
        def __init__(self, outputs: list[float]) -> None:
            self.outputs = np.array(outputs, dtype=float)

        def predict(self, x):  # type: ignore[no-untyped-def]
            return self.outputs[: len(x)]

    frame = pd.DataFrame(
        [
            {"horizon_days": 7.0, "current_approved": 2.0, "current_applied": 200.0, "feature_a": 1.0},
            {"horizon_days": 3.0, "current_approved": 80.0, "current_applied": 100.0, "feature_a": 2.0},
        ]
    )
    artifact = {
        "mode": "strategy",
        "baseline": {
            "mode": "single",
            "model": FakeRegressor([90.0, 60.0]),
            "feature_columns": ["feature_a"],
            "bias_corrections": {},
        },
        "posthog": {
            "mode": "single",
            "model": FakeRegressor([110.0, 70.0]),
            "feature_columns": ["feature_a"],
            "bias_corrections": {},
        },
        "applications_first": {
            "growth_artifact": {
                "feature_cols": ["current_applied"],
                "model": FakeRegressor([150.0, 0.0]),
                "target_col": "remaining_applied",
                "log_target": False,
                "bias_corrections": {},
            },
            "approval_artifact": {
                "feature_cols": ["current_approved", "pred_final_applied"],
                "model": FakeRegressor([220.0, 85.0]),
                "target_col": "final_approved",
                "log_target": False,
                "bias_corrections": {},
            },
            "application_growth_caps": {"7": 10.0, "3": 10.0},
        },
        "base_strategy_by_horizon": {"7": "posthog", "3": "baseline"},
        "strategy_by_horizon": {"7": "applications_first", "3": "baseline"},
    }

    preds = predict_signup_totals(frame, artifact)

    assert preds.tolist() == [220.0, 80.0]


def test_fit_signup_strategy_model_returns_strategy_artifact() -> None:
    signup_examples = build_signup_examples(sample_signup_df())
    train_event_ids = {f"ev-{idx}" for idx in range(18)}
    cal_event_ids = {f"ev-{idx}" for idx in range(18, 23)}
    signup_train = signup_examples[signup_examples["event_id"].isin(train_event_ids)].copy()
    signup_cal = signup_examples[signup_examples["event_id"].isin(cal_event_ids)].copy()

    artifact = fit_signup_strategy_model(signup_train, signup_cal)

    assert artifact["mode"] == "strategy"
    assert {"baseline", "posthog", "strategy_by_horizon"} <= set(artifact)
