"""
Frontend routes.

Full-page routes:
  GET /          → renders the main page (index.html)

HTMX partial routes — return HTML fragments, not full pages:
  POST /htmx/calculate       → runs a payroll calculation, returns _results.html
  GET  /htmx/history         → returns _history.html (list of saved runs)
  GET  /htmx/runs/{id}       → loads a saved run into the results panel
  DELETE /htmx/runs/{id}     → deletes a run, returns refreshed _history.html
"""

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from payroll.frontend.client import APIError, PayrollAPIClient

logger = logging.getLogger("payroll.frontend")

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Register a currency formatter so templates can write {{ value | currency }}
templates.env.filters["currency"] = lambda v: f"${v:,.2f}" if v is not None else "$0.00"
templates.env.filters["abs"] = abs

router = APIRouter()


# ---------------------------------------------------------------------------
# Full-page routes
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Render the main page with an empty results panel."""
    client = PayrollAPIClient()
    latest_run = None
    config = None
    ytd_fica_default = 0.0

    try:
        config = await client.get_config()
        history = await client.list_runs()
        if history:
            latest_run = await client.get_run(history[0]["id"])

            # Only pre-fill YTD FICA if the previous run is in the same year as now
            import datetime

            current_year = str(datetime.datetime.now().year)
            if latest_run.get("payPeriod", "").startswith(current_year):
                ytd_fica_default = latest_run.get("taxDetail", {}).get("ytdFicaAfter", 0.0)

    except Exception:
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
async def admin(request: Request) -> HTMLResponse:
    """Render the payroll configuration admin page."""
    client = PayrollAPIClient()
    try:
        config = await client.get_config()
        return templates.TemplateResponse(
            request,
            "admin.html",
            {"config": config, "saved": False},
        )
    except APIError as exc:
        return templates.TemplateResponse(
            request,
            "partials/_error.html",
            {"message": exc.detail, "status_code": exc.status_code},
            status_code=exc.status_code,
        )


@router.post("/admin", response_class=HTMLResponse)
async def admin_save(request: Request) -> HTMLResponse:
    """Save payroll configuration and render the admin page."""
    client = PayrollAPIClient()
    form = await request.form()

    def money(name: str) -> float:
        return float(form.get(name) or 0)

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
        config = await client.update_config(payload)
        return templates.TemplateResponse(
            request,
            "admin.html",
            {"config": config, "saved": True},
        )
    except APIError as exc:
        return templates.TemplateResponse(
            request,
            "admin.html",
            {
                "config": payload,
                "saved": False,
                "error": exc.detail,
                "status_code": exc.status_code,
            },
            status_code=exc.status_code,
        )


# ---------------------------------------------------------------------------
# HTMX partial routes
# ---------------------------------------------------------------------------


@router.post("/htmx/calculate", response_class=HTMLResponse)
async def htmx_calculate(
    request: Request,
    pay_period: str = Form(...),
    gross_salary: float = Form(...),
    health_insurance: float = Form(0.0),
    hsa_contribution: float = Form(0.0),
    health_in_income_tax: bool = Form(False),
    hsa_in_income_tax: bool = Form(False),
    ytd_fica_wages: float = Form(0.0),
    use_previous_ytd_fica: bool = Form(False),
    notes: str = Form(""),
) -> HTMLResponse:
    """Calculate payroll and return the results partial (never saves)."""
    client = PayrollAPIClient()
    try:
        result = await client.calculate(
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
        next_journal_no = 202
        try:
            config = await client.get_config()
            next_journal_no = config.get("nextJournalNo", 202)
        except Exception:
            pass

        return templates.TemplateResponse(
            request,
            "partials/_results.html",
            {
                "result": result,
                "history": [],
                "save_run": False,
                "next_journal_no": next_journal_no,
            },
        )

    except APIError as exc:
        return templates.TemplateResponse(
            request,
            "partials/_error.html",
            {"message": exc.detail, "status_code": exc.status_code},
            status_code=exc.status_code,
        )
    except Exception as exc:
        logger.exception("Unexpected error during calculate: %s", exc)
        return templates.TemplateResponse(
            request,
            "partials/_error.html",
            {"message": "An unexpected error occurred. Is the API running?"},
            status_code=500,
        )


@router.post("/htmx/save-run", response_class=HTMLResponse)
async def htmx_save_run(
    request: Request,
    pay_period: str = Form(...),
    gross_salary: float = Form(...),
    health_insurance: float = Form(0.0),
    hsa_contribution: float = Form(0.0),
    health_in_income_tax: bool = Form(False),
    hsa_in_income_tax: bool = Form(False),
    ytd_fica_wages: float = Form(0.0),
    notes: str = Form(""),
) -> HTMLResponse:
    """Save a calculated run to history and return the updated results partial."""
    client = PayrollAPIClient()
    try:
        result = await client.calculate(
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
        history = []
        next_journal_no = 202
        try:
            config = await client.get_config()
            next_journal_no = config.get("nextJournalNo", 202)
            history = await client.list_runs()
        except Exception:
            pass

        return templates.TemplateResponse(
            request,
            "partials/_results.html",
            {
                "result": result,
                "history": history,
                "save_run": True,
                "next_journal_no": next_journal_no,
            },
        )

    except APIError as exc:
        return templates.TemplateResponse(
            request,
            "partials/_error.html",
            {"message": exc.detail, "status_code": exc.status_code},
            status_code=exc.status_code,
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
async def htmx_history(request: Request) -> HTMLResponse:
    """Return the payroll history table partial."""
    client = PayrollAPIClient()
    try:
        history = await client.list_runs()
        return templates.TemplateResponse(
            request,
            "partials/_history.html",
            {"history": history},
        )
    except APIError as exc:
        return templates.TemplateResponse(
            request,
            "partials/_error.html",
            {"message": exc.detail},
            status_code=exc.status_code,
        )


@router.get("/htmx/runs/{run_id}", response_class=HTMLResponse)
async def htmx_get_run(request: Request, run_id: int) -> HTMLResponse:
    """Load a saved payroll run into the results panel."""
    client = PayrollAPIClient()
    try:
        result, config = await asyncio.gather(
            client.get_run(run_id),
            client.get_config(),
            return_exceptions=True,
        )
        if isinstance(result, Exception):
            raise result
        next_journal_no = config.get("nextJournalNo", 202) if isinstance(config, dict) else 202
        return templates.TemplateResponse(
            request,
            "partials/_results.html",
            {
                "result": result,
                "history": [],
                "save_run": False,
                "next_journal_no": next_journal_no,
            },
        )
    except APIError as exc:
        return templates.TemplateResponse(
            request,
            "partials/_error.html",
            {"message": exc.detail, "status_code": exc.status_code},
            status_code=exc.status_code,
        )


@router.delete("/htmx/runs/{run_id}", response_class=HTMLResponse)
async def htmx_delete_run(request: Request, run_id: int) -> HTMLResponse:
    """Delete a saved run and return the refreshed history partial."""
    client = PayrollAPIClient()
    try:
        await client.delete_run(run_id)
        history = await client.list_runs()
        return templates.TemplateResponse(
            request,
            "partials/_history.html",
            {"history": history},
        )
    except APIError as exc:
        return templates.TemplateResponse(
            request,
            "partials/_error.html",
            {"message": exc.detail},
            status_code=exc.status_code,
        )


@router.post("/htmx/increment-journal-no", response_class=HTMLResponse)
async def htmx_increment_journal_no(
    request: Request,
    count: int = Query(default=2, ge=1),
) -> HTMLResponse:
    """Increment next_journal_no in config after a QBO CSV export. Returns updated badge HTML."""
    client = PayrollAPIClient()
    try:
        config = await client.increment_journal_no(count)
        next_no = config.get("nextJournalNo", 202)
        return HTMLResponse(
            f'<span id="je-no-badge" class="badge bg-secondary ms-2">Next JE #: {next_no}</span>'
        )
    except Exception:
        return HTMLResponse('<span id="je-no-badge"></span>')
