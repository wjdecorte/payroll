import logging

from fastapi import APIRouter, Depends, Query, status
from sqlmodel import Session

from payroll import app_settings
from payroll.common.dependencies import get_session
from payroll.common.middleware import LogRoute

from .schemas import PayrollInput, PayrollResult, PayrollRunSummary
from .services import PayrollRunService

logger = logging.getLogger(f"{app_settings.logger_name}.payroll_run.router")

router = APIRouter(
    prefix="/payroll",
    tags=["payroll"],
    route_class=LogRoute,
)


# ---------------------------------------------------------------------------
# Calculate
# ---------------------------------------------------------------------------


@router.post(
    "/runs/calculate",
    response_model=PayrollResult,
    status_code=status.HTTP_201_CREATED,
    summary="Calculate a payroll run",
    description=(
        "Calculate all withholdings for a monthly payroll run. "
        "Set `saveRun=true` (default) to persist the run to history."
    ),
)
async def calculate_payroll(
    payload: PayrollInput,
    session: Session = Depends(get_session),
) -> PayrollResult:
    service = PayrollRunService(session)
    return service.calculate(payload)


# ---------------------------------------------------------------------------
# History — list
# ---------------------------------------------------------------------------


@router.get(
    "/runs",
    response_model=list[PayrollRunSummary],
    status_code=status.HTTP_200_OK,
    summary="List all saved payroll runs",
)
async def list_payroll_runs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[PayrollRunSummary]:
    service = PayrollRunService(session)
    return service.list_runs(limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# History — get by ID
# ---------------------------------------------------------------------------


@router.get(
    "/runs/{run_id}",
    response_model=PayrollResult,
    status_code=status.HTTP_200_OK,
    summary="Get a saved payroll run by ID",
)
async def get_payroll_run(
    run_id: int,
    session: Session = Depends(get_session),
) -> PayrollResult:
    service = PayrollRunService(session)
    return service.get_run(run_id)


# ---------------------------------------------------------------------------
# History — delete
# ---------------------------------------------------------------------------


@router.delete(
    "/runs/{run_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a saved payroll run",
)
async def delete_payroll_run(
    run_id: int,
    session: Session = Depends(get_session),
) -> None:
    service = PayrollRunService(session)
    service.delete_run(run_id)


# ---------------------------------------------------------------------------
# Tax constants (read-only)
# ---------------------------------------------------------------------------


@router.get(
    "/tax-constants",
    status_code=status.HTTP_200_OK,
    summary="View current tax year constants",
    description="Returns the tax rates and brackets currently in use. Update constants.py each January.",
)
async def get_tax_constants() -> dict:
    from payroll.tax.schemas import TaxConstantsSchema

    return TaxConstantsSchema().model_dump(by_alias=True)
