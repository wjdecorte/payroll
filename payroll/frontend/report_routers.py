"""
Frontend pages for the financial reports.

Full-page routes (not in the OpenAPI schema), all GET-only:
  GET /reports                   -> index of the six reports
  GET /reports/profit-and-loss   -> ``preset``/``start``/``end``, ``basis``, ``compare``
  GET /reports/balance-sheet     -> ``asOf``, ``basis``, ``compare``
  GET /reports/trial-balance     -> ``asOf``
  GET /reports/general-ledger    -> ``preset``/``start``/``end``, ``account``
  GET /reports/journal           -> ``preset``/``start``/``end``

Every route needs a signed-in user (router-level ``require_reader``). The picker is
a plain GET form, so the pages work without JavaScript. Query parameters are strings
and are parsed defensively: a bad preset, basis, comparison, date or account id
re-renders the page (picker included) with an inline alert and status 422, never a
500. Account rows link to the account register for the same window.
"""

from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from payroll.auth.permissions import require_reader
from payroll.common.dependencies import get_session
from payroll.exceptions import AppBaseError
from payroll.frontend.ledger_routers import _label, _parse_date
from payroll.frontend.routers import templates
from payroll.ledger.accounts import AccountService
from payroll.reports.basis import DEFAULT_BASIS, Basis
from payroll.reports.periods import (
    PRESETS,
    InvalidPeriodError,
    Period,
    company_today,
    resolve_period,
)
from payroll.reports.schemas import Invariant
from payroll.reports.statements import COMPARISONS, ReportService, fiscal_year_start

router = APIRouter(include_in_schema=False, dependencies=[Depends(require_reader)])

DEFAULT_PRESET = "this_month"
MAX_ACCOUNT_ID = 2**31 - 1  # integer primary key range
PRESET_LABELS = {
    "this_month": "This month",
    "last_month": "Last month",
    "this_quarter": "This quarter",
    "last_quarter": "Last quarter",
    "this_year": "This year",
    "last_year": "Last year",
    "ytd": "Year to date",
    "trailing_12": "Trailing 12 months",
    "custom": "Custom range",
}
COMPARE_LABELS = {
    "": "No comparison",
    "previous_period": "Previous period",
    "previous_year": "Previous year",
}
REPORTS = (
    (
        "Profit and Loss",
        "/reports/profit-and-loss",
        "Income, cost of goods sold and expenses for a period, with optional comparison.",
    ),
    (
        "Balance Sheet",
        "/reports/balance-sheet",
        "Assets, liabilities and equity as of a date, with optional comparison.",
    ),
    (
        "Trial Balance",
        "/reports/trial-balance",
        "Every account's debit or credit balance as of a date; debits must equal credits.",
    ),
    (
        "General Ledger",
        "/reports/general-ledger",
        "Each account's activity and running balance for a period.",
    ),
    (
        "Journal",
        "/reports/journal",
        "Every journal entry dated in a period, with all of its lines.",
    ),
)


@dataclass
class Picker:
    """What the shared picker form shows for one report."""

    action: str
    preset: str = ""
    start: str = ""
    end: str = ""
    as_of: str = ""
    basis: str = DEFAULT_BASIS.value
    compare: str = ""
    account: str = ""
    period: bool = False
    date: bool = False
    basis_toggle: bool = False
    comparison: bool = False
    accounts: bool = False


def _choices(session: Session) -> list[dict]:
    return [
        {"id": str(n.account.id), "label": _label(n.account, n.full_name)}
        for n in AccountService(session).tree(include_inactive=True)
    ]


def _page(
    request: Request,
    template: str,
    title: str,
    picker: Picker,
    session: Session,
    *,
    error: AppBaseError | None = None,
    **context: object,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        template,
        {
            "title": title,
            "picker": picker,
            "presets": [(p, PRESET_LABELS[p]) for p in (*PRESETS, "custom")],
            "compares": list(COMPARE_LABELS.items()),
            "bases": [(b.value, b.value.capitalize()) for b in Basis],
            "accounts": _choices(session) if picker.accounts else [],
            "error": error.message if error else None,
            **context,
        },
        status_code=error.http_code if error else 200,
    )


def _period(preset: str, start: str, end: str) -> Period:
    preset = preset or DEFAULT_PRESET
    if preset != "custom" and preset not in PRESETS:
        raise InvalidPeriodError(message="Choose a period from the list")
    return resolve_period(
        preset,
        company_today(),
        start=_parse_date(start, "Start"),
        end=_parse_date(end, "End"),
    )


def _basis(raw: str) -> Basis:
    try:
        return Basis(raw or DEFAULT_BASIS.value)
    except ValueError as exc:
        raise InvalidPeriodError(message="Choose cash or accrual basis") from exc


