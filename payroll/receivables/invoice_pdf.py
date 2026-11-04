"""The send-ready invoice PDF.

:func:`invoice_html` builds the print HTML (autoescaped Jinja; the logo is embedded as a
data URI because the PDF fetcher refuses every URL) and :func:`render_invoice_pdf` turns
it into bytes with the same refusing-fetcher WeasyPrint call the reports use. Only issued
invoices render: ``open`` ones, and ``void`` ones marked VOID. The balance due is the
derived one (:func:`~payroll.receivables.balances.invoice_state`), never ``total - applied``.
"""

import base64
from functools import lru_cache
from pathlib import Path

from sqlmodel import Session

from payroll.receivables.balances import invoice_state
from payroll.receivables.customers import CustomerService
from payroll.receivables.exceptions import InvoiceStateError
from payroll.receivables.invoices import InvoiceService
from payroll.receivables.settings import ReceivablesSettingsService
from payroll.reports.export import to_pdf
from payroll.reports.periods import company_today

LOGO = Path(__file__).resolve().parent.parent / "frontend" / "static" / "logo-icon.png"


@lru_cache(maxsize=1)
def _logo_uri() -> str:
    return "data:image/png;base64," + base64.b64encode(LOGO.read_bytes()).decode("ascii")


def invoice_html(session: Session, invoice_id: int) -> str:
    """The print HTML for an issued invoice (404 unknown, 409 draft)."""
    from payroll.frontend.routers import templates  # lazy: avoids a package import cycle

    service = InvoiceService(session)
    invoice = service.get(invoice_id)
    if invoice.status not in ("open", "void"):
        raise InvoiceStateError(message="Only an issued invoice can be downloaded as a PDF")
    state = invoice_state(session, invoice, company_today())
    return templates.env.get_template("invoices/pdf.html").render(
        invoice=invoice,
        state=state,
        customer=CustomerService(session).get(invoice.customer_id),
        lines=service.lines(invoice_id),
        company=ReceivablesSettingsService(session).get(),
        logo=_logo_uri(),
        is_void=invoice.status == "void",
    )


def render_invoice_pdf(session: Session, invoice_id: int) -> bytes:
    return to_pdf(invoice_html(session, invoice_id))
