"""
Frontend routes.

Full-page routes (not in the OpenAPI schema):
  GET  /          -> renders the main page (index.html)
  GET  /admin     -> payroll configuration page
  POST /admin     -> saves the configuration

HTMX partial routes — return HTML fragments, not full pages:
  POST /htmx/calculate       -> runs a payroll calculation, returns _results.html
  POST /htmx/save-run        -> calculates and saves, returns _results.html
  GET  /htmx/history         -> returns _history.html (list of saved runs)
  GET  /htmx/runs/{id}       -> loads a saved run into the results panel
  DELETE /htmx/runs/{id}     -> deletes a run, returns refreshed _history.html

The routes call the payroll services in-process (same session as the API).
Service results are rendered via ``model_dump(by_alias=True, mode="json")`` so the
templates keep consuming the same camelCase dict shape the HTTP API returned.
"""

import datetime
import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ValidationError
from sqlmodel import Session

from payroll import app_settings
from payroll.auth.dependencies import csrf_context
from payroll.common.dependencies import get_session
from payroll.common.money import round_money
from payroll.exceptions import AppBaseError
from payroll.payroll_run.config_schemas import PayrollConfigUpdate
from payroll.payroll_run.config_services import PayrollConfigService
from payroll.payroll_run.schemas import PayrollInput
from payroll.payroll_run.services import PayrollRunService

logger = logging.getLogger("payroll.frontend")

_TEMPLATES_DIR = Path(__file__).parent / "templates"
# csrf_context exposes ``csrf_token`` (session or pre-session token) to every template.
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR), context_processors=[csrf_context])


# Money reaches the templates as JSON strings ("8000.00"), but also as Decimal,
# int or float (e.g. the raw admin form payload on a validation error). These
# filters accept all of them; never test a money string for truthiness in a
# template ("0.00" is truthy) — use ``| nonzero``.


def _to_decimal(value: object) -> Decimal:
    return Decimal(str(value if value is not None else 0))


def _currency(value: object) -> str:
    """``{{ v | currency }}`` -> ``$1,234.50``."""
    return f"${round_money(_to_decimal(value)):,.2f}"


def _amount(value: object) -> str:
    """``{{ v | amount }}`` -> ``1234.50`` (for ``<input type=number>`` values)."""
    try:
        return str(round_money(_to_decimal(value)))
    except InvalidOperation:
        return str(value)  # re-render whatever the user typed


def _nonzero(value: object) -> bool:
    """``{% if v | nonzero %}`` — true for a non-zero amount."""
    return _to_decimal(value) != 0


def _abs(value: object) -> Decimal | int:
    return abs(value) if isinstance(value, int) else abs(_to_decimal(value))


templates.env.filters["currency"] = _currency
templates.env.filters["amount"] = _amount
templates.env.filters["nonzero"] = _nonzero
templates.env.filters["abs"] = _abs
templates.env.globals["app_name"] = app_settings.app_name
templates.env.globals["app_version"] = app_settings.app_version
templates.env.globals["build_number"] = app_settings.build_number
templates.env.globals["build_sha"] = app_settings.build_sha[:7]


def _dump(model: BaseModel) -> dict:
    """Render a service result as the camelCase JSON-shaped dict templates expect."""
    return model.model_dump(by_alias=True, mode="json")


def _dump_list(models: list[BaseModel]) -> list[dict]:
    return [_dump(m) for m in models]


def _error_partial(request: Request, exc: AppBaseError, *, with_status: bool = True):
    context = {"message": exc.message}
    if with_status:
        context["status_code"] = exc.http_code
    return templates.TemplateResponse(
        request, "partials/_error.html", context, status_code=exc.http_code
    )


router = APIRouter(include_in_schema=False)


# ---------------------------------------------------------------------------
# Full-page routes
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    """Render the main page with an empty results panel."""
    latest_run = None
    config = None
    ytd_fica_default: str | Decimal = Decimal("0.00")
    history: list[dict] = []

    try:
        config = _dump(PayrollConfigService(session).get_config())
        run_service = PayrollRunService(session)
        history = _dump_list(run_service.list_runs(limit=50))
        if history:
            latest_run = _dump(run_service.get_run(history[0]["id"]))

            # Only pre-fill YTD FICA if the previous run is in the same year as now
            current_year = str(datetime.datetime.now().year)
            if latest_run.get("payPeriod", "").startswith(current_year):
                ytd_fica_default = latest_run.get("taxDetail", {}).get("ytdFicaAfter", "0.00")

    except Exception:
        logger.exception("Could not load index data")
        history = []

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "history": history,
            "latest_run": latest_run,
            "config": config,
            "ytd_fica_default": ytd_fica_default,
        },
    )


