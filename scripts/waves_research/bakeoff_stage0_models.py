"""Clean Stage 0 bakeoff on the corrected hackathon-only cohort.

Compares signup forecasters on the same temporal split and reports:
1. Signup-only held-out error
2. End-to-end attendance error after plugging Stage 0 into the current v2 pipeline

This is the source of truth for deciding what Stage 0 model to keep.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sklearn.metrics import roc_auc_score

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cv_rank.waves.v2_pipeline import (
    TemporalSplit,
    build_attendance_frame,
    build_signup_examples,
    build_temporal_split,
    classify_modeling_events,
    derive_future_show_rates,
    fetch_attendance_snapshot_rows,
    fit_attendance_model,
    fit_signup_model,
    horizon_bucket_name,
    load_signup_velocity_frame,
    predict_attendance_probabilities,
    predict_signup_totals,
    suspect_attendance_event_ids,
    valid_attendance_event_ids,
)

try:
    from prophet import Prophet
except Exception:  # pragma: no cover
    Prophet = None  # type: ignore[assignment]


SUPPORTED_HORIZONS = (14, 7, 3, 1)
PRODUCTION_ATTENDANCE_HORIZONS = list(SUPPORTED_HORIZONS)


@dataclass(frozen=True)
class Stage0Artifact:
    name: str
    kind: str
    payload: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="results/waves_v2/stage0_bakeoff")
    parser.add_argument("--calibration-events", type=int, default=12)
    parser.add_argument("--test-events", type=int, default=10)
    parser.add_argument("--horizons", default="7,3")
    return parser.parse_args()


def parse_horizons(value: str) -> list[int]:
    horizons = [int(item.strip()) for item in value.split(",") if item.strip()]
    unknown = set(horizons) - set(SUPPORTED_HORIZONS)
    if unknown:
        raise ValueError(f"Unsupported horizons: {sorted(unknown)}")
    return horizons


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

    while total_events <= calibration_events + test_events and calibration_events > min_calibration:
        calibration_events -= 1
    while total_events <= calibration_events + test_events and test_events > min_test:
        test_events -= 1

    if total_events <= calibration_events + test_events or (total_events - calibration_events - test_events) < min_train:
        raise ValueError(
            f"Not enough filtered events ({total_events}) for a safe temporal split: "
            f"calibration={calibration_events}, test={test_events}"
        )
    return calibration_events, test_events


def summarize_errors(errors: np.ndarray) -> dict[str, float]:
    return {
        "mae": float(np.mean(np.abs(errors))),
        "bias": float(np.mean(errors)),
        "rmse": float(np.sqrt(np.mean(errors**2))),
    }


def fit_attendance_components(
    signup_df: pd.DataFrame,
    split: TemporalSplit,
    horizons: list[int],
) -> tuple[dict[str, Any], dict[str, float], set[str]]:
    suspect_ids = suspect_attendance_event_ids(signup_df)
    valid_train_ids = valid_attendance_event_ids(split.train_event_ids, signup_df)
    valid_cal_ids = valid_attendance_event_ids(split.calibration_event_ids, signup_df)

    train_frames = []
    cal_frames = []
    for horizon in horizons:
        train_snapshot = fetch_attendance_snapshot_rows(valid_train_ids, horizon_days=horizon)
        cal_snapshot = fetch_attendance_snapshot_rows(valid_cal_ids, horizon_days=horizon)
        train_frames.append(build_attendance_frame(train_snapshot, horizon_days=horizon))
        cal_frames.append(build_attendance_frame(cal_snapshot, horizon_days=horizon))

    attendance_train = pd.concat([frame for frame in train_frames if not frame.empty], ignore_index=True)
    attendance_cal = pd.concat([frame for frame in cal_frames if not frame.empty], ignore_index=True)
    attendance_artifact = fit_attendance_model(attendance_train, attendance_cal)
    future_show_rates = derive_future_show_rates(valid_train_ids + valid_cal_ids)
    return attendance_artifact, future_show_rates, suspect_ids


def calibration_bias_corrections(
    cal_examples: pd.DataFrame,
    raw_predictions: np.ndarray,
) -> dict[str, float]:
    if cal_examples.empty:
        return {}
    temp = cal_examples.copy()
    temp["raw_pred"] = raw_predictions
    temp["residual"] = temp["final_approved"] - temp["raw_pred"]
    return {
        str(int(horizon)): float(group["residual"].mean())
        for horizon, group in temp.groupby("horizon_days")
    }


def apply_bias_corrections(
    frame: pd.DataFrame,
    raw_predictions: np.ndarray,
    bias_corrections: dict[str, float],
) -> np.ndarray:
    adjustments = frame["horizon_days"].map(lambda value: bias_corrections.get(str(int(value)), 0.0)).to_numpy(dtype=float)
    adjusted = raw_predictions + adjustments
    return np.maximum(adjusted, frame["current_approved"].to_numpy(dtype=float))


def fit_multiplier_model(train_examples: pd.DataFrame, cal_examples: pd.DataFrame) -> Stage0Artifact:
    multipliers = {}
    for horizon, group in train_examples.groupby("horizon_days"):
        ratios = group["final_approved"] / group["current_approved"].clip(lower=1.0)
        multipliers[str(int(horizon))] = float(np.median(ratios.to_numpy()))

    cal_raw = predict_multiplier_totals(cal_examples, Stage0Artifact("multiplier_median", "multiplier", {"multipliers": multipliers, "bias_corrections": {}}))
    bias_corrections = calibration_bias_corrections(cal_examples, cal_raw)
    return Stage0Artifact(
        name="multiplier_median",
        kind="multiplier",
        payload={"multipliers": multipliers, "bias_corrections": bias_corrections},
    )


def predict_multiplier_totals(frame: pd.DataFrame, artifact: Stage0Artifact) -> np.ndarray:
    multipliers = artifact.payload["multipliers"]
    raw = frame.apply(
        lambda row: float(row["current_approved"]) * float(multipliers.get(str(int(row["horizon_days"])), 1.0)),
        axis=1,
    ).to_numpy(dtype=float)
    return apply_bias_corrections(frame, raw, artifact.payload.get("bias_corrections", {}))


def horizon_cap_multipliers(train_examples: pd.DataFrame) -> dict[str, float]:
    values = {}
    for horizon, group in train_examples.groupby("horizon_days"):
        ratios = group["final_approved"] / group["current_approved"].clip(lower=1.0)
        values[str(int(horizon))] = float(np.quantile(ratios.to_numpy(), 0.90))
    return values


def make_timeseries(event_row: dict[str, Any], horizon: int) -> pd.DataFrame:
    event_start = pd.Timestamp(event_row["event_start"]).to_pydatetime()
    points = []
    for days, column in [(21, "approved_t21"), (14, "approved_t14"), (7, "approved_t7"), (3, "approved_t3"), (1, "approved_t1")]:
        if days < horizon:
            continue
        value = float(event_row[column])
        if value <= 0:
            continue
        points.append({"ds": event_start - timedelta(days=days), "y": value})
    return pd.DataFrame(points)


def prophet_predict_single(
    event_row: dict[str, Any],
    *,
    horizon: int,
    logistic: bool,
    cap_multiplier: float,
    fallback_multiplier: float,
) -> float:
    if Prophet is None:
        return max(float(event_row[f"approved_t{horizon}"]), float(event_row[f"approved_t{horizon}"]) * fallback_multiplier)

    train_df = make_timeseries(event_row, horizon)
    current_approved = float(event_row[f"approved_t{horizon}"])
    if len(train_df) < 2:
        return max(current_approved, current_approved * fallback_multiplier)

    kwargs = {
        "growth": "logistic" if logistic else "linear",
        "changepoint_prior_scale": 0.05,
        "yearly_seasonality": False,
        "weekly_seasonality": False,
        "daily_seasonality": False,
    }
    model = Prophet(**kwargs)
    fit_df = train_df.copy()
    if logistic:
        cap = max(current_approved + 1.0, current_approved * cap_multiplier)
        fit_df["cap"] = cap
        fit_df["floor"] = 0.0

    try:
        model.fit(fit_df)
        future = model.make_future_dataframe(periods=horizon, freq="D", include_history=True)
        if logistic:
            future["cap"] = fit_df["cap"].iloc[0]
            future["floor"] = 0.0
        forecast = model.predict(future)
        prediction = float(forecast["yhat"].iloc[-1])
        return max(current_approved, prediction)
    except Exception:
        return max(current_approved, current_approved * fallback_multiplier)


def fit_prophet_model(
    train_examples: pd.DataFrame,
    cal_examples: pd.DataFrame,
    event_lookup: dict[str, dict[str, Any]],
    *,
    logistic: bool,
) -> Stage0Artifact:
    fallback = {}
    for horizon, group in train_examples.groupby("horizon_days"):
        ratios = group["final_approved"] / group["current_approved"].clip(lower=1.0)
        fallback[str(int(horizon))] = float(np.median(ratios.to_numpy()))
    caps = horizon_cap_multipliers(train_examples)

    raw_cal = []
    for row in cal_examples.to_dict("records"):
        event_row = event_lookup[str(row["event_id"])]
        raw_cal.append(
            prophet_predict_single(
                event_row,
                horizon=int(row["horizon_days"]),
                logistic=logistic,
                cap_multiplier=caps.get(str(int(row["horizon_days"])), 2.0),
                fallback_multiplier=fallback.get(str(int(row["horizon_days"])), 1.0),
            )
        )
    raw_cal_arr = np.array(raw_cal, dtype=float)
    bias_corrections = calibration_bias_corrections(cal_examples, raw_cal_arr)
    return Stage0Artifact(
        name="prophet_logistic" if logistic else "prophet_linear",
        kind="prophet_logistic" if logistic else "prophet_linear",
        payload={
            "fallback_multipliers": fallback,
            "cap_multipliers": caps,
            "bias_corrections": bias_corrections,
        },
    )


def predict_prophet_totals(
    frame: pd.DataFrame,
    artifact: Stage0Artifact,
    event_lookup: dict[str, dict[str, Any]],
) -> np.ndarray:
    logistic = artifact.kind == "prophet_logistic"
    raw = []
    for row in frame.to_dict("records"):
        event_row = event_lookup[str(row["event_id"])]
        horizon_key = str(int(row["horizon_days"]))
        raw.append(
            prophet_predict_single(
                event_row,
                horizon=int(row["horizon_days"]),
                logistic=logistic,
                cap_multiplier=artifact.payload["cap_multipliers"].get(horizon_key, 2.0),
                fallback_multiplier=artifact.payload["fallback_multipliers"].get(horizon_key, 1.0),
            )
        )
    return apply_bias_corrections(frame, np.array(raw, dtype=float), artifact.payload.get("bias_corrections", {}))


def fit_stage0_models(
    signup_df: pd.DataFrame,
    split: TemporalSplit,
) -> tuple[list[Stage0Artifact], pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, dict[str, Any]]]:
    signup_examples = build_signup_examples(signup_df)
    train_examples = signup_examples[signup_examples["event_id"].isin(split.train_event_ids)].copy()
    cal_examples = signup_examples[signup_examples["event_id"].isin(split.calibration_event_ids)].copy()
    test_examples = signup_examples[signup_examples["event_id"].isin(split.test_event_ids)].copy()
    event_lookup = {
        str(row["event_id"]): row
        for row in signup_df.to_dict("records")
    }

    artifacts = [
        Stage0Artifact("xgboost_v2", "xgboost", {"artifact": fit_signup_model(train_examples, cal_examples)}),
        fit_multiplier_model(train_examples, cal_examples),
        fit_prophet_model(train_examples, cal_examples, event_lookup, logistic=False),
        fit_prophet_model(train_examples, cal_examples, event_lookup, logistic=True),
    ]
    return artifacts, train_examples, cal_examples, test_examples, event_lookup


def predict_stage0_totals(
    frame: pd.DataFrame,
    artifact: Stage0Artifact,
    event_lookup: dict[str, dict[str, Any]],
) -> np.ndarray:
    if artifact.kind == "xgboost":
        return predict_signup_totals(frame, artifact.payload["artifact"])
    if artifact.kind == "multiplier":
        return predict_multiplier_totals(frame, artifact)
    if artifact.kind in {"prophet_linear", "prophet_logistic"}:
        return predict_prophet_totals(frame, artifact, event_lookup)
    raise ValueError(f"Unsupported artifact kind: {artifact.kind}")


def evaluate_model(
    artifact: Stage0Artifact,
    *,
    horizon: int,
    test_examples: pd.DataFrame,
    signup_df: pd.DataFrame,
    split: TemporalSplit,
    attendance_artifact: dict[str, Any],
    future_show_rates: dict[str, float],
    suspect_ids: set[str],
    event_lookup: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], pd.DataFrame]:
    horizon_name = horizon_bucket_name(horizon)
    signup_lookup = signup_df.set_index("event_id")

    signup_test = test_examples[test_examples["horizon_days"] == float(horizon)].copy()
    signup_test["pred_final_approved"] = predict_stage0_totals(signup_test, artifact, event_lookup)
    signup_test["signup_error"] = signup_test["pred_final_approved"] - signup_test["final_approved"]

    valid_test_ids = [event_id for event_id in split.test_event_ids if event_id not in suspect_ids]
    snapshot = fetch_attendance_snapshot_rows(valid_test_ids, horizon_days=horizon)
    attendance_test = build_attendance_frame(snapshot, horizon_days=horizon)

    if attendance_test.empty:
        return {
            "signup": summarize_errors(signup_test["signup_error"].to_numpy()),
            "attendance": None,
            "n_signup_events": int(len(signup_test)),
            "n_attendance_events": 0,
        }, pd.DataFrame()

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

    merged = current_agg.merge(signup_test[["event_id", "pred_final_approved"]], on="event_id", how="left")
    merged["pred_final_approved"] = merged["pred_final_approved"].fillna(merged["current_approved"])
    merged["additional_approved_pred"] = np.maximum(merged["pred_final_approved"] - merged["current_approved"], 0.0)
    merged["pred_total_attendance"] = merged["pred_current"] + merged["additional_approved_pred"] * future_show_rates[horizon_name]
    merged["actual_total_attendance"] = merged["event_id"].map(signup_lookup["actual_attended"])
    merged["title"] = merged["event_id"].map(signup_lookup["title"])
    merged["horizon_days"] = horizon
    merged["future_show_rate"] = future_show_rates[horizon_name]
    merged["current_error"] = merged["pred_current"] - merged["actual_current"]
    merged["total_error"] = merged["pred_total_attendance"] - merged["actual_total_attendance"]
    merged["model_name"] = artifact.name

    summary = {
        "signup": summarize_errors(signup_test["signup_error"].to_numpy()),
        "attendance": {
            "person_auc": float(auc),
            "current_only": summarize_errors(merged["current_error"].to_numpy()),
            "total_attendance": summarize_errors(merged["total_error"].to_numpy()),
        },
        "n_signup_events": int(len(signup_test)),
        "n_attendance_events": int(len(merged)),
    }
    return summary, merged


def main() -> None:
    args = parse_args()
    horizons = parse_horizons(args.horizons)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_signup_df = load_signup_velocity_frame()
    audited_df = classify_modeling_events(raw_signup_df)
    signup_df = audited_df[audited_df["eligible_for_signup_model"]].copy()

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

    print("Stage 0 bakeoff cohort:")
    print(f"  Raw events: {len(raw_signup_df)}")
    print(f"  Hackathon events: {len(signup_df)}")
    print(f"  Reliable attendance labels: {int(audited_df['eligible_for_attendance_model'].sum())}")
    print(f"  Train/Cal/Test: {len(split.train_event_ids)}/{len(split.calibration_event_ids)}/{len(split.test_event_ids)}")
    print(f"  Evaluation horizons: {horizons}")
    print(f"  Attendance train horizons: {PRODUCTION_ATTENDANCE_HORIZONS}")

    # Keep Stage 0 model comparisons aligned with the production v2 pipeline:
    # the attendance side is always trained on the full horizon set, while
    # evaluation can focus on a subset of horizons.
    attendance_artifact, future_show_rates, suspect_ids = fit_attendance_components(
        signup_df,
        split,
        PRODUCTION_ATTENDANCE_HORIZONS,
    )
    artifacts, train_examples, cal_examples, test_examples, event_lookup = fit_stage0_models(signup_df, split)

    summaries: dict[str, dict[str, Any]] = {}
    combined_rows = []
    for horizon in horizons:
        horizon_summary: dict[str, Any] = {}
        print(f"\nHorizon T-{horizon}")
        for artifact in artifacts:
            summary, rows = evaluate_model(
                artifact,
                horizon=horizon,
                test_examples=test_examples,
                signup_df=signup_df,
                split=split,
                attendance_artifact=attendance_artifact,
                future_show_rates=future_show_rates,
                suspect_ids=suspect_ids,
                event_lookup=event_lookup,
            )
            horizon_summary[artifact.name] = summary
            if not rows.empty:
                combined_rows.append(rows)

            signup_stats = summary["signup"]
            attendance_stats = summary["attendance"]
            if attendance_stats is None:
                print(
                    f"  {artifact.name:<18} signup MAE={signup_stats['mae']:.1f} "
                    f"Bias={signup_stats['bias']:+.1f} | attendance n=0"
                )
            else:
                total_stats = attendance_stats["total_attendance"]
                print(
                    f"  {artifact.name:<18} signup MAE={signup_stats['mae']:.1f} "
                    f"Bias={signup_stats['bias']:+.1f} | "
                    f"total attendance MAE={total_stats['mae']:.1f} Bias={total_stats['bias']:+.1f}"
                )
        summaries[str(horizon)] = horizon_summary

    (output_dir / "summary.json").write_text(json.dumps(
        {
            "event_counts": {
                "raw": int(len(raw_signup_df)),
                "hackathon": int(len(signup_df)),
                "reliable_attendance": int(audited_df["eligible_for_attendance_model"].sum()),
            },
            "split": {
                "train_event_ids": list(split.train_event_ids),
                "calibration_event_ids": list(split.calibration_event_ids),
                "test_event_ids": list(split.test_event_ids),
            },
            "horizons": horizons,
            "future_show_rates": future_show_rates,
            "models": summaries,
        },
        indent=2,
    ))
    audited_df.to_csv(output_dir / "event_audit.csv", index=False)
    if combined_rows:
        combined_df = pd.concat(combined_rows, ignore_index=True)
        combined_df.to_csv(output_dir / "attendance_predictions.csv", index=False)
        # Legacy-compatible alias so old analyses do not accidentally read stale files.
        combined_df.to_csv(output_dir / "event_level_results.csv", index=False)

    signup_rows = []
    for horizon in horizons:
        subset = test_examples[test_examples["horizon_days"] == float(horizon)].copy()
        for artifact in artifacts:
            subset_copy = subset.copy()
            subset_copy["pred_final_approved"] = predict_stage0_totals(subset_copy, artifact, event_lookup)
            subset_copy["signup_error"] = subset_copy["pred_final_approved"] - subset_copy["final_approved"]
            subset_copy["model_name"] = artifact.name
            signup_rows.append(subset_copy)
    if signup_rows:
        pd.concat(signup_rows, ignore_index=True).to_csv(output_dir / "signup_predictions.csv", index=False)

    print(f"\nResults written to {output_dir}")


if __name__ == "__main__":
    main()
