"""Train and evaluate the rebuilt Waves v2 attendance pipeline.

Pipeline:
1. Temporal event split: train / calibration / test
2. Stage 0 signup-volume model (event-level)
3. Direct attendance model for currently approved applicants (person-level, horizon-aware)
4. Stage 3 future-approved show-rate estimates from non-suspect train+cal events
5. Held-out evaluation by horizon
"""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sklearn.metrics import roc_auc_score

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cv_rank.waves.v2_pipeline import (
    TemporalSplit,
    build_application_augmented_signup_examples,
    build_signup_examples,
    build_temporal_split,
    build_attendance_frame,
    classify_modeling_events,
    derive_future_show_rates,
    fetch_attendance_snapshot_rows,
    fit_attendance_model,
    fit_signup_model,
    fit_signup_strategy_model,
    horizon_bucket_name,
    load_signup_velocity_frame,
    predict_attendance_probabilities,
    predict_signup_totals,
    save_artifact_bundle,
    suspect_attendance_event_ids,
    valid_attendance_event_ids,
)
from cv_rank.waves.stage0_funnel import (
    build_funnel_signup_examples,
    fit_hybrid_signup_artifact,
    load_application_velocity_frame,
    merge_signup_and_funnel_examples,
    predict_hybrid_signup_totals,
)


SUPPORTED_HORIZONS = (14, 7, 3, 1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="results/waves_v2/latest")
    parser.add_argument("--calibration-events", type=int, default=12)
    parser.add_argument("--test-events", type=int, default=10)
    parser.add_argument("--horizons", default="14,7,3,1")
    parser.add_argument(
        "--stage0-mode",
        choices=("direct", "hybrid"),
        default="direct",
        help="Signup forecasting path to use (default: direct).",
    )
    return parser.parse_args()


def parse_horizons(value: str) -> list[int]:
    raw_items = [item.strip() for item in value.split(",") if item.strip()]
    if not raw_items:
        raise ValueError("At least one horizon must be provided")

    horizons: list[int] = []
    duplicates: list[int] = []
    seen: set[int] = set()
    for item in raw_items:
        try:
            horizon = int(item)
        except ValueError as exc:
            raise ValueError(f"Invalid horizon value: {item!r}") from exc
        horizons.append(horizon)
        if horizon in seen and horizon not in duplicates:
            duplicates.append(horizon)
        seen.add(horizon)

    unknown = set(horizons) - set(SUPPORTED_HORIZONS)
    if unknown:
        raise ValueError(f"Unsupported horizons: {sorted(unknown)}")
    if duplicates:
        raise ValueError(f"Duplicate horizons are not allowed: {duplicates}")
    return horizons


def event_id_set(split: TemporalSplit, part: str) -> set[str]:
    return set(getattr(split, f"{part}_event_ids"))


def choose_split_sizes(
    total_events: int,
    requested_calibration: int,
    requested_test: int,
    *,
    min_train: int = 15,
    min_calibration: int = 5,
    min_test: int = 5,
) -> tuple[int, int]:
    calibration_events = requested_calibration
    test_events = requested_test
    if total_events > calibration_events + test_events and (total_events - calibration_events - test_events) >= min_train:
        return calibration_events, test_events

    test_events = max(min_test, min(requested_test, round(total_events * 0.25)))
    calibration_events = max(min_calibration, min(requested_calibration, round(total_events * 0.2)))

    def split_is_safe(calibration_size: int, test_size: int) -> bool:
        return (
            total_events > calibration_size + test_size
            and (total_events - calibration_size - test_size) >= min_train
        )

    def reduce_toward_floor(
        calibration_size: int,
        test_size: int,
        *,
        calibration_floor: int,
        test_floor: int,
    ) -> tuple[int, int, bool]:
        calibration_slack = calibration_size - calibration_floor
        test_slack = test_size - test_floor
        if calibration_slack <= 0 and test_slack <= 0:
            return calibration_size, test_size, False
        if calibration_slack >= test_slack and calibration_slack > 0:
            return calibration_size - 1, test_size, True
        if test_slack > 0:
            return calibration_size, test_size - 1, True
        return calibration_size, test_size, False

    for calibration_floor, test_floor in (
        (min_calibration, min_test),
        (1, 1),
    ):
        while not split_is_safe(calibration_events, test_events):
            calibration_events, test_events, changed = reduce_toward_floor(
                calibration_events,
                test_events,
                calibration_floor=calibration_floor,
                test_floor=test_floor,
            )
            if not changed:
                break

    if not split_is_safe(calibration_events, test_events):
        raise ValueError(
            f"Not enough filtered events ({total_events}) for a safe temporal split: "
            f"calibration={calibration_events}, test={test_events}"
        )
    return calibration_events, test_events


