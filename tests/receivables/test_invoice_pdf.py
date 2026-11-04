"""Invoice PDF: the print HTML (tested without Pango), the route, audit and access."""

# ruff: noqa: F811  (the ``chart`` fixture is re-exported by conftest and used as an argument)

import socket
import threading
from datetime import date
from decimal import Decimal

import pytest
from sqlmodel import select

from payroll.audit.models import AuditLog
from payroll.receivables.invoice_pdf import invoice_html, render_invoice_pdf
from payroll.receivables.invoices import InvoiceLineInput, InvoiceService
from payroll.receivables.payments import ApplicationInput, PaymentService
from payroll.receivables.settings import ReceivablesSettingsService
from tests.ledger.conftest import chart  # noqa: F401
from tests.reports.test_export import pdf_ready  # noqa: F401

D = Decimal
DAY = date(2026, 3, 1)
XSS = "<script>alert(1)</script>"


@pytest.fixture(autouse=True)
def _committed_fixtures(session, owner_user, customer, item):
    """Commit the fixture rows: a service rollback must not wipe them mid-test."""
    session.commit()


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr("payroll.receivables.invoice_pdf.company_today", lambda: date(2026, 4, 1))


def _issue(session, owner_user, customer, item, lines=None, **kwargs):
    lines = lines or [InvoiceLineInput(item.id, "Consulting work", D("2"), D("150.00"))]
    draft = InvoiceService(session).create_draft(
        actor=owner_user, customer_id=customer.id, invoice_date=DAY, lines=lines, **kwargs
    )
    invoice = InvoiceService(session).issue(draft.id, actor=owner_user)
    session.commit()
    return invoice


def _pay(session, owner_user, customer, chart, invoice, amount):
    PaymentService(session).record(
        actor=owner_user,
        customer_id=customer.id,
        received_date=date(2026, 3, 10),
        amount=D(amount),
        deposit_account_id=chart["Undeposited Funds"].id,
        method="check",
        applications=[ApplicationInput(invoice.id, D(amount))],
    )
    session.commit()


def test_html_contains_number_customer_lines_total_and_balance_due(
    session, owner_user, customer, item
):
    customer.billing_address = "1 Main St\nAtlanta, GA"
    session.add(customer)
    settings = ReceivablesSettingsService(session).get()
    settings.company_name = "DCI Consulting LLC"
    settings.company_address = "9 Company Rd"
    settings.remit_to = "Remit to PO Box 7"
    settings.invoice_footer = "Thank you for your business"
    session.add(settings)
    session.commit()
    invoice = _issue(session, owner_user, customer, item)
    html = invoice_html(session, invoice.id)
    for expected in (
        f"Invoice #{invoice.number}",
        "Acme Corp",
        "1 Main St",
        "Atlanta, GA",
        "Consulting work",
        "$150.00",
        "$300.00",
        "2026-03-01",
        "2026-03-31",
        "Net 30",
        "DCI Consulting LLC",
        "9 Company Rd",
        "Remit to PO Box 7",
        "Thank you for your business",
        "data:image/png;base64,",
    ):
        assert expected in html, expected
    assert "Balance due" in html
    assert "$0.00" not in html.split("Balance due")[1][:80]
    assert "VOID" not in html


def test_html_escapes_markup_and_wraps_long_text(session, owner_user, customer, item):
    customer.name = f"Evil {XSS}"
    customer.billing_address = "<b>bold</b>"
    session.add(customer)
    settings = ReceivablesSettingsService(session).get()
    settings.company_name = "<i>Co</i>"
    settings.invoice_footer = "<u>foot</u>"
    session.add(settings)
    session.commit()
    long_word = "W" * 400
    invoice = _issue(
        session,
        owner_user,
        customer,
        item,
        lines=[InvoiceLineInput(item.id, f"<img src=x> {long_word}", D("1"), D("5.00"))],
        memo="<script>memo</script>",
    )
    html = invoice_html(session, invoice.id)
    for raw in (XSS, "<b>bold</b>", "<i>Co</i>", "<u>foot</u>", "<img src=x>", "<script>memo"):
        assert raw not in html, raw
    for escaped in ("Evil &lt;script&gt;", "&lt;b&gt;bold", "&lt;i&gt;Co", "&lt;img src=x&gt;"):
        assert escaped in html, escaped
    assert long_word in html
    assert "overflow-wrap" in html


