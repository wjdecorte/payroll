"""The date range the books support (ledger entries and reports alike)."""

from datetime import date

MIN_SUPPORTED_DATE = date(1900, 1, 1)
MAX_SUPPORTED_DATE = date(2200, 12, 31)


def is_supported_date(value: date) -> bool:
    return MIN_SUPPORTED_DATE <= value <= MAX_SUPPORTED_DATE


def supported_range_text() -> str:
    return f"{MIN_SUPPORTED_DATE.isoformat()} to {MAX_SUPPORTED_DATE.isoformat()}"
