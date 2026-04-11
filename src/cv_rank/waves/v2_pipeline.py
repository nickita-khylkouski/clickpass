from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

from .event_strength import EVENT_STRENGTH_SIGNAL_PREFIXES, load_first_submission_map
from .predict import city_to_timezone, compute_tz_offset_diff, tz_distance_bucket
from .posthog_features import build_person_attendance_features


SIGNUP_VELOCITY_CSV = Path(__file__).resolve().parents[3] / "results" / "signup_velocity.csv"
ENGAGEMENT_PROFILES_CSV = Path(__file__).resolve().parents[3] / "results" / "engagement_profiles.csv"
POSTHOG_EVENT_VELOCITY_CSV = Path(__file__).resolve().parents[3] / "results" / "posthog_event_velocity.csv"
USER_HACKATHON_HISTORY_CSV = (
    Path(__file__).resolve().parents[3]
    / "local_data"
    / "agent_analysis"
    / "user_hackathon_history.csv"
)
SIGNUP_BIAS_CORRECTION_PRIOR_STRENGTH = 8.0

HACKATHON_TITLE_PATTERNS = (
    r"\bhackathon\b",
    r"\bhack\b",
    r"\bsuperhack\b",
    r"ハッカソン",
    r"해커톤",
)

NON_HACKATHON_TITLE_HINTS = (
    "hack night",
    "happy hour",
    "afterparty",
    "after party",
    "workshop",
    "birthday party",
    "showcase",
    "demo day",
    "mixer",
    "co-working",
    "coworking",
    "show & tell",
    "gtm",
)

EVENT_TEXT_PATTERNS = {
    "mentions_prizes": re.compile(r"\b(prizes?|cash prizes?|awards?|bount(?:y|ies)|win)\b", re.IGNORECASE),
    "mentions_ai": re.compile(r"\b(ai|llm|agentic|agents|generative|model|multimodal)\b", re.IGNORECASE),
    "mentions_beginner": re.compile(r"\b(beginner|students?|all levels|new to|first[- ]time)\b", re.IGNORECASE),
    "mentions_team": re.compile(r"\b(team|teammate|squad|cofounder|collaborat)\w*\b", re.IGNORECASE),
    "mentions_build": re.compile(r"\b(build|ship|prototype|demo|hack)\w*\b", re.IGNORECASE),
}

EVENT_STRENGTH_SIGNALS = (
    "invited",
    "linked_invited",
    "linked_invited_applicant",
    "tracked_applied",
    "repeat_builder_applied",
)


@dataclass(frozen=True)
class TemporalSplit:
    train_event_ids: tuple[str, ...]
    calibration_event_ids: tuple[str, ...]
    test_event_ids: tuple[str, ...]


def is_hackathon_title(title: str | None) -> bool:
    value = str(title or "").strip()
    if not value:
        return False

    lowered = value.lower()
    if any(hint in lowered for hint in NON_HACKATHON_TITLE_HINTS):
        return False

    return any(re.search(pattern, value, re.IGNORECASE) for pattern in HACKATHON_TITLE_PATTERNS)


def is_ancillary_event_title(title: str | None) -> bool:
    lowered = str(title or "").strip().lower()
    return bool(lowered) and any(hint in lowered for hint in NON_HACKATHON_TITLE_HINTS)


def is_hackathon_event(
    title: str | None,
    *,
    is_platform_hackathon: bool | None = None,
) -> bool:
    if is_ancillary_event_title(title):
        return False
    return bool(is_platform_hackathon) or is_hackathon_title(title)


