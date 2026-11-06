"""
Report runs: query-string parameters -> validated parameters -> a report.

The report pages and the CSV / PDF exports (page URLs and API twins) all go through
these functions, so an exported file always holds exactly what the page shows and
bad input is refused identically. Parameters arrive as raw strings (the picker is a
plain GET form) and are parsed defensively: anything invalid raises
:class:`~payroll.reports.periods.InvalidPeriodError` (HTTP 422), never a 500.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date

from sqlmodel import Session

from payroll.expenses.reports import OwnerReport, owner_reimbursement_report
from payroll.receivables.balances import ARAging, ar_aging
from payroll.reports.basis import DEFAULT_BASIS, Basis
from payroll.reports.periods import (
    PRESETS,
    InvalidPeriodError,
    Period,
    company_today,
    ensure_supported_date,
    parse_date,
    resolve_period,
)
from payroll.reports.schemas import (
    GeneralLedger,
    Invariant,
    JournalReport,
    Statement,
    TrialBalance,
)
from payroll.reports.statements import COMPARISONS, ReportService

DEFAULT_PRESET = "this_month"
MAX_ACCOUNT_ID = 2**31 - 1  # integer primary key range

Report = Statement | TrialBalance | GeneralLedger | JournalReport | ARAging | OwnerReport
Query = Mapping[str, str]


@dataclass(frozen=True)
class ReportRun:
    """A computed report plus what identifies it: slug, title, validated parameters.

    ``stem`` is the download file name without extension. It is built only from the
    slug and validated dates, never from user text.
    """

    name: str
    title: str
    report: Report
    params: dict[str, object]
    stem: str
    period: Period | None = None


def parse_period(preset: str, start: str, end: str) -> Period:
    preset = preset or DEFAULT_PRESET
    if preset != "custom" and preset not in PRESETS:
        raise InvalidPeriodError(message="Choose a period from the list")
    return resolve_period(
        preset,
        company_today(),
        start=parse_date(start, "Start"),
        end=parse_date(end, "End"),
    )


def parse_basis(raw: str) -> Basis:
    try:
        return Basis(raw or DEFAULT_BASIS.value)
    except ValueError as exc:
        raise InvalidPeriodError(message="Choose cash or accrual basis") from exc


def parse_compare(raw: str) -> str | None:
    if raw and raw not in COMPARISONS:
        raise InvalidPeriodError(message="Choose a comparison from the list")
    return raw or None


def parse_account_id(raw: str) -> int | None:
    if not raw:
        return None
    if len(raw) > 10 or not (raw.isascii() and raw.isdigit()) or int(raw) > MAX_ACCOUNT_ID:
        raise InvalidPeriodError(message="Choose a valid account")
    return int(raw)


def trial_invariants(trial: TrialBalance) -> list[Invariant]:
    return [
        Invariant(
            name="Debits equal credits",
            ok=trial.balanced,
            detail=(
                "Total debits equal total credits"
                if trial.balanced
                else "Total debits and total credits differ"
            ),
        )
    ]


def _window(start: date, end: date) -> str:
    return f"{start.isoformat()}_to_{end.isoformat()}"


def run_profit_and_loss(session: Session, q: Query) -> ReportRun:
    period = parse_period(q.get("preset", ""), q.get("start", ""), q.get("end", ""))
    basis = parse_basis(q.get("basis", ""))
    compare = parse_compare(q.get("compare", ""))
    statement = ReportService(session).profit_and_loss(period, basis=basis, compare=compare)
    params = {
        "preset": q.get("preset", "") or DEFAULT_PRESET,
        "start": period.start,
        "end": period.end,
        "basis": basis.value,
        "compare": compare,
    }
    stem = f"profit-and-loss-{_window(period.start, period.end)}"
    return ReportRun("profit-and-loss", "Profit and Loss", statement, params, stem, period)


def run_balance_sheet(session: Session, q: Query) -> ReportRun:
    as_of = parse_date(q.get("asOf", ""), "As-of date") or company_today()
    basis = parse_basis(q.get("basis", ""))
    compare = parse_compare(q.get("compare", ""))
    statement = ReportService(session).balance_sheet(as_of, basis=basis, compare=compare)
    params = {"asOf": as_of, "basis": basis.value, "compare": compare}
    return ReportRun("balance-sheet", "Balance Sheet", statement, params, f"balance-sheet-{as_of}")


def run_trial_balance(session: Session, q: Query) -> ReportRun:
    as_of = parse_date(q.get("asOf", ""), "As-of date") or company_today()
    trial = ReportService(session).trial_balance(as_of)
    return ReportRun(
        "trial-balance", "Trial Balance", trial, {"asOf": as_of}, f"trial-balance-{as_of}"
    )


def run_general_ledger(session: Session, q: Query) -> ReportRun:
    period = parse_period(q.get("preset", ""), q.get("start", ""), q.get("end", ""))
    account_id = parse_account_id(q.get("account", ""))
    ledger = ReportService(session).general_ledger(period.start, period.end, account_id)
    params = {"start": period.start, "end": period.end, "account": account_id}
    stem = f"general-ledger-{_window(period.start, period.end)}"
    return ReportRun("general-ledger", "General Ledger", ledger, params, stem, period)


def run_journal(session: Session, q: Query) -> ReportRun:
    period = parse_period(q.get("preset", ""), q.get("start", ""), q.get("end", ""))
    entries = ReportService(session).journal(period.start, period.end)
    journal = JournalReport(start=period.start, end=period.end, entries=entries)
    params = {"start": period.start, "end": period.end}
    stem = f"journal-{_window(period.start, period.end)}"
    return ReportRun("journal", "Journal", journal, params, stem, period)


def run_ar_aging(session: Session, q: Query) -> ReportRun:
    as_of = parse_date(q.get("asOf", ""), "As-of date") or company_today()
    aging = ar_aging(session, as_of)
    return ReportRun("ar-aging", "A/R Aging", aging, {"asOf": as_of}, f"ar-aging-{as_of}")


def _supported_date(raw: str, label: str) -> date | None:
    value = parse_date(raw, label)
    return ensure_supported_date(value) if value is not None else None


def run_owner_reimbursement(session: Session, q: Query) -> ReportRun:
    """Owner reimbursement: ``as_of`` (default today), ``date_from`` / ``date_to`` (the year so far)."""
    today = company_today()
    as_of = _supported_date(q.get("as_of", ""), "As-of date") or today
    date_to = _supported_date(q.get("date_to", ""), "To date") or today
    date_from = _supported_date(q.get("date_from", ""), "From date") or date(date_to.year, 1, 1)
    if date_from > date_to:
        raise InvalidPeriodError(message="The From date cannot be after the To date")
    report = owner_reimbursement_report(session, as_of, date_from, date_to)
    params = {"as_of": as_of, "date_from": date_from, "date_to": date_to}
    return ReportRun(
        "owner-reimbursement",
        "Owner Reimbursement",
        report,
        params,
        f"owner-reimbursement-{as_of}",
    )


RUNNERS: dict[str, Callable[[Session, Query], ReportRun]] = {
    "profit-and-loss": run_profit_and_loss,
    "balance-sheet": run_balance_sheet,
    "trial-balance": run_trial_balance,
    "general-ledger": run_general_ledger,
    "journal": run_journal,
    "ar-aging": run_ar_aging,
}

# Page and download only: no JSON API twin (see payroll.reports.routers, which uses RUNNERS).
PAGE_RUNNERS: dict[str, Callable[[Session, Query], ReportRun]] = {
    **RUNNERS,
    "owner-reimbursement": run_owner_reimbursement,
}
