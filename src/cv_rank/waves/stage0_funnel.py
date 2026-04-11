from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .v2_pipeline import event_posthog_stage0_features, event_signup_metadata_features


APPLICATION_VELOCITY_CSV = Path(__file__).resolve().parents[3] / "results" / "application_velocity.csv"
POSTHOG_STAGE0_SIGNALS = ("pageview", "apply", "register_click", "registration", "guest_list", "add_to_calendar")
EVENT_STRENGTH_SIGNALS = (
    "invited",
    "linked_invited",
    "linked_invited_applicant",
    "tracked_applied",
    "repeat_builder_applied",
)
SUPPORTED_HORIZONS = (14, 7, 3, 1)
PREV_HORIZON = {14: 21, 7: 14, 3: 7, 1: 3}
PREV_WINDOW = {14: 7.0, 7: 7.0, 3: 4.0, 1: 2.0}
PREV2_WINDOW = {14: 7.0, 7: 7.0, 3: 7.0, 1: 4.0}


def load_application_velocity_frame(csv_path: Path = APPLICATION_VELOCITY_CSV) -> pd.DataFrame:
    df = pd.read_csv(csv_path, parse_dates=["event_start"])
    numeric_cols = [
        "actual_attended",
        "applied_t21",
        "applied_t14",
        "applied_t7",
        "applied_t3",
        "applied_t1",
        "applied_t0",
        "approved_t0",
        "approved_notification_t21",
        "approved_notification_t14",
        "approved_notification_t7",
        "approved_notification_t3",
        "approved_notification_t1",
        "approved_notification_t0",
        "approval_notification_gap_t0",
        "approval_count_gap_t0",
        "approval_rate_t0",
        "approval_notification_rate_t21",
        "approval_notification_rate_t14",
        "approval_notification_rate_t7",
        "approval_notification_rate_t3",
        "approval_notification_rate_t1",
        "approval_notification_rate_t0",
        "applied_growth_t7_to_t0",
        "approved_notification_to_final_growth_t7_to_t0",
        "applied_growth_t3_to_t0",
        "approved_notification_to_final_growth_t3_to_t0",
        "show_rate_from_final_approved_t0",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    if "is_platform_hackathon" in df.columns:
        df["is_platform_hackathon"] = (
            df["is_platform_hackathon"]
            .map(lambda value: str(value).strip().lower() in {"1", "true", "t", "yes"})
            .fillna(False)
            .astype(bool)
        )
    return df.sort_values("event_start").reset_index(drop=True)


def build_funnel_signup_examples(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for row in df.to_dict("records"):
        for horizon in SUPPORTED_HORIZONS:
            applied_now = float(row[f"applied_t{horizon}"] or 0.0)
            approved_now = float(row[f"approved_notification_t{horizon}"] or 0.0)
            if applied_now < 20:
                continue

            prev_h = PREV_HORIZON[horizon]
            applied_prev = float(row[f"applied_t{prev_h}"] or 0.0)
            applied_prev2 = (
                float(row["applied_t21"] or 0.0)
                if horizon == 14
                else float(row[f"applied_t{PREV_HORIZON[prev_h]}"] or 0.0)
            )
            approved_prev = float(row[f"approved_notification_t{prev_h}"] or 0.0)
            approved_prev2 = (
                float(row["approved_notification_t21"] or 0.0)
                if horizon == 14
                else float(row[f"approved_notification_t{PREV_HORIZON[prev_h]}"] or 0.0)
            )

            example = {
                "event_id": str(row["event_id"]),
                "event_start": row["event_start"],
                "title": row.get("title") or "",
                "city": row.get("city") or "",
                "is_platform_hackathon": float(bool(row.get("is_platform_hackathon"))),
                "horizon_days": float(horizon),
                "log_horizon_days": float(np.log1p(horizon)),
                "final_applied": float(row["applied_t0"] or 0.0),
                "final_approved": float(row["approved_t0"] or 0.0),
            }
            example.update(event_signup_metadata_features(row, horizon_days=float(horizon)))
            if any(str(column).startswith("ph_") for column in row.keys()):
                example.update(event_posthog_stage0_features(row, horizon=horizon))
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
                    approved_now,
                    approved_prev,
                    approved_prev2,
                    horizon=horizon,
                    prefix="approved_proxy",
                )
            )
            example["current_approval_rate_proxy"] = approved_now / applied_now if applied_now > 0 else 0.0
            example["current_pending_proxy"] = max(applied_now - approved_now, 0.0)
            example["proxy_gap_to_final"] = float(row.get("approval_notification_gap_t0") or 0.0)
            example["remaining_applied"] = max(example["final_applied"] - example["current_applied"], 0.0)
            rows.append(example)

    return pd.DataFrame(rows)


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
    velocity_recent = (current - prev) / recent_window if prev > 0 else (current / recent_window if current > 0 else 0.0)
    velocity_prev = (prev - prev2) / prev_window if prev2 > 0 else (prev / prev_window if prev > 0 else 0.0)
    acceleration = velocity_recent - velocity_prev
    ratio_to_prev = current / prev if prev > 0 else (current if current > 0 else 1.0)
    remaining_like = max(current - prev, 0.0)

    return {
        f"current_{prefix}": current,
        f"previous_{prefix}": prev,
        f"previous2_{prefix}": prev2,
        f"velocity_recent_{prefix}": velocity_recent,
        f"velocity_prev_{prefix}": velocity_prev,
        f"acceleration_{prefix}": acceleration,
        f"ratio_to_prev_{prefix}": ratio_to_prev,
        f"remaining_like_{prefix}": remaining_like,
        f"log_current_{prefix}": math.log1p(current),
        f"{prefix}_x_horizon": current * float(horizon),
        f"{prefix}_velocity_x_horizon": velocity_recent * float(horizon),
    }


