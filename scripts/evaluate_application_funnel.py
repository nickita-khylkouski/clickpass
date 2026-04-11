"""Evaluate corrected application->approval Stage 0 models on hackathon events.

This script compares four forecasting paths on the same temporal split:

1. `direct_proxy_only`
   Predict final approved count from approval-notification proxy features only.
2. `applications_only`
   Predict final applications from application-curve features.
3. `app_augmented_single_stage`
   Predict final approved count directly from both application and approval-proxy features.
4. `two_stage_funnel`
   Predict final applications first, then predict final approved count using
   current approval-proxy state plus the predicted final applications.

Targets and inputs use the corrected funnel artifact:
- `approved_t0` is the production-aligned final approved label.
- `approved_notification_t*` are explicit incomplete proxy counts by horizon.
"""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cv_rank.waves.v2_pipeline import build_temporal_split, classify_modeling_events


APPLICATION_CSV = Path(__file__).parent.parent / "results" / "application_velocity.csv"
SUPPORTED_HORIZONS = (14, 7, 3, 1)
PREV_HORIZON = {14: 21, 7: 14, 3: 7, 1: 3}
PREV_WINDOW = {14: 7.0, 7: 7.0, 3: 4.0, 1: 2.0}
PREV2_WINDOW = {14: 7.0, 7: 7.0, 3: 7.0, 1: 4.0}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-csv", default=str(APPLICATION_CSV))
    parser.add_argument("--output-dir", default="results/waves_v2/application_funnel_eval")
    parser.add_argument("--calibration-events", type=int, default=12)
    parser.add_argument("--test-events", type=int, default=10)
    parser.add_argument("--max-approval-gap", type=int, default=10)
    return parser.parse_args()


def summarize_errors(errors: np.ndarray) -> dict[str, float]:
    return {
        "mae": float(np.mean(np.abs(errors))),
        "bias": float(np.mean(errors)),
        "rmse": float(np.sqrt(np.mean(errors ** 2))),
    }


def choose_split_sizes(
    total_events: int,
    requested_calibration: int,
    requested_test: int,
    *,
    min_train: int = 10,
    min_calibration: int = 4,
    min_test: int = 4,
) -> tuple[int, int]:
    calibration_events = requested_calibration
    test_events = requested_test
    if total_events > calibration_events + test_events and (total_events - calibration_events - test_events) >= min_train:
        return calibration_events, test_events

    calibration_events = max(min_calibration, min(requested_calibration, round(total_events * 0.2)))
    test_events = max(min_test, min(requested_test, round(total_events * 0.2)))

    def split_is_safe(calibration_size: int, test_size: int) -> bool:
        return total_events > calibration_size + test_size and (total_events - calibration_size - test_size) >= min_train

    while not split_is_safe(calibration_events, test_events):
        if calibration_events > min_calibration and calibration_events >= test_events:
            calibration_events -= 1
            continue
        if test_events > min_test:
            test_events -= 1
            continue
        if calibration_events > 1:
            calibration_events -= 1
            continue
        if test_events > 1:
            test_events -= 1
            continue
        break

    if not split_is_safe(calibration_events, test_events):
        raise ValueError("Not enough events for a safe temporal split")
    return calibration_events, test_events


def _base_temporal_features(
    current: float,
    prev: float,
    prev2: float,
    *,
    horizon: int,
    prefix: str,
) -> dict[str, float]:
    recent_window = PREV_WINDOW[horizon]
    prev_window = PREV2_WINDOW[horizon]
    velocity_recent = (current - prev) / recent_window if prev > 0 else 0.0
    velocity_prev = (prev - prev2) / prev_window if prev2 > 0 else 0.0
    acceleration = velocity_recent - velocity_prev
    ratio_to_prev = current / prev if prev > 0 else 1.0
    remaining = max(0.0, current - prev)

    return {
        f"current_{prefix}": current,
        f"previous_{prefix}": prev,
        f"previous2_{prefix}": prev2,
        f"velocity_recent_{prefix}": velocity_recent,
        f"velocity_prev_{prefix}": velocity_prev,
        f"acceleration_{prefix}": acceleration,
        f"ratio_to_prev_{prefix}": ratio_to_prev,
        f"remaining_like_{prefix}": remaining,
        f"log_current_{prefix}": float(np.log1p(current)),
        f"{prefix}_x_horizon": current * float(horizon),
        f"{prefix}_velocity_x_horizon": velocity_recent * float(horizon),
    }


