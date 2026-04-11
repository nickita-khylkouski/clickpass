"""Benchmark Waves v2 across multiple temporal evaluation protocols.

This script avoids relying on one promoted split by evaluating the current
train/eval pipeline on:
1. The existing single temporal holdout
2. Rolling-origin temporal folds
3. Blocked future holdouts of 2 and 3 events

It also writes:
- per-fold metrics
- per-event predictions
- per-person calibration metrics
- slice summaries for low-approved / high-backlog / family regimes
"""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    log_loss,
    roc_auc_score,
)

REPO_ROOT = Path(__file__).parent.parent
load_dotenv(REPO_ROOT / ".env")
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from scripts.train_waves_v2 import choose_split_sizes, fit_models, parse_horizons
from cv_rank.waves import v2_pipeline as waves_v2_pipeline
from cv_rank.waves.posthog_features import (
    POSTHOG_ATTENDANCE_EVENTS,
    fetch_person_attendance_events,
    load_posthog_config,
)
from cv_rank.waves.stage0_funnel import (
    build_funnel_signup_examples,
    load_application_velocity_frame,
    merge_signup_and_funnel_examples,
    predict_hybrid_signup_totals,
)
from cv_rank.waves.v2_pipeline import (
    TemporalSplit,
    build_application_augmented_signup_examples,
    build_attendance_frame,
    build_signup_examples,
    build_temporal_split,
    classify_modeling_events,
    fetch_attendance_snapshot_rows,
    horizon_bucket_name,
    load_signup_velocity_frame,
    predict_attendance_probabilities,
    predict_signup_totals,
    suspect_attendance_event_ids,
    valid_attendance_event_ids,
)


DEFAULT_CALIBRATION_EVENTS = 4
DEFAULT_ROLLING_TEST_EVENTS = 4
DEFAULT_BLOCKED_MAX_FOLDS = 4
MIN_TRAIN_EVENTS = 14
LOW_APPROVED_T7_THRESHOLD = 10
HIGH_BACKLOG_PENDING_SHARE_T7_THRESHOLD = 0.65
APPLICATION_RICH_T7_THRESHOLD = 180
CALIBRATION_BIN_EDGES = np.linspace(0.0, 1.0, 11)
PERSON_FEATURE_CACHE: dict[tuple[int, str], pd.DataFrame] = {}
PERSON_ATTENDANCE_GROUPS: dict[tuple[str, str, str], list[pd.Timestamp]] | None = None


def _snapshot_cache_key(snapshot_df: pd.DataFrame, *, horizon_days: int) -> tuple[int, str]:
    if snapshot_df.empty:
        return (horizon_days, "empty")
    key_cols = [col for col in ["user_id", "event_id", "event_slug", "event_start", "approved_at"] if col in snapshot_df]
    normalized = snapshot_df[key_cols].copy()
    for col in key_cols:
        if pd.api.types.is_datetime64_any_dtype(normalized[col]):
            normalized[col] = pd.to_datetime(normalized[col], utc=True, errors="coerce").astype(str)
        else:
            normalized[col] = normalized[col].fillna("").astype(str)
    normalized = normalized.sort_values(key_cols).reset_index(drop=True)
    digest = hashlib.sha1(normalized.to_csv(index=False).encode("utf-8")).hexdigest()
    return (horizon_days, digest)


def _cached_build_person_attendance_features(
    snapshot_df: pd.DataFrame,
    *,
    horizon_days: int,
    config: Any | None = None,
) -> pd.DataFrame:
    if PERSON_ATTENDANCE_GROUPS is not None:
        rows: list[dict[str, Any]] = []
        for row in snapshot_df.to_dict("records"):
            user_id = str(row["user_id"])
            event_id = str(row["event_id"])
            event_slug = str(row.get("event_slug") or "").strip().lower()
            event_start = pd.Timestamp(row["event_start"])
            if event_start.tzinfo is None:
                event_start = event_start.tz_localize("UTC")
            snapshot_time = event_start - pd.Timedelta(days=float(horizon_days))
            approved_at = pd.Timestamp(row["approved_at"]) if row.get("approved_at") is not None else None
            if approved_at is not None and approved_at.tzinfo is None:
                approved_at = approved_at.tz_localize("UTC")

            def _count_metrics(event_name: str) -> tuple[float, float, float]:
                timestamps = sorted(PERSON_ATTENDANCE_GROUPS.get((user_id, event_slug, event_name), []))
                total = float(sum(1 for ts in timestamps if ts <= snapshot_time))
                recent = float(
                    sum(1 for ts in timestamps if snapshot_time - pd.Timedelta(days=7) <= ts <= snapshot_time)
                )
                after_approval = 0.0
                if approved_at is not None:
                    after_approval = float(sum(1 for ts in timestamps if approved_at <= ts <= snapshot_time))
                return total, recent, after_approval

            open_total, open_recent, open_after = _count_metrics(POSTHOG_ATTENDANCE_EVENTS["open_messages"])
            guest_total, guest_recent, _ = _count_metrics(POSTHOG_ATTENDANCE_EVENTS["guest_list"])
            chat_total, chat_recent, chat_after = _count_metrics(POSTHOG_ATTENDANCE_EVENTS["chat_send"])
            calendar_total, _, _ = _count_metrics(POSTHOG_ATTENDANCE_EVENTS["add_to_calendar"])

            rows.append(
                {
                    "user_id": user_id,
                    "event_id": event_id,
                    "ph_open_messages_count": open_total,
                    "ph_open_messages_recent_7d": open_recent,
                    "ph_open_messages_after_approval": open_after,
                    "ph_guest_list_count": guest_total,
                    "ph_guest_list_recent_7d": guest_recent,
                    "ph_chat_send_count": chat_total,
                    "ph_chat_send_recent_7d": chat_recent,
                    "ph_chat_send_after_approval": chat_after,
                    "ph_add_to_calendar_count": calendar_total,
                    "ph_add_to_calendar_flag": float(calendar_total > 0),
                }
            )
        return pd.DataFrame(rows)

    cache_key = _snapshot_cache_key(snapshot_df, horizon_days=horizon_days)
    cached = PERSON_FEATURE_CACHE.get(cache_key)
    if cached is not None:
        return cached.copy()
    result = ORIGINAL_BUILD_PERSON_ATTENDANCE_FEATURES(
        snapshot_df,
        horizon_days=horizon_days,
        config=config,
    )
    PERSON_FEATURE_CACHE[cache_key] = result.copy()
    return result