def merge_signup_and_funnel_examples(
    signup_examples: pd.DataFrame,
    funnel_examples: pd.DataFrame,
) -> pd.DataFrame:
    if signup_examples.empty and funnel_examples.empty:
        return signup_examples.copy()
    if signup_examples.empty:
        return funnel_examples.copy()
    if funnel_examples.empty:
        return signup_examples.copy()

    merged = signup_examples.merge(
        funnel_examples,
        on=["event_id", "horizon_days"],
        how="outer",
        suffixes=("", "_funnel"),
    )

    def coalesce_column(primary: str, fallback: str) -> None:
        if fallback not in merged.columns:
            return
        if primary not in merged.columns:
            merged[primary] = merged[fallback]
            return
        merged[primary] = merged[primary].where(merged[primary].notna(), merged[fallback])

    for primary, fallback in (
        ("event_start", "event_start_funnel"),
        ("title", "title_funnel"),
        ("city", "city_funnel"),
        ("is_platform_hackathon", "is_platform_hackathon_funnel"),
        ("log_horizon_days", "log_horizon_days_funnel"),
        ("final_approved", "final_approved_funnel"),
        ("current_approved", "current_approved_proxy"),
        ("previous_approved", "previous_approved_proxy"),
        ("previous2_approved", "previous2_approved_proxy"),
        ("velocity_recent", "velocity_recent_approved_proxy"),
        ("velocity_prev", "velocity_prev_approved_proxy"),
        ("acceleration", "acceleration_approved_proxy"),
        ("ratio_to_prev", "ratio_to_prev_approved_proxy"),
        ("log_current_approved", "log_current_approved_proxy"),
        ("current_x_horizon", "approved_proxy_x_horizon"),
        ("velocity_x_horizon", "approved_proxy_velocity_x_horizon"),
    ):
        coalesce_column(primary, fallback)

    if {"final_approved", "current_approved"} <= set(merged.columns):
        merged["additional_approved"] = (
            pd.to_numeric(merged["final_approved"], errors="coerce").fillna(0.0)
            - pd.to_numeric(merged["current_approved"], errors="coerce").fillna(0.0)
        )

    cleanup_cols = [col for col in merged.columns if col.endswith("_funnel")]
    if cleanup_cols:
        merged = merged.drop(columns=cleanup_cols)

    return merged.sort_values(["event_start", "event_id", "horizon_days"]).reset_index(drop=True)


