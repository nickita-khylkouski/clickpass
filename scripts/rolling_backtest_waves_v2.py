"""Run a small rolling temporal backtest for Waves v2.

This evaluates multiple future windows instead of a single final holdout.
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

from scripts.train_waves_v2 import fit_models, evaluate_models
from cv_rank.waves.v2_pipeline import (
    TemporalSplit,
    classify_modeling_events,
    load_signup_velocity_frame,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="results/waves_v2/rolling_backtest_v1")
    parser.add_argument("--stage0-mode", choices=("direct", "hybrid"), default="direct")
    parser.add_argument("--horizons", default="7,3,1")
    return parser.parse_args()


def parse_horizons(raw: str) -> list[int]:
    return [int(part.strip()) for part in raw.split(",") if part.strip()]


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    signup_df = classify_modeling_events(load_signup_velocity_frame())
    signup_df = signup_df[signup_df["eligible_for_signup_model"]].copy()
    signup_df = signup_df.sort_values("event_start").reset_index(drop=True)
    event_ids = signup_df["event_id"].astype(str).tolist()
    horizons = parse_horizons(args.horizons)

    # 30 hackathon events in current cohort -> 3 rolling folds of 4 cal / 4 test
    fold_specs = [
        ("fold1", 0, 18, 18, 22, 22, 26),
        ("fold2", 0, 20, 20, 24, 24, 28),
        ("fold3", 0, 22, 22, 26, 26, 30),
    ]

    summary_rows: list[dict[str, float | int | str]] = []

    for fold_name, tr0, tr1, c0, c1, t0, t1 in fold_specs:
        if t1 > len(event_ids):
            continue
        split = TemporalSplit(
            train_event_ids=tuple(event_ids[tr0:tr1]),
            calibration_event_ids=tuple(event_ids[c0:c1]),
            test_event_ids=tuple(event_ids[t0:t1]),
        )

        attendance_artifact, signup_artifact, future_show_rates, suspect_ids, _ = fit_models(
            signup_df,
            split,
            horizons,
            stage0_mode=args.stage0_mode,
        )
        summary, _ = evaluate_models(
            signup_df,
            split,
            attendance_artifact,
            signup_artifact,
            future_show_rates,
            suspect_ids,
            horizons,
            stage0_mode=args.stage0_mode,
        )
        for horizon in map(str, horizons):
            if horizon not in summary["attendance"]:
                continue
            summary_rows.append(
                {
                    "fold": fold_name,
                    "horizon": int(horizon),
                    "attendance_mae": float(summary["attendance"][horizon]["total_attendance"]["mae"]),
                    "attendance_bias": float(summary["attendance"][horizon]["total_attendance"]["bias"]),
                    "attendance_n": int(summary["attendance"][horizon]["n_events"]),
                    "signup_mae": float(summary["signup"][horizon]["final_approved"]["mae"]),
                    "signup_bias": float(summary["signup"][horizon]["final_approved"]["bias"]),
                    "signup_n": int(summary["signup"][horizon]["n_events"]),
                    "person_auc": float(summary["attendance"][horizon]["person_auc"]),
                }
            )

    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv(output_dir / "fold_metrics.csv", index=False)

    aggregate_df = (
        summary_df.groupby("horizon", as_index=False)
        .agg(
            folds=("fold", "count"),
            attendance_mae_mean=("attendance_mae", "mean"),
            attendance_mae_median=("attendance_mae", "median"),
            attendance_bias_mean=("attendance_bias", "mean"),
            signup_mae_mean=("signup_mae", "mean"),
            signup_bias_mean=("signup_bias", "mean"),
            person_auc_mean=("person_auc", "mean"),
        )
        .sort_values("horizon")
    )
    aggregate_df.to_csv(output_dir / "aggregate_metrics.csv", index=False)
    with (output_dir / "aggregate_metrics.json").open("w") as f:
        json.dump(aggregate_df.to_dict("records"), f, indent=2)

    print(summary_df.to_string(index=False))
    print("\nAggregate by horizon")
    print(aggregate_df.to_string(index=False))


if __name__ == "__main__":
    main()
