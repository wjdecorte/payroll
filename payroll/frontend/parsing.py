"""Strict parsing of form and path text, shared by the page routers.

Everything is turned into its Python type before a service sees it. A bad value raises
``error`` (default ``InvalidExpenseError``, a 422) so the page can show it as a message.
"""

import re
from datetime import date

from payroll.common.dates import is_supported_date, supported_range_text
from payroll.exceptions import AppBaseError
from payroll.expenses.exceptions import InvalidExpenseError

MAX_ID = 2_147_483_647  # the largest value of an integer column

_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _parse_id(
    raw: object, label: str, error: type[AppBaseError] = InvalidExpenseError
) -> int | None:
    """A positive database id from text; blank -> None; anything else -> a 422."""
    text = _text(raw)
    if not text:
        return None
    if not (text.isascii() and text.isdigit() and len(text) <= 10):
        raise error(message=f"{label} is not valid")
    value = int(text)
    if not 1 <= value <= MAX_ID:
        raise error(message=f"{label} is not valid")
    return value


def _path_id(
    raw: str, label: str = "The id", error: type[AppBaseError] = InvalidExpenseError
) -> int:
    value = _parse_id(raw, label, error)
    if value is None:
        raise error(message=f"{label} is not valid")
    return value


def _parse_date(
    raw: object, label: str, error: type[AppBaseError] = InvalidExpenseError
) -> date | None:
    """A strict ``YYYY-MM-DD`` calendar date (never a datetime); blank -> None."""
    text = _text(raw)
    if not text:
        return None
    if not _ISO_DATE.fullmatch(text):
        raise error(message=f"{label} must be a date like 2026-03-05")
    try:
        value = date.fromisoformat(text)
    except ValueError as exc:
        raise error(message=f"{label} is not a valid date") from exc
    if not is_supported_date(value):
        raise error(message=f"{label} must be within {supported_range_text()}")
    return value