def build_examples(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for row in df.to_dict("records"):
        for horizon in SUPPORTED_HORIZONS:
            applied_now = float(row[f"applied_t{horizon}"] or 0.0)
            approval_now = float(row[f"approved_notification_t{horizon}"] or 0.0)
            if applied_now < 20:
                continue

            prev_h = PREV_HORIZON[horizon]
            applied_prev = float(row[f"applied_t{prev_h}"] or 0.0)
            applied_prev2 = float(row["applied_t21"] or 0.0) if horizon == 14 else float(row[f"applied_t{PREV_HORIZON[prev_h]}"] or 0.0)
            approval_prev = float(row[f"approved_notification_t{prev_h}"] or 0.0)
            approval_prev2 = (
                float(row["approved_notification_t21"] or 0.0)
                if horizon == 14
                else float(row[f"approved_notification_t{PREV_HORIZON[prev_h]}"] or 0.0)
            )

            example = {
                "event_id": str(row["event_id"]),
                "event_start": row["event_start"],
                "title": row["title"],
                "city": row.get("city") or "",
                "is_platform_hackathon": float(bool(row.get("is_platform_hackathon"))),
                "horizon_days": float(horizon),
                "log_horizon_days": float(np.log1p(horizon)),
                "final_applied": float(row["applied_t0"] or 0.0),
                "final_approved": float(row["approved_t0"] or 0.0),
            }
            example.update(
                _base_temporal_features(
                    applied_now,
                    applied_prev,
                    applied_prev2,
                    horizon=horizon,
                    prefix="applied",
                )
            )
            example.update(
                _base_temporal_features(
                    approval_now,
                    approval_prev,
                    approval_prev2,
                    horizon=horizon,
                    prefix="approved_proxy",
                )
            )
            example["current_approval_rate_proxy"] = approval_now / applied_now if applied_now > 0 else 0.0
            example["current_pending_proxy"] = max(applied_now - approval_now, 0.0)
            example["proxy_gap_to_final"] = float(row["approval_notification_gap_t0"] or 0.0)
            example["remaining_applied"] = max(example["final_applied"] - example["current_applied"], 0.0)
            rows.append(example)

    return pd.DataFrame(rows)


def _build_regressor() -> tuple[Any, str]:
    try:
        from xgboost import XGBRegressor

        return (
            XGBRegressor(
                n_estimators=300,
                max_depth=4,
                learning_rate=0.05,
                subsample=0.8,
                colsample_bytree=0.8,
                min_child_weight=5,
                reg_lambda=1.0,
                random_state=42,
                objective="reg:squarederror",
                eval_metric="mae",
                verbosity=0,
            ),
            "xgboost",
        )
    except Exception:  # pragma: no cover
        from sklearn.ensemble import HistGradientBoostingRegressor

        return (
            HistGradientBoostingRegressor(
                max_iter=300,
                max_depth=4,
                learning_rate=0.05,
                min_samples_leaf=5,
                l2_regularization=1.0,
                random_state=42,
            ),
            "sklearn_hist_gradient_boosting",
        )


def fit_regressor(
    train_df: pd.DataFrame,
    cal_df: pd.DataFrame,
    *,
    feature_cols: list[str],
    target_col: str,
    log_target: bool = False,
) -> dict[str, Any]:
    model, backend = _build_regressor()
    x_train = train_df[feature_cols].astype(float)
    y_train_raw = train_df[target_col].astype(float)
    y_train = np.log1p(y_train_raw) if log_target else y_train_raw
    x_cal = cal_df.reindex(columns=feature_cols, fill_value=0.0).astype(float)
    y_cal_raw = cal_df[target_col].astype(float)

    model.fit(x_train, y_train)

    bias_corrections: dict[str, float] = {}
    if not cal_df.empty:
        raw_cal = model.predict(x_cal)
        if log_target:
            raw_cal = np.expm1(raw_cal)
        temp = cal_df.copy()
        temp["residual"] = y_cal_raw.to_numpy() - raw_cal
        for horizon, group in temp.groupby("horizon_days"):
            bias_corrections[str(int(horizon))] = float(group["residual"].mean())

    return {
        "model": model,
        "backend": backend,
        "feature_cols": feature_cols,
        "bias_corrections": bias_corrections,
        "target_col": target_col,
        "log_target": log_target,
    }


def predict_regressor(artifact: dict[str, Any], df: pd.DataFrame) -> np.ndarray:
    x = df.reindex(columns=artifact["feature_cols"], fill_value=0.0).astype(float)
    preds = artifact["model"].predict(x)
    if artifact.get("log_target"):
        preds = np.expm1(preds)
    if artifact["bias_corrections"]:
        preds = preds + df["horizon_days"].map(
            lambda h: artifact["bias_corrections"].get(str(int(h)), 0.0)
        ).to_numpy(dtype=float)

    if artifact["target_col"] == "final_applied":
        return np.maximum(preds, df["current_applied"].to_numpy(dtype=float))
    if artifact["target_col"] == "remaining_applied":
        return np.maximum(preds, 0.0)
    return np.maximum(preds, df["current_approved_proxy"].to_numpy(dtype=float))


def metrics_by_horizon(frame: pd.DataFrame, *, pred_col: str, target_col: str) -> dict[str, dict[str, float]]:
    summary: dict[str, dict[str, float]] = {}
    for horizon in SUPPORTED_HORIZONS:
        subset = frame[frame["horizon_days"] == float(horizon)].copy()
        if subset.empty:
            continue
        errors = subset[pred_col] - subset[target_col]
        summary[str(horizon)] = summarize_errors(errors.to_numpy(dtype=float))
    return summary


def application_growth_caps(train_df: pd.DataFrame, *, quantile: float = 0.9) -> dict[str, float]:
    caps: dict[str, float] = {}
    grouped = train_df.groupby("horizon_days")
    for horizon, group in grouped:
        ratios = group["final_applied"] / group["current_applied"].clip(lower=1.0)
        caps[str(int(horizon))] = float(np.quantile(ratios.to_numpy(dtype=float), quantile))
    return caps


def apply_application_caps(frame: pd.DataFrame, *, pred_final_col: str, caps: dict[str, float]) -> None:
    capped_values = []
    for _, row in frame.iterrows():
        horizon_key = str(int(row["horizon_days"]))
        cap = caps.get(horizon_key)
        pred_final = float(row[pred_final_col])
        if cap is not None:
            pred_final = min(pred_final, float(row["current_applied"]) * cap)
        pred_final = max(pred_final, float(row["current_applied"]))
        capped_values.append(pred_final)
    frame[pred_final_col] = capped_values
    frame["pred_remaining_applied"] = np.maximum(
        frame[pred_final_col] - frame["current_applied"],
        0.0,
    )


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)

    raw_df = pd.read_csv(args.input_csv, parse_dates=["event_start"])
    classified = classify_modeling_events(raw_df)
    clean_df = (
        classified[
            classified["eligible_for_signup_model"]
            & (classified["approval_notification_gap_t0"].abs() <= args.max_approval_gap)
        ]
        .sort_values("event_start")
        .reset_index(drop=True)
        .copy()
    )
    if clean_df.empty:
        raise ValueError("No clean hackathon events remain after filtering")

    calibration_events, test_events = choose_split_sizes(
        len(clean_df),
        args.calibration_events,
        args.test_events,
    )
    split = build_temporal_split(clean_df, calibration_events=calibration_events, test_events=test_events)

    examples = build_examples(clean_df)
    train_df = examples[examples["event_id"].isin(split.train_event_ids)].copy()
    cal_df = examples[examples["event_id"].isin(split.calibration_event_ids)].copy()
    test_df = examples[examples["event_id"].isin(split.test_event_ids)].copy()

    applied_feature_cols = [
        col
        for col in train_df.columns
        if col.startswith(
            (
                "current_applied",
                "previous_applied",
                "previous2_applied",
                "velocity_recent_applied",
                "velocity_prev_applied",
                "acceleration_applied",
                "ratio_to_prev_applied",
                "remaining_like_applied",
                "log_current_applied",
                "applied_x_horizon",
                "applied_velocity_x_horizon",
            )
        )
        or col in {"horizon_days", "log_horizon_days", "is_platform_hackathon"}
    ]
    direct_approval_feature_cols = [
        col
        for col in train_df.columns
        if col.startswith(
            (
                "current_approved_proxy",
                "previous_approved_proxy",
                "previous2_approved_proxy",
                "velocity_recent_approved_proxy",
                "velocity_prev_approved_proxy",
                "acceleration_approved_proxy",
                "ratio_to_prev_approved_proxy",
                "remaining_like_approved_proxy",
                "log_current_approved_proxy",
                "approved_proxy_x_horizon",
                "approved_proxy_velocity_x_horizon",
            )
        )
        or col in {"horizon_days", "log_horizon_days", "is_platform_hackathon", "current_approval_rate_proxy", "current_pending_proxy"}
    ]
    app_augmented_feature_cols = sorted(
        set(direct_approval_feature_cols)
        | {
            "current_applied",
            "previous_applied",
            "velocity_recent_applied",
            "acceleration_applied",
            "ratio_to_prev_applied",
            "log_current_applied",
            "applied_x_horizon",
            "applied_velocity_x_horizon",
        }
    )

    application_model = fit_regressor(
        train_df,
        cal_df,
        feature_cols=applied_feature_cols,
        target_col="remaining_applied",
        log_target=True,
    )
    app_caps = application_growth_caps(train_df)
    for frame in (train_df, cal_df, test_df):
        frame["pred_remaining_applied"] = predict_regressor(application_model, frame)
        frame["pred_final_applied"] = frame["current_applied"] + frame["pred_remaining_applied"]
        apply_application_caps(frame, pred_final_col="pred_final_applied", caps=app_caps)

    direct_model = fit_regressor(
        train_df,
        cal_df,
        feature_cols=direct_approval_feature_cols,
        target_col="final_approved",
    )
    single_stage_model = fit_regressor(
        train_df,
        cal_df,
        feature_cols=app_augmented_feature_cols,
        target_col="final_approved",
    )

    funnel_feature_cols = sorted(
        set(direct_approval_feature_cols)
        | {"pred_final_applied", "pred_remaining_applied", "current_applied", "log_current_applied"}
    )
    two_stage_model = fit_regressor(
        train_df,
        cal_df,
        feature_cols=funnel_feature_cols,
        target_col="final_approved",
    )

    test_df["pred_final_approved_direct"] = predict_regressor(direct_model, test_df)
    test_df["pred_final_approved_single_stage"] = predict_regressor(single_stage_model, test_df)
    test_df["pred_final_approved_two_stage"] = predict_regressor(two_stage_model, test_df)

    summary = {
        "event_counts": {
            "raw": int(len(raw_df)),
            "eligible_clean_hackathons": int(len(clean_df)),
        },
        "split": {
            "train": int(len(split.train_event_ids)),
            "calibration": int(len(split.calibration_event_ids)),
            "test": int(len(split.test_event_ids)),
        },
        "backends": {
            "application_model": application_model["backend"],
            "direct_model": direct_model["backend"],
            "single_stage_model": single_stage_model["backend"],
            "two_stage_model": two_stage_model["backend"],
        },
        "application_growth_caps": app_caps,
        "applications": metrics_by_horizon(test_df, pred_col="pred_final_applied", target_col="final_applied"),
        "approvals": {
            "direct_proxy_only": metrics_by_horizon(
                test_df,
                pred_col="pred_final_approved_direct",
                target_col="final_approved",
            ),
            "app_augmented_single_stage": metrics_by_horizon(
                test_df,
                pred_col="pred_final_approved_single_stage",
                target_col="final_approved",
            ),
            "two_stage_funnel": metrics_by_horizon(
                test_df,
                pred_col="pred_final_approved_two_stage",
                target_col="final_approved",
            ),
        },
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    clean_df.to_csv(output_dir / "event_audit.csv", index=False)
    test_df.to_csv(output_dir / "test_event_predictions.csv", index=False)
    with (output_dir / "summary.json").open("w") as handle:
        json.dump(summary, handle, indent=2)

    print("Corrected application funnel Stage 0 evaluation")
    print(f"  Raw events: {len(raw_df)}")
    print(f"  Clean hackathon events: {len(clean_df)}")
    print(
        "  Split: "
        f"{len(split.train_event_ids)}/{len(split.calibration_event_ids)}/{len(split.test_event_ids)}"
    )
    print("\nApplications model")
    for horizon, stats in summary["applications"].items():
        print(f"  T-{horizon}: MAE={stats['mae']:.1f} Bias={stats['bias']:+.1f} RMSE={stats['rmse']:.1f}")

    for model_name, horizons in summary["approvals"].items():
        print(f"\n{model_name}")
        for horizon, stats in horizons.items():
            print(f"  T-{horizon}: MAE={stats['mae']:.1f} Bias={stats['bias']:+.1f} RMSE={stats['rmse']:.1f}")
    print(f"\nResults written to {output_dir}")


if __name__ == "__main__":
    main()
