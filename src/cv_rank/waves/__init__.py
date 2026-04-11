"""Per-person show probability prediction for CV hackathons."""

from .predict import (
    city_to_timezone,
    compute_tz_offset_diff,
    tz_distance_bucket,
    page_views_bucket,
    prior_event_count_bucket,
    signup_percentile_bucket,
    SHOW_RATES_BY_TIMING,
    TZ_DISTANCE_MULTIPLIERS,
)
from .model import (
    expected_future_attendance,
    future_approved_show_rate,
    predict_show_probability,
    predict_batch,
    train_model,
)

__all__ = [
    "city_to_timezone",
    "compute_tz_offset_diff",
    "tz_distance_bucket",
    "page_views_bucket",
    "prior_event_count_bucket",
    "signup_percentile_bucket",
    "expected_future_attendance",
    "future_approved_show_rate",
    "predict_show_probability",
    "predict_batch",
    "train_model",
    "SHOW_RATES_BY_TIMING",
    "TZ_DISTANCE_MULTIPLIERS",
]