@router.get("/admin", response_class=HTMLResponse)
async def admin(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    """Render the payroll configuration admin page."""
    try:
        config = _dump(PayrollConfigService(session).get_config())
        return templates.TemplateResponse(
            request,
            "admin.html",
            {"config": config, "saved": False},
        )
    except AppBaseError as exc:
        return _error_partial(request, exc)


@router.post("/admin", response_class=HTMLResponse)
async def admin_save(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    """Save payroll configuration and render the admin page."""
    form = await request.form()

    def money(name: str) -> str:
        # Passed through as text; PayrollConfigUpdate validates it as exact Money.
        return form.get(name) or "0"

    def checked(name: str) -> bool:
        return form.get(name) == "true"

    payload = {
        "defaultGrossSalary": money("default_gross_salary"),
        "defaultHealthInsurance": money("default_health_insurance"),
        "defaultHsaContribution": money("default_hsa_contribution"),
        "defaultHealthInIncomeTax": checked("default_health_in_income_tax"),
        "defaultHsaInIncomeTax": checked("default_hsa_in_income_tax"),
        "defaultUsePreviousYtdFica": checked("default_use_previous_ytd_fica"),
        "defaultSaveRun": checked("default_save_run"),
        "defaultNotes": form.get("default_notes") or None,
        "acctOfficerCompensation": form.get("acct_officer_compensation"),
        "acctPayrollTaxExpense": form.get("acct_payroll_tax_expense"),
        "acctHealthInsuranceExp": form.get("acct_health_insurance_exp"),
        "acctHsaExpense": form.get("acct_hsa_expense"),
        "acctFedTaxPayable": form.get("acct_fed_tax_payable"),
        "acctGaTaxPayable": form.get("acct_ga_tax_payable"),
        "acctSsPayableEe": form.get("acct_ss_payable_ee"),
        "acctSsPayableEr": form.get("acct_ss_payable_er"),
        "acctMedicarePayableEe": form.get("acct_medicare_payable_ee"),
        "acctMedicarePayableEr": form.get("acct_medicare_payable_er"),
        "acctHealthInsPayable": form.get("acct_health_ins_payable"),
        "acctHsaPayable": form.get("acct_hsa_payable"),
        "acctChecking": form.get("acct_checking"),
        "federalDueDateNote": form.get("federal_due_date_note"),
        "georgiaDueDateNote": form.get("georgia_due_date_note"),
        "nextJournalNo": int(form.get("next_journal_no") or 202),
    }

    try:
        update = PayrollConfigUpdate.model_validate(payload)
        config = _dump(PayrollConfigService(session).update_config(update))
        return templates.TemplateResponse(
            request,
            "admin.html",
            {"config": config, "saved": True},
        )
    except (AppBaseError, ValidationError) as exc:
        if isinstance(exc, ValidationError):
            detail, status_code = str(exc), 422
        else:
            detail, status_code = exc.message, exc.http_code
        return templates.TemplateResponse(
            request,
            "admin.html",
            {
                "config": payload,
                "saved": False,
                "error": detail,
                "status_code": status_code,
            },
            status_code=status_code,
        )


# ---------------------------------------------------------------------------
# HTMX partial routes
# ---------------------------------------------------------------------------


def _next_journal_no(session: Session) -> int:
    try:
        return PayrollConfigService(session).get_config().next_journal_no
    except Exception:
        return 202


@router.post("/htmx/calculate", response_class=HTMLResponse)
async def htmx_calculate(
    request: Request,
    pay_period: str = Form(...),
    gross_salary: Decimal = Form(...),
    health_insurance: Decimal = Form(Decimal("0.00")),
    hsa_contribution: Decimal = Form(Decimal("0.00")),
    health_in_income_tax: bool = Form(False),
    hsa_in_income_tax: bool = Form(False),
    ytd_fica_wages: Decimal = Form(Decimal("0.00")),
    use_previous_ytd_fica: bool = Form(False),
    notes: str = Form(""),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """Calculate payroll and return the results partial (never saves)."""
    try:
        payload = PayrollInput.model_validate(
            {
                "payPeriod": pay_period,
                "grossSalary": gross_salary,
                "healthInsurance": health_insurance,
                "hsaContribution": hsa_contribution,
                "healthInIncomeTax": health_in_income_tax,
                "hsaInIncomeTax": hsa_in_income_tax,
                "ytdFicaWages": ytd_fica_wages,
                "usePreviousYtdFica": use_previous_ytd_fica,
                "saveRun": False,
                "notes": notes or None,
            }
        )
        result = _dump(PayrollRunService(session).calculate(payload))

        return templates.TemplateResponse(
            request,
            "partials/_results.html",
            {
                "result": result,
                "history": [],
                "save_run": False,
                "next_journal_no": _next_journal_no(session),
            },
        )

    except AppBaseError as exc:
        return _error_partial(request, exc)
    except ValidationError as exc:
        return templates.TemplateResponse(
            request,
            "partials/_error.html",
            {"message": str(exc), "status_code": 422},
            status_code=422,
        )
    except Exception as exc:
        logger.exception("Unexpected error during calculate: %s", exc)
        return templates.TemplateResponse(
            request,
            "partials/_error.html",
            {"message": "An unexpected error occurred."},
            status_code=500,
        )


@router.post("/htmx/save-run", response_class=HTMLResponse)
async def htmx_save_run(
    request: Request,
    pay_period: str = Form(...),
    gross_salary: Decimal = Form(...),
    health_insurance: Decimal = Form(Decimal("0.00")),
    hsa_contribution: Decimal = Form(Decimal("0.00")),
    health_in_income_tax: bool = Form(False),
    hsa_in_income_tax: bool = Form(False),
    ytd_fica_wages: Decimal = Form(Decimal("0.00")),
    notes: str = Form(""),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """Save a calculated run to history and return the updated results partial."""
    try:
        payload = PayrollInput.model_validate(
            {
                "payPeriod": pay_period,
                "grossSalary": gross_salary,
                "healthInsurance": health_insurance,
                "hsaContribution": hsa_contribution,
                "healthInIncomeTax": health_in_income_tax,
                "hsaInIncomeTax": hsa_in_income_tax,
                "ytdFicaWages": ytd_fica_wages,
                "usePreviousYtdFica": False,
                "saveRun": True,
                "notes": notes or None,
            }
        )
        run_service = PayrollRunService(session)
        result = _dump(run_service.calculate(payload))
        history: list[dict] = []
        try:
            history = _dump_list(run_service.list_runs(limit=50))
        except Exception:
            pass

        return templates.TemplateResponse(
            request,
            "partials/_results.html",
            {
                "result": result,
                "history": history,
                "save_run": True,
                "next_journal_no": _next_journal_no(session),
            },
        )

    except AppBaseError as exc:
        return _error_partial(request, exc)
    except ValidationError as exc:
        return templates.TemplateResponse(
            request,
            "partials/_error.html",
            {"message": str(exc), "status_code": 422},
            status_code=422,
        )
    except Exception as exc:
        logger.exception("Unexpected error during save-run: %s", exc)
        return templates.TemplateResponse(
            request,
            "partials/_error.html",
            {"message": "An unexpected error occurred saving the run."},
            status_code=500,
        )


@router.get("/htmx/history", response_class=HTMLResponse)
async def htmx_history(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    """Return the payroll history table partial."""
    try:
        history = _dump_list(PayrollRunService(session).list_runs(limit=50))
        return templates.TemplateResponse(
            request,
            "partials/_history.html",
            {"history": history},
        )
    except AppBaseError as exc:
        return _error_partial(request, exc, with_status=False)


@router.get("/htmx/runs/{run_id}", response_class=HTMLResponse)
async def htmx_get_run(
    request: Request, run_id: int, session: Session = Depends(get_session)
) -> HTMLResponse:
    """Load a saved payroll run into the results panel."""
    try:
        result = _dump(PayrollRunService(session).get_run(run_id))
        return templates.TemplateResponse(
            request,
            "partials/_results.html",
            {
                "result": result,
                "history": [],
                "save_run": False,
                "next_journal_no": _next_journal_no(session),
            },
        )
    except AppBaseError as exc:
        return _error_partial(request, exc)


@router.delete("/htmx/runs/{run_id}", response_class=HTMLResponse)
async def htmx_delete_run(
    request: Request, run_id: int, session: Session = Depends(get_session)
) -> HTMLResponse:
    """Delete a saved run and return the refreshed history partial."""
    try:
        run_service = PayrollRunService(session)
        run_service.delete_run(run_id)
        history = _dump_list(run_service.list_runs(limit=50))
        return templates.TemplateResponse(
            request,
            "partials/_history.html",
            {"history": history},
        )
    except AppBaseError as exc:
        return _error_partial(request, exc, with_status=False)


@router.post("/htmx/increment-journal-no", response_class=HTMLResponse)
async def htmx_increment_journal_no(
    count: int = Query(default=2, ge=1),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """Increment next_journal_no in config after a QBO CSV export. Returns updated badge HTML."""
    try:
        config = PayrollConfigService(session).increment_journal_no(count)
        return HTMLResponse(
            f'<span id="je-no-badge" class="badge bg-secondary ms-2">'
            f"Next JE #: {config.next_journal_no}</span>"
        )
    except Exception:
        return HTMLResponse('<span id="je-no-badge"></span>')
