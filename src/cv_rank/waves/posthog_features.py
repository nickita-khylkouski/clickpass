from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.request import Request, urlopen

import pandas as pd


POSTHOG_STAGE0_EVENTS: dict[str, str] = {
    "pageview": "$pageview",
    "apply": "completion/apply",
    "register_click": "click/event-register-button",
    "registration": "event/registration",
    "guest_list": "event/view-guest-list",
    "add_to_calendar": "event/add-to-calendar",
}
POSTHOG_EVENT_VELOCITY_EVENTS: dict[str, str] = {
    **POSTHOG_STAGE0_EVENTS,
    "open_messages": "event/open-messages",
    "chat_send": "event/chat-send-message",
    "submission_view": "hackathon/view-submission",
}
POSTHOG_ATTENDANCE_EVENTS: dict[str, str] = {
    "open_messages": "event/open-messages",
    "guest_list": "event/view-guest-list",
    "chat_send": "event/chat-send-message",
    "add_to_calendar": "event/add-to-calendar",
}

POSTHOG_COUNT_HORIZONS = (21, 14, 7, 3, 1, 0)
POSTHOG_LIVE_LOOKBACKS = (14, 7, 4, 2)
EVENT_SLUG_EXPR = """
lower(
  case
    when match(toString(properties.eventId), '^[0-9a-fA-F-]{36}$')
      then extract(
        coalesce(
          toString(properties.url),
          toString(properties.eventUrl),
          toString(properties.$current_url),
          toString(properties.$pathname)
        ),
        '/e/([^/?]+)'
      )
    else coalesce(
      nullIf(toString(properties.eventId), ''),
      extract(
        coalesce(
          toString(properties.url),
          toString(properties.eventUrl),
          toString(properties.$current_url),
          toString(properties.$pathname)
        ),
        '/e/([^/?]+)'
      ),
      ''
    )
  end
)
""".strip()


@dataclass(frozen=True)
class PostHogConfig:
    api_host: str
    project_id: str
    api_key: str


def load_posthog_config() -> PostHogConfig | None:
    ingest_host = os.environ.get("POSTHOG_HOST", "").rstrip("/")
    api_host = os.environ.get("POSTHOG_API_HOST", "").rstrip("/")
    if not api_host and ingest_host == "https://us.i.posthog.com":
        api_host = "https://us.posthog.com"
    project_id = os.environ.get("POSTHOG_PROJECT_ID", "").strip()
    api_key = os.environ.get("POSTHOG_API_KEY", "").strip()
    if not api_host or not project_id or not api_key:
        return None
    return PostHogConfig(api_host=api_host, project_id=project_id, api_key=api_key)