def build_augmented_signup_examples(signup_df: pd.DataFrame) -> pd.DataFrame:
    return build_application_augmented_signup_examples(signup_df)


def fit_models(
    signup_df: pd.DataFrame,
    split: TemporalSplit,
    horizons: list[int],
    *,
    stage0_mode: str,
) -> tuple[dict, dict, dict[str, float], set[str], pd.DataFrame]:
    suspect_ids = suspect_attendance_event_ids(signup_df)

    signup_examples = build_signup_examples(signup_df)
    merged_signup_examples = signup_examples.copy()
    train_signup = signup_examples[signup_examples["event_id"].isin(split.train_event_ids)].copy()
    cal_signup = signup_examples[signup_examples["event_id"].isin(split.calibration_event_ids)].copy()

    if stage0_mode == "hybrid":
        application_df = load_application_velocity_frame()
        application_df = classify_modeling_events(application_df)
        clean_application_df = application_df[
            application_df["eligible_for_signup_model"]
            & (application_df["approval_notification_gap_t0"].abs() <= 10)
        ].copy()
        funnel_examples = build_funnel_signup_examples(clean_application_df)
        merged_signup_examples = merge_signup_and_funnel_examples(signup_examples, funnel_examples)
        train_funnel = merged_signup_examples[
            merged_signup_examples["event_id"].isin(split.train_event_ids)
            & merged_signup_examples["current_applied"].notna()
        ].copy()
        cal_funnel = merged_signup_examples[
            merged_signup_examples["event_id"].isin(split.calibration_event_ids)
            & merged_signup_examples["current_applied"].notna()
        ].copy()
        signup_artifact = fit_hybrid_signup_artifact(
            train_signup,
            cal_signup,
            train_funnel,
            cal_funnel,
            direct_fit_fn=fit_signup_model,
            direct_predict_fn=predict_signup_totals,
        )
    else:
        merged_signup_examples = build_augmented_signup_examples(signup_df)
        train_augmented = merged_signup_examples[
            merged_signup_examples["event_id"].isin(split.train_event_ids)
            & merged_signup_examples["current_applied"].notna()
        ].copy()
        cal_augmented = merged_signup_examples[
            merged_signup_examples["event_id"].isin(split.calibration_event_ids)
            & merged_signup_examples["current_applied"].notna()
        ].copy()
        signup_artifact = fit_signup_strategy_model(
            train_signup,
            cal_signup,
            augmented_train_df=train_augmented,
            augmented_calibration_df=cal_augmented,
            applications_first_train_df=train_augmented,
            applications_first_calibration_df=cal_augmented,
        )

    valid_train_ids = valid_attendance_event_ids(split.train_event_ids, signup_df)
    valid_cal_ids = valid_attendance_event_ids(split.calibration_event_ids, signup_df)

    train_frames = []
    cal_frames = []
    for horizon in horizons:
        train_snapshot = fetch_attendance_snapshot_rows(valid_train_ids, horizon_days=horizon)
        cal_snapshot = fetch_attendance_snapshot_rows(valid_cal_ids, horizon_days=horizon)
        train_frames.append(build_attendance_frame(train_snapshot, horizon_days=horizon))
        cal_frames.append(build_attendance_frame(cal_snapshot, horizon_days=horizon))

    attendance_train = pd.concat([df for df in train_frames if not df.empty], ignore_index=True)
    attendance_cal = pd.concat([df for df in cal_frames if not df.empty], ignore_index=True)
    attendance_artifact = fit_attendance_model(attendance_train, attendance_cal)

    future_show_rates = derive_future_show_rates(valid_train_ids + valid_cal_ids)
    return attendance_artifact, signup_artifact, future_show_rates, suspect_ids, merged_signup_examples


def summarize_errors(errors: np.ndarray) -> dict[str, float]:
    return {
        "mae": float(np.mean(np.abs(errors))),
        "bias": float(np.mean(errors)),
        "rmse": float(np.sqrt(np.mean(errors ** 2))),
    }


def build_residual_diagnostics(evaluation_rows: pd.DataFrame) -> pd.DataFrame:
    if evaluation_rows.empty:
        return evaluation_rows.copy()

    diagnostics = evaluation_rows.copy()
    diagnostics["pred_future_attendance"] = (
        diagnostics["pred_total_attendance"] - diagnostics["pred_current"]
    )
    diagnostics["actual_future_attendance"] = (
        diagnostics["actual_total_attendance"] - diagnostics["actual_current"]
    )
    diagnostics["future_attendance_error"] = (
        diagnostics["pred_future_attendance"] - diagnostics["actual_future_attendance"]
    )
    diagnostics["abs_total_error"] = diagnostics["total_error"].abs()
    diagnostics["abs_current_error"] = diagnostics["current_error"].abs()
    if "signup_error" in diagnostics.columns:
        diagnostics["abs_signup_error"] = diagnostics["signup_error"].abs()
    return diagnostics.sort_values(
        ["horizon_days", "abs_total_error"],
        ascending=[True, False],
    )


