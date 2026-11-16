"""
CSV / PDF downloads of the report pages (and their JSON-API twins).

``GET <base>/<report>.csv`` and ``.pdf`` take the same query string as the report
page and go through the same parsing, validation and :class:`ReportService` calls
(:mod:`payroll.reports.runs`), so a file always equals what the page shows and bad
input is refused identically (422, no file, no audit row). Any signed-in role may
export. A successful export writes a ``report.export`` audit row (entity = the report
slug, after = the validated parameters and the format) and commits it; the file name
is the report slug plus validated dates only, never user text.
"""

from collections.abc import Callable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlmodel import Session

from payroll.audit.services import record
from payroll.auth.models import User
from payroll.auth.permissions import require_reader
from payroll.common.dependencies import get_session
from payroll.expenses.reports import OwnerReport
from payroll.frontend.routers import templates
from payroll.payroll_run.liability import LiabilityReport
from payroll.receivables.balances import ARAging
from payroll.reports.export import to_csv, to_pdf
from payroll.reports.runs import RUNNERS, Query, ReportRun, trial_invariants
from payroll.reports.schemas import GeneralLedger, JournalReport, Statement, TrialBalance

Runner = Callable[[Session, Query], ReportRun]
MEDIA_TYPES = {"csv": "text/csv; charset=utf-8", "pdf": "application/pdf"}


def render_print_html(run: ReportRun) -> str:
    """The print-optimized HTML for ``run`` (autoescaped; only ever fed to WeasyPrint)."""
    report = run.report
    context: dict[str, object] = {"title": run.title}
    if isinstance(report, Statement):
        context.update(statement=report, invariants=report.invariants)
    elif isinstance(report, TrialBalance):
        context.update(trial=report, invariants=trial_invariants(report))
    elif isinstance(report, GeneralLedger):
        context.update(ledger=report)
    elif isinstance(report, JournalReport):
        context.update(journal=report)
    elif isinstance(report, ARAging):
        context.update(aging=report, invariants=report.invariants)
    elif isinstance(report, OwnerReport):
        context.update(owner=report)
    elif isinstance(report, LiabilityReport):
        context.update(liability=report)
    return templates.env.get_template("reports/print.html").render(context)


def export_response(session: Session, user: User, request: Request, run: ReportRun, fmt: str):
    """Build the file, audit the export in one transaction, and return the download."""
    if fmt == "csv":
        body = to_csv(run.report).encode("utf-8")
    else:
        body = to_pdf(render_print_html(run))
    try:
        record(
            session,
            action="report.export",
            user_id=user.id,
            entity=run.name,
            after={**run.params, "format": fmt},
            ip=request.client.host if request.client else None,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    return Response(
        body,
        media_type=MEDIA_TYPES[fmt],
        headers={
            "Content-Disposition": f'attachment; filename="{run.stem}.{fmt}"',
            "Cache-Control": "no-store",
        },
    )


def add_export_routes(router: APIRouter, base: str, runners: dict[str, Runner] = RUNNERS) -> None:
    """Register ``<base>/<report>.csv`` and ``.pdf`` for every report in ``runners``."""
    for name, runner in runners.items():
        for fmt in MEDIA_TYPES:
            router.add_api_route(
                f"{base}/{name}.{fmt}",
                _endpoint(runner, fmt),
                methods=["GET"],
                response_class=Response,
                summary=f"{name.replace('-', ' ').capitalize()} as {fmt.upper()}",
            )


def _endpoint(runner, fmt: str):
    def export(
        request: Request,
        session: Session = Depends(get_session),
        user: User = Depends(require_reader),
    ) -> Response:
        run = runner(session, request.query_params)
        return export_response(session, user, request, run, fmt)

    return export