ORIGINAL_BUILD_PERSON_ATTENDANCE_FEATURES = waves_v2_pipeline.build_person_attendance_features
waves_v2_pipeline.build_person_attendance_features = _cached_build_person_attendance_features


def prepare_person_posthog_cache(signup_df: pd.DataFrame, horizons: list[int]) -> None:
    global PERSON_ATTENDANCE_GROUPS

    valid_event_ids = valid_attendance_event_ids(signup_df["event_id"].astype(str).tolist(), signup_df)
    snapshots: list[pd.DataFrame] = []
    for horizon in horizons:
        snapshot = fetch_attendance_snapshot_rows(valid_event_ids, horizon_days=horizon)
        if not snapshot.empty:
            snapshots.append(snapshot)
    if not snapshots:
        PERSON_ATTENDANCE_GROUPS = {}
        return

    person_snapshot_df = pd.concat(snapshots, ignore_index=True)
    keep_cols = [col for col in ["user_id", "event_id", "event_slug", "event_start", "approved_at"] if col in person_snapshot_df]
    person_snapshot_df = person_snapshot_df[keep_cols].drop_duplicates().copy()

    event_slugs = sorted(person_snapshot_df["event_slug"].dropna().astype(str).str.strip().str.lower().unique().tolist())
    user_ids = sorted(person_snapshot_df["user_id"].dropna().astype(str).str.strip().unique().tolist())
    if not event_slugs or not user_ids:
        PERSON_ATTENDANCE_GROUPS = {}
        return

    config = load_posthog_config()
    if config is None:
        PERSON_ATTENDANCE_GROUPS = {}
        return

    event_starts = pd.to_datetime(person_snapshot_df["event_start"], utc=True, errors="coerce")
    since = (event_starts.min() - pd.Timedelta(days=60)).to_pydatetime()
    until = event_starts.max().to_pydatetime()
    raw = fetch_person_attendance_events(
        event_slugs=event_slugs,
        user_ids=user_ids,
        since=since,
        until=until,
        config=config,
    )
    grouped: dict[tuple[str, str, str], list[pd.Timestamp]] = {}
    for row in raw.to_dict("records"):
        key = (str(row["user_id"]), str(row["event_slug"]), str(row["event_name"]))
        grouped.setdefault(key, []).append(pd.Timestamp(row["timestamp"]))
    PERSON_ATTENDANCE_GROUPS = grouped


@dataclass(frozen=True)
class ProtocolSplit:
    protocol: str
    fold: str
    split: TemporalSplit


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="results/waves_v2/benchmark_suite_v1")
    parser.add_argument("--stage0-mode", choices=("direct", "hybrid"), default="direct")
    parser.add_argument("--horizons", default="7,3,1")
    parser.add_argument("--single-calibration-events", type=int, default=12)
    parser.add_argument("--single-test-events", type=int, default=10)
    parser.add_argument("--rolling-folds", type=int, default=3)
    parser.add_argument("--rolling-calibration-events", type=int, default=DEFAULT_CALIBRATION_EVENTS)
    parser.add_argument("--rolling-test-events", type=int, default=DEFAULT_ROLLING_TEST_EVENTS)
    parser.add_argument("--blocked-calibration-events", type=int, default=DEFAULT_CALIBRATION_EVENTS)
    parser.add_argument("--blocked-sizes", default="2,3")
    parser.add_argument("--blocked-max-folds", type=int, default=DEFAULT_BLOCKED_MAX_FOLDS)
    return parser.parse_args()


