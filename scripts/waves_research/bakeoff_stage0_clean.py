"""Clean Stage 0 bakeoff on the corrected hackathon-only cohort.

Compares signup forecasters on the same temporal split and measures:
1. Signup-only error (final approved count)
2. End-to-end total attendance error using the current attendance model

Models:
- xgb: current v2 signup regressor
- multiplier: train-derived median growth multiplier
- prophet_linear: per-event Prophet with linear growth
- prophet_logistic: per-event Prophet with train-derived capacity multiplier
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from prophet import Prophet
from sklearn.metrics import roc_auc_score

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cv_rank.waves.v2_pipeline import (
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
    valid_attendance_event_ids,
)


SUPPORTED_HORIZONS = (7, 3)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--horizons", default="7,3")
    parser.add_argument("--calibration-events", type=int, default=12)
    parser.add_argument("--test-events", type=int, default=10)
    parser.add_argument("--output-dir", default="results/waves_v2/stage0_bakeoff_clean")
    return parser.parse_args()


def parse_horizons(value: str) -> list[int]:
    horizons = [int(item.strip()) for item in value.split(",") if item.strip()]
    unknown = set(horizons) - set(SUPPORTED_HORIZONS)
    if unknown:
        raise ValueError(f"Unsupported horizons: {sorted(unknown)}")
    return horizons


def summarize_errors(errors: np.ndarray) -> dict[str, float]:
    return {
        "mae": float(np.mean(np.abs(errors))),
        "bias": float(np.mean(errors)),
        "rmse": float(np.sqrt(np.mean(errors ** 2))),
    }


def horizon_examples(signup_df: pd.DataFrame, horizon: int) -> pd.DataFrame:
    examples = build_signup_examples(signup_df)
    examples = examples[examples["horizon_days"] == float(horizon)].copy()
    source_cols = [
        "event_id",
        "event_start",
        "title",
        "approved_t21",
        "approved_t14",
        "approved_t7",
        "approved_t3",
        "approved_t1",
        "approved_t0",
        "actual_attended",
    ]
    source = signup_df[source_cols].copy()
    return examples.merge(source, on=["event_id", "event_start", "title"], how="left")


def train_multiplier_model(train_df: pd.DataFrame, cal_df: pd.DataFrame) -> dict[str, float]:
    growth = np.where(
        train_df["current_approved"] > 0,
        train_df["final_approved"] / train_df["current_approved"],
        1.0,
    )
    multiplier = float(np.median(growth))
    raw_cal = cal_df["current_approved"].to_numpy(dtype=float) * multiplier
    correction = float((cal_df["final_approved"].to_numpy(dtype=float) - raw_cal).mean()) if not cal_df.empty else 0.0
    return {"multiplier": multiplier, "bias_correction": correction}


def predict_multiplier(frame: pd.DataFrame, artifact: dict[str, float]) -> np.ndarray:
    preds = frame["current_approved"].to_numpy(dtype=float) * artifact["multiplier"] + artifact["bias_correction"]
    return np.maximum(preds, frame["current_approved"].to_numpy(dtype=float))


def prophet_timeseries(row: pd.Series, horizon: int) -> pd.DataFrame:
    event_date = pd.Timestamp(row["event_start"]).to_pydatetime()
    points: list[dict[str, Any]] = []
    specs = [
        (21, "approved_t21"),
        (14, "approved_t14"),
        (7, "approved_t7"),
        (3, "approved_t3"),
        (1, "approved_t1"),
    ]
    for days_before, column in specs:
        if days_before < horizon:
            continue
        points.append(
            {
                "ds": event_date - timedelta(days=days_before),
                "y": float(row[column]),
            }
        )
    return pd.DataFrame(points).sort_values("ds").reset_index(drop=True)


def train_prophet_artifact(
    train_df: pd.DataFrame,
    cal_df: pd.DataFrame,
    *,
    logistic: bool,
) -> dict[str, float]:
    growth = np.where(
        train_df["current_approved"] > 0,
        train_df["final_approved"] / train_df["current_approved"],
        1.0,
    )
    fallback_multiplier = float(np.median(growth))
    cap_multiplier = float(np.quantile(growth, 0.9))
    cap_multiplier = max(cap_multiplier, fallback_multiplier, 1.05)

    cal_preds = []
    for _, row in cal_df.iterrows():
        cal_preds.append(
            forecast_with_prophet_row(
                row,
                horizon=int(row["horizon_days"]),
                fallback_multiplier=fallback_multiplier,
                cap_multiplier=cap_multiplier,
                logistic=logistic,
            )
        )

    correction = 0.0
    if len(cal_preds) > 0:
        correction = float((cal_df["final_approved"].to_numpy(dtype=float) - np.array(cal_preds)).mean())

    return {
        "fallback_multiplier": fallback_multiplier,
        "cap_multiplier": cap_multiplier,
        "bias_correction": correction,
        "logistic": float(logistic),
    }


def forecast_with_prophet_row(
    row: pd.Series,
    *,
    horizon: int,
    fallback_multiplier: float,
    cap_multiplier: float,
    logistic: bool,
) -> float:
    current = float(row["current_approved"])
    if current < 5:
        return current

    series = prophet_timeseries(row, horizon)
    if len(series) < 2:
        return max(current, current * fallback_multiplier)

    try:
        model_kwargs: dict[str, Any] = {
            "growth": "logistic" if logistic else "linear",
            "changepoint_prior_scale": 0.05,
            "yearly_seasonality": False,
            "weekly_seasonality": False,
            "daily_seasonality": False,
        }
        work = series.copy()
        if logistic:
            cap = max(current * cap_multiplier, float(work["y"].max()) * 1.05, current + 1.0)
            work["cap"] = cap
            work["floor"] = 0.0

        model = Prophet(**model_kwargs)
        model.fit(work)
        future = model.make_future_dataframe(periods=horizon, freq="D")
        if logistic:
            future["cap"] = float(work["cap"].iloc[0])
            future["floor"] = 0.0
        forecast = model.predict(future)
        pred = float(forecast["yhat"].iloc[-1])
        if not math.isfinite(pred):
            return max(current, current * fallback_multiplier)
        return max(current, pred)
    except Exception:
        return max(current, current * fallback_multiplier)


def predict_prophet(frame: pd.DataFrame, artifact: dict[str, float]) -> np.ndarray:
    preds = []
    logistic = bool(artifact["logistic"])
    for _, row in frame.iterrows():
        preds.append(
            forecast_with_prophet_row(
                row,
                horizon=int(row["horizon_days"]),
                fallback_multiplier=float(artifact["fallback_multiplier"]),
                cap_multiplier=float(artifact["cap_multiplier"]),
                logistic=logistic,
            )
        )
    preds = np.array(preds, dtype=float) + float(artifact["bias_correction"])
    return np.maximum(preds, frame["current_approved"].to_numpy(dtype=float))


def fit_attendance_artifact(signup_df: pd.DataFrame, split, horizon: int) -> tuple[dict[str, Any], float, list[str]]:
    valid_train_ids = valid_attendance_event_ids(split.train_event_ids, signup_df)
    valid_cal_ids = valid_attendance_event_ids(split.calibration_event_ids, signup_df)
    valid_test_ids = valid_attendance_event_ids(split.test_event_ids, signup_df)

    train_frame = build_attendance_frame(fetch_attendance_snapshot_rows(valid_train_ids, horizon_days=horizon), horizon_days=horizon)
    cal_frame = build_attendance_frame(fetch_attendance_snapshot_rows(valid_cal_ids, horizon_days=horizon), horizon_days=horizon)
    artifact = fit_attendance_model(train_frame, cal_frame)
    future_show_rates = derive_future_show_rates(valid_train_ids + valid_cal_ids)
    return artifact, float(future_show_rates[horizon_bucket_name(horizon)]), valid_test_ids


def evaluate_stage0_model(
    model_name: str,
    predictor: Callable[[pd.DataFrame, Any], np.ndarray],
    artifact: Any,
    signup_h: pd.DataFrame,
    signup_df: pd.DataFrame,
    split,
    horizon: int,
    attendance_artifact: dict[str, Any],
    future_show_rate: float,
    valid_test_ids: list[str],
) -> tuple[dict[str, Any], pd.DataFrame]:
    test_signup = signup_h[signup_h["event_id"].isin(split.test_event_ids)].copy()
    test_signup["pred_final_approved"] = predictor(test_signup, artifact)
    test_signup["signup_error"] = test_signup["pred_final_approved"] - test_signup["final_approved"]

    signup_summary = summarize_errors(test_signup["signup_error"].to_numpy(dtype=float))
    signup_summary["n_events"] = int(len(test_signup))

    snapshot = fetch_attendance_snapshot_rows(valid_test_ids, horizon_days=horizon)
    attendance_test = build_attendance_frame(snapshot, horizon_days=horizon)
    probs = predict_attendance_probabilities(attendance_test, attendance_artifact)
    attendance_test = attendance_test.copy()
    attendance_test["pred_p_show"] = probs

    try:
        person_auc = float(roc_auc_score(attendance_test["showed_up"], probs))
    except ValueError:
        person_auc = float("nan")

    current_agg = (
        attendance_test.groupby("event_id")
        .agg(
            pred_current=("pred_p_show", "sum"),
            actual_current=("showed_up", "sum"),
            current_approved=("user_id", "count"),
        )
        .reset_index()
    )
    merged = current_agg.merge(
        test_signup[["event_id", "title", "final_approved", "pred_final_approved"]],
        on="event_id",
        how="left",
    )
    merged["additional_approved_pred"] = np.maximum(merged["pred_final_approved"] - merged["current_approved"], 0.0)
    merged["pred_total_attendance"] = merged["pred_current"] + merged["additional_approved_pred"] * future_show_rate
    actual_lookup = signup_df.set_index("event_id")["actual_attended"]
    merged["actual_total_attendance"] = merged["event_id"].map(actual_lookup)
    merged["current_error"] = merged["pred_current"] - merged["actual_current"]
    merged["total_error"] = merged["pred_total_attendance"] - merged["actual_total_attendance"]
    merged["model"] = model_name
    merged["horizon_days"] = horizon
    merged["future_show_rate"] = future_show_rate

    attendance_summary = {
        "person_auc": person_auc,
        "current_only": summarize_errors(merged["current_error"].to_numpy(dtype=float)),
        "total_attendance": summarize_errors(merged["total_error"].to_numpy(dtype=float)),
        "n_events": int(len(merged)),
    }

    return {
        "signup": signup_summary,
        "attendance": attendance_summary,
    }, merged


def main() -> None:
    args = parse_args()
    horizons = parse_horizons(args.horizons)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_signup_df = load_signup_velocity_frame()
    event_audit = classify_modeling_events(raw_signup_df)
    signup_df = event_audit[event_audit["eligible_for_signup_model"]].copy()
    split = build_temporal_split(
        signup_df,
        calibration_events=args.calibration_events,
        test_events=args.test_events,
    )

    all_results: dict[str, Any] = {
        "event_selection": {
            "raw_event_count": int(len(raw_signup_df)),
            "signup_eligible_event_count": int(len(signup_df)),
            "attendance_eligible_event_count": int(event_audit["eligible_for_attendance_model"].sum()),
        },
        "split": {
            "train_event_ids": list(split.train_event_ids),
            "calibration_event_ids": list(split.calibration_event_ids),
            "test_event_ids": list(split.test_event_ids),
        },
        "horizons": {},
    }
    all_event_rows: list[pd.DataFrame] = []

    print("Clean Stage 0 bakeoff")
    print(f"  Raw events: {len(raw_signup_df)}")
    print(f"  Signup-eligible hackathons: {len(signup_df)}")
    print(f"  Reliable attendance labels: {int(event_audit['eligible_for_attendance_model'].sum())}")
    print(f"  Split: train={len(split.train_event_ids)} cal={len(split.calibration_event_ids)} test={len(split.test_event_ids)}")

    for horizon in horizons:
        signup_h = horizon_examples(signup_df, horizon)
        train_h = signup_h[signup_h["event_id"].isin(split.train_event_ids)].copy()
        cal_h = signup_h[signup_h["event_id"].isin(split.calibration_event_ids)].copy()

        attendance_artifact, future_show_rate, valid_test_ids = fit_attendance_artifact(signup_df, split, horizon)

        model_specs: list[tuple[str, Callable[[pd.DataFrame, Any], np.ndarray], Any]] = []
        xgb_artifact = fit_signup_model(train_h, cal_h)
        model_specs.append(("xgb", predict_signup_totals, xgb_artifact))

        multiplier_artifact = train_multiplier_model(train_h, cal_h)
        model_specs.append(("multiplier", predict_multiplier, multiplier_artifact))

        prophet_linear_artifact = train_prophet_artifact(train_h, cal_h, logistic=False)
        model_specs.append(("prophet_linear", predict_prophet, prophet_linear_artifact))

        prophet_logistic_artifact = train_prophet_artifact(train_h, cal_h, logistic=True)
        model_specs.append(("prophet_logistic", predict_prophet, prophet_logistic_artifact))

        horizon_results: dict[str, Any] = {
            "future_show_rate": future_show_rate,
            "models": {},
        }

        print(f"\nHorizon T-{horizon}")
        for model_name, predictor, artifact in model_specs:
            summary, event_rows = evaluate_stage0_model(
                model_name,
                predictor,
                artifact,
                signup_h,
                signup_df,
                split,
                horizon,
                attendance_artifact,
                future_show_rate,
                valid_test_ids,
            )
            horizon_results["models"][model_name] = summary
            all_event_rows.append(event_rows)

            signup_stats = summary["signup"]
            total_stats = summary["attendance"]["total_attendance"]
            print(
                f"  {model_name:<16} "
                f"signup MAE={signup_stats['mae']:.1f} bias={signup_stats['bias']:+.1f} | "
                f"total MAE={total_stats['mae']:.1f} bias={total_stats['bias']:+.1f}"
            )

        all_results["horizons"][str(horizon)] = horizon_results

    (output_dir / "event_audit.csv").write_text(event_audit.to_csv(index=False))
    pd.concat(all_event_rows, ignore_index=True).to_csv(output_dir / "event_level_results.csv", index=False)
    with (output_dir / "summary.json").open("w") as f:
        json.dump(all_results, f, indent=2)

    print(f"\nWrote results to {output_dir}")


if __name__ == "__main__":
    main()
