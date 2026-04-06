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

import logging
from pathlib import Path

from fastapi import APIRouter, Form, Request
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
    try:
        history = await client.list_runs()
    except Exception:
        history = []

    return templates.TemplateResponse(
        request,
        "index.html",
        {"history": history},
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
    save_run: bool = Form(False),
    notes: str = Form(""),
) -> HTMLResponse:
    """Calculate payroll and return the results partial."""
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
                "saveRun": save_run,
                "notes": notes or None,
            }
        )
        # Fetch updated history after a successful save
        history = []
        if save_run:
            try:
                history = await client.list_runs()
            except Exception:
                pass

        return templates.TemplateResponse(
            request,
            "partials/_results.html",
            {"result": result, "history": history, "save_run": save_run},
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
        result = await client.get_run(run_id)
        return templates.TemplateResponse(
            request,
            "partials/_results.html",
            {"result": result, "history": [], "save_run": False},
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