def direct_signup_feature_columns(df: pd.DataFrame) -> list[str]:
    return [
        "horizon_days",
        "current_approved",
        "previous_approved",
        "previous2_approved",
        "velocity_recent",
        "velocity_prev",
        "acceleration",
        "ratio_to_prev",
        "log_current_approved",
        "log_horizon_days",
        "current_x_horizon",
        "velocity_x_horizon",
    ]


def applied_feature_columns(df: pd.DataFrame) -> list[str]:
    return [
        col
        for col in df.columns
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


def single_stage_feature_columns(df: pd.DataFrame) -> list[str]:
    return sorted(
        set(direct_signup_feature_columns(df))
        | {
            "current_applied",
            "previous_applied",
            "velocity_recent_applied",
            "acceleration_applied",
            "ratio_to_prev_applied",
            "log_current_applied",
            "applied_x_horizon",
            "applied_velocity_x_horizon",
            "current_approval_rate_proxy",
            "current_pending_proxy",
            "is_platform_hackathon",
        }
    )


def two_stage_feature_columns(df: pd.DataFrame) -> list[str]:
    return sorted(
        set(direct_signup_feature_columns(df))
        | {
            "current_applied",
            "log_current_applied",
            "pred_final_applied",
            "pred_remaining_applied",
            "current_approval_rate_proxy",
            "current_pending_proxy",
            "is_platform_hackathon",
        }
    )


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

    if artifact["target_col"] == "remaining_applied":
        return np.maximum(preds, 0.0)
    return preds


def application_growth_caps(train_df: pd.DataFrame, *, quantile: float = 0.9) -> dict[str, float]:
    caps: dict[str, float] = {}
    for horizon, group in train_df.groupby("horizon_days"):
        ratios = group["final_applied"] / group["current_applied"].clip(lower=1.0)
        caps[str(int(horizon))] = float(np.quantile(ratios.to_numpy(dtype=float), quantile))
    return caps


def apply_application_caps(frame: pd.DataFrame, *, pred_final_col: str, caps: dict[str, float]) -> None:
    capped = []
    for _, row in frame.iterrows():
        horizon_key = str(int(row["horizon_days"]))
        cap = caps.get(horizon_key)
        pred_final = float(row[pred_final_col])
        if cap is not None:
            pred_final = min(pred_final, float(row["current_applied"]) * cap)
        pred_final = max(pred_final, float(row["current_applied"]))
        capped.append(pred_final)
    frame[pred_final_col] = capped
    frame["pred_remaining_applied"] = np.maximum(frame[pred_final_col] - frame["current_applied"], 0.0)


def fit_hybrid_signup_artifact(
    direct_train_df: pd.DataFrame,
    direct_cal_df: pd.DataFrame,
    funnel_train_df: pd.DataFrame,
    funnel_cal_df: pd.DataFrame,
    *,
    direct_fit_fn,
    direct_predict_fn,
) -> dict[str, Any]:
    direct_artifact = direct_fit_fn(direct_train_df, direct_cal_df)
    if funnel_train_df.empty or funnel_cal_df.empty:
        return {"mode": "direct_only", "direct": direct_artifact}

    application_model = fit_regressor(
        funnel_train_df,
        funnel_cal_df,
        feature_cols=applied_feature_columns(funnel_train_df),
        target_col="remaining_applied",
        log_target=True,
    )
    app_caps = application_growth_caps(funnel_train_df)
    for frame in (funnel_train_df, funnel_cal_df):
        frame["pred_remaining_applied"] = predict_regressor(application_model, frame)
        frame["pred_final_applied"] = frame["current_applied"] + frame["pred_remaining_applied"]
        apply_application_caps(frame, pred_final_col="pred_final_applied", caps=app_caps)

    single_stage_model = fit_regressor(
        funnel_train_df,
        funnel_cal_df,
        feature_cols=single_stage_feature_columns(funnel_train_df),
        target_col="final_approved",
    )
    two_stage_model = fit_regressor(
        funnel_train_df,
        funnel_cal_df,
        feature_cols=two_stage_feature_columns(funnel_train_df),
        target_col="final_approved",
    )

    cal_eval = funnel_cal_df.copy()
    cal_eval["pred_direct"] = direct_predict_fn(cal_eval, direct_artifact)
    cal_eval["pred_single_stage"] = np.maximum(
        predict_regressor(single_stage_model, cal_eval),
        cal_eval["current_approved_proxy"].to_numpy(dtype=float),
    )
    cal_eval["pred_two_stage"] = np.maximum(
        predict_regressor(two_stage_model, cal_eval),
        cal_eval["current_approved_proxy"].to_numpy(dtype=float),
    )

    strategies: dict[str, str] = {}
    for horizon in SUPPORTED_HORIZONS:
        subset = cal_eval[cal_eval["horizon_days"] == float(horizon)].copy()
        if subset.empty:
            strategies[str(horizon)] = "direct"
            continue
        actual = subset["final_approved"].to_numpy(dtype=float)
        candidates = {
            "direct": subset["pred_direct"].to_numpy(dtype=float),
            "single_stage": subset["pred_single_stage"].to_numpy(dtype=float),
            "two_stage": subset["pred_two_stage"].to_numpy(dtype=float),
        }
        best_name = min(
            candidates,
            key=lambda name: float(np.mean(np.abs(candidates[name] - actual))),
        )
        strategies[str(horizon)] = best_name

    return {
        "mode": "hybrid",
        "direct": direct_artifact,
        "application_model": application_model,
        "single_stage_model": single_stage_model,
        "two_stage_model": two_stage_model,
        "application_growth_caps": app_caps,
        "strategy_by_horizon": strategies,
    }


def predict_hybrid_signup_totals(
    frame: pd.DataFrame,
    artifact: dict[str, Any],
    *,
    direct_predict_fn,
) -> np.ndarray:
    if artifact.get("mode") == "hybrid":
        direct_artifact = artifact["direct"]
    elif "direct" in artifact:
        direct_artifact = artifact["direct"]
    else:
        direct_artifact = artifact
    direct_preds = direct_predict_fn(frame, direct_artifact)
    if artifact.get("mode") != "hybrid":
        return direct_preds

    available = set(frame.columns)
    if not {"current_applied", "current_approved_proxy"} <= available:
        return direct_preds

    temp = frame.copy()
    temp["pred_remaining_applied"] = predict_regressor(artifact["application_model"], temp)
    temp["pred_final_applied"] = temp["current_applied"] + temp["pred_remaining_applied"]
    apply_application_caps(temp, pred_final_col="pred_final_applied", caps=artifact["application_growth_caps"])

    pred_single = np.maximum(
        predict_regressor(artifact["single_stage_model"], temp),
        temp["current_approved_proxy"].fillna(0.0).to_numpy(dtype=float),
    )
    pred_two = np.maximum(
        predict_regressor(artifact["two_stage_model"], temp),
        temp["current_approved_proxy"].fillna(0.0).to_numpy(dtype=float),
    )

    final_preds = direct_preds.copy()
    strategies = artifact.get("strategy_by_horizon", {})
    for pos, row in enumerate(temp.to_dict("records")):
        if pd.isna(row.get("current_applied")) or pd.isna(row.get("current_approved_proxy")):
            continue
        strategy = strategies.get(str(int(row["horizon_days"])), "direct")
        if strategy == "single_stage":
            final_preds[pos] = pred_single[pos]
        elif strategy == "two_stage":
            final_preds[pos] = pred_two[pos]
    return np.maximum(final_preds, frame["current_approved"].to_numpy(dtype=float))


def build_live_signup_row(event: dict[str, Any]) -> dict[str, Any]:
    horizon_days = float(event["horizon_days"])
    if horizon_days >= 10:
        bucket = 14
    elif horizon_days >= 5:
        bucket = 7
    elif horizon_days >= 2:
        bucket = 3
    else:
        bucket = 1

    current_approved = float(event["approved_count"])
    current_applied = float(event["applied_count"])

    if bucket == 14:
        previous_approved = float(event["approved_ago_7d"])
        previous2_approved = 0.0
        previous_applied = float(event["applied_ago_7d"])
        previous2_applied = 0.0
        recent_window = 7.0
        prev_window = 7.0
    elif bucket == 7:
        previous_approved = float(event["approved_ago_7d"])
        previous2_approved = float(event["approved_ago_14d"])
        previous_applied = float(event["applied_ago_7d"])
        previous2_applied = float(event["applied_ago_14d"])
        recent_window = 7.0
        prev_window = 7.0
    elif bucket == 3:
        previous_approved = float(event["approved_ago_4d"])
        previous2_approved = float(event["approved_ago_11d"])
        previous_applied = float(event["applied_ago_4d"])
        previous2_applied = float(event["applied_ago_11d"])
        recent_window = 4.0
        prev_window = 7.0
    else:
        previous_approved = float(event["approved_ago_2d"])
        previous2_approved = float(event["approved_ago_6d"])
        previous_applied = float(event["applied_ago_2d"])
        previous2_applied = float(event["applied_ago_6d"])
        recent_window = 2.0
        prev_window = 4.0

    velocity_recent = (
        (current_approved - previous_approved) / recent_window
        if previous_approved > 0
        else (current_approved / recent_window if current_approved > 0 else 0.0)
    )
    velocity_prev = (
        (previous_approved - previous2_approved) / prev_window
        if previous2_approved > 0
        else (previous_approved / prev_window if previous_approved > 0 else 0.0)
    )
    acceleration = velocity_recent - velocity_prev
    ratio_to_prev = current_approved / previous_approved if previous_approved > 0 else (current_approved if current_approved > 0 else 1.0)
    current_approved_safe = max(current_approved, 1.0)
    current_applied_safe = max(current_applied, 1.0)

    row: dict[str, Any] = {
        "event_id": str(event["id"]),
        "event_start": event["startDateTime"],
        "title": event.get("title") or "",
        "city": event.get("city") or "",
        "horizon_days": float(bucket),
        "current_approved": current_approved,
        "previous_approved": previous_approved,
        "previous2_approved": previous2_approved,
        "velocity_recent": velocity_recent,
        "velocity_prev": velocity_prev,
        "acceleration": acceleration,
        "ratio_to_prev": ratio_to_prev,
        "current_approved_zero_flag": float(current_approved <= 0),
        "current_approved_lt5_flag": float(current_approved < 5),
        "previous_approved_zero_flag": float(previous_approved <= 0),
        "log_current_approved": math.log1p(current_approved),
        "log_horizon_days": math.log1p(float(bucket)),
        "current_x_horizon": current_approved * float(bucket),
        "velocity_x_horizon": velocity_recent * float(bucket),
        "current_approved_proxy": current_approved,
        "current_applied": current_applied,
        "is_platform_hackathon": float(bool(event.get("isPlatformHackathon"))),
    }
    row.update(
        _base_temporal_features(
            current_applied,
            previous_applied,
            previous2_applied,
            horizon=bucket,
            prefix="applied",
        )
    )
    row["current_approval_rate_proxy"] = current_approved / current_applied if current_applied > 0 else 0.0
    row["current_pending_proxy"] = max(current_applied - current_approved, 0.0)
    row["pending_to_current_approved_ratio"] = (
        max(current_applied - current_approved, 0.0) / current_approved if current_approved > 0 else current_applied
    )
    row["approval_sparse_flag"] = float(current_approved < 5)
    row["current_applied_lt20_flag"] = float(current_applied < 20)
    row.update(event_signup_metadata_features(event, horizon_days=float(bucket)))
    for signal in POSTHOG_STAGE0_SIGNALS:
        current_signal = float(event.get(f"ph_{signal}_now", 0.0) or 0.0)
        if bucket == 14:
            previous_signal = float(event.get(f"ph_{signal}_ago_7d", 0.0) or 0.0)
        elif bucket == 7:
            previous_signal = float(event.get(f"ph_{signal}_ago_7d", 0.0) or 0.0)
        elif bucket == 3:
            previous_signal = float(event.get(f"ph_{signal}_ago_4d", 0.0) or 0.0)
        else:
            previous_signal = float(event.get(f"ph_{signal}_ago_2d", 0.0) or 0.0)
        signal_velocity = (current_signal - previous_signal) / recent_window if recent_window > 0 else 0.0
        row[f"ph_{signal}_current"] = current_signal
        row[f"ph_{signal}_log_current"] = math.log1p(max(current_signal, 0.0))
        row[f"ph_{signal}_velocity_recent"] = signal_velocity
        row[f"ph_{signal}_per_current_approved"] = current_signal / current_approved_safe
        row[f"ph_{signal}_per_current_applied"] = current_signal / current_applied_safe

    for signal in EVENT_STRENGTH_SIGNALS:
        current_signal = float(event.get(f"{signal}_now", 0.0) or 0.0)
        if bucket == 14:
            previous_signal = float(event.get(f"{signal}_ago_7d", 0.0) or 0.0)
        elif bucket == 7:
            previous_signal = float(event.get(f"{signal}_ago_7d", 0.0) or 0.0)
        elif bucket == 3:
            previous_signal = float(event.get(f"{signal}_ago_4d", 0.0) or 0.0)
        else:
            previous_signal = float(event.get(f"{signal}_ago_2d", 0.0) or 0.0)
        signal_velocity = (current_signal - previous_signal) / recent_window if recent_window > 0 else 0.0
        row[f"{signal}_current"] = current_signal
        row[f"{signal}_log_current"] = math.log1p(max(current_signal, 0.0))
        row[f"{signal}_velocity_recent"] = signal_velocity
        row[f"{signal}_per_current_applied"] = current_signal / current_applied_safe

    invited = max(row.get("invited_current", 0.0), 0.0)
    linked_invited = max(row.get("linked_invited_current", 0.0), 0.0)
    linked_invited_applicant = max(row.get("linked_invited_applicant_current", 0.0), 0.0)
    tracked_applied = max(row.get("tracked_applied_current", 0.0), 0.0)
    repeat_builder_applied = max(row.get("repeat_builder_applied_current", 0.0), 0.0)
    row["linked_invited_share_of_invited"] = linked_invited / invited if invited > 0 else 0.0
    row["linked_invited_applicant_share"] = linked_invited_applicant / current_applied_safe
    row["tracked_applied_share"] = tracked_applied / current_applied_safe
    row["repeat_builder_applied_share"] = repeat_builder_applied / current_applied_safe

    pageviews = max(row.get("ph_pageview_current", 0.0), 0.0)
    applies = max(row.get("ph_apply_current", 0.0), 0.0)
    register_clicks = max(row.get("ph_register_click_current", 0.0), 0.0)
    registrations = max(row.get("ph_registration_current", 0.0), 0.0)
    guest_list = max(row.get("ph_guest_list_current", 0.0), 0.0)
    add_to_calendar = max(row.get("ph_add_to_calendar_current", 0.0), 0.0)
    row["ph_apply_per_pageview"] = applies / pageviews if pageviews > 0 else 0.0
    row["ph_apply_per_register_click"] = applies / register_clicks if register_clicks > 0 else 0.0
    row["ph_register_click_per_pageview"] = register_clicks / pageviews if pageviews > 0 else 0.0
    row["ph_registration_per_register_click"] = registrations / register_clicks if register_clicks > 0 else 0.0
    row["ph_guest_list_per_pageview"] = guest_list / pageviews if pageviews > 0 else 0.0
    row["ph_add_to_calendar_per_pageview"] = add_to_calendar / pageviews if pageviews > 0 else 0.0
    return row
