from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd


DEFAULT_USER_HISTORY_CSV = (
    Path(__file__).resolve().parents[3]
    / "local_data"
    / "agent_analysis"
    / "user_hackathon_history.csv"
)

EVENT_STRENGTH_SIGNAL_PREFIXES = (
    "invited",
    "linked_invited",
    "linked_invited_applicant",
    "tracked_applied",
    "repeat_builder_applied",
)


def _to_timestamp(value: Any) -> pd.Timestamp | None:
    if value is None or value == "":
        return None
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


@lru_cache(maxsize=1)
def load_first_submission_map(csv_path: str | None = None) -> dict[str, pd.Timestamp]:
    path = Path(csv_path) if csv_path else DEFAULT_USER_HISTORY_CSV
    if not path.exists():
        return {}

    history_df = pd.read_csv(path, usecols=["user_id", "first_submission_at"])
    history_df["user_id"] = history_df["user_id"].astype(str)
    history_df["first_submission_at"] = pd.to_datetime(
        history_df["first_submission_at"],
        errors="coerce",
        utc=True,
    )
    history_df = history_df.dropna(subset=["user_id", "first_submission_at"])
    return {
        str(row["user_id"]): pd.Timestamp(row["first_submission_at"])
        for row in history_df.to_dict("records")
    }


def compute_event_strength_at_cutoffs(
    applicants: list[dict[str, Any]],
    invites: list[dict[str, Any]],
    *,
    cutoffs: dict[str, Any],
    first_submission_map: dict[str, pd.Timestamp] | None = None,
) -> dict[str, float]:
    first_submission_map = first_submission_map or {}

    applicant_rows: list[tuple[str, pd.Timestamp, bool, bool]] = []
    for applicant in applicants:
        applied_at = _to_timestamp(applicant.get("applied_at"))
        if applied_at is None:
            continue
        user_id = str(applicant.get("user_id") or "")
        tracked_application = bool(applicant.get("tracked_application"))
        first_submission_at = first_submission_map.get(user_id)
        repeat_builder = bool(first_submission_at is not None and first_submission_at < applied_at)
        applicant_rows.append((user_id, applied_at, tracked_application, repeat_builder))

    invite_rows: list[tuple[pd.Timestamp, str | None]] = []
    first_invite_by_user: dict[str, pd.Timestamp] = {}
    for invite in invites:
        invited_at = _to_timestamp(invite.get("invited_at"))
        if invited_at is None:
            continue
        user_raw = str(invite.get("user_id") or "").strip()
        user_id = user_raw if user_raw else None
        invite_rows.append((invited_at, user_id))
        if user_id is not None:
            first_seen = first_invite_by_user.get(user_id)
            if first_seen is None or invited_at < first_seen:
                first_invite_by_user[user_id] = invited_at

    results: dict[str, float] = {}
    normalized_cutoffs = {
        label: cutoff_ts
        for label, cutoff in cutoffs.items()
        if (cutoff_ts := _to_timestamp(cutoff)) is not None
    }

    for label, cutoff in normalized_cutoffs.items():
        invited_count = 0
        linked_invited_users: set[str] = set()
        linked_invited_applicant_count = 0
        tracked_applied_count = 0
        repeat_builder_applied_count = 0

        for invited_at, invited_user_id in invite_rows:
            if invited_at <= cutoff:
                invited_count += 1
                if invited_user_id is not None:
                    linked_invited_users.add(invited_user_id)

        for user_id, applied_at, tracked_application, repeat_builder in applicant_rows:
            if applied_at > cutoff:
                continue
            if tracked_application:
                tracked_applied_count += 1
            if repeat_builder:
                repeat_builder_applied_count += 1
            first_invited_at = first_invite_by_user.get(user_id)
            if first_invited_at is not None and first_invited_at <= applied_at and first_invited_at <= cutoff:
                linked_invited_applicant_count += 1

        results[f"invited_{label}"] = float(invited_count)
        results[f"linked_invited_{label}"] = float(len(linked_invited_users))
        results[f"linked_invited_applicant_{label}"] = float(linked_invited_applicant_count)
        results[f"tracked_applied_{label}"] = float(tracked_applied_count)
        results[f"repeat_builder_applied_{label}"] = float(repeat_builder_applied_count)

    return results
