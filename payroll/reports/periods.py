import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from payroll import app_settings
from payroll.common.dates import (
    is_supported_date,
    supported_range_text,
)
from payroll.exceptions import AppBaseError

PRESETS: tuple[str, ...] = (
    "this_month",
    "last_month",
    "this_quarter",
    "last_quarter",
    "this_year",
    "last_year",
    "ytd",
    "trailing_12",
)


@dataclass
class InvalidPeriodError(AppBaseError):
    message: str = "Invalid reporting period"
    http_code: int = 422


def ensure_supported_date(value: date) -> date:
    """``value`` if it lies in the supported range, else :class:`InvalidPeriodError`."""
    if not is_supported_date(value):
        raise InvalidPeriodError(
            message=f"{value.isoformat()} is outside the supported dates ({supported_range_text()})"
        )
    return value


@dataclass(frozen=True)
class Period:
    start: date
    end: date
    label: str


def company_today(now: datetime | None = None) -> date:
    """Today's date in the company timezone; `now` is an aware instant (default: current time)."""
    instant = now if now is not None else datetime.now(UTC)
    return instant.astimezone(ZoneInfo(app_settings.company_timezone)).date()


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _shift_months(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _month(year: int, month: int, label: str) -> Period:
    return Period(date(year, month, 1), _month_end(year, month), label)


def _quarter(year: int, month: int, label: str) -> Period:
    first = (month - 1) // 3 * 3 + 1
    return Period(date(year, first, 1), _month_end(year, first + 2), label)


def _shift_year(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:  # Feb 29 -> Feb 28
        return value.replace(year=value.year + years, day=28)


def _resolve(
    preset: str,
    today: date | None = None,
    *,
    start: date | None = None,
    end: date | None = None,
) -> Period:
    if preset == "custom":
        if start is None or end is None:
            raise InvalidPeriodError("A custom period requires both start and end dates")
        ensure_supported_date(start)
        ensure_supported_date(end)
        if start > end:
            raise InvalidPeriodError("Period start must not be after its end")
        return Period(start, end, f"{start.isoformat()} to {end.isoformat()}")
    if preset not in PRESETS:
        raise InvalidPeriodError(f"Unknown period preset '{preset}'")
    today = today or company_today()
    if preset == "this_month":
        return _month(today.year, today.month, "This month")
    if preset == "last_month":
        year, month = _shift_months(today.year, today.month, -1)
        return _month(year, month, "Last month")
    if preset == "this_quarter":
        return _quarter(today.year, today.month, "This quarter")
    if preset == "last_quarter":
        year, month = _shift_months(today.year, today.month, -3)
        return _quarter(year, month, "Last quarter")
    if preset == "this_year":
        return Period(date(today.year, 1, 1), date(today.year, 12, 31), "This year")
    if preset == "last_year":
        return Period(date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), "Last year")
    if preset == "ytd":
        return Period(date(today.year, 1, 1), today, "Year to date")
    return Period(_shift_year(today, -1) + timedelta(days=1), today, "Trailing 12 months")


def resolve_period(
    preset: str,
    today: date | None = None,
    *,
    start: date | None = None,
    end: date | None = None,
) -> Period:
    """The period for ``preset``; every date in it lies in the supported range."""
    try:
        period = _resolve(preset, today, start=start, end=end)
    except (ValueError, OverflowError) as exc:  # date arithmetic ran off the calendar
        raise InvalidPeriodError(message="The period is outside the supported dates") from exc
    ensure_supported_date(period.start)
    ensure_supported_date(period.end)
    return period


def previous_period(period: Period) -> Period:
    """The comparison period before ``period``; :class:`InvalidPeriodError` if out of range."""
    try:
        return _supported(_previous_period(period))
    except (ValueError, OverflowError) as exc:
        raise InvalidPeriodError(
            message="The comparison period is outside the supported dates"
        ) from exc


def _supported(period: Period) -> Period:
    ensure_supported_date(period.start)
    ensure_supported_date(period.end)
    return period


def _previous_period(period: Period) -> Period:
    start, end = period.start, period.end
    if start.day == 1 and end == _month_end(end.year, end.month):
        months = (end.year - start.year) * 12 + end.month - start.month + 1
        if months in (1, 3, 12) and (months == 1 or (start.month - 1) % months == 0):
            year, month = _shift_months(start.year, start.month, -months)
            prior_end = _month_end(*_shift_months(year, month, months - 1))
            return Period(date(year, month, 1), prior_end, f"Previous {period.label}")
    days = (end - start).days + 1
    prior_end = start - timedelta(days=1)
    return Period(prior_end - timedelta(days=days - 1), prior_end, f"Previous {period.label}")


def previous_year(period: Period) -> Period:
    """The same dates one year earlier; :class:`InvalidPeriodError` if out of range."""
    try:
        return _supported(
            Period(
                _shift_year(period.start, -1),
                _shift_year(period.end, -1),
                f"{period.label} (prior year)",
            )
        )
    except (ValueError, OverflowError) as exc:
        raise InvalidPeriodError(
            message="The comparison period is outside the supported dates"
        ) from exc