def evaluate_models(
    signup_df: pd.DataFrame,
    split: TemporalSplit,
    attendance_artifact: dict,
    signup_artifact: dict,
    future_show_rates: dict[str, float],
    suspect_ids: set[str],
    horizons: list[int],
    *,
    stage0_mode: str,
) -> tuple[dict[str, Any], pd.DataFrame]:
    signup_examples = build_signup_examples(signup_df)
    if stage0_mode == "hybrid":
        application_df = load_application_velocity_frame()
        application_df = classify_modeling_events(application_df)
        clean_application_df = application_df[
            application_df["eligible_for_signup_model"]
            & (application_df["approval_notification_gap_t0"].abs() <= 10)
        ].copy()
        funnel_examples = build_funnel_signup_examples(clean_application_df)
        signup_examples = merge_signup_and_funnel_examples(signup_examples, funnel_examples)
    signup_lookup = signup_df.set_index("event_id")
    signup_test = signup_examples[signup_examples["event_id"].isin(split.test_event_ids)].copy()
    if stage0_mode == "hybrid":
        signup_test["pred_final_approved"] = predict_hybrid_signup_totals(
            signup_test,
            signup_artifact,
            direct_predict_fn=predict_signup_totals,
        )
    else:
        signup_examples = build_augmented_signup_examples(signup_df)
        signup_test = signup_examples[signup_examples["event_id"].isin(split.test_event_ids)].copy()
        signup_test["pred_final_approved"] = predict_signup_totals(signup_test, signup_artifact)

    eval_rows: list[dict[str, Any]] = []
    summary: dict[str, Any] = {"attendance": {}, "signup": {}}

    for horizon in horizons:
        horizon_name = horizon_bucket_name(horizon)
        valid_test_ids = [event_id for event_id in split.test_event_ids if event_id not in suspect_ids]
        snapshot = fetch_attendance_snapshot_rows(valid_test_ids, horizon_days=horizon)
        attendance_test = build_attendance_frame(snapshot, horizon_days=horizon)
        if attendance_test.empty:
            continue

        probs = predict_attendance_probabilities(attendance_test, attendance_artifact)
        attendance_test = attendance_test.copy()
        attendance_test["pred_p_show"] = probs

        try:
            auc = roc_auc_score(attendance_test["showed_up"], probs)
        except ValueError:
            auc = float("nan")

        current_agg = (
            attendance_test.groupby("event_id")
            .agg(
                pred_current=("pred_p_show", "sum"),
                actual_current=("showed_up", "sum"),
                current_approved=("user_id", "count"),
            )
            .reset_index()
        )

        signup_h = signup_test[signup_test["horizon_days"] == float(horizon)][["event_id", "pred_final_approved"]]
        merged = current_agg.merge(signup_h, on="event_id", how="left")
        merged["pred_final_approved"] = merged["pred_final_approved"].fillna(merged["current_approved"])
        merged["final_approved"] = merged["event_id"].map(signup_lookup["approved_t0"])
        merged["additional_approved_pred"] = np.maximum(
            merged["pred_final_approved"] - merged["current_approved"], 0.0
        )
        merged["actual_additional_approved"] = np.maximum(
            merged["final_approved"] - merged["current_approved"], 0.0
        )
        merged["pred_total_attendance"] = (
            merged["pred_current"] + merged["additional_approved_pred"] * future_show_rates[horizon_name]
        )
        merged["actual_total_attendance"] = merged["event_id"].map(signup_lookup["actual_attended"])
        merged["title"] = merged["event_id"].map(signup_lookup["title"])
        merged["horizon_days"] = horizon
        merged["future_show_rate"] = future_show_rates[horizon_name]
        merged["signup_error"] = merged["pred_final_approved"] - merged["final_approved"]
        merged["current_error"] = merged["pred_current"] - merged["actual_current"]
        merged["total_error"] = merged["pred_total_attendance"] - merged["actual_total_attendance"]

        for row in merged.to_dict("records"):
            eval_rows.append(row)

        summary["attendance"][str(horizon)] = {
            "person_auc": auc,
            "current_only": summarize_errors(merged["current_error"].to_numpy()),
            "total_attendance": summarize_errors(merged["total_error"].to_numpy()),
            "n_events": int(len(merged)),
        }

    for horizon in horizons:
        subset = signup_test[signup_test["horizon_days"] == float(horizon)].copy()
        if subset.empty:
            continue
        subset["error"] = subset["pred_final_approved"] - subset["final_approved"]
        summary["signup"][str(horizon)] = {
            "final_approved": summarize_errors(subset["error"].to_numpy()),
            "n_events": int(len(subset)),
        }

    return summary, pd.DataFrame(eval_rows)


