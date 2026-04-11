"""Evaluate simple application->approval funnel baselines on low-gap events.

Current approval inputs are notification-derived proxies at each horizon.
The evaluation target is the production-aligned final approved label at T0:
the current approved-status count at event start.
"""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cv_rank.waves.v2_pipeline import build_temporal_split, classify_modeling_events


SUPPORTED_HORIZONS = (14, 7, 3, 1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-csv", default="results/application_velocity.csv")
    parser.add_argument("--output-dir", default="results/waves_v2/application_funnel_baseline")
    parser.add_argument("--max-approval-gap", type=int, default=10)
    parser.add_argument("--calibration-events", type=int, default=5)
    parser.add_argument("--test-events", type=int, default=5)
    return parser.parse_args()


def choose_split_sizes(total_events: int, requested_calibration: int, requested_test: int) -> tuple[int, int]:
    calibration = min(requested_calibration, max(3, round(total_events * 0.2)))
    test = min(requested_test, max(3, round(total_events * 0.2)))

    while total_events <= calibration + test + 8 and calibration > 3:
        calibration -= 1
    while total_events <= calibration + test + 8 and test > 3:
        test -= 1

    if total_events <= calibration + test + 8:
        raise ValueError(f"Not enough clean events for split: total={total_events}, cal={calibration}, test={test}")
    return calibration, test


def summarize(errors: np.ndarray) -> dict[str, float]:
    return {
        "mae": float(np.mean(np.abs(errors))),
        "bias": float(np.mean(errors)),
        "rmse": float(np.sqrt(np.mean(errors ** 2))),
    }


def median_ratio(train_df: pd.DataFrame, numerator: str, denominator: str, *, horizon: int) -> float:
    subset = train_df[train_df["horizon_days"] == float(horizon)].copy()
    ratios = subset[numerator] / subset[denominator].clip(lower=1.0)
    return float(np.median(ratios.to_numpy()))


def median_delta(train_df: pd.DataFrame, final_col: str, current_col: str, *, horizon: int) -> float:
    subset = train_df[train_df["horizon_days"] == float(horizon)].copy()
    return float(np.median((subset[final_col] - subset[current_col]).to_numpy()))


def build_examples(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, float | str]] = []
    for row in df.to_dict("records"):
        for horizon in SUPPORTED_HORIZONS:
            applied_now = float(row[f"applied_t{horizon}"] or 0)
            approved_notification_now = float(row[f"approved_notification_t{horizon}"] or 0)
            if applied_now < 20:
                continue
            final_applied = float(row["applied_t0"] or 0)
            final_approved = float(row["approved_t0"] or 0)
            rows.append(
                {
                    "event_id": str(row["event_id"]),
                    "title": str(row["title"]),
                    "event_start": row["event_start"],
                    "horizon_days": float(horizon),
                    "current_applied": applied_now,
                    "current_approved_notification_proxy": approved_notification_now,
                    "current_approval_rate_proxy": (
                        approved_notification_now / applied_now if applied_now > 0 else 0.0
                    ),
                    "final_applied": final_applied,
                    "final_approved": final_approved,
                    "final_approval_rate": final_approved / final_applied if final_applied > 0 else 0.0,
                }
            )
    return pd.DataFrame(rows)


def evaluate(train_df: pd.DataFrame, test_df: pd.DataFrame) -> tuple[dict[str, dict], pd.DataFrame]:
    results: dict[str, dict] = {}
    rows: list[dict[str, object]] = []

    for horizon in SUPPORTED_HORIZONS:
        train_h = train_df[train_df["horizon_days"] == float(horizon)].copy()
        test_h = test_df[test_df["horizon_days"] == float(horizon)].copy()
        if train_h.empty or test_h.empty:
            continue

        approved_growth = median_ratio(
            train_h,
            "final_approved",
            "current_approved_notification_proxy",
            horizon=horizon,
        )
        applied_growth = median_ratio(train_h, "final_applied", "current_applied", horizon=horizon)
        rate_delta = median_delta(
            train_h,
            "final_approval_rate",
            "current_approval_rate_proxy",
            horizon=horizon,
        )
        final_rate_median = float(np.median(train_h["final_approval_rate"].to_numpy()))

        direct_pred = np.maximum(
            test_h["current_approved_notification_proxy"].to_numpy(dtype=float) * approved_growth,
            test_h["current_approved_notification_proxy"].to_numpy(dtype=float),
        )

        pred_applied = np.maximum(
            test_h["current_applied"].to_numpy(dtype=float) * applied_growth,
            test_h["current_applied"].to_numpy(dtype=float),
        )
        pred_rate_delta = np.clip(
            test_h["current_approval_rate_proxy"].to_numpy(dtype=float) + rate_delta,
            0.0,
            1.0,
        )
        funnel_pred_delta = np.maximum(
            pred_applied * pred_rate_delta,
            test_h["current_approved_notification_proxy"].to_numpy(dtype=float),
        )
        funnel_pred_final = np.maximum(
            pred_applied * final_rate_median,
            test_h["current_approved_notification_proxy"].to_numpy(dtype=float),
        )

        actual = test_h["final_approved"].to_numpy(dtype=float)
        methods = {
            "direct_approved_multiplier": direct_pred,
            "funnel_rate_delta": funnel_pred_delta,
            "funnel_final_rate_median": funnel_pred_final,
        }

        results[str(horizon)] = {}
        for name, pred in methods.items():
            errors = pred - actual
            results[str(horizon)][name] = summarize(errors)
            for idx, event_row in test_h.reset_index(drop=True).iterrows():
                rows.append(
                    {
                        "event_id": event_row["event_id"],
                        "title": event_row["title"],
                        "horizon_days": horizon,
                        "model_name": name,
                        "current_applied": event_row["current_applied"],
                        "current_approved_notification_proxy": event_row["current_approved_notification_proxy"],
                        "pred_final_approved": float(pred[idx]),
                        "actual_final_approved_status_t0": event_row["final_approved"],
                        "approval_error": float(errors[idx]),
                    }
                )

    return results, pd.DataFrame(rows)


