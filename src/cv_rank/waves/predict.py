"""Per-person attendance prediction helpers for CV hackathons.

Bucket functions for categorizing applicant features (timing, prior rate,
timezone distance) and timezone utilities for computing geographic distance
between applicant and event locations.

All empirical constants derived from 21 in-person CV hackathons (7k+ approved applicants)
analyzed from the platform production database.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Empirical constants (platform DB, 21 hackathons, 6,954 approved applicants)
# ---------------------------------------------------------------------------

# Show rates by application timing (days before event)
# Later applicants show up MORE — opposite of naive assumption.
SHOW_RATES_BY_TIMING: dict[str, dict[str, Any]] = {
    "21+":   {"min_days": 21, "max_days": 999, "show_rate": 0.32, "n": 700},
    "14-21": {"min_days": 14, "max_days": 21,  "show_rate": 0.40, "n": 900},
    "7-14":  {"min_days": 7,  "max_days": 14,  "show_rate": 0.39, "n": 1500},
    "3-7":   {"min_days": 3,  "max_days": 7,   "show_rate": 0.46, "n": 1800},
    "1-3":   {"min_days": 1,  "max_days": 3,   "show_rate": 0.53, "n": 1200},
    "<1":    {"min_days": 0,  "max_days": 1,    "show_rate": 0.58, "n": 850},
}
OVERALL_SHOW_RATE = 0.449  # weighted average across all buckets

# Show rates by prior personal attendance rate.
PRIOR_RATE_MULTIPLIERS: dict[str, float] = {
    "75-100%":    1.20,   # 54.0% show rate
    "50-75%":     1.00,   # ~45%
    "25-50%":     0.89,   # ~40%
    "0-25%":      0.69,   # 31.1%
    "no_history": 1.00,   # 44.9% (use overall average)
}

TZ_DISTANCE_MULTIPLIERS: dict[str, float] = {
    "local":  1.00,   # 44.8% empirical
    "near":   0.57,   # 25.6%
    "medium": 0.50,   # 22.2%
    "far":    0.31,   # 14.1%
}


# ---------------------------------------------------------------------------
# Bucket functions
# ---------------------------------------------------------------------------

def timing_bucket(days_before_event: float) -> str:
    """Map days-before-event to a timing bucket key."""
    if days_before_event < 0:
        days_before_event = 0.0  # applied after event started; treat as day-of
    if days_before_event >= 21:
        return "21+"
    elif days_before_event >= 14:
        return "14-21"
    elif days_before_event >= 7:
        return "7-14"
    elif days_before_event >= 3:
        return "3-7"
    elif days_before_event >= 1:
        return "1-3"
    else:
        return "<1"


def prior_rate_bucket(rate: float | None) -> str:
    """Map a personal prior show rate to a bucket key."""
    if rate is None:
        return "no_history"
    rate = max(0.0, min(1.0, rate))  # clamp to [0, 1]
    if rate >= 0.75:
        return "75-100%"
    elif rate >= 0.50:
        return "50-75%"
    elif rate >= 0.25:
        return "25-50%"
    else:
        return "0-25%"


def prior_event_count_bucket(count: int) -> str:
    """Map number of prior events to a bucket key."""
    if count <= 0:
        return "none"
    if count == 1:
        return "1"
    if count <= 3:
        return "2-3"
    return "4+"


def page_views_bucket(count: int) -> str:
    """Map event page view count to a display bucket."""
    if count <= 0:
        return "0"
    if count == 1:
        return "1"
    if count <= 3:
        return "2-3"
    if count <= 5:
        return "4-5"
    if count <= 10:
        return "6-10"
    return "11+"


def signup_percentile_bucket(pct_rank: float) -> str:
    """Map signup percentile rank (0-1) to a cohort bucket."""
    if pct_rank < 0.10:
        return "first_10"
    if pct_rank < 0.25:
        return "10-25"
    if pct_rank < 0.50:
        return "25-50"
    if pct_rank < 0.75:
        return "50-75"
    if pct_rank < 0.90:
        return "75-90"
    return "last_10"


# ---------------------------------------------------------------------------
# Timezone distance helpers
# ---------------------------------------------------------------------------

CITY_TIMEZONES: dict[str, str] = {
    "san francisco": "America/Los_Angeles",
    "sf": "America/Los_Angeles",
    "new york": "America/New_York",
    "nyc": "America/New_York",
    "los angeles": "America/Los_Angeles",
    "austin": "America/Chicago",
    "seattle": "America/Los_Angeles",
    "singapore": "Asia/Singapore",
    "london": "Europe/London",
    "paris": "Europe/Paris",
    "bengaluru": "Asia/Kolkata",
    "bangalore": "Asia/Kolkata",
}

# Fallback: match keywords in event title for non-English city fields
TITLE_TZ_HINTS: dict[str, str] = {
    "서울": "Asia/Seoul",
    "seoul": "Asia/Seoul",
    "東京": "Asia/Tokyo",
    "tokyo": "Asia/Tokyo",
    "bengaluru": "Asia/Kolkata",
    "london": "Europe/London",
    "paris": "Europe/Paris",
    "singapore": "Asia/Singapore",
}


def city_to_timezone(city: str | None, event_title: str = "") -> str:
    """Map an event city (or title) to an IANA timezone string."""
    import re

    if city:
        key = city.lower().split(",")[0].strip()
        if key in CITY_TIMEZONES:
            return CITY_TIMEZONES[key]
        # word-boundary match: "san francisco, ca" → "san francisco"
        # avoids false positives like "parish" → "paris"
        for name, tz in CITY_TIMEZONES.items():
            if re.search(r"\b" + re.escape(name) + r"\b", key):
                return tz

    # title-based fallback for non-English cities
    title_lower = event_title.lower()
    for hint, tz in TITLE_TZ_HINTS.items():
        # non-ASCII hints (Korean/Japanese) are unique enough for substring match;
        # ASCII hints use word boundaries to avoid false positives
        if hint.isascii():
            if re.search(r"\b" + re.escape(hint) + r"\b", title_lower):
                return tz
        elif hint in title_lower:
            return tz

    return "America/Los_Angeles"  # default (most CV events are in SF)


def compute_tz_offset_diff(applicant_tz: str, event_tz: str, event_date=None) -> float:
    """Compute absolute UTC offset difference in hours between two timezones.

    Uses event_date for DST-accurate offsets when provided, otherwise uses
    today's date.
    """
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo

        ref = event_date if event_date is not None else _dt.now()
        if not isinstance(ref, _dt):
            ref = _dt(ref.year, ref.month, ref.day, 12, 0)
        app_offset = ref.replace(tzinfo=ZoneInfo(applicant_tz)).utcoffset()
        evt_offset = ref.replace(tzinfo=ZoneInfo(event_tz)).utcoffset()
        if app_offset is None or evt_offset is None:
            return 5.0  # unknown → assume "medium" distance, not local
        return abs((app_offset - evt_offset).total_seconds()) / 3600
    except Exception:
        return 5.0  # unknown → assume "medium" distance, not local


def tz_distance_bucket(offset_hours: float) -> str:
    """Bucket a timezone offset difference into distance categories."""
    if offset_hours <= 1:
        return "local"
    elif offset_hours <= 3:
        return "near"
    elif offset_hours <= 8:
        return "medium"
    else:
        return "far"