def main() -> None:
    args = parse_args()
    horizons = parse_horizons(args.horizons)
    output_dir = Path(args.output_dir)

    raw_signup_df = load_signup_velocity_frame()
    event_audit = classify_modeling_events(raw_signup_df)
    signup_df = event_audit[event_audit["eligible_for_signup_model"]].copy()
    calibration_events, test_events = choose_split_sizes(
        len(signup_df),
        args.calibration_events,
        args.test_events,
    )
    split = build_temporal_split(
        signup_df,
        calibration_events=calibration_events,
        test_events=test_events,
    )

    print("Training split:")
    print(f"  Raw events: {len(raw_signup_df)}")
    print(f"  Hackathon events: {len(signup_df)}")
    print(
        "  Reliable attendance labels: "
        f"{int(event_audit['eligible_for_attendance_model'].sum())}"
    )
    print(f"  Train events: {len(split.train_event_ids)}")
    print(f"  Calibration events: {len(split.calibration_event_ids)}")
    print(f"  Test events: {len(split.test_event_ids)}")
    print(f"  Horizons: {horizons}")
    print(f"  Stage 0 mode: {args.stage0_mode}")

    attendance_artifact, signup_artifact, future_show_rates, suspect_ids, _ = fit_models(
        signup_df,
        split,
        horizons,
        stage0_mode=args.stage0_mode,
    )

    evaluation_summary, evaluation_rows = evaluate_models(
        signup_df,
        split,
        attendance_artifact,
        signup_artifact,
        future_show_rates,
        suspect_ids,
        horizons,
        stage0_mode=args.stage0_mode,
    )

    metadata = {
        "created_at": datetime.utcnow().isoformat() + "Z",
        "model_version": "waves_v2",
        "stage0_mode": args.stage0_mode,
        "event_selection": {
            "raw_event_count": int(len(raw_signup_df)),
            "hackathon_event_count": int(len(signup_df)),
            "reliable_attendance_event_count": int(event_audit["eligible_for_attendance_model"].sum()),
            "platform_hackathon_event_count": int(event_audit["is_platform_hackathon"].sum()),
            "ancillary_event_count": int(event_audit["is_ancillary_event_format"].sum()),
        },
        "split": {
            "train_event_ids": list(split.train_event_ids),
            "calibration_event_ids": list(split.calibration_event_ids),
            "test_event_ids": list(split.test_event_ids),
            "requested_calibration_events": int(args.calibration_events),
            "requested_test_events": int(args.test_events),
            "actual_calibration_events": int(calibration_events),
            "actual_test_events": int(test_events),
        },
        "horizons": horizons,
        "future_show_rates": future_show_rates,
        "suspect_attendance_event_ids": sorted(suspect_ids),
        "evaluation_summary": evaluation_summary,
    }

    save_artifact_bundle(
        output_dir,
        attendance_artifact=attendance_artifact,
        signup_artifact=signup_artifact,
        metadata=metadata,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    event_audit.to_csv(output_dir / "event_audit.csv", index=False)
    evaluation_rows.to_csv(output_dir / "test_event_predictions.csv", index=False)
    build_residual_diagnostics(evaluation_rows).to_csv(
        output_dir / "residual_diagnostics.csv",
        index=False,
    )
    with (output_dir / "evaluation_summary.json").open("w") as f:
        json.dump(evaluation_summary, f, indent=2)

    print("\nHeld-out evaluation summary:")
    for horizon in horizons:
        key = str(horizon)
        if key in evaluation_summary["signup"]:
            stats = evaluation_summary["signup"][key]["final_approved"]
            print(
                f"  Signup T-{horizon}: "
                f"MAE={stats['mae']:.1f} Bias={stats['bias']:+.1f} RMSE={stats['rmse']:.1f}"
            )
        if key in evaluation_summary["attendance"]:
            current_stats = evaluation_summary["attendance"][key]["current_only"]
            total_stats = evaluation_summary["attendance"][key]["total_attendance"]
            auc = evaluation_summary["attendance"][key]["person_auc"]
            print(
                f"  Attendance T-{horizon}: "
                f"AUC={auc:.3f} "
                f"Current MAE={current_stats['mae']:.1f} Bias={current_stats['bias']:+.1f} | "
                f"Total MAE={total_stats['mae']:.1f} Bias={total_stats['bias']:+.1f}"
            )

    print(f"\nArtifacts written to {output_dir}")


if __name__ == "__main__":
    main()
