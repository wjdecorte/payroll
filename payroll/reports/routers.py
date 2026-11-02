import logging
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from payroll import app_settings
from payroll.auth.permissions import require_reader
from payroll.common.dependencies import get_session
from payroll.common.middleware import LogRoute
from payroll.reports.schemas import GeneralLedger, JournalReportEntry, Register, TrialBalance
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