def _run_hogql(query: str, *, name: str, config: PostHogConfig) -> dict[str, Any]:
    payload = json.dumps(
        {
            "query": {
                "kind": "HogQLQuery",
                "query": query,
            },
            "name": name,
        }
    ).encode()
    req = Request(
        f"{config.api_host}/api/projects/{config.project_id}/query/",
        data=payload,
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {config.api_key}")
    req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def _quoted_csv(values: Iterable[str]) -> str:
    escaped = [value.replace("\\", "\\\\").replace("'", "\\'") for value in values]
    return ", ".join(f"'{value}'" for value in escaped)


def _chunked(values: list[str], size: int) -> Iterable[list[str]]:
    for idx in range(0, len(values), size):
        yield values[idx : idx + size]


def fetch_hourly_stage0_counts(
    *,
    event_slugs: list[str],
    since: datetime,
    until: datetime | None = None,
    config: PostHogConfig | None = None,
    event_map: dict[str, str] | None = None,
    slug_chunk_size: int = 20,
) -> pd.DataFrame:
    if not event_slugs:
        return pd.DataFrame(columns=["event_name", "event_slug", "bucket_time", "count"])
    config = config or load_posthog_config()
    if config is None:
        raise ValueError("Missing PostHog configuration in environment")
    event_map = event_map or POSTHOG_STAGE0_EVENTS
    until_clause = (
        f"and timestamp < toDateTime('{until.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}')"
        if until is not None
        else ""
    )
    frames: list[pd.DataFrame] = []
    unique_slugs = sorted({str(value).strip().lower() for value in event_slugs if str(value).strip()})
    if not unique_slugs:
        return pd.DataFrame(columns=["event_name", "event_slug", "bucket_time", "count"])

    for signal_name, posthog_event in event_map.items():
        event_literal = _quoted_csv([posthog_event])
        for slug_chunk in _chunked(unique_slugs, slug_chunk_size):
            slug_list = _quoted_csv(slug_chunk)
            query = f"""
                select
                  {EVENT_SLUG_EXPR} as event_slug,
                  toStartOfHour(timestamp) as bucket_time,
                  count() as count
                from events
                where timestamp >= toDateTime('{since.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}')
                  {until_clause}
                  and event = {event_literal}
                group by event_slug, bucket_time
                having event_slug in ({slug_list})
                  and event_slug != ''
                order by event_slug, bucket_time
                limit 500000
            """
            result = _run_hogql(
                query,
                name=f"cv-rank-posthog-hourly-{signal_name}",
                config=config,
            )
            rows = result.get("results", [])
            if not rows:
                continue
            frame = pd.DataFrame(rows, columns=result["columns"])
            frame["event_name"] = posthog_event
            frame["bucket_time"] = pd.to_datetime(frame["bucket_time"], utc=True)
            frame["count"] = pd.to_numeric(frame["count"], errors="coerce").fillna(0.0)
            frame["event_slug"] = frame["event_slug"].fillna("").astype(str).str.lower()
            frames.append(frame[["event_name", "event_slug", "bucket_time", "count"]])

    if not frames:
        return pd.DataFrame(columns=["event_name", "event_slug", "bucket_time", "count"])
    return pd.concat(frames, ignore_index=True)


def build_event_velocity_features(
    events_df: pd.DataFrame,
    hourly_counts: pd.DataFrame,
    *,
    event_map: dict[str, str] | None = None,
) -> pd.DataFrame:
    if events_df.empty:
        return pd.DataFrame()
    event_map = event_map or POSTHOG_STAGE0_EVENTS

    grouped: dict[tuple[str, str], list[tuple[pd.Timestamp, float]]] = {}
    for row in hourly_counts.to_dict("records"):
        key = (str(row["event_slug"]), str(row["event_name"]))
        buckets = grouped.setdefault(key, [])
        bucket_time = pd.Timestamp(row["bucket_time"])
        count = float(row["count"])
        buckets.append((bucket_time, count))

    rows: list[dict[str, Any]] = []
    for event in events_df.to_dict("records"):
        slug = str(event.get("slug") or "").strip().lower()
        if not slug:
            continue
        event_start = pd.Timestamp(event["event_start"])
        if event_start.tzinfo is None:
            event_start = event_start.tz_localize("UTC")
        feature_row: dict[str, Any] = {
            "event_id": str(event["event_id"]),
            "slug": slug,
        }
        for signal_name, posthog_event in event_map.items():
            buckets = sorted(grouped.get((slug, posthog_event), []), key=lambda item: item[0])
            for horizon in POSTHOG_COUNT_HORIZONS:
                cutoff = event_start if horizon == 0 else event_start - pd.Timedelta(days=horizon)
                value = sum(count for ts, count in buckets if ts <= cutoff)
                feature_row[f"ph_{signal_name}_t{horizon}"] = float(value)
        rows.append(feature_row)
    return pd.DataFrame(rows)


def build_live_stage0_features(
    *,
    event_slugs: list[str],
    reference_time: datetime | None = None,
    config: PostHogConfig | None = None,
) -> pd.DataFrame:
    if not event_slugs:
        return pd.DataFrame()
    reference_time = reference_time or datetime.now(timezone.utc)
    since = reference_time - timedelta(days=45)
    hourly_counts = fetch_hourly_stage0_counts(
        event_slugs=event_slugs,
        since=since,
        until=reference_time,
        config=config,
    )
    grouped: dict[tuple[str, str], list[tuple[pd.Timestamp, float]]] = {}
    for row in hourly_counts.to_dict("records"):
        key = (str(row["event_slug"]), str(row["event_name"]))
        buckets = grouped.setdefault(key, [])
        bucket_time = pd.Timestamp(row["bucket_time"])
        buckets.append((bucket_time, float(row["count"])))

    rows: list[dict[str, Any]] = []
    for slug in event_slugs:
        slug_value = str(slug).strip().lower()
        feature_row: dict[str, Any] = {"slug": slug_value}
        for signal_name, posthog_event in POSTHOG_STAGE0_EVENTS.items():
            buckets = sorted(grouped.get((slug_value, posthog_event), []), key=lambda item: item[0])
            feature_row[f"ph_{signal_name}_now"] = float(sum(count for _, count in buckets))
            for lookback in POSTHOG_LIVE_LOOKBACKS:
                cutoff = reference_time - timedelta(days=lookback)
                feature_row[f"ph_{signal_name}_ago_{lookback}d"] = float(
                    sum(count for ts, count in buckets if ts <= cutoff)
                )
        rows.append(feature_row)
    return pd.DataFrame(rows)


def fetch_person_attendance_events(
    *,
    event_slugs: list[str],
    user_ids: list[str],
    since: datetime,
    until: datetime | None = None,
    config: PostHogConfig | None = None,
) -> pd.DataFrame:
    if not event_slugs or not user_ids:
        return pd.DataFrame(columns=["event_name", "event_slug", "user_id", "timestamp"])
    config = config or load_posthog_config()
    if config is None:
        return pd.DataFrame(columns=["event_name", "event_slug", "user_id", "timestamp"])

    event_list = _quoted_csv(POSTHOG_ATTENDANCE_EVENTS.values())
    until_clause = (
        f"and timestamp < toDateTime('{until.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}')"
        if until is not None
        else ""
    )
    frames: list[pd.DataFrame] = []
    unique_slugs = sorted({str(value).strip().lower() for value in event_slugs if str(value).strip()})
    unique_users = sorted({str(value).strip() for value in user_ids if str(value).strip()})
    for slug_chunk in _chunked(unique_slugs, 50):
        slug_list = _quoted_csv(slug_chunk)
        for user_chunk in _chunked(unique_users, 500):
            user_list = _quoted_csv(user_chunk)
            query = f"""
                select event_name, event_slug, user_id, timestamp
                from (
                    select
                      event as event_name,
                      {EVENT_SLUG_EXPR} as event_slug,
                      nullIf(toString(properties.$user_id), '') as user_id,
                      timestamp
                    from events
                    where timestamp >= toDateTime('{since.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}')
                      {until_clause}
                      and event in ({event_list})
                )
                where event_slug in ({slug_list})
                  and user_id in ({user_list})
                order by event_name, event_slug, user_id, timestamp
                limit 500000
            """
            result = _run_hogql(query, name="cv-rank-posthog-attendance-events", config=config)
            rows = result.get("results", [])
            if not rows:
                continue
            frame = pd.DataFrame(rows, columns=result["columns"])
            frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, format="mixed")
            frame["event_slug"] = frame["event_slug"].fillna("").astype(str).str.lower()
            frame["user_id"] = frame["user_id"].fillna("").astype(str)
            frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=["event_name", "event_slug", "user_id", "timestamp"])
    return pd.concat(frames, ignore_index=True)