def fetch_platform_event_flags(
    event_ids: list[str] | tuple[str, ...],
    *,
    dsn: str | None = None,
) -> pd.DataFrame:
    import psycopg2

    if not event_ids:
        return pd.DataFrame(columns=["event_id", "is_platform_hackathon"])

    dsn = dsn or os.environ.get("PLATFORM_DATABASE_URL")
    if not dsn:
        return pd.DataFrame(columns=["event_id", "is_platform_hackathon"])

    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id::text AS event_id, COALESCE("isPlatformHackathon", false) AS is_platform_hackathon
            FROM "PlatformEvent"
            WHERE id::text = ANY(%s)
            """,
            (list(event_ids),),
        )
        rows = cur.fetchall()
        cur.close()
        return pd.DataFrame(rows, columns=["event_id", "is_platform_hackathon"])
    finally:
        conn.close()


def load_signup_velocity_frame(csv_path: Path = SIGNUP_VELOCITY_CSV) -> pd.DataFrame:
    df = pd.read_csv(csv_path, parse_dates=["event_start"])
    numeric_cols = [
        "approved_t21",
        "approved_t14",
        "approved_t7",
        "approved_t3",
        "approved_t1",
        "approved_t0",
        "actual_attended",
        "show_rate",
        "velocity_21_to_14",
        "velocity_14_to_7",
        "velocity_7_to_3",
        "velocity_3_to_1",
        "velocity_1_to_0",
        "accel_14_to_7",
        "growth_t14_to_t0",
        "growth_t7_to_t0",
        "growth_t3_to_t0",
        "additional_t7_to_t0",
        "additional_t3_to_t0",
        "capacity",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    for col in df.columns:
        if col.startswith(EVENT_STRENGTH_SIGNAL_PREFIXES):
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    text_cols = ["description", "description_summary", "details"]
    for col in text_cols:
        if col in df.columns:
            df[col] = df[col].fillna("").astype(str)
    if "is_platform_hackathon" in df.columns:
        df["is_platform_hackathon"] = (
            df["is_platform_hackathon"]
            .map(lambda value: str(value).strip().lower() in {"1", "true", "t", "yes"})
            .fillna(False)
            .astype(bool)
        )
    if "approval_required" in df.columns:
        df["approval_required"] = (
            df["approval_required"]
            .map(lambda value: str(value).strip().lower() in {"1", "true", "t", "yes"})
            .fillna(False)
            .astype(bool)
        )
    if POSTHOG_EVENT_VELOCITY_CSV.exists():
        posthog_df = pd.read_csv(POSTHOG_EVENT_VELOCITY_CSV)
        posthog_df["event_id"] = posthog_df["event_id"].astype(str)
        numeric_posthog_cols = [col for col in posthog_df.columns if col.startswith("ph_")]
        for col in numeric_posthog_cols:
            posthog_df[col] = pd.to_numeric(posthog_df[col], errors="coerce").fillna(0.0)
        df["event_id"] = df["event_id"].astype(str)
        df = df.merge(posthog_df.drop(columns=["slug"], errors="ignore"), on="event_id", how="left")
        for col in numeric_posthog_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    return df.sort_values("event_start").reset_index(drop=True)


def classify_modeling_events(signup_df: pd.DataFrame) -> pd.DataFrame:
    df = signup_df.copy()
    df["event_id"] = df["event_id"].astype(str)
    df["is_hackathon_title"] = df["title"].map(is_hackathon_title)
    if "is_platform_hackathon" not in df.columns:
        flag_df = fetch_platform_event_flags(df["event_id"].astype(str).tolist())
        if not flag_df.empty:
            df = df.merge(flag_df, on="event_id", how="left")
        else:
            df["is_platform_hackathon"] = False
    df["is_platform_hackathon"] = df["is_platform_hackathon"].fillna(False).astype(bool)

    df["is_ancillary_event_format"] = df["title"].map(is_ancillary_event_title)
    df["suspect_attendance_label"] = (
        ((df["approved_t0"] >= 50) & (df["actual_attended"] <= 1))
        | (df["actual_attended"] > df["approved_t0"])
    )
    df["eligible_for_signup_model"] = df.apply(
        lambda row: is_hackathon_event(
            row.get("title"),
            is_platform_hackathon=bool(row.get("is_platform_hackathon", False)),
        ),
        axis=1,
    )
    df["eligible_for_attendance_model"] = (
        df["eligible_for_signup_model"] & ~df["suspect_attendance_label"]
    )
    return df


def build_temporal_split(
    signup_df: pd.DataFrame,
    *,
    calibration_events: int = 12,
    test_events: int = 10,
) -> TemporalSplit:
    if len(signup_df) <= calibration_events + test_events:
        raise ValueError("Not enough events for requested split")

    train_df = signup_df.iloc[: -(calibration_events + test_events)]
    cal_df = signup_df.iloc[-(calibration_events + test_events) : -test_events]
    test_df = signup_df.iloc[-test_events:]
    return TemporalSplit(
        train_event_ids=tuple(train_df["event_id"].astype(str)),
        calibration_event_ids=tuple(cal_df["event_id"].astype(str)),
        test_event_ids=tuple(test_df["event_id"].astype(str)),
    )


def _record_value(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in record and record[key] is not None:
            return record[key]
    return None


def _normalize_event_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _temporal_velocity(current: float, previous: float, window: float) -> float:
    if window <= 0:
        return 0.0
    if previous > 0:
        return (current - previous) / window
    if current > 0:
        return current / window
    return 0.0


def _temporal_ratio(current: float, previous: float) -> float:
    if previous > 0:
        return current / previous
    if current > 0:
        return current
    return 1.0


def event_signup_metadata_features(
    record: dict[str, Any],
    *,
    horizon_days: float | None = None,
) -> dict[str, float]:
    title = _normalize_event_text(_record_value(record, "title"))
    description = _normalize_event_text(_record_value(record, "description"))
    description_summary = _normalize_event_text(_record_value(record, "description_summary", "descriptionSummary"))
    details = _normalize_event_text(_record_value(record, "details"))
    text_blob = " ".join(part for part in [title, description_summary, description, details] if part).strip()
    capacity_raw = _record_value(record, "capacity")
    try:
        capacity = max(float(capacity_raw or 0.0), 0.0)
    except (TypeError, ValueError):
        capacity = 0.0
    approval_required = bool(_record_value(record, "approval_required", "approvalRequired"))
    is_platform_hackathon = bool(_record_value(record, "is_platform_hackathon", "isPlatformHackathon"))
    text_chars = float(len(text_blob))
    text_words = float(len(text_blob.split())) if text_blob else 0.0
    features = {
        "event_is_platform_hackathon": float(is_platform_hackathon),
        "event_approval_required": float(approval_required),
        "event_has_capacity_limit": float(capacity > 0),
        "event_log_capacity": math.log1p(capacity),
        "event_log_text_chars": math.log1p(text_chars),
        "event_log_text_words": math.log1p(text_words),
    }
    for feature_name, pattern in EVENT_TEXT_PATTERNS.items():
        features[f"event_{feature_name}"] = float(bool(pattern.search(text_blob)))
    if horizon_days is not None and int(float(horizon_days)) not in {7, 1}:
        return {name: 0.0 for name in features}
    return features


def suspect_attendance_event_ids(signup_df: pd.DataFrame) -> set[str]:
    """Incomplete or impossible attendance labels should not train/evaluate the attendance model."""
    if "suspect_attendance_label" in signup_df.columns:
        mask = signup_df["suspect_attendance_label"].astype(bool)
    else:
        mask = ((signup_df["approved_t0"] >= 50) & (signup_df["actual_attended"] <= 1)) | (
            signup_df["actual_attended"] > signup_df["approved_t0"]
        )
    return set(signup_df.loc[mask, "event_id"].astype(str))


def valid_attendance_event_ids(event_ids: list[str] | tuple[str, ...], signup_df: pd.DataFrame) -> list[str]:
    suspects = suspect_attendance_event_ids(signup_df)
    return [event_id for event_id in event_ids if event_id not in suspects]


def accumulate_prior_history_before_snapshot(
    user_history: list[tuple[pd.Timestamp, float, float]],
    *,
    snapshot_time: pd.Timestamp,
) -> tuple[float, float]:
    prior_events = 0.0
    prior_attended = 0.0
    for hist_start, hist_events, hist_attended in user_history:
        if hist_start < snapshot_time:
            prior_events += hist_events
            prior_attended += hist_attended
    return prior_events, prior_attended


def horizon_bucket_name(horizon_days: int | float) -> str:
    if horizon_days >= 10:
        return "t14"
    if horizon_days >= 6:
        return "t7"
    if horizon_days >= 2:
        return "t3"
    return "t1"


def derive_future_show_rates(
    event_ids: list[str] | tuple[str, ...],
    *,
    engagement_csv_path: Path = ENGAGEMENT_PROFILES_CSV,
) -> dict[str, float]:
    df = pd.read_csv(engagement_csv_path)
    df["event_id"] = df["event_id"].astype(str)
    df["showed_up"] = pd.to_numeric(df["showed_up"], errors="coerce").fillna(0).astype(int)
    df = df[df["event_id"].isin(event_ids)]

    bucket_sets = {
        "t14": {"T-14 to T-7", "T-7 to T-3", "T-3 to T-1", "T-1 to T-0"},
        "t7": {"T-7 to T-3", "T-3 to T-1", "T-1 to T-0"},
        "t3": {"T-3 to T-1", "T-1 to T-0"},
        "t1": {"T-1 to T-0"},
    }

    rates: dict[str, float] = {}
    for name, buckets in bucket_sets.items():
        subset = df[df["signup_bucket"].isin(buckets)]
        if subset.empty:
            raise ValueError(f"No engagement rows found for {name}")
        rates[name] = float(subset["showed_up"].mean())
    return rates


POSTHOG_STAGE0_SIGNALS = (
    "pageview",
    "apply",
    "register_click",
    "registration",
    "guest_list",
    "add_to_calendar",
)


def event_posthog_stage0_features(record: dict[str, Any], *, horizon: int) -> dict[str, float]:
    if horizon not in {14, 7, 3, 1}:
        return {}

    prev_horizon = {14: 21, 7: 14, 3: 7, 1: 3}[horizon]
    recent_window = float(max(prev_horizon - horizon, 1))
    current_approved = max(float(record.get(f"approved_t{horizon}", 0.0) or 0.0), 0.0)
    current_approved_safe = max(current_approved, 1.0)
    features: dict[str, float] = {}
    for signal in POSTHOG_STAGE0_SIGNALS:
        current = float(record.get(f"ph_{signal}_t{horizon}", 0.0) or 0.0)
        previous = float(record.get(f"ph_{signal}_t{prev_horizon}", 0.0) or 0.0)
        velocity = (current - previous) / recent_window
        features[f"ph_{signal}_current"] = current
        features[f"ph_{signal}_log_current"] = math.log1p(max(current, 0.0))
        features[f"ph_{signal}_velocity_recent"] = velocity
        features[f"ph_{signal}_per_current_approved"] = current / current_approved_safe

    pageviews = max(features.get("ph_pageview_current", 0.0), 0.0)
    applies = max(features.get("ph_apply_current", 0.0), 0.0)
    register_clicks = max(features.get("ph_register_click_current", 0.0), 0.0)
    registrations = max(features.get("ph_registration_current", 0.0), 0.0)
    guest_list = max(features.get("ph_guest_list_current", 0.0), 0.0)
    add_to_calendar = max(features.get("ph_add_to_calendar_current", 0.0), 0.0)
    features["ph_apply_per_pageview"] = applies / pageviews if pageviews > 0 else 0.0
    features["ph_apply_per_register_click"] = applies / register_clicks if register_clicks > 0 else 0.0
    features["ph_register_click_per_pageview"] = register_clicks / pageviews if pageviews > 0 else 0.0
    features["ph_registration_per_register_click"] = registrations / register_clicks if register_clicks > 0 else 0.0
    features["ph_guest_list_per_pageview"] = guest_list / pageviews if pageviews > 0 else 0.0
    features["ph_add_to_calendar_per_pageview"] = add_to_calendar / pageviews if pageviews > 0 else 0.0
    return features


def event_strength_stage0_features(
    record: dict[str, Any],
    *,
    horizon: int,
    current_applied: float | None = None,
) -> dict[str, float]:
    if horizon not in {14, 7, 3, 1}:
        return {}

    prev_horizon = {14: 21, 7: 14, 3: 7, 1: 3}[horizon]
    recent_window = float(max(prev_horizon - horizon, 1))
    current_applied_safe = max(float(current_applied or 0.0), 1.0) if current_applied is not None else None

    features: dict[str, float] = {}
    for signal in EVENT_STRENGTH_SIGNALS:
        current = float(record.get(f"{signal}_t{horizon}", 0.0) or 0.0)
        previous = float(record.get(f"{signal}_t{prev_horizon}", 0.0) or 0.0)
        velocity = (current - previous) / recent_window
        features[f"{signal}_current"] = current
        features[f"{signal}_log_current"] = math.log1p(max(current, 0.0))
        features[f"{signal}_velocity_recent"] = velocity
        if current_applied_safe is not None:
            features[f"{signal}_per_current_applied"] = current / current_applied_safe

    invited = max(features.get("invited_current", 0.0), 0.0)
    linked_invited = max(features.get("linked_invited_current", 0.0), 0.0)
    linked_invited_applicant = max(features.get("linked_invited_applicant_current", 0.0), 0.0)
    tracked_applied = max(features.get("tracked_applied_current", 0.0), 0.0)
    repeat_builder_applied = max(features.get("repeat_builder_applied_current", 0.0), 0.0)

    features["linked_invited_share_of_invited"] = linked_invited / invited if invited > 0 else 0.0
    if current_applied_safe is not None:
        features["linked_invited_applicant_share"] = linked_invited_applicant / current_applied_safe
        features["tracked_applied_share"] = tracked_applied / current_applied_safe
        features["repeat_builder_applied_share"] = repeat_builder_applied / current_applied_safe
    return features


def build_signup_examples(signup_df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    horizon_specs = [
        (14, "approved_t14", "approved_t21", None),
        (7, "approved_t7", "approved_t14", "approved_t21"),
        (3, "approved_t3", "approved_t7", "approved_t14"),
        (1, "approved_t1", "approved_t3", "approved_t7"),
    ]

    for row in signup_df.to_dict("records"):
        for horizon, current_col, prev_col, prev2_col in horizon_specs:
            current_approved = float(row[current_col])
            if current_approved < 5:
                continue

            previous = float(row[prev_col]) if prev_col else 0.0
            previous2 = float(row[prev2_col]) if prev2_col else 0.0

            recent_window = max(abs(horizon - ({14: 21, 7: 14, 3: 7, 1: 3}[horizon])), 1)
            prev_window = {14: 7, 7: 7, 3: 4, 1: 4}[horizon]

            velocity_recent = _temporal_velocity(current_approved, previous, recent_window)
            velocity_prev = _temporal_velocity(previous, previous2, prev_window)
            acceleration = velocity_recent - velocity_prev
            ratio_to_prev = _temporal_ratio(current_approved, previous)

            rows.append(
                {
                    "event_id": str(row["event_id"]),
                    "event_start": row["event_start"],
                    "title": row["title"],
                    "city": row.get("city") or "",
                    "horizon_days": float(horizon),
                    "current_approved": current_approved,
                    "previous_approved": previous,
                    "previous2_approved": previous2,
                    "velocity_recent": velocity_recent,
                    "velocity_prev": velocity_prev,
                    "acceleration": acceleration,
                    "ratio_to_prev": ratio_to_prev,
                    "current_approved_zero_flag": float(current_approved <= 0),
                    "current_approved_lt5_flag": float(current_approved < 5),
                    "previous_approved_zero_flag": float(previous <= 0),
                    "log_current_approved": math.log1p(current_approved),
                    "log_horizon_days": math.log1p(horizon),
                    "current_x_horizon": current_approved * float(horizon),
                    "velocity_x_horizon": velocity_recent * float(horizon),
                    "final_approved": float(row["approved_t0"]),
                    "additional_approved": float(row["approved_t0"]) - current_approved,
                }
            )
            rows[-1].update(event_signup_metadata_features(row, horizon_days=float(horizon)))
            rows[-1].update(event_posthog_stage0_features(row, horizon=horizon))
            rows[-1].update(event_strength_stage0_features(row, horizon=horizon))

    return pd.DataFrame(rows)


def build_application_augmented_signup_examples(signup_df: pd.DataFrame) -> pd.DataFrame:
    from .stage0_funnel import load_application_velocity_frame

    application_df = load_application_velocity_frame()
    application_df["event_id"] = application_df["event_id"].astype(str)

    signup_enrichment_cols = [
        "event_id",
        "event_start",
        "title",
        "city",
        "description",
        "description_summary",
        "details",
        "capacity",
        "approval_required",
        "is_platform_hackathon",
        "approved_t21",
        "approved_t14",
        "approved_t7",
        "approved_t3",
        "approved_t1",
        "approved_t0",
        "actual_attended",
    ] + [col for col in signup_df.columns if col.startswith("ph_")]
    signup_enrichment_cols += [
        col
        for col in signup_df.columns
        if col.startswith(EVENT_STRENGTH_SIGNAL_PREFIXES)
    ]
    merged_df = signup_df[signup_enrichment_cols].drop_duplicates("event_id").merge(
        application_df[
            [
                "event_id",
                "applied_t21",
                "applied_t14",
                "applied_t7",
                "applied_t3",
                "applied_t1",
                "applied_t0",
            ]
        ],
        on="event_id",
        how="left",
    )

    rows: list[dict[str, Any]] = []
    horizon_specs = [
        (14, "approved_t14", "approved_t21", None, "applied_t14", "applied_t21", None, 7.0, 7.0),
        (7, "approved_t7", "approved_t14", "approved_t21", "applied_t7", "applied_t14", "applied_t21", 7.0, 7.0),
        (3, "approved_t3", "approved_t7", "approved_t14", "applied_t3", "applied_t7", "applied_t14", 4.0, 7.0),
        (1, "approved_t1", "approved_t3", "approved_t7", "applied_t1", "applied_t3", "applied_t7", 2.0, 4.0),
    ]

    for record in merged_df.to_dict("records"):
        for (
            horizon,
            current_col,
            prev_col,
            prev2_col,
            current_applied_col,
            prev_applied_col,
            prev2_applied_col,
            recent_window,
            prev_window,
        ) in horizon_specs:
            current_approved = float(record.get(current_col, 0.0) or 0.0)
            current_applied = float(record.get(current_applied_col, 0.0) or 0.0)
            if current_approved < 5 and current_applied < 20:
                continue

            previous_approved = float(record.get(prev_col, 0.0) or 0.0)
            previous2_approved = float(record.get(prev2_col, 0.0) or 0.0) if prev2_col else 0.0
            previous_applied = float(record.get(prev_applied_col, 0.0) or 0.0)
            previous2_applied = float(record.get(prev2_applied_col, 0.0) or 0.0) if prev2_applied_col else 0.0

            velocity_recent = _temporal_velocity(current_approved, previous_approved, recent_window)
            velocity_prev = _temporal_velocity(previous_approved, previous2_approved, prev_window)
            applied_velocity_recent = _temporal_velocity(current_applied, previous_applied, recent_window)
            applied_velocity_prev = _temporal_velocity(previous_applied, previous2_applied, prev_window)

            example = {
                "event_id": str(record["event_id"]),
                "event_start": record["event_start"],
                "title": record.get("title") or "",
                "city": record.get("city") or "",
                "horizon_days": float(horizon),
                "current_approved": current_approved,
                "previous_approved": previous_approved,
                "previous2_approved": previous2_approved,
                "velocity_recent": velocity_recent,
                "velocity_prev": velocity_prev,
                "acceleration": velocity_recent - velocity_prev,
                "ratio_to_prev": _temporal_ratio(current_approved, previous_approved),
                "current_approved_zero_flag": float(current_approved <= 0),
                "current_approved_lt5_flag": float(current_approved < 5),
                "previous_approved_zero_flag": float(previous_approved <= 0),
                "log_current_approved": math.log1p(current_approved),
                "log_horizon_days": math.log1p(float(horizon)),
                "current_x_horizon": current_approved * float(horizon),
                "velocity_x_horizon": velocity_recent * float(horizon),
                "final_approved": float(record.get("approved_t0", 0.0) or 0.0),
                "current_applied": current_applied,
                "previous_applied": previous_applied,
                "previous2_applied": previous2_applied,
                "velocity_recent_applied": applied_velocity_recent,
                "velocity_prev_applied": applied_velocity_prev,
                "acceleration_applied": applied_velocity_recent - applied_velocity_prev,
                "ratio_to_prev_applied": _temporal_ratio(current_applied, previous_applied),
                "log_current_applied": math.log1p(current_applied),
                "applied_x_horizon": current_applied * float(horizon),
                "applied_velocity_x_horizon": applied_velocity_recent * float(horizon),
                "current_approval_rate_proxy": current_approved / current_applied if current_applied > 0 else 0.0,
                "current_pending_proxy": max(current_applied - current_approved, 0.0),
                "pending_to_current_approved_ratio": (
                    max(current_applied - current_approved, 0.0) / current_approved if current_approved > 0 else current_applied
                ),
                "approval_sparse_flag": float(current_approved < 5),
                "current_applied_lt20_flag": float(current_applied < 20),
                "final_applied": float(record.get("applied_t0", 0.0) or 0.0),
                "remaining_applied": max(float(record.get("applied_t0", 0.0) or 0.0) - current_applied, 0.0),
            }
            example.update(event_signup_metadata_features(record, horizon_days=float(horizon)))
            example.update(event_posthog_stage0_features(record, horizon=horizon))
            example.update(
                event_strength_stage0_features(
                    record,
                    horizon=horizon,
                    current_applied=current_applied,
                )
            )
            for signal in POSTHOG_STAGE0_SIGNALS:
                current_signal = float(example.get(f"ph_{signal}_current", 0.0) or 0.0)
                example[f"ph_{signal}_per_current_applied"] = (
                    current_signal / current_applied if current_applied > 0 else 0.0
                )
            rows.append(example)

    return pd.DataFrame(rows)


def base_signup_feature_columns(df: pd.DataFrame) -> list[str]:
    return [
        "horizon_days",
        "current_approved",
        "previous_approved",
        "previous2_approved",
        "velocity_recent",
        "velocity_prev",
        "acceleration",
        "ratio_to_prev",
        "current_approved_zero_flag",
        "current_approved_lt5_flag",
        "previous_approved_zero_flag",
        "log_current_approved",
        "log_horizon_days",
        "current_x_horizon",
        "velocity_x_horizon",
        "event_is_platform_hackathon",
        "event_approval_required",
        "event_has_capacity_limit",
        "event_log_capacity",
        "event_log_text_chars",
        "event_log_text_words",
        "event_mentions_prizes",
        "event_mentions_ai",
        "event_mentions_beginner",
        "event_mentions_team",
        "event_mentions_build",
    ]


def posthog_signup_feature_columns(df: pd.DataFrame) -> list[str]:
    return [
        "ph_pageview_current",
        "ph_pageview_log_current",
        "ph_pageview_velocity_recent",
        "ph_pageview_per_current_approved",
        "ph_apply_current",
        "ph_apply_log_current",
        "ph_apply_velocity_recent",
        "ph_apply_per_current_approved",
        "ph_register_click_current",
        "ph_register_click_log_current",
        "ph_register_click_velocity_recent",
        "ph_register_click_per_current_approved",
        "ph_registration_current",
        "ph_registration_log_current",
        "ph_registration_velocity_recent",
        "ph_registration_per_current_approved",
        "ph_guest_list_current",
        "ph_guest_list_log_current",
        "ph_guest_list_velocity_recent",
        "ph_guest_list_per_current_approved",
        "ph_add_to_calendar_current",
        "ph_add_to_calendar_log_current",
        "ph_add_to_calendar_velocity_recent",
        "ph_add_to_calendar_per_current_approved",
        "ph_apply_per_pageview",
        "ph_apply_per_register_click",
        "ph_register_click_per_pageview",
        "ph_registration_per_register_click",
        "ph_guest_list_per_pageview",
        "ph_add_to_calendar_per_pageview",
    ]


def event_strength_signup_feature_columns(df: pd.DataFrame) -> list[str]:
    optional_columns = [
        "invited_current",
        "invited_log_current",
        "invited_velocity_recent",
        "linked_invited_current",
        "linked_invited_log_current",
        "linked_invited_velocity_recent",
        "linked_invited_applicant_current",
        "linked_invited_applicant_log_current",
        "linked_invited_applicant_velocity_recent",
        "tracked_applied_current",
        "tracked_applied_log_current",
        "tracked_applied_velocity_recent",
        "repeat_builder_applied_current",
        "repeat_builder_applied_log_current",
        "repeat_builder_applied_velocity_recent",
        "linked_invited_share_of_invited",
    ]
    return [col for col in optional_columns if col in df.columns]


def application_augmented_signup_feature_columns(df: pd.DataFrame) -> list[str]:
    optional_columns = [
        "current_applied",
        "previous_applied",
        "previous2_applied",
        "velocity_recent_applied",
        "velocity_prev_applied",
        "acceleration_applied",
        "ratio_to_prev_applied",
        "log_current_applied",
        "applied_x_horizon",
        "applied_velocity_x_horizon",
        "current_approved_proxy",
        "previous_approved_proxy",
        "previous2_approved_proxy",
        "velocity_recent_approved_proxy",
        "velocity_prev_approved_proxy",
        "acceleration_approved_proxy",
        "ratio_to_prev_approved_proxy",
        "log_current_approved_proxy",
        "approved_proxy_x_horizon",
        "approved_proxy_velocity_x_horizon",
        "current_approval_rate_proxy",
        "current_pending_proxy",
        "pending_to_current_approved_ratio",
        "approval_sparse_flag",
        "current_applied_lt20_flag",
        "ph_pageview_per_current_applied",
        "ph_apply_per_current_applied",
        "ph_register_click_per_current_applied",
        "ph_registration_per_current_applied",
        "ph_guest_list_per_current_applied",
        "ph_add_to_calendar_per_current_applied",
        "invited_per_current_applied",
        "linked_invited_per_current_applied",
        "linked_invited_applicant_per_current_applied",
        "tracked_applied_per_current_applied",
        "repeat_builder_applied_per_current_applied",
        "linked_invited_applicant_share",
        "tracked_applied_share",
        "repeat_builder_applied_share",
    ]
    return signup_feature_columns(df) + [col for col in optional_columns if col in df.columns]


def applications_first_growth_feature_columns(df: pd.DataFrame) -> list[str]:
    optional_columns = [
        "horizon_days",
        "log_horizon_days",
        "current_applied",
        "log_current_applied",
        "velocity_recent_applied",
        "velocity_prev_applied",
        "acceleration_applied",
        "ratio_to_prev_applied",
        "ph_pageview_current",
        "ph_pageview_log_current",
        "ph_pageview_velocity_recent",
        "ph_pageview_per_current_applied",
        "ph_apply_current",
        "ph_apply_log_current",
        "ph_apply_velocity_recent",
        "ph_apply_per_current_applied",
        "ph_register_click_current",
        "ph_register_click_log_current",
        "ph_register_click_velocity_recent",
        "ph_register_click_per_current_applied",
        "ph_registration_current",
        "ph_registration_log_current",
        "ph_registration_velocity_recent",
        "ph_registration_per_current_applied",
        "invited_current",
        "invited_log_current",
        "invited_velocity_recent",
        "invited_per_current_applied",
        "linked_invited_current",
        "linked_invited_log_current",
        "linked_invited_velocity_recent",
        "linked_invited_per_current_applied",
        "linked_invited_applicant_current",
        "linked_invited_applicant_log_current",
        "linked_invited_applicant_velocity_recent",
        "linked_invited_applicant_per_current_applied",
        "linked_invited_applicant_share",
        "tracked_applied_current",
        "tracked_applied_log_current",
        "tracked_applied_velocity_recent",
        "tracked_applied_per_current_applied",
        "tracked_applied_share",
        "repeat_builder_applied_current",
        "repeat_builder_applied_log_current",
        "repeat_builder_applied_velocity_recent",
        "repeat_builder_applied_per_current_applied",
        "repeat_builder_applied_share",
        "event_log_capacity",
        "event_log_text_chars",
        "event_log_text_words",
        "event_mentions_prizes",
        "event_mentions_ai",
        "event_mentions_beginner",
        "event_mentions_team",
        "event_mentions_build",
    ]
    return [col for col in optional_columns if col in df.columns]


def applications_first_approval_feature_columns(df: pd.DataFrame) -> list[str]:
    optional_columns = [
        "horizon_days",
        "log_horizon_days",
        "current_approved",
        "log_current_approved",
        "current_approved_zero_flag",
        "current_approved_lt5_flag",
        "current_applied",
        "log_current_applied",
        "pred_final_applied",
        "pred_remaining_applied",
        "current_approval_rate_proxy",
        "current_pending_proxy",
        "pending_to_current_approved_ratio",
        "approval_sparse_flag",
        "velocity_recent",
        "velocity_recent_applied",
        "ph_pageview_current",
        "ph_apply_current",
        "ph_registration_current",
        "ph_register_click_current",
        "ph_apply_per_current_applied",
        "ph_registration_per_current_applied",
        "invited_current",
        "linked_invited_current",
        "linked_invited_applicant_current",
        "linked_invited_applicant_share",
        "tracked_applied_current",
        "tracked_applied_share",
        "repeat_builder_applied_current",
        "repeat_builder_applied_share",
        "linked_invited_share_of_invited",
        "event_log_capacity",
        "event_log_text_chars",
        "event_log_text_words",
        "event_mentions_prizes",
        "event_mentions_ai",
        "event_mentions_beginner",
        "event_mentions_team",
        "event_mentions_build",
    ]
    return [col for col in optional_columns if col in df.columns]


def signup_feature_columns(df: pd.DataFrame) -> list[str]:
    return base_signup_feature_columns(df) + posthog_signup_feature_columns(df) + event_strength_signup_feature_columns(df)


def fetch_attendance_snapshot_rows(
    event_ids: list[str] | tuple[str, ...],
    *,
    horizon_days: int,
    dsn: str | None = None,
) -> pd.DataFrame:
    import psycopg2
    import psycopg2.extras

    if not event_ids:
        return pd.DataFrame()

    dsn = dsn or os.environ["PLATFORM_DATABASE_URL"]
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        snapshot_query = f"""
            WITH approval_times AS (
                SELECT
                    (pn.data->>'eventId')::uuid AS event_id,
                    COALESCE(
                        pn.data->>'applicantUserId',
                        pn."userId"::text
                    ) AS applicant_user_id,
                    MIN(pn."createdAt") AS approved_at
                FROM "PlatformNotification" pn
                WHERE pn.type = 'event_application_status_change'
                  AND pn.data->>'status' = 'approved'
                  AND pn.data->>'eventId' IS NOT NULL
                  AND (pn.data->>'eventId') = ANY(%s)
                GROUP BY 1, 2
            )
            SELECT
                pe.id::text AS event_id,
                pe.slug AS event_slug,
                pe.title,
                pe.city,
                pe."startDateTime" AS event_start,
                ea."userId"::text AS user_id,
                ea."createdAt" AS applied_at,
                (ea."utmTrackingId" IS NOT NULL) AS tracked_application_flag,
                ea."appliedFromTimeZone" AS applicant_tz,
                EXTRACT(EPOCH FROM (pe."startDateTime" - ea."createdAt")) / 86400.0 AS days_before_event,
                ea."checkedIn"::int AS showed_up,
                at.approved_at,
                0::bigint AS page_views,
                0::bigint AS recent_view_count_7d,
                0::bigint AS views_after_approval,
                9999.0::double precision AS days_since_last_view,
                0::bigint AS distinct_active_view_days,
                9999.0::double precision AS approval_to_first_view_hours,
                false AS notification_read,
                9999.0::double precision AS notification_read_latency_hours,
                0::bigint AS notification_read_bucket_same_day,
                0::bigint AS notification_read_bucket_one_to_three_days,
                0::bigint AS notification_read_bucket_after_three_days,
                false AS invited_user_flag
            FROM "EventApplicant" ea
            JOIN "PlatformEvent" pe ON pe.id = ea."eventId"
            JOIN approval_times at
              ON at.event_id = pe.id
             AND at.applicant_user_id = ea."userId"::text
            WHERE pe.id::text = ANY(%s)
              AND ea.status = 'approved'
              AND at.approved_at <= pe."startDateTime" - INTERVAL '{int(horizon_days)} days'
            ORDER BY pe."startDateTime", ea."createdAt"
        """
        cur.execute(snapshot_query, (list(event_ids), list(event_ids)))
        rows = cur.fetchall()
        cur.close()
        snapshot_df = pd.DataFrame(rows)
        if snapshot_df.empty:
            return snapshot_df

        user_ids = snapshot_df["user_id"].dropna().astype(str).unique().tolist()
        event_id_list = snapshot_df["event_id"].dropna().astype(str).unique().tolist()

        pv_cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        pv_cur.execute(
            f"""
            SELECT
                i."userId"::text AS user_id,
                i.properties->>'eventId' AS event_id,
                i."createdAt" AS view_at
            FROM "Insight" i
            JOIN "PlatformEvent" pe ON pe.id::text = i.properties->>'eventId'
            WHERE i."eventName" = 'PAGE_VIEW'
              AND i.properties->>'eventId' = ANY(%s)
              AND i."userId"::text = ANY(%s)
              AND i."createdAt" <= pe."startDateTime" - INTERVAL '{int(horizon_days)} days'
            ORDER BY i."userId", i.properties->>'eventId', i."createdAt"
            """,
            (event_id_list, user_ids),
        )
        pv_rows = pv_cur.fetchall()
        pv_cur.close()

        notif_cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        notif_cur.execute(
            f"""
            SELECT
                COALESCE(
                    pn.data->>'applicantUserId',
                    pn."userId"::text
                ) AS user_id,
                pn.data->>'eventId' AS event_id,
                MIN(pn."createdAt") AS first_notif_created_at,
                MIN(pn."updatedAt") FILTER (WHERE pn.read) AS first_read_at
            FROM "PlatformNotification" pn
            JOIN "PlatformEvent" pe ON pe.id::text = pn.data->>'eventId'
            WHERE pn.type = 'event_application_status_change'
              AND pn.data->>'status' = 'approved'
              AND pn.data->>'eventId' = ANY(%s)
              AND COALESCE(
                    pn.data->>'applicantUserId',
                    pn."userId"::text
                  ) = ANY(%s)
              AND pn."createdAt" <= pe."startDateTime" - INTERVAL '{int(horizon_days)} days'
            GROUP BY 1, 2
            """,
            (event_id_list, user_ids),
        )
        notif_rows = notif_cur.fetchall()
        notif_cur.close()

        invite_cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        invite_cur.execute(
            f"""
            SELECT
                ei."eventId"::text AS event_id,
                ei."userId"::text AS user_id,
                MIN(ei."createdAt") AS first_invited_at
            FROM "EventInvitation" ei
            JOIN "PlatformEvent" pe ON pe.id = ei."eventId"
            WHERE ei."eventId"::text = ANY(%s)
              AND ei."userId"::text = ANY(%s)
              AND ei."invitationType"::text = 'attend'
              AND ei."createdAt" <= pe."startDateTime" - INTERVAL '{int(horizon_days)} days'
            GROUP BY 1, 2
            """,
            (event_id_list, user_ids),
        )
        invite_rows = invite_cur.fetchall()
        invite_cur.close()

        view_times_by_key: dict[tuple[str, str], list[pd.Timestamp]] = {}
        for row in pv_rows:
            key = (str(row["user_id"]), str(row["event_id"]))
            view_times_by_key.setdefault(key, []).append(pd.Timestamp(row["view_at"]))

        notif_by_key: dict[tuple[str, str], tuple[pd.Timestamp | None, pd.Timestamp | None]] = {}
        for row in notif_rows:
            key = (str(row["user_id"]), str(row["event_id"]))
            created_at = pd.Timestamp(row["first_notif_created_at"]) if row["first_notif_created_at"] is not None else None
            read_at = pd.Timestamp(row["first_read_at"]) if row["first_read_at"] is not None else None
            notif_by_key[key] = (created_at, read_at)

        invite_by_key: dict[tuple[str, str], pd.Timestamp] = {}
        for row in invite_rows:
            key = (str(row["user_id"]), str(row["event_id"]))
            invite_by_key[key] = pd.Timestamp(row["first_invited_at"])

        metric_rows: list[dict[str, Any]] = []
        for row in snapshot_df.to_dict("records"):
            key = (str(row["user_id"]), str(row["event_id"]))
            event_start = pd.Timestamp(row["event_start"])
            snapshot_time = event_start - pd.Timedelta(days=float(horizon_days))
            approved_at = pd.Timestamp(row["approved_at"]) if row.get("approved_at") is not None else None
            view_times = sorted(view_times_by_key.get(key, []))

            page_views = float(len(view_times))
            recent_view_count_7d = float(
                sum(1 for view_at in view_times if view_at >= snapshot_time - pd.Timedelta(days=7))
            )
            distinct_active_view_days = float(len({view_at.date() for view_at in view_times}))

            if view_times:
                last_view_at = view_times[-1]
                days_since_last_view = max((snapshot_time - last_view_at).total_seconds() / 86400.0, 0.0)
            else:
                days_since_last_view = 9999.0

            views_after_approval = 0.0
            approval_to_first_view_hours = 9999.0
            if approved_at is not None:
                post_approval_views = [view_at for view_at in view_times if view_at >= approved_at]
                views_after_approval = float(len(post_approval_views))
                if post_approval_views:
                    approval_to_first_view_hours = max(
                        (post_approval_views[0] - approved_at).total_seconds() / 3600.0,
                        0.0,
                    )

            notif_created_at, notif_read_at = notif_by_key.get(key, (None, None))
            notification_read = bool(
                notif_created_at is not None
                and notif_created_at < snapshot_time
                and notif_read_at is not None
                and notif_read_at <= snapshot_time
            )
            if notification_read and notif_created_at is not None and notif_read_at is not None:
                notification_read_latency_hours = max(
                    (notif_read_at - notif_created_at).total_seconds() / 3600.0,
                    0.0,
                )
            else:
                notification_read_latency_hours = 9999.0

            same_day = float(notification_read and notification_read_latency_hours <= 24.0)
            one_to_three_days = float(notification_read and 24.0 < notification_read_latency_hours <= 72.0)
            after_three_days = float(notification_read and notification_read_latency_hours > 72.0)

            metric_rows.append(
                {
                    "user_id": key[0],
                    "event_id": key[1],
                    "page_views": page_views,
                    "recent_view_count_7d": recent_view_count_7d,
                    "views_after_approval": views_after_approval,
                    "days_since_last_view": days_since_last_view,
                    "distinct_active_view_days": distinct_active_view_days,
                    "approval_to_first_view_hours": approval_to_first_view_hours,
                    "notification_read": notification_read,
                    "notification_read_latency_hours": notification_read_latency_hours,
                    "notification_read_bucket_same_day": same_day,
                    "notification_read_bucket_one_to_three_days": one_to_three_days,
                    "notification_read_bucket_after_three_days": after_three_days,
                    "invited_user_flag": float(key in invite_by_key),
                }
            )

        metric_df = pd.DataFrame(metric_rows)
        snapshot_df = snapshot_df.drop(
            columns=[
                "page_views",
                "recent_view_count_7d",
                "views_after_approval",
                "days_since_last_view",
                "distinct_active_view_days",
                "approval_to_first_view_hours",
                "notification_read",
                "notification_read_latency_hours",
                "notification_read_bucket_same_day",
                "notification_read_bucket_one_to_three_days",
                "notification_read_bucket_after_three_days",
                "invited_user_flag",
            ]
        ).merge(metric_df, on=["user_id", "event_id"], how="left")
        history_cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        history_cur.execute(
            """
            SELECT
                ea2."userId"::text AS user_id,
                pe2."startDateTime" AS event_start,
                COUNT(*) FILTER (WHERE ea2.status = 'approved') AS prior_events,
                COALESCE(SUM(ea2."checkedIn"::int) FILTER (WHERE ea2.status = 'approved'), 0) AS prior_attended
            FROM "EventApplicant" ea2
            JOIN "PlatformEvent" pe2 ON pe2.id = ea2."eventId"
            WHERE pe2."startDateTime" IS NOT NULL
              AND ea2.status = 'approved'
              AND ea2."userId"::text = ANY(%s)
            GROUP BY ea2."userId", pe2."startDateTime"
            ORDER BY ea2."userId", pe2."startDateTime"
            """,
            (user_ids,),
        )
        history_rows = history_cur.fetchall()
        history_cur.close()

        history_by_user: dict[str, list[tuple[pd.Timestamp, float, float]]] = {}
        for row in history_rows:
            user_id = str(row["user_id"])
            history_by_user.setdefault(user_id, []).append(
                (
                    pd.Timestamp(row["event_start"]),
                    float(row["prior_events"] or 0.0),
                    float(row["prior_attended"] or 0.0),
                )
            )

        prior_events_values: list[float] = []
        prior_attended_values: list[float] = []
        for row in snapshot_df.to_dict("records"):
            event_start = pd.Timestamp(row["event_start"])
            snapshot_time = event_start - pd.Timedelta(days=float(horizon_days))
            user_history = history_by_user.get(str(row["user_id"]), [])
            prior_events, prior_attended = accumulate_prior_history_before_snapshot(
                user_history,
                snapshot_time=snapshot_time,
            )
            prior_events_values.append(prior_events)
            prior_attended_values.append(prior_attended)

        snapshot_df["prior_events"] = prior_events_values
        snapshot_df["prior_attended"] = prior_attended_values
        posthog_person_df = build_person_attendance_features(snapshot_df, horizon_days=horizon_days)
        snapshot_df = snapshot_df.merge(posthog_person_df, on=["user_id", "event_id"], how="left")
        return snapshot_df
    finally:
        conn.close()


def build_attendance_frame(snapshot_df: pd.DataFrame, *, horizon_days: int) -> pd.DataFrame:
    if snapshot_df.empty:
        return pd.DataFrame()

    df = snapshot_df.copy()
    df["days_before_event"] = pd.to_numeric(df["days_before_event"], errors="coerce").fillna(0.0)
    df["applied_at"] = pd.to_datetime(df["applied_at"], errors="coerce", utc=True)
    df["prior_events"] = pd.to_numeric(df["prior_events"], errors="coerce").fillna(0.0)
    df["prior_attended"] = pd.to_numeric(df["prior_attended"], errors="coerce").fillna(0.0)
    df["page_views"] = pd.to_numeric(df["page_views"], errors="coerce").fillna(0.0)
    df["recent_view_count_7d"] = pd.to_numeric(df["recent_view_count_7d"], errors="coerce").fillna(0.0)
    df["views_after_approval"] = pd.to_numeric(df["views_after_approval"], errors="coerce").fillna(0.0)
    df["days_since_last_view"] = pd.to_numeric(df["days_since_last_view"], errors="coerce").fillna(9999.0)
    df["distinct_active_view_days"] = pd.to_numeric(df["distinct_active_view_days"], errors="coerce").fillna(0.0)
    df["approval_to_first_view_hours"] = pd.to_numeric(df["approval_to_first_view_hours"], errors="coerce").fillna(9999.0)
    df["notification_read"] = df["notification_read"].astype(bool)
    df["notification_read_latency_hours"] = pd.to_numeric(
        df["notification_read_latency_hours"], errors="coerce"
    ).fillna(9999.0)
    df["notification_read_bucket_same_day"] = pd.to_numeric(
        df["notification_read_bucket_same_day"], errors="coerce"
    ).fillna(0.0)
    df["notification_read_bucket_one_to_three_days"] = pd.to_numeric(
        df["notification_read_bucket_one_to_three_days"], errors="coerce"
    ).fillna(0.0)
    df["notification_read_bucket_after_three_days"] = pd.to_numeric(
        df["notification_read_bucket_after_three_days"], errors="coerce"
    ).fillna(0.0)
    df["tracked_application_flag"] = pd.to_numeric(
        df["tracked_application_flag"], errors="coerce"
    ).fillna(0.0)
    df["invited_user_flag"] = pd.to_numeric(df["invited_user_flag"], errors="coerce").fillna(0.0)
    for col in [
        "ph_open_messages_count",
        "ph_open_messages_recent_7d",
        "ph_open_messages_after_approval",
        "ph_guest_list_count",
        "ph_guest_list_recent_7d",
        "ph_chat_send_count",
        "ph_chat_send_recent_7d",
        "ph_chat_send_after_approval",
        "ph_add_to_calendar_count",
        "ph_add_to_calendar_flag",
    ]:
        if col not in df.columns:
            df[col] = 0.0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    df["showed_up"] = pd.to_numeric(df["showed_up"], errors="coerce").fillna(0).astype(int)
    df["horizon_days"] = float(horizon_days)
    df["prior_rate"] = np.where(df["prior_events"] > 0, df["prior_attended"] / df["prior_events"], -1.0)
    df["has_prior_history"] = (df["prior_events"] > 0).astype(int)
    df["log_page_views"] = np.log1p(df["page_views"])
    df["log_recent_view_count_7d"] = np.log1p(df["recent_view_count_7d"])
    df["log_views_after_approval"] = np.log1p(df["views_after_approval"])
    df["log_ph_open_messages_count"] = np.log1p(df["ph_open_messages_count"])
    df["log_ph_guest_list_count"] = np.log1p(df["ph_guest_list_count"])
    df["log_ph_chat_send_count"] = np.log1p(df["ph_chat_send_count"])
    df["log_prior_events"] = np.log1p(df["prior_events"])
    df["log_days_before_event"] = np.log1p(df["days_before_event"].clip(lower=0))
    df["log_horizon_days"] = np.log1p(df["horizon_days"])
    df["elapsed_days"] = (df["days_before_event"] - df["horizon_days"]).clip(lower=0.25)
    df["page_views_per_elapsed_day"] = df["page_views"] / df["elapsed_days"]
    df["recent_views_per_elapsed_day"] = df["recent_view_count_7d"] / df["elapsed_days"]
    df["page_views_x_horizon"] = df["log_page_views"] * df["log_horizon_days"]
    df["recent_views_x_horizon"] = df["log_recent_view_count_7d"] * df["log_horizon_days"]
    df["notif_x_horizon"] = df["notification_read"].astype(int) * df["log_horizon_days"]
    df["notif_x_views"] = df["notification_read"].astype(int) * df["log_page_views"]
    df["ph_open_messages_x_horizon"] = df["log_ph_open_messages_count"] * df["log_horizon_days"]
    df["ph_chat_send_x_horizon"] = df["log_ph_chat_send_count"] * df["log_horizon_days"]
    df["ph_calendar_x_horizon"] = df["ph_add_to_calendar_flag"] * df["log_horizon_days"]
    df["ph_commitment_signal_count"] = (
        (df["ph_open_messages_count"] > 0).astype(int)
        + (df["ph_guest_list_count"] > 0).astype(int)
        + (df["ph_chat_send_count"] > 0).astype(int)
        + (df["ph_add_to_calendar_flag"] > 0).astype(int)
    )
    df["notification_latency_known"] = (df["notification_read_latency_hours"] < 9999.0).astype(int)
    df["days_since_last_view_capped"] = df["days_since_last_view"].clip(lower=0.0, upper=30.0)
    df["approval_to_first_view_hours_capped"] = df["approval_to_first_view_hours"].clip(lower=0.0, upper=24.0 * 14.0)
    df["recent_signup_flag"] = (df["days_before_event"] <= 3).astype(int)
    df["late_signup_flag"] = (df["days_before_event"] <= 7).astype(int)

    first_submission_map = load_first_submission_map(str(USER_HACKATHON_HISTORY_CSV))
    df["first_submission_at"] = df["user_id"].astype(str).map(first_submission_map)
    df["first_submission_at"] = pd.to_datetime(df["first_submission_at"], errors="coerce", utc=True)
    df["repeat_builder_flag"] = (
        df["first_submission_at"].notna()
        & df["applied_at"].notna()
        & (df["first_submission_at"] < df["applied_at"])
    ).astype(int)
    repeat_builder_age_days = (
        (df["applied_at"] - df["first_submission_at"]).dt.total_seconds() / 86400.0
    )
    df["repeat_builder_age_days_capped"] = repeat_builder_age_days.where(
        df["repeat_builder_flag"].astype(bool),
        0.0,
    ).fillna(0.0).clip(lower=0.0, upper=3650.0)
    df["repeat_builder_x_horizon"] = df["repeat_builder_flag"] * df["log_horizon_days"]
    df["invited_x_horizon"] = df["invited_user_flag"] * df["log_horizon_days"]
    df["tracked_x_horizon"] = df["tracked_application_flag"] * df["log_horizon_days"]

    tz_offsets = []
    tz_buckets = []
    for row in df.itertuples(index=False):
        city = getattr(row, "city", "")
        title = getattr(row, "title", "")
        city_str = "" if pd.isna(city) else str(city)
        title_str = "" if pd.isna(title) else str(title)
        event_tz = city_to_timezone(city_str, title_str)
        offset = compute_tz_offset_diff(
            getattr(row, "applicant_tz", None) or "America/Los_Angeles",
            event_tz,
            event_date=getattr(row, "event_start", None),
        )
        tz_offsets.append(offset)
        tz_buckets.append(tz_distance_bucket(offset))

    df["tz_offset_hrs"] = tz_offsets
    df["abs_tz_offset_hrs"] = np.abs(df["tz_offset_hrs"])
    df["tz_distance_bucket"] = tz_buckets
    df = pd.get_dummies(df, columns=["tz_distance_bucket"], prefix="tz", dtype=float)
    return df


def attendance_feature_columns(df: pd.DataFrame) -> list[str]:
    base_cols = [
        "days_before_event",
        "horizon_days",
        "log_horizon_days",
        "page_views",
        "log_page_views",
        "recent_view_count_7d",
        "log_recent_view_count_7d",
        "views_after_approval",
        "log_views_after_approval",
        "days_since_last_view_capped",
        "distinct_active_view_days",
        "approval_to_first_view_hours_capped",
        "notification_read",
        "notification_read_latency_hours",
        "notification_latency_known",
        "notification_read_bucket_same_day",
        "notification_read_bucket_one_to_three_days",
        "notification_read_bucket_after_three_days",
        "prior_events",
        "prior_attended",
        "prior_rate",
        "has_prior_history",
        "log_prior_events",
        "elapsed_days",
        "page_views_per_elapsed_day",
        "recent_views_per_elapsed_day",
        "page_views_x_horizon",
        "recent_views_x_horizon",
        "notif_x_horizon",
        "notif_x_views",
        "ph_open_messages_count",
        "ph_open_messages_recent_7d",
        "ph_open_messages_after_approval",
        "log_ph_open_messages_count",
        "ph_guest_list_count",
        "ph_guest_list_recent_7d",
        "log_ph_guest_list_count",
        "ph_chat_send_count",
        "ph_chat_send_recent_7d",
        "ph_chat_send_after_approval",
        "log_ph_chat_send_count",
        "ph_add_to_calendar_count",
        "ph_add_to_calendar_flag",
        "ph_open_messages_x_horizon",
        "ph_chat_send_x_horizon",
        "ph_calendar_x_horizon",
        "ph_commitment_signal_count",
        "tracked_application_flag",
        "tracked_x_horizon",
        "invited_user_flag",
        "invited_x_horizon",
        "repeat_builder_flag",
        "repeat_builder_age_days_capped",
        "repeat_builder_x_horizon",
        "tz_offset_hrs",
        "abs_tz_offset_hrs",
        "recent_signup_flag",
        "late_signup_flag",
    ]
    tz_dummy_cols = sorted(
        col
        for col in df.columns
        if col.startswith("tz_") and col not in {"tz_offset_hrs", "tz_distance_bucket"}
    )
    return base_cols + tz_dummy_cols


def _build_attendance_estimator(scale_pos_weight: float) -> tuple[Any, str, dict[str, Any]]:
    try:
        from xgboost import XGBClassifier

        return (
            XGBClassifier(
                n_estimators=400,
                max_depth=4,
                learning_rate=0.05,
                subsample=0.8,
                colsample_bytree=0.8,
                min_child_weight=20,
                reg_lambda=1.0,
                random_state=42,
                eval_metric="logloss",
                scale_pos_weight=scale_pos_weight,
                verbosity=0,
            ),
            "xgboost",
            {},
        )
    except Exception:  # pragma: no cover - exercised via import failure test
        from sklearn.ensemble import HistGradientBoostingClassifier

        return (
            HistGradientBoostingClassifier(
                max_iter=400,
                max_depth=4,
                learning_rate=0.05,
                min_samples_leaf=20,
                l2_regularization=1.0,
                random_state=42,
            ),
            "sklearn_hist_gradient_boosting",
            {},
        )


def _build_signup_estimator() -> tuple[Any, str]:
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
    except Exception:  # pragma: no cover - exercised via import failure test
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


def fit_attendance_model(
    train_df: pd.DataFrame,
    calibration_df: pd.DataFrame,
) -> dict[str, Any]:
    from sklearn.isotonic import IsotonicRegression

    feature_cols = attendance_feature_columns(train_df)
    x_train = train_df[feature_cols].astype(float)
    y_train = train_df["showed_up"].astype(int)
    x_cal = calibration_df.reindex(columns=feature_cols, fill_value=0.0).astype(float)
    y_cal = calibration_df["showed_up"].astype(int)

    pos_rate = max(float(y_train.mean()), 1e-6)
    scale_pos_weight = (1.0 - pos_rate) / pos_rate

    model, backend, fit_kwargs = _build_attendance_estimator(scale_pos_weight)
    if backend == "sklearn_hist_gradient_boosting":
        fit_kwargs["sample_weight"] = np.where(
            y_train.to_numpy() == 1,
            scale_pos_weight,
            1.0,
        )
    model.fit(x_train, y_train, **fit_kwargs)

    calibrator = None
    if len(np.unique(y_cal)) > 1:
        raw_cal = model.predict_proba(x_cal)[:, 1]
        calibrator = IsotonicRegression(out_of_bounds="clip")
        calibrator.fit(raw_cal, y_cal)

    return {
        "model": model,
        "calibrator": calibrator,
        "feature_columns": feature_cols,
        "positive_rate": pos_rate,
        "backend": backend,
    }


def predict_attendance_probabilities(
    frame: pd.DataFrame,
    artifact: dict[str, Any],
) -> np.ndarray:
    feature_cols = artifact["feature_columns"]
    x = frame.reindex(columns=feature_cols, fill_value=0.0).astype(float)
    raw = artifact["model"].predict_proba(x)[:, 1]
    calibrator = artifact.get("calibrator")
    if calibrator is not None:
        raw = calibrator.predict(raw)
    return np.clip(raw, 0.0, 1.0)


def _shrunken_bias_corrections(
    calibration_df: pd.DataFrame,
    raw_predictions: np.ndarray,
    *,
    prior_strength: float,
) -> dict[str, float]:
    temp = calibration_df.copy()
    temp["raw_pred"] = raw_predictions
    temp["residual"] = temp["final_approved"] - temp["raw_pred"]
    corrections: dict[str, float] = {}
    for horizon_value, horizon_df in temp.groupby("horizon_days"):
        count = float(len(horizon_df))
        shrink = count / (count + max(prior_strength, 0.0)) if count > 0 else 0.0
        corrections[str(int(horizon_value))] = float(horizon_df["residual"].mean()) * shrink
    return corrections


def fit_signup_model(
    train_df: pd.DataFrame,
    calibration_df: pd.DataFrame,
    *,
    feature_cols: list[str] | None = None,
    prior_strength: float = SIGNUP_BIAS_CORRECTION_PRIOR_STRENGTH,
) -> dict[str, Any]:
    feature_cols = feature_cols or signup_feature_columns(train_df)
    x_train = train_df[feature_cols].astype(float)
    y_train = train_df["final_approved"].astype(float)
    x_cal = calibration_df.reindex(columns=feature_cols, fill_value=0.0).astype(float)
    y_cal = calibration_df["final_approved"].astype(float)

    model, backend = _build_signup_estimator()
    model.fit(x_train, y_train)

    residual_quantiles = None
    bias_corrections: dict[str, float] = {}
    if not calibration_df.empty:
        raw_cal = model.predict(x_cal)
        residuals = y_cal.to_numpy() - raw_cal
        residual_quantiles = {
            "p10": float(np.quantile(residuals, 0.10)),
            "p50": float(np.quantile(residuals, 0.50)),
            "p90": float(np.quantile(residuals, 0.90)),
        }
        bias_corrections = _shrunken_bias_corrections(
            calibration_df,
            raw_cal,
            prior_strength=prior_strength,
        )

    return {
        "mode": "single",
        "model": model,
        "feature_columns": feature_cols,
        "residual_quantiles": residual_quantiles,
        "bias_corrections": bias_corrections,
        "backend": backend,
        "bias_correction_prior_strength": float(prior_strength),
    }

def fit_signup_strategy_model(
    train_df: pd.DataFrame,
    calibration_df: pd.DataFrame,
    *,
    augmented_train_df: pd.DataFrame | None = None,
    augmented_calibration_df: pd.DataFrame | None = None,
    applications_first_train_df: pd.DataFrame | None = None,
    applications_first_calibration_df: pd.DataFrame | None = None,
    prior_strength: float = SIGNUP_BIAS_CORRECTION_PRIOR_STRENGTH,
) -> dict[str, Any]:
    calibration_df = calibration_df.reset_index(drop=True).copy()
    baseline_artifact = fit_signup_model(
        train_df,
        calibration_df,
        feature_cols=base_signup_feature_columns(train_df),
        prior_strength=prior_strength,
    )
    posthog_artifact = fit_signup_model(
        train_df,
        calibration_df,
        feature_cols=signup_feature_columns(train_df),
        prior_strength=prior_strength,
    )

    selection_df = calibration_df
    app_augmented_artifact = None
    applications_first_artifact = None
    if augmented_train_df is not None and augmented_calibration_df is not None:
        augmented_train_df = augmented_train_df.reset_index(drop=True).copy()
        augmented_calibration_df = augmented_calibration_df.reset_index(drop=True).copy()
        if not augmented_train_df.empty and not augmented_calibration_df.empty:
            app_augmented_artifact = fit_signup_model(
                augmented_train_df,
                augmented_calibration_df,
                feature_cols=application_augmented_signup_feature_columns(augmented_train_df),
                prior_strength=prior_strength,
            )
            selection_df = augmented_calibration_df

    if applications_first_train_df is not None and applications_first_calibration_df is not None:
        applications_first_train_df = applications_first_train_df.reset_index(drop=True).copy()
        applications_first_calibration_df = applications_first_calibration_df.reset_index(drop=True).copy()
        if not applications_first_train_df.empty and not applications_first_calibration_df.empty:
            from .stage0_funnel import (
                application_growth_caps,
                apply_application_caps,
                fit_regressor,
                predict_regressor,
            )

            growth_artifact = fit_regressor(
                applications_first_train_df,
                applications_first_calibration_df,
                feature_cols=applications_first_growth_feature_columns(applications_first_train_df),
                target_col="remaining_applied",
                log_target=True,
            )
            app_caps = application_growth_caps(applications_first_train_df)
            for frame in (applications_first_train_df, applications_first_calibration_df):
                frame["pred_remaining_applied"] = predict_regressor(growth_artifact, frame)
                frame["pred_final_applied"] = frame["current_applied"] + frame["pred_remaining_applied"]
                apply_application_caps(frame, pred_final_col="pred_final_applied", caps=app_caps)
            approval_artifact = fit_regressor(
                applications_first_train_df,
                applications_first_calibration_df,
                feature_cols=applications_first_approval_feature_columns(applications_first_train_df),
                target_col="final_approved",
            )
            applications_first_artifact = {
                "growth_artifact": growth_artifact,
                "approval_artifact": approval_artifact,
                "application_growth_caps": app_caps,
            }
            selection_df = applications_first_calibration_df

    baseline_preds = _predict_signup_totals_raw_single(selection_df, baseline_artifact)
    posthog_preds = _predict_signup_totals_raw_single(selection_df, posthog_artifact)
    base_strategy_by_horizon: dict[str, str] = {}
    strategy_by_horizon: dict[str, str] = {}
    app_augmented_preds = None
    if app_augmented_artifact is not None:
        app_augmented_preds = _predict_signup_totals_raw_single(selection_df, app_augmented_artifact)
    applications_first_preds = None
    if applications_first_artifact is not None:
        applications_first_preds = predict_applications_first_signup_totals(
            selection_df,
            applications_first_artifact,
        )

    for horizon_value, horizon_df in selection_df.groupby("horizon_days"):
        idx = horizon_df.index.to_numpy(dtype=int)
        actual = horizon_df["final_approved"].to_numpy(dtype=float)
        base_candidates = {
            "baseline": baseline_preds[idx],
            "posthog": posthog_preds[idx],
        }
        best_base = min(
            base_candidates,
            key=lambda name: float(np.mean(np.abs(base_candidates[name] - actual))),
        )
        base_strategy_by_horizon[str(int(horizon_value))] = best_base

        candidates = dict(base_candidates)
        if app_augmented_preds is not None and float(horizon_value) >= 7.0:
            candidates["app_augmented"] = app_augmented_preds[idx]
        if applications_first_preds is not None and float(horizon_value) == 7.0:
            candidates["applications_first"] = applications_first_preds[idx]
        best_name = min(
            candidates,
            key=lambda name: float(np.mean(np.abs(candidates[name] - actual))),
        )
        strategy_by_horizon[str(int(horizon_value))] = best_name

    artifact = {
        "mode": "strategy",
        "baseline": baseline_artifact,
        "posthog": posthog_artifact,
        "base_strategy_by_horizon": base_strategy_by_horizon,
        "strategy_by_horizon": strategy_by_horizon,
        "bias_correction_prior_strength": float(prior_strength),
    }
    if app_augmented_artifact is not None:
        artifact["app_augmented"] = app_augmented_artifact
    if applications_first_artifact is not None:
        artifact["applications_first"] = applications_first_artifact
    return artifact


def predict_applications_first_signup_totals(frame: pd.DataFrame, artifact: dict[str, Any]) -> np.ndarray:
    from .stage0_funnel import apply_application_caps, predict_regressor

    if frame.empty:
        return np.array([], dtype=float)

    temp = frame.copy()
    missing_current_applied = "current_applied" not in temp.columns
    if missing_current_applied:
        return temp["current_approved"].to_numpy(dtype=float)

    temp["pred_remaining_applied"] = predict_regressor(artifact["growth_artifact"], temp)
    temp["pred_final_applied"] = temp["current_applied"] + temp["pred_remaining_applied"]
    apply_application_caps(
        temp,
        pred_final_col="pred_final_applied",
        caps=artifact["application_growth_caps"],
    )
    preds = predict_regressor(artifact["approval_artifact"], temp)
    return np.maximum(preds, temp["current_approved"].to_numpy(dtype=float))


def _predict_signup_totals_raw_single(frame: pd.DataFrame, artifact: dict[str, Any]) -> np.ndarray:
    feature_cols = artifact["feature_columns"]
    x = frame.reindex(columns=feature_cols, fill_value=0.0).astype(float)
    preds = artifact["model"].predict(x)
    return np.maximum(preds, frame["current_approved"].to_numpy(dtype=float))


def _predict_signup_totals_single(frame: pd.DataFrame, artifact: dict[str, Any]) -> np.ndarray:
    preds = _predict_signup_totals_raw_single(frame, artifact)
    bias_corrections = artifact.get("bias_corrections", {})
    if bias_corrections:
        adjustments = frame["horizon_days"].map(lambda h: bias_corrections.get(str(int(h)), 0.0)).to_numpy(dtype=float)
        preds = preds + adjustments
    return np.maximum(preds, frame["current_approved"].to_numpy(dtype=float))


def predict_signup_totals(frame: pd.DataFrame, artifact: dict[str, Any]) -> np.ndarray:
    if artifact.get("mode") == "strategy":
        baseline_preds = _predict_signup_totals_single(frame, artifact["baseline"])
        posthog_preds = _predict_signup_totals_single(frame, artifact["posthog"])
        final_preds = baseline_preds.copy()
        base_strategy_by_horizon = artifact.get("base_strategy_by_horizon", {})
        if not base_strategy_by_horizon:
            base_strategy_by_horizon = artifact.get("strategy_by_horizon", {})
        for pos, horizon_value in enumerate(frame["horizon_days"].to_numpy(dtype=float)):
            if base_strategy_by_horizon.get(str(int(horizon_value))) == "posthog":
                final_preds[pos] = posthog_preds[pos]

        app_augmented_artifact = artifact.get("app_augmented")
        app_augmented_preds = None
        if app_augmented_artifact is not None:
            app_augmented_preds = _predict_signup_totals_single(frame, app_augmented_artifact)
        applications_first_artifact = artifact.get("applications_first")
        applications_first_preds = None
        if applications_first_artifact is not None:
            applications_first_preds = predict_applications_first_signup_totals(
                frame,
                applications_first_artifact,
            )
        strategy_by_horizon = artifact.get("strategy_by_horizon", {})
        current_applied = (
            frame["current_applied"].to_numpy(dtype=float)
            if "current_applied" in frame.columns
            else np.full(len(frame), np.nan)
        )
        for pos, horizon_value in enumerate(frame["horizon_days"].to_numpy(dtype=float)):
            strategy = strategy_by_horizon.get(str(int(horizon_value)))
            if strategy == "posthog":
                final_preds[pos] = posthog_preds[pos]
            elif strategy == "app_augmented" and app_augmented_preds is not None and not np.isnan(current_applied[pos]):
                final_preds[pos] = app_augmented_preds[pos]
            elif (
                strategy == "applications_first"
                and applications_first_preds is not None
                and not np.isnan(current_applied[pos])
            ):
                final_preds[pos] = applications_first_preds[pos]
        return np.maximum(final_preds, frame["current_approved"].to_numpy(dtype=float))
    return _predict_signup_totals_single(frame, artifact)


def save_artifact_bundle(
    output_dir: Path,
    *,
    attendance_artifact: dict[str, Any],
    signup_artifact: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(attendance_artifact, output_dir / "attendance_model.joblib")
    joblib.dump(signup_artifact, output_dir / "signup_model.joblib")
    with (output_dir / "metadata.json").open("w") as f:
        json.dump(metadata, f, indent=2, default=str)


def load_artifact_bundle(output_dir: Path) -> dict[str, Any]:
    return {
        "attendance": joblib.load(output_dir / "attendance_model.joblib"),
        "signup": joblib.load(output_dir / "signup_model.joblib"),
        "metadata": json.loads((output_dir / "metadata.json").read_text()),
    }