def parse_int_csv(raw: str) -> list[int]:
    values = [int(part.strip()) for part in raw.split(",") if part.strip()]
    if not values:
        raise ValueError("Expected at least one integer value")
    return values


def family_label(title: str | None) -> str:
    lowered = str(title or "").lower()
    if "gemini" in lowered:
        return "gemini"
    if "openenv" in lowered:
        return "openenv"
    if "nvidia" in lowered:
        return "nvidia"
    if "cartesia" in lowered:
        return "cartesia"
    return "other"


def make_split(event_ids: list[str], *, train_end: int, cal_end: int, test_end: int) -> TemporalSplit:
    return TemporalSplit(
        train_event_ids=tuple(event_ids[:train_end]),
        calibration_event_ids=tuple(event_ids[train_end:cal_end]),
        test_event_ids=tuple(event_ids[cal_end:test_end]),
    )


def build_rolling_origin_splits(
    event_ids: list[str],
    *,
    folds: int,
    calibration_events: int,
    test_events: int,
    min_train_events: int = MIN_TRAIN_EVENTS,
) -> list[ProtocolSplit]:
    max_folds = max((len(event_ids) - min_train_events - calibration_events) // test_events, 0)
    folds = min(folds, max_folds)
    if folds <= 0:
        return []

    first_test_start = len(event_ids) - folds * test_events
    splits: list[ProtocolSplit] = []
    for idx in range(folds):
        test_start = first_test_start + idx * test_events
        cal_start = test_start - calibration_events
        if cal_start < min_train_events:
            continue
        split = make_split(
            event_ids,
            train_end=cal_start,
            cal_end=test_start,
            test_end=test_start + test_events,
        )
        splits.append(ProtocolSplit(protocol="rolling_origin", fold=f"rolling_{idx + 1}", split=split))
    return splits


def build_blocked_future_splits(
    event_ids: list[str],
    *,
    block_size: int,
    calibration_events: int,
    max_folds: int,
    min_train_events: int = MIN_TRAIN_EVENTS,
) -> list[ProtocolSplit]:
    possible_folds = max((len(event_ids) - min_train_events - calibration_events) // block_size, 0)
    folds = min(max_folds, possible_folds)
    if folds <= 0:
        return []

    first_test_start = len(event_ids) - folds * block_size
    splits: list[ProtocolSplit] = []
    for idx in range(folds):
        test_start = first_test_start + idx * block_size
        cal_start = test_start - calibration_events
        if cal_start < min_train_events:
            continue
        split = make_split(
            event_ids,
            train_end=cal_start,
            cal_end=test_start,
            test_end=test_start + block_size,
        )
        splits.append(
            ProtocolSplit(
                protocol=f"blocked_future_k{block_size}",
                fold=f"k{block_size}_{idx + 1}",
                split=split,
            )
        )
    return splits


def safe_auc(y_true: np.ndarray, y_prob: np.ndarray) -> float:
    if len(np.unique(y_true)) < 2:
        return float("nan")
    return float(roc_auc_score(y_true, y_prob))


def safe_pr_auc(y_true: np.ndarray, y_prob: np.ndarray) -> float:
    if len(np.unique(y_true)) < 2:
        return float("nan")
    return float(average_precision_score(y_true, y_prob))


def safe_log_loss(y_true: np.ndarray, y_prob: np.ndarray) -> float:
    clipped = np.clip(y_prob, 1e-6, 1 - 1e-6)
    return float(log_loss(y_true, clipped, labels=[0, 1]))


def summarize_errors(errors: np.ndarray) -> dict[str, float]:
    if len(errors) == 0:
        return {"mae": float("nan"), "bias": float("nan"), "rmse": float("nan")}
    return {
        "mae": float(np.mean(np.abs(errors))),
        "bias": float(np.mean(errors)),
        "rmse": float(np.sqrt(np.mean(errors**2))),
    }


def wape(errors: np.ndarray, actuals: np.ndarray) -> float:
    denom = float(np.sum(np.abs(actuals)))
    if denom <= 0:
        return float("nan")
    return float(np.sum(np.abs(errors)) / denom)


def calibration_bin_rows(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    *,
    protocol: str,
    fold: str,
    horizon: int,
) -> list[dict[str, Any]]:
    if len(y_true) == 0:
        return []
    rows: list[dict[str, Any]] = []
    bin_ids = np.digitize(y_prob, CALIBRATION_BIN_EDGES[1:-1], right=False)
    for idx in range(len(CALIBRATION_BIN_EDGES) - 1):
        mask = bin_ids == idx
        if not np.any(mask):
            continue
        lower = float(CALIBRATION_BIN_EDGES[idx])
        upper = float(CALIBRATION_BIN_EDGES[idx + 1])
        rows.append(
            {
                "protocol": protocol,
                "fold": fold,
                "horizon": horizon,
                "bin_index": idx,
                "bin_lower": lower,
                "bin_upper": upper,
                "count": int(mask.sum()),
                "avg_pred": float(np.mean(y_prob[mask])),
                "avg_actual": float(np.mean(y_true[mask])),
            }
        )
    return rows


def build_signup_test_predictions(
    signup_df: pd.DataFrame,
    split: TemporalSplit,
    signup_artifact: dict[str, Any],
    *,
    stage0_mode: str,
) -> pd.DataFrame:
    if stage0_mode == "hybrid":
        signup_examples = build_signup_examples(signup_df)
        application_df = classify_modeling_events(load_application_velocity_frame())
        clean_application_df = application_df[
            application_df["eligible_for_signup_model"]
            & (application_df["approval_notification_gap_t0"].abs() <= 10)
        ].copy()
        funnel_examples = build_funnel_signup_examples(clean_application_df)
        signup_examples = merge_signup_and_funnel_examples(signup_examples, funnel_examples)
        signup_test = signup_examples[signup_examples["event_id"].isin(split.test_event_ids)].copy()
        signup_test["pred_final_approved"] = predict_hybrid_signup_totals(
            signup_test,
            signup_artifact,
            direct_predict_fn=predict_signup_totals,
        )
        return signup_test

    signup_examples = build_application_augmented_signup_examples(signup_df)
    signup_test = signup_examples[signup_examples["event_id"].isin(split.test_event_ids)].copy()
    signup_test["pred_final_approved"] = predict_signup_totals(signup_test, signup_artifact)
    return signup_test


def evaluate_split_detailed(
    signup_df: pd.DataFrame,
    protocol_split: ProtocolSplit,
    *,
    attendance_artifact: dict[str, Any],
    signup_artifact: dict[str, Any],
    future_show_rates: dict[str, float],
    horizons: list[int],
    stage0_mode: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    split = protocol_split.split
    signup_lookup = signup_df.set_index("event_id")
    suspect_ids = suspect_attendance_event_ids(signup_df)
    signup_test = build_signup_test_predictions(signup_df, split, signup_artifact, stage0_mode=stage0_mode)

    event_rows: list[dict[str, Any]] = []
    signup_rows: list[dict[str, Any]] = []
    person_rows: list[dict[str, Any]] = []
    calibration_rows: list[dict[str, Any]] = []
    fold_rows: list[dict[str, Any]] = []

    for horizon in horizons:
        horizon_signup_all = signup_test[signup_test["horizon_days"] == float(horizon)].copy()
        if not horizon_signup_all.empty:
            horizon_signup_all["signup_error"] = (
                horizon_signup_all["pred_final_approved"] - horizon_signup_all["final_approved"]
            )
            horizon_signup_all["protocol"] = protocol_split.protocol
            horizon_signup_all["fold"] = protocol_split.fold
            horizon_signup_all["horizon"] = horizon
            horizon_signup_all["family"] = horizon_signup_all["title"].map(family_label)
            if "current_pending_proxy" not in horizon_signup_all.columns:
                horizon_signup_all["current_pending_proxy"] = (
                    horizon_signup_all["current_applied"] - horizon_signup_all["current_approved"]
                ).clip(lower=0)
            horizon_signup_all["pending_share_t7"] = np.where(
                horizon_signup_all["current_applied"].to_numpy(dtype=float) > 0,
                horizon_signup_all["current_pending_proxy"].to_numpy(dtype=float)
                / np.maximum(horizon_signup_all["current_applied"].to_numpy(dtype=float), 1.0),
                0.0,
            )
            horizon_signup_all["low_approved_t7"] = (
                (horizon == 7) & (horizon_signup_all["current_approved"] <= LOW_APPROVED_T7_THRESHOLD)
            ).astype(int)
            horizon_signup_all["zero_approved_t7"] = (
                (horizon == 7) & (horizon_signup_all["current_approved"] == 0)
            ).astype(int)
            horizon_signup_all["high_backlog_t7"] = (
                (horizon == 7)
                & (horizon_signup_all["pending_share_t7"] >= HIGH_BACKLOG_PENDING_SHARE_T7_THRESHOLD)
            ).astype(int)
            horizon_signup_all["application_rich_approval_sparse_t7"] = (
                (horizon == 7)
                & (horizon_signup_all["current_applied"] >= APPLICATION_RICH_T7_THRESHOLD)
                & (horizon_signup_all["current_approved"] <= LOW_APPROVED_T7_THRESHOLD)
            ).astype(int)
            signup_rows.extend(horizon_signup_all.to_dict("records"))

        valid_test_ids = [event_id for event_id in split.test_event_ids if event_id not in suspect_ids]
        snapshot = fetch_attendance_snapshot_rows(valid_test_ids, horizon_days=horizon)
        attendance_test = build_attendance_frame(snapshot, horizon_days=horizon)
        if attendance_test.empty:
            continue

        probs = predict_attendance_probabilities(attendance_test, attendance_artifact)
        attendance_test = attendance_test.copy()
        attendance_test["pred_p_show"] = probs
        attendance_test["protocol"] = protocol_split.protocol
        attendance_test["fold"] = protocol_split.fold
        attendance_test["horizon_days"] = horizon

        y_true = attendance_test["showed_up"].to_numpy(dtype=int)
        y_prob = attendance_test["pred_p_show"].to_numpy(dtype=float)
        person_auc = safe_auc(y_true, y_prob)
        person_pr_auc = safe_pr_auc(y_true, y_prob)
        person_brier = float(brier_score_loss(y_true, np.clip(y_prob, 0.0, 1.0)))
        person_log_loss = safe_log_loss(y_true, y_prob)

        calibration_rows.extend(
            calibration_bin_rows(
                y_true,
                y_prob,
                protocol=protocol_split.protocol,
                fold=protocol_split.fold,
                horizon=horizon,
            )
        )

        person_rows.extend(
            attendance_test[
                [
                    "event_id",
                    "user_id",
                    "showed_up",
                    "pred_p_show",
                    "title",
                    "days_before_event",
                    "page_views",
                    "recent_view_count_7d",
                    "notification_read",
                    "protocol",
                    "fold",
                    "horizon_days",
                ]
            ].to_dict("records")
        )

        current_agg = (
            attendance_test.groupby("event_id")
            .agg(
                pred_current=("pred_p_show", "sum"),
                actual_current=("showed_up", "sum"),
                current_approved=("user_id", "count"),
            )
            .reset_index()
        )

        horizon_signup = signup_test[signup_test["horizon_days"] == float(horizon)].copy()
        horizon_signup_cols = [
            "event_id",
            "pred_final_approved",
            "final_approved",
            "current_applied",
            "current_pending_proxy",
            "pending_to_current_approved_ratio",
            "approval_sparse_flag",
            "current_applied_lt20_flag",
            "title",
        ]
        available_signup_cols = [col for col in horizon_signup_cols if col in horizon_signup.columns]
        merged = current_agg.merge(horizon_signup[available_signup_cols], on="event_id", how="left")
        merged["pred_final_approved"] = merged["pred_final_approved"].fillna(merged["current_approved"])
        merged["final_approved"] = merged["event_id"].map(signup_lookup["approved_t0"])
        merged["actual_total_attendance"] = merged["event_id"].map(signup_lookup["actual_attended"])
        merged["title"] = merged["title"].fillna(merged["event_id"].map(signup_lookup["title"]))
        merged["current_applied"] = pd.to_numeric(merged.get("current_applied"), errors="coerce").fillna(
            merged["current_approved"]
        )
        merged["current_pending_proxy"] = pd.to_numeric(
            merged.get("current_pending_proxy"), errors="coerce"
        ).fillna((merged["current_applied"] - merged["current_approved"]).clip(lower=0))
        merged["pending_to_current_approved_ratio"] = pd.to_numeric(
            merged.get("pending_to_current_approved_ratio"), errors="coerce"
        ).replace([np.inf, -np.inf], np.nan)
        safe_denom = np.maximum(merged["current_approved"].to_numpy(dtype=float), 1.0)
        merged["pending_to_current_approved_ratio"] = merged["pending_to_current_approved_ratio"].fillna(
            merged["current_pending_proxy"] / safe_denom
        )
        merged["approval_sparse_flag"] = pd.to_numeric(
            merged.get("approval_sparse_flag"), errors="coerce"
        ).fillna((merged["current_approved"] < 5).astype(float))
        merged["current_applied_lt20_flag"] = pd.to_numeric(
            merged.get("current_applied_lt20_flag"), errors="coerce"
        ).fillna((merged["current_applied"] < 20).astype(float))

        additional_approved_pred = np.maximum(
            merged["pred_final_approved"].to_numpy(dtype=float) - merged["current_approved"].to_numpy(dtype=float),
            0.0,
        )
        actual_additional_approved = np.maximum(
            merged["final_approved"].to_numpy(dtype=float) - merged["current_approved"].to_numpy(dtype=float),
            0.0,
        )

        horizon_name = horizon_bucket_name(horizon)
        merged["pred_total_attendance"] = (
            merged["pred_current"] + additional_approved_pred * future_show_rates[horizon_name]
        )
        merged["signup_error"] = merged["pred_final_approved"] - merged["final_approved"]
        merged["current_error"] = merged["pred_current"] - merged["actual_current"]
        merged["total_error"] = merged["pred_total_attendance"] - merged["actual_total_attendance"]
        merged["additional_approved_pred"] = additional_approved_pred
        merged["actual_additional_approved"] = actual_additional_approved
        merged["future_show_rate"] = future_show_rates[horizon_name]
        merged["protocol"] = protocol_split.protocol
        merged["fold"] = protocol_split.fold
        merged["horizon"] = horizon
        merged["family"] = merged["title"].map(family_label)
        pending_share_t7 = np.where(
            merged["current_applied"].to_numpy(dtype=float) > 0,
            merged["current_pending_proxy"].to_numpy(dtype=float)
            / np.maximum(merged["current_applied"].to_numpy(dtype=float), 1.0),
            0.0,
        )
        merged["pending_share_t7"] = pending_share_t7
        merged["low_approved_t7"] = (
            (horizon == 7) & (merged["current_approved"] <= LOW_APPROVED_T7_THRESHOLD)
        ).astype(int)
        merged["zero_approved_t7"] = ((horizon == 7) & (merged["current_approved"] == 0)).astype(int)
        merged["high_backlog_t7"] = (
            (horizon == 7) & (merged["pending_share_t7"] >= HIGH_BACKLOG_PENDING_SHARE_T7_THRESHOLD)
        ).astype(int)
        merged["application_rich_approval_sparse_t7"] = (
            (horizon == 7)
            & (merged["current_applied"] >= APPLICATION_RICH_T7_THRESHOLD)
            & (merged["current_approved"] <= LOW_APPROVED_T7_THRESHOLD)
        ).astype(int)

        event_rows.extend(merged.to_dict("records"))

        total_errors = merged["total_error"].to_numpy(dtype=float)
        current_errors = merged["current_error"].to_numpy(dtype=float)
        signup_errors = horizon_signup_all["signup_error"].to_numpy(dtype=float)
        actual_totals = merged["actual_total_attendance"].to_numpy(dtype=float)
        actual_current = merged["actual_current"].to_numpy(dtype=float)
        actual_final_approved = horizon_signup_all["final_approved"].to_numpy(dtype=float)
        signup_stats = summarize_errors(signup_errors)
        fold_rows.append(
            {
                "protocol": protocol_split.protocol,
                "fold": protocol_split.fold,
                "horizon": horizon,
                "n_events": int(len(merged)),
                "n_signup_events": int(len(horizon_signup_all)),
                "n_people": int(len(attendance_test)),
                "attendance_mae": summarize_errors(total_errors)["mae"],
                "attendance_bias": summarize_errors(total_errors)["bias"],
                "attendance_rmse": summarize_errors(total_errors)["rmse"],
                "attendance_wape": wape(total_errors, actual_totals),
                "current_mae": summarize_errors(current_errors)["mae"],
                "current_bias": summarize_errors(current_errors)["bias"],
                "current_wape": wape(current_errors, actual_current),
                "signup_mae": signup_stats["mae"],
                "signup_bias": signup_stats["bias"],
                "signup_rmse": signup_stats["rmse"],
                "signup_wape": wape(signup_errors, actual_final_approved),
                "person_auc": person_auc,
                "person_pr_auc": person_pr_auc,
                "person_brier": person_brier,
                "person_log_loss": person_log_loss,
            }
        )

    return (
        pd.DataFrame(fold_rows),
        pd.DataFrame(event_rows),
        pd.DataFrame(signup_rows),
        pd.DataFrame(person_rows),
        pd.DataFrame(calibration_rows),
    )


def summarize_protocol_metrics(fold_metrics_df: pd.DataFrame) -> pd.DataFrame:
    if fold_metrics_df.empty:
        return pd.DataFrame()
    return (
        fold_metrics_df.groupby(["protocol", "horizon"], as_index=False)
        .agg(
            folds=("fold", "nunique"),
            attendance_mae_mean=("attendance_mae", "mean"),
            attendance_mae_median=("attendance_mae", "median"),
            attendance_bias_mean=("attendance_bias", "mean"),
            attendance_wape_mean=("attendance_wape", "mean"),
            signup_mae_mean=("signup_mae", "mean"),
            signup_bias_mean=("signup_bias", "mean"),
            signup_wape_mean=("signup_wape", "mean"),
            person_auc_mean=("person_auc", "mean"),
            person_pr_auc_mean=("person_pr_auc", "mean"),
            person_brier_mean=("person_brier", "mean"),
            person_log_loss_mean=("person_log_loss", "mean"),
            total_events=("n_events", "sum"),
            total_signup_events=("n_signup_events", "sum"),
            total_people=("n_people", "sum"),
        )
        .sort_values(["protocol", "horizon"])
    )


def build_slice_summary(event_df: pd.DataFrame) -> pd.DataFrame:
    if event_df.empty:
        return pd.DataFrame()

    slice_frames: list[pd.DataFrame] = []

    def add_slice(slice_name: str, mask: pd.Series) -> None:
        subset = event_df[mask].copy()
        if subset.empty:
            return
        grouped = (
            subset.groupby(["protocol", "horizon"], as_index=False)
            .agg(
                n_events=("event_id", "count"),
                attendance_mae=("total_error", lambda s: float(np.mean(np.abs(s)))),
                attendance_bias=("total_error", "mean"),
                signup_mae=("signup_error", lambda s: float(np.mean(np.abs(s)))),
                signup_bias=("signup_error", "mean"),
                current_mae=("current_error", lambda s: float(np.mean(np.abs(s)))),
                current_bias=("current_error", "mean"),
            )
            .assign(slice=slice_name)
        )
        slice_frames.append(grouped)

    add_slice("all_events", event_df["event_id"].notna())
    add_slice("low_approved_t7", (event_df["horizon"] == 7) & (event_df["low_approved_t7"] == 1))
    add_slice("zero_approved_t7", (event_df["horizon"] == 7) & (event_df["zero_approved_t7"] == 1))
    add_slice("high_backlog_t7", (event_df["horizon"] == 7) & (event_df["high_backlog_t7"] == 1))
    add_slice(
        "application_rich_approval_sparse_t7",
        (event_df["horizon"] == 7) & (event_df["application_rich_approval_sparse_t7"] == 1),
    )

    for family in sorted(event_df["family"].dropna().unique()):
        add_slice(f"family:{family}", event_df["family"] == family)

    if not slice_frames:
        return pd.DataFrame()
    return pd.concat(slice_frames, ignore_index=True).sort_values(["slice", "protocol", "horizon"])


def write_markdown_summary(
    output_path: Path,
    *,
    metadata: dict[str, Any],
    aggregate_df: pd.DataFrame,
    slice_df: pd.DataFrame,
) -> None:
    lines = [
        "# Waves v2 Benchmark Suite",
        "",
        "## Configuration",
        "",
        f"- Stage 0 mode: `{metadata['stage0_mode']}`",
        f"- Horizons: `{metadata['horizons']}`",
        f"- Hackathon events: `{metadata['hackathon_event_count']}`",
        f"- Reliable attendance events: `{metadata['reliable_attendance_event_count']}`",
        "",
        "## Aggregate Metrics",
        "",
    ]
    if aggregate_df.empty:
        lines.append("No aggregate metrics were produced.")
    else:
        for row in aggregate_df.to_dict("records"):
            lines.append(
                "- "
                f"{row['protocol']} T-{int(row['horizon'])}: "
                f"attendance MAE `{row['attendance_mae_mean']:.1f}` "
                f"(median `{row['attendance_mae_median']:.1f}`), "
                f"bias `{row['attendance_bias_mean']:+.1f}`, "
                f"signup MAE `{row['signup_mae_mean']:.1f}`, "
                f"AUC `{row['person_auc_mean']:.3f}`, "
                f"Brier `{row['person_brier_mean']:.3f}`"
            )

    if not slice_df.empty:
        lines.extend(
            [
                "",
                "## Key Slices",
                "",
            ]
        )
        top_slice_rows = slice_df[slice_df["slice"] != "all_events"].head(18)
        for row in top_slice_rows.to_dict("records"):
            lines.append(
                "- "
                f"{row['slice']} | {row['protocol']} T-{int(row['horizon'])}: "
                f"attendance MAE `{row['attendance_mae']:.1f}`, "
                f"bias `{row['attendance_bias']:+.1f}`, "
                f"n `{int(row['n_events'])}`"
            )

    output_path.write_text("\n".join(lines) + "\n")


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    horizons = parse_horizons(args.horizons)
    blocked_sizes = parse_int_csv(args.blocked_sizes)

    raw_signup_df = load_signup_velocity_frame()
    event_audit = classify_modeling_events(raw_signup_df)
    signup_df = event_audit[event_audit["eligible_for_signup_model"]].copy()
    signup_df = signup_df.sort_values("event_start").reset_index(drop=True)
    event_ids = signup_df["event_id"].astype(str).tolist()
    PERSON_FEATURE_CACHE.clear()
    prepare_person_posthog_cache(signup_df, horizons)

    single_calibration, single_test = choose_split_sizes(
        len(signup_df),
        args.single_calibration_events,
        args.single_test_events,
        min_train=MIN_TRAIN_EVENTS,
        min_calibration=4,
        min_test=4,
    )
    protocol_splits: list[ProtocolSplit] = [
        ProtocolSplit(
            protocol="single_holdout",
            fold="single_holdout",
            split=build_temporal_split(
                signup_df,
                calibration_events=single_calibration,
                test_events=single_test,
            ),
        )
    ]
    protocol_splits.extend(
        build_rolling_origin_splits(
            event_ids,
            folds=args.rolling_folds,
            calibration_events=args.rolling_calibration_events,
            test_events=args.rolling_test_events,
        )
    )
    for block_size in blocked_sizes:
        protocol_splits.extend(
            build_blocked_future_splits(
                event_ids,
                block_size=block_size,
                calibration_events=args.blocked_calibration_events,
                max_folds=args.blocked_max_folds,
            )
        )

    all_fold_metrics: list[pd.DataFrame] = []
    all_event_rows: list[pd.DataFrame] = []
    all_signup_rows: list[pd.DataFrame] = []
    all_person_rows: list[pd.DataFrame] = []
    all_calibration_rows: list[pd.DataFrame] = []

    for index, protocol_split in enumerate(protocol_splits, start=1):
        print(
            f"[{index}/{len(protocol_splits)}] "
            f"{protocol_split.protocol}/{protocol_split.fold} "
            f"train={len(protocol_split.split.train_event_ids)} "
            f"cal={len(protocol_split.split.calibration_event_ids)} "
            f"test={len(protocol_split.split.test_event_ids)}",
            flush=True,
        )
        attendance_artifact, signup_artifact, future_show_rates, _, _ = fit_models(
            signup_df,
            protocol_split.split,
            horizons,
            stage0_mode=args.stage0_mode,
        )
        fold_metrics_df, event_rows_df, signup_rows_df, person_rows_df, calibration_rows_df = evaluate_split_detailed(
            signup_df,
            protocol_split,
            attendance_artifact=attendance_artifact,
            signup_artifact=signup_artifact,
            future_show_rates=future_show_rates,
            horizons=horizons,
            stage0_mode=args.stage0_mode,
        )
        if not fold_metrics_df.empty:
            all_fold_metrics.append(fold_metrics_df)
        if not event_rows_df.empty:
            all_event_rows.append(event_rows_df)
        if not signup_rows_df.empty:
            all_signup_rows.append(signup_rows_df)
        if not person_rows_df.empty:
            all_person_rows.append(person_rows_df)
        if not calibration_rows_df.empty:
            all_calibration_rows.append(calibration_rows_df)
        if not fold_metrics_df.empty:
            horizon_summaries = ", ".join(
                f"T-{int(row['horizon'])}:att_mae={row['attendance_mae']:.1f},signup_mae={row['signup_mae']:.1f}"
                for row in fold_metrics_df.to_dict("records")
            )
            print(f"  completed {protocol_split.fold}: {horizon_summaries}", flush=True)

    fold_metrics_df = pd.concat(all_fold_metrics, ignore_index=True) if all_fold_metrics else pd.DataFrame()
    event_rows_df = pd.concat(all_event_rows, ignore_index=True) if all_event_rows else pd.DataFrame()
    signup_rows_df = pd.concat(all_signup_rows, ignore_index=True) if all_signup_rows else pd.DataFrame()
    person_rows_df = pd.concat(all_person_rows, ignore_index=True) if all_person_rows else pd.DataFrame()
    calibration_rows_df = (
        pd.concat(all_calibration_rows, ignore_index=True) if all_calibration_rows else pd.DataFrame()
    )

    aggregate_df = summarize_protocol_metrics(fold_metrics_df)
    slice_df = build_slice_summary(event_rows_df)

    metadata = {
        "stage0_mode": args.stage0_mode,
        "horizons": horizons,
        "raw_event_count": int(len(raw_signup_df)),
        "hackathon_event_count": int(len(signup_df)),
        "reliable_attendance_event_count": int(event_audit["eligible_for_attendance_model"].sum()),
        "protocols": [
            {
                "protocol": item.protocol,
                "fold": item.fold,
                "train_events": len(item.split.train_event_ids),
                "calibration_events": len(item.split.calibration_event_ids),
                "test_events": len(item.split.test_event_ids),
                "train_event_ids": list(item.split.train_event_ids),
                "calibration_event_ids": list(item.split.calibration_event_ids),
                "test_event_ids": list(item.split.test_event_ids),
            }
            for item in protocol_splits
        ],
        "slice_thresholds": {
            "low_approved_t7_threshold": LOW_APPROVED_T7_THRESHOLD,
            "high_backlog_pending_share_t7_threshold": HIGH_BACKLOG_PENDING_SHARE_T7_THRESHOLD,
            "application_rich_t7_threshold": APPLICATION_RICH_T7_THRESHOLD,
        },
    }

    fold_metrics_df.to_csv(output_dir / "fold_metrics.csv", index=False)
    event_rows_df.to_csv(output_dir / "event_predictions.csv", index=False)
    signup_rows_df.to_csv(output_dir / "signup_predictions.csv", index=False)
    person_rows_df.to_csv(output_dir / "person_predictions.csv", index=False)
    calibration_rows_df.to_csv(output_dir / "calibration_bins.csv", index=False)
    aggregate_df.to_csv(output_dir / "aggregate_metrics.csv", index=False)
    slice_df.to_csv(output_dir / "slice_metrics.csv", index=False)
    with (output_dir / "metadata.json").open("w") as f:
        json.dump(metadata, f, indent=2)
    with (output_dir / "aggregate_metrics.json").open("w") as f:
        json.dump(aggregate_df.to_dict("records"), f, indent=2)
    write_markdown_summary(output_dir / "README.md", metadata=metadata, aggregate_df=aggregate_df, slice_df=slice_df)

    print("Protocols executed:")
    for item in protocol_splits:
        print(
            f"  {item.protocol}/{item.fold}: "
            f"train={len(item.split.train_event_ids)} "
            f"cal={len(item.split.calibration_event_ids)} "
            f"test={len(item.split.test_event_ids)}"
        )
    print("\nAggregate metrics:")
    if aggregate_df.empty:
        print("  No metrics produced.")
    else:
        print(aggregate_df.to_string(index=False))
    print(f"\nArtifacts written to {output_dir}")


if __name__ == "__main__":
    main()
