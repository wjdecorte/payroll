import base64
import os
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from payroll import Settings
from payroll.reports.periods import (
    PRESETS,
    InvalidPeriodError,
    Period,
    company_today,
    previous_period,
    previous_year,
    resolve_period,
)

TODAY = date(2026, 11, 2)


@pytest.mark.parametrize(
    ("preset", "start", "end"),
    [
        ("this_month", date(2026, 11, 1), date(2026, 11, 30)),
        ("last_month", date(2026, 10, 1), date(2026, 10, 31)),
        ("this_quarter", date(2026, 10, 1), date(2026, 12, 31)),
        ("last_quarter", date(2026, 7, 1), date(2026, 9, 30)),
        ("this_year", date(2026, 1, 1), date(2026, 12, 31)),
        ("last_year", date(2025, 1, 1), date(2025, 12, 31)),
        ("ytd", date(2026, 1, 1), date(2026, 11, 2)),
        ("trailing_12", date(2025, 11, 3), date(2026, 11, 2)),
    ],
)
def test_presets(preset, start, end):
    period = resolve_period(preset, TODAY)
    assert (period.start, period.end) == (start, end)
    assert period.label


def test_presets_constant_matches():
    assert PRESETS == (
        "this_month",
        "last_month",
        "this_quarter",
        "last_quarter",
        "this_year",
        "last_year",
        "ytd",
        "trailing_12",
    )


def test_january_wraps_to_prior_year():
    today = date(2026, 1, 15)
    last_month = resolve_period("last_month", today)
    assert (last_month.start, last_month.end) == (date(2025, 12, 1), date(2025, 12, 31))
    last_quarter = resolve_period("last_quarter", today)
    assert (last_quarter.start, last_quarter.end) == (date(2025, 10, 1), date(2025, 12, 31))


def test_leap_day():
    today = date(2028, 2, 29)
    assert resolve_period("trailing_12", today).start == date(2027, 3, 1)
    month = resolve_period("this_month", today)
    assert month.end == date(2028, 2, 29)
    prior = previous_year(month)
    assert (prior.start, prior.end) == (date(2027, 2, 1), date(2027, 2, 28))


def test_custom_requires_both_dates_and_order():
    period = resolve_period("custom", TODAY, start=date(2026, 3, 1), end=date(2026, 3, 5))
    assert (period.start, period.end) == (date(2026, 3, 1), date(2026, 3, 5))
    with pytest.raises(InvalidPeriodError):
        resolve_period("custom", TODAY, start=date(2026, 3, 1))
    with pytest.raises(InvalidPeriodError):
        resolve_period("custom", TODAY, end=date(2026, 3, 1))
    with pytest.raises(InvalidPeriodError):
        resolve_period("custom", TODAY, start=date(2026, 3, 5), end=date(2026, 3, 1))
    assert InvalidPeriodError.__dataclass_fields__["http_code"].default == 422


def test_previous_period_calendar_vs_arbitrary():
    q3 = resolve_period("custom", TODAY, start=date(2026, 7, 1), end=date(2026, 9, 30))
    q2 = previous_period(q3)
    assert (q2.start, q2.end) == (date(2026, 4, 1), date(2026, 6, 30))
    month = previous_period(resolve_period("this_month", TODAY))
    assert (month.start, month.end) == (date(2026, 10, 1), date(2026, 10, 31))
    year = previous_period(resolve_period("this_year", TODAY))
    assert (year.start, year.end) == (date(2025, 1, 1), date(2025, 12, 31))
    jan = previous_period(Period(date(2026, 1, 1), date(2026, 1, 31), "Jan"))
    assert (jan.start, jan.end) == (date(2025, 12, 1), date(2025, 12, 31))
    q1 = previous_period(Period(date(2026, 1, 1), date(2026, 3, 31), "Q1"))
    assert (q1.start, q1.end) == (date(2025, 10, 1), date(2025, 12, 31))
    odd = previous_period(Period(date(2026, 3, 10), date(2026, 3, 20), "odd"))
    assert (odd.start, odd.end) == (date(2026, 2, 27), date(2026, 3, 9))


def test_previous_year_shifts_both_ends():
    shifted = previous_year(Period(date(2028, 2, 29), date(2028, 2, 29), "leap"))
    assert (shifted.start, shifted.end) == (date(2027, 2, 28), date(2027, 2, 28))


def test_company_today_uses_company_timezone():
    instant = datetime(2026, 11, 3, 2, 0, tzinfo=UTC)
    assert company_today(instant) == date(2026, 11, 2)
    assert isinstance(company_today(), date)


def test_unknown_preset_rejected():
    with pytest.raises(InvalidPeriodError):
        resolve_period("next_decade", TODAY)


def test_invalid_timezone_setting_fails_fast():
    key = base64.b64encode(os.urandom(32)).decode()
    with pytest.raises(ValidationError):
        Settings(encryption_key=key, company_timezone="Mars/Olympus")
    assert Settings(encryption_key=key, company_timezone="UTC").company_timezone == "UTC"
