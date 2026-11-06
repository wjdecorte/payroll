"""
Frontend pages for the financial reports.

Full-page routes (not in the OpenAPI schema), all GET-only:
  GET /reports                   -> index of the reports
  GET /reports/profit-and-loss   -> ``preset``/``start``/``end``, ``basis``, ``compare``
  GET /reports/balance-sheet     -> ``asOf``, ``basis``, ``compare``
  GET /reports/trial-balance     -> ``asOf``
  GET /reports/general-ledger    -> ``preset``/``start``/``end``, ``account``
  GET /reports/journal           -> ``preset``/``start``/``end``
  GET /reports/ar-aging          -> ``asOf``
  GET /reports/owner-reimbursement -> ``as_of``, ``date_from``, ``date_to``
  GET /reports/<report>.csv|.pdf -> the same report as a download (same query string)

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
from payroll.frontend.ledger_routers import _label
from payroll.frontend.report_export import add_export_routes
from payroll.frontend.routers import templates
from payroll.ledger.accounts import AccountService
from payroll.reports.basis import DEFAULT_BASIS, Basis
from payroll.reports.periods import PRESETS
from payroll.reports.runs import (
    DEFAULT_PRESET,
    PAGE_RUNNERS,
    run_ar_aging,
    run_balance_sheet,
    run_general_ledger,
    run_journal,
    run_owner_reimbursement,
    run_profit_and_loss,
    run_trial_balance,
    trial_invariants,
)
from payroll.reports.statements import fiscal_year_start

router = APIRouter(include_in_schema=False, dependencies=[Depends(require_reader)])

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
    (
        "A/R Aging",
        "/reports/ar-aging",
        "What each customer owes by days past due, open invoices and credits, as of a date.",
    ),
    (
        "Owner Reimbursement",
        "/reports/owner-reimbursement",
        "What the company owes the owner, the open expenses you paid personally, and reimbursements.",
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
    owner: bool = False
    date_from: str = ""
    date_to: str = ""


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
            "export_query": request.url.query,
            **context,
        },
        status_code=error.http_code if error else 200,
    )


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
    query = {"preset": preset, "start": start, "end": end, "basis": basis, "compare": compare}
    try:
        run = run_profit_and_loss(session, query)
    except AppBaseError as exc:
        return _page(
            request, "reports/statement.html", "Profit and Loss", picker, session, error=exc
        )
    return _page(
        request, "reports/statement.html", "Profit and Loss", picker, session, statement=run.report
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
        run = run_balance_sheet(session, {"asOf": asOf, "basis": basis, "compare": compare})
    except AppBaseError as exc:
        return _page(request, "reports/statement.html", "Balance Sheet", picker, session, error=exc)
    return _page(
        request, "reports/statement.html", "Balance Sheet", picker, session, statement=run.report
    )


@router.get("/reports/trial-balance", response_class=HTMLResponse)
async def trial_balance(
    request: Request,
    asOf: str = "",  # noqa: N803  (query parameter name)
    session: Session = Depends(get_session),
) -> HTMLResponse:
    picker = Picker("/reports/trial-balance", as_of=asOf, date=True)
    try:
        trial = run_trial_balance(session, {"asOf": asOf}).report
    except AppBaseError as exc:
        return _page(
            request,
            "reports/trial_balance.html",
            "Trial Balance",
            picker,
            session,
            error=exc,
        )
    return _page(
        request,
        "reports/trial_balance.html",
        "Trial Balance",
        picker,
        session,
        trial=trial,
        invariants=trial_invariants(trial),
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
        run = run_general_ledger(
            session, {"preset": preset, "start": start, "end": end, "account": account}
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
        request, "reports/general_ledger.html", "General Ledger", picker, session, ledger=run.report
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
        run = run_journal(session, {"preset": preset, "start": start, "end": end})
    except AppBaseError as exc:
        return _page(request, "reports/journal.html", "Journal", picker, session, error=exc)
    return _page(
        request,
        "reports/journal.html",
        "Journal",
        picker,
        session,
        entries=run.report.entries,
        period=run.period,
    )


@router.get("/reports/ar-aging", response_class=HTMLResponse)
async def ar_aging_report(
    request: Request,
    asOf: str = "",  # noqa: N803  (query parameter name)
    session: Session = Depends(get_session),
) -> HTMLResponse:
    picker = Picker("/reports/ar-aging", as_of=asOf, date=True)
    try:
        aging = run_ar_aging(session, {"asOf": asOf}).report
    except AppBaseError as exc:
        return _page(request, "reports/ar_aging.html", "A/R Aging", picker, session, error=exc)
    return _page(
        request,
        "reports/ar_aging.html",
        "A/R Aging",
        picker,
        session,
        aging=aging,
        invariants=aging.invariants,
        names={r.customer_id: r.customer_name for r in aging.rows},
    )


@router.get("/reports/owner-reimbursement", response_class=HTMLResponse)
async def owner_reimbursement_report_page(
    request: Request,
    as_of: str = "",
    date_from: str = "",
    date_to: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    picker = Picker(
        "/reports/owner-reimbursement",
        as_of=as_of,
        date_from=date_from,
        date_to=date_to,
        owner=True,
    )
    query = {"as_of": as_of, "date_from": date_from, "date_to": date_to}
    try:
        report = run_owner_reimbursement(session, query).report
    except AppBaseError as exc:
        return _page(
            request,
            "reports/owner_reimbursement.html",
            "Owner Reimbursement",
            picker,
            session,
            error=exc,
        )
    picker.as_of, picker.date_from, picker.date_to = (
        report.as_of.isoformat(),
        report.date_from.isoformat(),
        report.date_to.isoformat(),
    )
    return _page(
        request,
        "reports/owner_reimbursement.html",
        "Owner Reimbursement",
        picker,
        session,
        owner=report,
    )


# GET /reports/<report>.csv and .pdf: the same query string, validation and report as
# the page above (see payroll.frontend.report_export). Owner reimbursement is page-only:
# its downloads are registered here and there is no API twin.
add_export_routes(router, "/reports", PAGE_RUNNERS)
