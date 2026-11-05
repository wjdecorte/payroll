"""A/R Aging report: page, CSV and PDF exports."""

# ruff: noqa: F811  (fixtures re-exported below are used as arguments)

import csv
import io
from datetime import date
from decimal import Decimal

import pytest

from payroll.frontend.report_export import render_print_html
from payroll.receivables.customers import CustomerService
from payroll.receivables.invoices import InvoiceLineInput, InvoiceService
from payroll.receivables.items import ItemService
from payroll.receivables.payments import ApplicationInput, PaymentService
from payroll.reports.runs import run_ar_aging
from tests.ledger.conftest import chart  # noqa: F401
from tests.reports.test_export import pdf_ready  # noqa: F401

XSS = "<script>alert(1)</script>"
FORMULA = '=HYPERLINK("http://evil.example","x")'
PAGE = "/reports/ar-aging"


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr("payroll.reports.runs.company_today", lambda: date(2026, 4, 15))


@pytest.fixture
def books_ar(session, owner_user, chart):
    """Two customers: one owes 100.00 (30.00 paid, 20.00 over by 15 days), one has credit."""
    customers = CustomerService(session)
    acme = customers.create(actor=owner_user, name=FORMULA)
    zed = customers.create(actor=owner_user, name=f"Zed {XSS}")
    item = ItemService(session).create(
        actor=owner_user,
        name="Consulting",
        unit_price=Decimal("100.00"),
        income_account_id=chart["Services Revenue"].id,
    )
    invoices = InvoiceService(session)
    draft = invoices.create_draft(
        actor=owner_user,
        customer_id=acme.id,
        invoice_date=date(2026, 3, 1),
        terms_days=30,
        lines=[InvoiceLineInput(item.id, "Work", Decimal("1"), Decimal("100.00"))],
    )
    invoice = invoices.issue(draft.id, actor=owner_user)
    payments = PaymentService(session)
    payments.record(
        actor=owner_user,
        customer_id=acme.id,
        received_date=date(2026, 3, 10),
        amount=Decimal("30.00"),
        deposit_account_id=chart["Undeposited Funds"].id,
        method="check",
        applications=[ApplicationInput(invoice.id, Decimal("30.00"))],
    )
    payments.record(
        actor=owner_user,
        customer_id=zed.id,
        received_date=date(2026, 3, 11),
        amount=Decimal("45.00"),
        deposit_account_id=chart["Undeposited Funds"].id,
        method="check",
    )
    session.commit()
    return invoice


def _rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text, newline="")))


def test_page_renders_for_viewer(viewer_client, books_ar):
    page = viewer_client.get(f"{PAGE}?asOf=2026-04-15")
    assert page.status_code == 200
    text = page.text
    assert "A/R Aging" in text and "Balanced" in text
    assert "$70.00" in text and "$45.00" in text  # owed, and Zed's credit
    assert f"/invoices/{books_ar.id}" in text and "#1001" in text
    assert ">15<" in text  # days overdue: due 2026-03-31
    assert 'href="/reports/ar-aging.csv?asOf=2026-04-15"' in text
    assert XSS not in text and "Zed &lt;script&gt;" in text


def test_page_defaults_to_today_and_is_listed(owner_client, books_ar):
    assert "As of 2026-04-15" in owner_client.get(PAGE).text
    assert 'href="/reports/ar-aging"' in owner_client.get("/reports").text


def test_page_flags_manual_journal_difference(owner_client, books_ar, session, chart):
    from sqlmodel import select

    from payroll.auth.models import User
    from payroll.ledger.models import SourceType
    from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft

    owner = session.exec(select(User)).first()
    LedgerPostingService(session).post(
        EntryDraft(
            date(2026, 4, 1),
            [
                LineDraft(chart["Accounts Receivable"].id, debit=Decimal("12.00")),
                LineDraft(chart["Services Revenue"].id, credit=Decimal("12.00")),
            ],
            source_type=SourceType.MANUAL,
        ),
        actor=owner,
    )
    session.commit()
    page = owner_client.get(PAGE)
    assert page.status_code == 200
    assert "Out of balance" in page.text and "12.00" in page.text


@pytest.mark.parametrize("bad", ["garbage", "2026-02-30", "0001-01-01", "2201-01-01"])
@pytest.mark.parametrize("suffix", ["", ".csv", ".pdf"])
def test_invalid_as_of_is_422_not_500(owner_client, books_ar, bad, suffix):
    response = owner_client.get(f"{PAGE}{suffix}?asOf={bad}")
    assert response.status_code == 422
    if not suffix:
        assert 'role="alert"' in response.text


def test_csv_neutralizes_formula_customer_name(owner_client, books_ar, session):
    response = owner_client.get(f"{PAGE}.csv?asOf=2026-04-15")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert 'filename="ar-aging-2026-04-15.csv"' in response.headers["content-disposition"]
    rows = _rows(response.text)
    names = [r[0] for r in rows if r]
    assert "'" + FORMULA in names
    assert FORMULA not in names
    acme = next(r for r in rows if r[:1] == ["'" + FORMULA])
    assert acme[1:] == ["0.00", "70.00", "0.00", "0.00", "0.00", "70.00", "0.00"]
    zed = next(r for r in rows if r[:1] and r[0].startswith("Zed"))
    assert zed[-1] == "45.00" and zed[-2] == "0.00"
    detail = next(r for r in rows if r[:1] == ["1001"])
    assert detail[2:] == ["2026-03-01", "2026-03-31", "15", "100.00", "30.00", "70.00"]
    assert ["Ledger A/R balance", "25.00"] in rows
    assert ["Difference", "0.00"] in rows
    assert rows[-1][:2] == ["ar_subledger_matches_ledger", "OK"]


def test_csv_export_is_audited(owner_client, books_ar, session):
    from sqlmodel import select

    from payroll.audit.models import AuditLog

    owner_client.get(f"{PAGE}.csv?asOf=2026-04-15")
    row = session.exec(select(AuditLog).where(AuditLog.action == "report.export")).one()
    assert (row.entity, row.after["format"]) == ("ar-aging", "csv")


def test_pdf_endpoint_returns_pdf(owner_client, books_ar, pdf_ready):
    response = owner_client.get(f"{PAGE}.pdf?asOf=2026-04-15")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    assert 'filename="ar-aging-2026-04-15.pdf"' in response.headers["content-disposition"]


def test_print_html_escapes_and_lists_invoices(session, books_ar):
    html = render_print_html(run_ar_aging(session, {"asOf": "2026-04-15"}))
    assert "<h1>A/R Aging</h1>" in html
    assert XSS not in html and "Zed &lt;script&gt;" in html
    assert "#1001" in html and "$70.00" in html and "OK" in html
    assert "<script" not in html.replace("&lt;script", "")


def test_empty_books_render_and_export(owner_client, chart):
    page = owner_client.get(PAGE)
    assert page.status_code == 200 and "No open invoices" in page.text
    rows = _rows(owner_client.get(f"{PAGE}.csv").text)
    assert ["Total", "0.00", "0.00", "0.00", "0.00", "0.00", "0.00", "0.00"] in rows
