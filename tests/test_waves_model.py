from __future__ import annotations

import pytest

from cv_rank.waves.model import (
    FUTURE_APPROVED_SHOW_RATES,
    expected_future_attendance,
    forecast_signup_count,
    future_approved_show_rate,
)


def test_future_approved_show_rate_uses_horizon_buckets() -> None:
    assert future_approved_show_rate(7.0) == pytest.approx(FUTURE_APPROVED_SHOW_RATES["t7"])
    assert future_approved_show_rate(3.0) == pytest.approx(FUTURE_APPROVED_SHOW_RATES["t3"])
    assert future_approved_show_rate(1.0) == pytest.approx(FUTURE_APPROVED_SHOW_RATES["t1"])


def test_expected_future_attendance_is_non_negative() -> None:
    assert expected_future_attendance(-5, 7.0) == 0.0
    assert expected_future_attendance(100, 7.0) == pytest.approx(100 * FUTURE_APPROVED_SHOW_RATES["t7"])


def test_forecast_signup_count_never_drops_below_current() -> None:
    forecasted = forecast_signup_count(
        current_approved=120,
        horizon_days=7.0,
        approved_t14=110,
        approved_t21=100,
    )
    assert forecasted >= 120