def _compare(raw: str) -> str | None:
    if raw and raw not in COMPARISONS:
        raise InvalidPeriodError(message="Choose a comparison from the list")
    return raw or None


def _account_id(raw: str) -> int | None:
    if not raw:
        return None
    if len(raw) > 10 or not (raw.isascii() and raw.isdigit()) or int(raw) > MAX_ACCOUNT_ID:
        raise InvalidPeriodError(message="Choose a valid account")
    return int(raw)


@router.get("/reports", response_class=HTMLResponse)
async def reports_index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "reports/index.html", {"reports": REPORTS})


@router.get("/reports/profit-and-loss", response_class=HTMLResponse)
async def profit_and_loss(
    request: Request,
    preset: str = "",
    start: str = "",
    end: str = "",
    basis: str = "",
    compare: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    picker = Picker(
        "/reports/profit-and-loss",
        preset=preset or DEFAULT_PRESET,
        start=start,
        end=end,
        basis=basis or DEFAULT_BASIS.value,
        compare=compare,
        period=True,
        basis_toggle=True,
        comparison=True,
    )
    try:
        statement = ReportService(session).profit_and_loss(
            _period(preset, start, end), basis=_basis(basis), compare=_compare(compare)
        )
    except AppBaseError as exc:
        return _page(
            request, "reports/statement.html", "Profit and Loss", picker, session, error=exc
        )
    return _page(
        request, "reports/statement.html", "Profit and Loss", picker, session, statement=statement
    )


@router.get("/reports/balance-sheet", response_class=HTMLResponse)
async def balance_sheet(
    request: Request,
    asOf: str = "",  # noqa: N803  (query parameter name)
    basis: str = "",
    compare: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    picker = Picker(
        "/reports/balance-sheet",
        as_of=asOf,
        basis=basis or DEFAULT_BASIS.value,
        compare=compare,
        date=True,
        basis_toggle=True,
        comparison=True,
    )
    try:
        statement = ReportService(session).balance_sheet(
            _parse_date(asOf, "As-of date") or company_today(),
            basis=_basis(basis),
            compare=_compare(compare),
        )
    except AppBaseError as exc:
        return _page(request, "reports/statement.html", "Balance Sheet", picker, session, error=exc)
    return _page(
        request, "reports/statement.html", "Balance Sheet", picker, session, statement=statement
    )


@router.get("/reports/trial-balance", response_class=HTMLResponse)
async def trial_balance(
    request: Request,
    asOf: str = "",  # noqa: N803  (query parameter name)
    session: Session = Depends(get_session),
) -> HTMLResponse:
    picker = Picker("/reports/trial-balance", as_of=asOf, date=True)
    try:
        trial = ReportService(session).trial_balance(
            _parse_date(asOf, "As-of date") or company_today()
        )
    except AppBaseError as exc:
        return _page(
            request,
            "reports/trial_balance.html",
            "Trial Balance",
            picker,
            session,
            error=exc,
        )
    invariants = [
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
    return _page(
        request,
        "reports/trial_balance.html",
        "Trial Balance",
        picker,
        session,
        trial=trial,
        invariants=invariants,
        year_start=fiscal_year_start(trial.as_of),
    )


@router.get("/reports/general-ledger", response_class=HTMLResponse)
async def general_ledger(
    request: Request,
    preset: str = "",
    start: str = "",
    end: str = "",
    account: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    picker = Picker(
        "/reports/general-ledger",
        preset=preset or DEFAULT_PRESET,
        start=start,
        end=end,
        account=account,
        period=True,
        accounts=True,
    )
    try:
        period = _period(preset, start, end)
        ledger = ReportService(session).general_ledger(
            period.start, period.end, _account_id(account)
        )
    except AppBaseError as exc:
        return _page(
            request,
            "reports/general_ledger.html",
            "General Ledger",
            picker,
            session,
            error=exc,
        )
    return _page(
        request, "reports/general_ledger.html", "General Ledger", picker, session, ledger=ledger
    )


@router.get("/reports/journal", response_class=HTMLResponse)
async def journal_report(
    request: Request,
    preset: str = "",
    start: str = "",
    end: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    picker = Picker(
        "/reports/journal",
        preset=preset or DEFAULT_PRESET,
        start=start,
        end=end,
        period=True,
    )
    try:
        period = _period(preset, start, end)
        entries = ReportService(session).journal(period.start, period.end)
    except AppBaseError as exc:
        return _page(request, "reports/journal.html", "Journal", picker, session, error=exc)
    return _page(
        request,
        "reports/journal.html",
        "Journal",
        picker,
        session,
        entries=entries,
        period=period,
    )