def main() -> None:
    args = parse_args()
    input_path = Path(args.input_csv)
    output_dir = Path(args.output_dir)

    funnel_df = pd.read_csv(input_path)
    if funnel_df.empty:
        raise ValueError(f"No events found in {input_path}")

    required_columns = {
        "event_id",
        "title",
        "event_start",
        "applied_t0",
        "approved_t0",
        "approval_notification_gap_t0",
    }
    required_columns.update({f"applied_t{horizon}" for horizon in SUPPORTED_HORIZONS})
    required_columns.update({f"approved_notification_t{horizon}" for horizon in SUPPORTED_HORIZONS})
    missing_columns = sorted(required_columns - set(funnel_df.columns))
    if missing_columns:
        raise ValueError(
            "Input CSV is missing required explicit approval columns: "
            + ", ".join(missing_columns)
        )

    funnel_df["event_start"] = pd.to_datetime(funnel_df["event_start"])
    classified = classify_modeling_events(funnel_df)
    clean_df = classified[
        classified["eligible_for_signup_model"]
        & (classified["approval_notification_gap_t0"].abs() <= args.max_approval_gap)
    ].copy()
    if clean_df.empty:
        raise ValueError("No eligible clean events remain after applying the approval notification gap filter")

    calibration_events, test_events = choose_split_sizes(
        len(clean_df),
        args.calibration_events,
        args.test_events,
    )
    split = build_temporal_split(clean_df, calibration_events=calibration_events, test_events=test_events)

    examples = build_examples(clean_df)
    if examples.empty:
        raise ValueError("No horizon examples were created from the clean event cohort")
    train_examples = examples[examples["event_id"].isin(split.train_event_ids)].copy()
    test_examples = examples[examples["event_id"].isin(split.test_event_ids)].copy()
    summary, event_rows = evaluate(train_examples, test_examples)

    metadata = {
        "max_approval_gap": int(args.max_approval_gap),
        "current_approved_proxy_source": "approved_notification_t* (notification-derived proxy)",
        "final_approved_label": "approved_t0 (current approved status count at T0)",
        "event_counts": {
            "all": int(len(funnel_df)),
            "eligible_clean": int(len(clean_df)),
        },
        "split": {
            "train_event_ids": list(split.train_event_ids),
            "calibration_event_ids": list(split.calibration_event_ids),
            "test_event_ids": list(split.test_event_ids),
        },
        "summary": summary,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    clean_df.to_csv(output_dir / "clean_events.csv", index=False)
    event_rows.to_csv(output_dir / "event_level_results.csv", index=False)
    with (output_dir / "summary.json").open("w") as handle:
        json.dump(metadata, handle, indent=2)

    print("Application funnel baseline cohort:")
    print(f"  Total extracted events: {len(funnel_df)}")
    print(f"  Clean eligible events: {len(clean_df)}")
    print(f"  Train/Cal/Test: {len(split.train_event_ids)}/{len(split.calibration_event_ids)}/{len(split.test_event_ids)}")
    print("  Current approval proxy: approval notifications by horizon")
    print("  Final approved label: current approved status count at T0")
    print(f"  Max approval notification gap allowed: {args.max_approval_gap}")
    for horizon in SUPPORTED_HORIZONS:
        key = str(horizon)
        if key not in summary:
            continue
        print(f"\nHorizon T-{horizon}")
        for model_name, stats in summary[key].items():
            print(
                f"  {model_name:<26} "
                f"MAE={stats['mae']:.1f} Bias={stats['bias']:+.1f} RMSE={stats['rmse']:.1f}"
            )
    print(f"\nResults written to {output_dir}")


if __name__ == "__main__":
    main()