def build_person_attendance_features(
    snapshot_df: pd.DataFrame,
    *,
    horizon_days: int,
    config: PostHogConfig | None = None,
) -> pd.DataFrame:
    feature_names = (
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
    )
    if snapshot_df.empty:
        return pd.DataFrame(columns=["user_id", "event_id", *feature_names])

    def _zero_feature_rows() -> pd.DataFrame:
        rows = [
            {"user_id": str(row["user_id"]), "event_id": str(row["event_id"]), **{name: 0.0 for name in feature_names}}
            for row in snapshot_df.to_dict("records")
        ]
        return pd.DataFrame(rows)

    config = config or load_posthog_config()
    if config is None or "event_slug" not in snapshot_df.columns:
        return _zero_feature_rows()

    event_slugs = snapshot_df["event_slug"].dropna().astype(str).str.strip().str.lower().tolist()
    user_ids = snapshot_df["user_id"].dropna().astype(str).str.strip().tolist()
    event_starts = pd.to_datetime(snapshot_df["event_start"], utc=True, errors="coerce")
    if event_starts.empty:
        return _zero_feature_rows()
    since = (event_starts.min() - pd.Timedelta(days=60)).to_pydatetime()
    until = event_starts.max().to_pydatetime()
    try:
        raw = fetch_person_attendance_events(
            event_slugs=event_slugs,
            user_ids=user_ids,
            since=since,
            until=until,
            config=config,
        )
    except Exception:
        return _zero_feature_rows()
    grouped: dict[tuple[str, str, str], list[pd.Timestamp]] = {}
    for row in raw.to_dict("records"):
        key = (str(row["user_id"]), str(row["event_slug"]), str(row["event_name"]))
        grouped.setdefault(key, []).append(pd.Timestamp(row["timestamp"]))

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
            timestamps = sorted(grouped.get((user_id, event_slug, event_name), []))
            total = float(sum(1 for ts in timestamps if ts <= snapshot_time))
            recent = float(sum(1 for ts in timestamps if snapshot_time - pd.Timedelta(days=7) <= ts <= snapshot_time))
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
