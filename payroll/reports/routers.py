import logging
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from payroll import app_settings
from payroll.auth.permissions import require_reader
from payroll.common.dependencies import get_session
from payroll.common.middleware import LogRoute
from payroll.reports.basis import DEFAULT_BASIS, Basis
from payroll.reports.periods import company_today, resolve_period
from payroll.reports.schemas import (
    GeneralLedger,
    JournalReportEntry,
    Register,
    Statement,
    TrialBalance,
)
from payroll.reports.statements import ReportService

logger = logging.getLogger(f"{app_settings.logger_name}.reports.router")

# Reports are read-only: every route is a GET open to any signed-in role.
router = APIRouter(
    prefix="/ledger/reports",
    tags=["reports"],
    route_class=LogRoute,
    dependencies=[Depends(require_reader)],
)


@router.get("/trial-balance", response_model=TrialBalance, summary="Trial balance")
async def trial_balance(
    as_of: date = Query(alias="asOf"),
    include_zero: bool = Query(default=False, alias="includeZero"),
    session: Session = Depends(get_session),
) -> TrialBalance:
    return ReportService(session).trial_balance(as_of, include_zero=include_zero)


@router.get("/general-ledger", response_model=GeneralLedger, summary="General ledger")
async def general_ledger(
    start: date,
    end: date,
    account_id: int | None = Query(default=None, alias="accountId"),
    session: Session = Depends(get_session),
) -> GeneralLedger:
    return ReportService(session).general_ledger(start, end, account_id)


@router.get("/journal", response_model=list[JournalReportEntry], summary="Journal")
async def journal(
    start: date, end: date, session: Session = Depends(get_session)
) -> list[JournalReportEntry]:
    return ReportService(session).journal(start, end)


@router.get("/account-register/{account_id}", response_model=Register, summary="Account register")
async def account_register(
    account_id: int, start: date, end: date, session: Session = Depends(get_session)
) -> Register:
    return ReportService(session).account_register(account_id, start, end)


Comparison = Literal["previous_period", "previous_year"]


@router.get("/profit-and-loss", response_model=Statement, summary="Profit and loss")
async def profit_and_loss(
    preset: str | None = None,
    start: date | None = None,
    end: date | None = None,
    basis: Basis = DEFAULT_BASIS,
    compare: Comparison | None = None,
    session: Session = Depends(get_session),
) -> Statement:
    """``preset`` defaults to ``custom`` when ``start``/``end`` are given, else ``this_month``."""
    if preset is None:
        preset = "custom" if start is not None or end is not None else "this_month"
    period = resolve_period(preset, company_today(), start=start, end=end)
    return ReportService(session).profit_and_loss(period, basis=basis, compare=compare)


@router.get("/balance-sheet", response_model=Statement, summary="Balance sheet")
async def balance_sheet(
    as_of: date | None = Query(default=None, alias="asOf"),
    basis: Basis = DEFAULT_BASIS,
    compare: Comparison | None = None,
    session: Session = Depends(get_session),
) -> Statement:
    """``asOf`` defaults to the company's today."""
    return ReportService(session).balance_sheet(
        as_of or company_today(), basis=basis, compare=compare
    )


# CSV / PDF twins (``/ledger/reports/<report>.csv`` and ``.pdf``) take the report
# pages' query strings; imported late because the frontend package imports reports.
from payroll.frontend.report_export import add_export_routes  # noqa: E402

add_export_routes(router, "")