def test_html_shows_paid_and_balance_after_partial_payment(
    session, owner_user, customer, item, chart
):
    invoice = _issue(session, owner_user, customer, item)  # total 300.00
    _pay(session, owner_user, customer, chart, invoice, "120.00")
    html = invoice_html(session, invoice.id)
    assert "$120.00" in html
    paid_at = html.index("Paid")
    assert "$120.00" in html[paid_at : paid_at + 120]
    due_at = html.index("Balance due")
    assert "$180.00" in html[due_at : due_at + 120]


def test_void_invoice_html_is_marked_void(session, owner_user, customer, item):
    invoice = _issue(session, owner_user, customer, item)
    InvoiceService(session).void(invoice.id, actor=owner_user, void_date=date(2026, 3, 5))
    session.commit()
    html = invoice_html(session, invoice.id)
    assert "VOID" in html
    assert f"Invoice #{invoice.number}" in html


def test_draft_invoice_pdf_is_409(session, owner_user, customer, item, owner_client):
    draft = InvoiceService(session).create_draft(
        actor=owner_user,
        customer_id=customer.id,
        invoice_date=DAY,
        lines=[InvoiceLineInput(item.id, "x", D("1"), D("5.00"))],
    )
    session.commit()
    with pytest.raises(Exception) as info:
        invoice_html(session, draft.id)
    assert info.value.http_code == 409
    assert owner_client.get(f"/invoices/{draft.id}.pdf").status_code == 409


def test_missing_invoice_404(owner_client, session):
    assert owner_client.get("/invoices/99999.pdf").status_code == 404
    with pytest.raises(Exception) as info:
        invoice_html(session, 99999)
    assert info.value.http_code == 404


def test_pdf_endpoint_returns_pdf_bytes_and_filename(
    session, owner_user, customer, item, owner_client, pdf_ready
):
    invoice = _issue(session, owner_user, customer, item)
    response = owner_client.get(f"/invoices/{invoice.id}.pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    assert (
        f'attachment; filename="Invoice-{invoice.number}.pdf"'
        == response.headers["content-disposition"]
    )
    assert render_invoice_pdf(session, invoice.id).startswith(b"%PDF")


def test_pdf_cannot_fetch_urls(session, owner_user, customer, item, pdf_ready, monkeypatch):
    from payroll.reports import export

    attempts: list[str] = []
    real = export.NoFetch.__call__

    def spy(self, url, *args, **kwargs):
        attempts.append(url)
        return real(self, url, *args, **kwargs)

    monkeypatch.setattr(export.NoFetch, "__call__", spy)
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(0.5)
    port = server.getsockname()[1]
    connections: list[bool] = []

    def accept():
        try:
            server.accept()
            connections.append(True)
        except OSError:
            pass

    thread = threading.Thread(target=accept)
    thread.start()
    description = f'<img src="http://127.0.0.1:{port}/x.png">'
    invoice = _issue(
        session,
        owner_user,
        customer,
        item,
        lines=[InvoiceLineInput(item.id, description, D("1"), D("5.00"))],
    )
    html = invoice_html(session, invoice.id)
    assert f'<img src="http://127.0.0.1:{port}' not in html
    pdf = render_invoice_pdf(session, invoice.id)
    thread.join()
    server.close()
    assert pdf.startswith(b"%PDF")
    assert connections == []
    assert not any(a.startswith("http://") for a in attempts)


def test_viewer_can_download_pdf_and_it_is_audited(
    session, owner_user, customer, item, viewer_client, monkeypatch
):
    monkeypatch.setattr("payroll.receivables.invoice_pdf.to_pdf", lambda html: b"%PDF-fake")
    invoice = _issue(session, owner_user, customer, item)
    response = viewer_client.get(f"/invoices/{invoice.id}.pdf")
    assert response.status_code == 200
    assert response.content == b"%PDF-fake"
    row = session.exec(select(AuditLog).where(AuditLog.action == "invoice.pdf")).one()
    assert (row.entity, row.entity_id) == ("invoice", str(invoice.id))
