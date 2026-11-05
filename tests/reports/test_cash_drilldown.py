"""Cash-basis drill-down: the account register reads the same rows as the statements.

Data is built through the receivables services (see ``test_cash_basis``, whose fixtures
are reused). Base scenario: invoice of 1,000.00 dated 2026-01-10 over Consulting (4100)
600.00 and Training (4200) 400.00; 500.00 paid and applied on 2026-02-05.
"""

# ruff: noqa: F811  (fixtures are re-exported from test_cash_basis and used as arguments)

import html
import re
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa

from payroll.ledger.models import JournalEntry, JournalLine
from payroll.reports import queries
from payroll.reports.basis import Basis
from payroll.reports.periods import Period
from payroll.reports.statements import ReportService
from tests.reports.test_cash_basis import (  # noqa: F401  (fixtures)
    AR,
    FEB,
    JAN,
    MAR,
    YEAR,
    _committed_fixtures,
    customer,
    income,
    items,
    make,
)

D = Decimal
CASH, ACCRUAL = Basis.CASH, Basis.ACCRUAL
A, B = "Consulting", "Training"


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr("payroll.reports.runs.company_today", lambda: date(2026, 2, 15))
    monkeypatch.setattr("payroll.frontend.ledger_routers.company_today", lambda: date(2026, 2, 15))


@pytest.fixture
def paid(make):
    invoice = make.invoice()
    payment = make.pay(date(2026, 2, 5), "500.00", (invoice, "500.00"))
    return invoice, payment


def _register(session, account, period, basis):
    return ReportService(session).account_register(
        account.id, period.start, period.end, basis=basis
    )


def _hrefs(text: str) -> list[str]:
    return [html.unescape(h) for h in re.findall(r'href="([^"]*)"', text)]


def test_register_cash_basis_lists_application_rows_for_income_account(
    session, chart, paid, owner_client
):
    invoice, payment = paid
    register = _register(session, chart[A], FEB, CASH)
    [line] = register.lines
    assert line.memo == f"Invoice #{invoice.number} paid"
    assert (line.debit, line.credit, line.balance) == (D("0"), D("300.00"), D("300.00"))
    assert (line.payment_id, line.invoice_id) == (payment.id, invoice.id)
    assert line.line_no is None and line.source_type == "payment"
    assert register.basis == "cash"

    page = owner_client.get(
        f"/accounts/{chart[A].id}/register?start=2026-02-01&end=2026-02-28&basis=cash"
    )
    assert page.status_code == 200
    assert "Cash basis view" in page.text
    assert f"Invoice #{invoice.number} paid" in page.text
    assert f"/payments/{payment.id}" in _hrefs(page.text)
    assert f"/invoices/{invoice.id}" in _hrefs(page.text)
    # The invoice's own accrual line is not a cash row.
    january = _register(session, chart[A], JAN, CASH)
    assert january.lines == [] and january.closing_balance == D("0.00")


@pytest.mark.parametrize("basis", [CASH, ACCRUAL])
def test_register_total_equals_pnl_cell(session, chart, make, basis):
    first = make.invoice()
    second = make.invoice(date(2026, 2, 20), lines=((A, "250.00"),))
    paid_in_full = make.pay(date(2026, 2, 5), "700.00", (first, "700.00"))
    make.pay(date(2026, 3, 5), "100.00", (first, "100.00"))
    make.pay(date(2026, 3, 8), "250.00", (second, "250.00"))
    make.void_payment(paid_in_full, date(2026, 3, 20))
    report = ReportService(session)
    compared = 0
    for period in (JAN, FEB, MAR, YEAR, Period(date(2026, 2, 3), date(2026, 3, 10), "odd")):
        statement = report.profit_and_loss(period, basis=basis)
        for row in statement.rows:
            if row.kind != "account":
                continue
            register = report.account_register(
                row.account_id, period.start, period.end, basis=basis
            )
            net = register.closing_balance - register.opening_balance
            assert net == row.amounts[0], (basis, period.label, row.label)
            compared += 1 if row.amounts[0] != 0 else 0
    assert compared >= 5


def test_register_default_is_ledger_as_before(session, chart, paid, owner_client):
    invoice, _ = paid
    default = ReportService(session).account_register(chart[A].id, JAN.start, JAN.end)
    ledger = ReportService(session).account_register(chart[A].id, JAN.start, JAN.end, basis=None)
    assert default == ledger and default.basis == "ledger"
    [line] = default.lines  # the invoice entry's line, as posted
    assert (line.source_type, line.credit, line.line_no) == ("invoice", D("600.00"), 2)
    assert line.invoice_id is None and line.payment_id is None
    page = owner_client.get(f"/accounts/{chart[A].id}/register?start=2026-01-01&end=2026-01-31")
    assert page.status_code == 200
    assert "Cash basis view" not in page.text and "Ledger view" in page.text
    assert f"/journal/{line.entry_id}" in _hrefs(page.text)
    explicit = owner_client.get(
        f"/accounts/{chart[A].id}/register?start=2026-01-01&end=2026-01-31&basis=ledger"
    )
    assert explicit.text == page.text


def test_ar_register_cash_shows_credit_only(session, chart, make):
    invoice = make.invoice()
    make.pay(date(2026, 2, 5), "500.00")  # received, not applied
    cash = _register(session, chart[AR], YEAR, CASH)
    assert [(ln.debit, ln.credit) for ln in cash.lines] == [(D("0.00"), D("500.00"))]
    assert cash.closing_balance == D("-500.00") and cash.total_debit == D("0.00")
    accrual = _register(session, chart[AR], YEAR, ACCRUAL)
    assert [(ln.debit, ln.credit) for ln in accrual.lines] == [
        (D("1000.00"), D("0.00")),
        (D("0.00"), D("500.00")),
    ]
    assert invoice.number is not None


def test_register_opening_balance_cash_vs_accrual(session, chart, paid):
    assert _register(session, chart[A], FEB, ACCRUAL).opening_balance == D("600.00")
    assert _register(session, chart[A], FEB, CASH).opening_balance == D("0.00")
    assert _register(session, chart[A], MAR, CASH).opening_balance == D("300.00")
    assert _register(session, chart[A], MAR, ACCRUAL).opening_balance == D("600.00")
    # opening + period net = closing, on both bases
    for basis in (CASH, ACCRUAL):
        reg = _register(session, chart[A], FEB, basis)
        assert reg.closing_balance == reg.opening_balance + sum(
            (ln.credit - ln.debit for ln in reg.lines), D("0")
        )


def test_pnl_page_cells_link_with_basis(owner_client, session, chart, paid):
    sales = chart[A].id
    cash = owner_client.get(
        "/reports/profit-and-loss?preset=custom&start=2026-02-01&end=2026-02-28"
    )
    assert f"/accounts/{sales}/register?start=2026-02-01&end=2026-02-28&basis=cash" in _hrefs(
        cash.text
    )
    accrual = owner_client.get(
        "/reports/profit-and-loss?preset=custom&start=2026-01-01&end=2026-01-31&basis=accrual"
    )
    assert f"/accounts/{sales}/register?start=2026-01-01&end=2026-01-31&basis=accrual" in _hrefs(
        accrual.text
    )
    sheet = owner_client.get("/reports/balance-sheet?asOf=2026-02-15&basis=accrual")
    assert f"/accounts/{chart[AR].id}/register?start=2026-01-01&end=2026-02-15&basis=accrual" in (
        _hrefs(sheet.text)
    )
    # and the linked register shows the same figure as the statement cell
    linked = owner_client.get(
        f"/accounts/{sales}/register?start=2026-02-01&end=2026-02-28&basis=cash"
    )
    assert "$300.00" in linked.text


def test_basis_toggle_keeps_period_and_compare(owner_client, paid):
    page = owner_client.get(
        "/reports/profit-and-loss?preset=custom&start=2026-02-01&end=2026-02-28"
        "&compare=previous_period&basis=accrual"
    ).text
    assert re.search(r'name="start"[^>]*value="2026-02-01"', page)
    assert re.search(r'name="end"[^>]*value="2026-02-28"', page)
    assert re.search(r'<option value="previous_period" selected>', page)
    assert re.search(r'value="accrual"[^>]*checked', page)
    assert re.search(r'<option value="custom" selected>', page)
    # the toggle is a radio inside the picker form that submits those same fields
    form = re.search(r"<form method=\"get\".*?</form>", page, re.S).group(0)
    assert 'name="basis"' in form and 'name="compare"' in form and 'name="preset"' in form


@pytest.mark.parametrize("basis", ["weekly", "Cash", "%00", "cash,accrual"])
def test_unknown_basis_is_422(owner_client, session, chart, paid, basis):
    response = owner_client.get(f"/accounts/{chart[A].id}/register?basis={basis}")
    assert response.status_code == 422
    assert (
        owner_client.get(
            f"/payroll/api/v1/ledger/reports/account-register/{chart[A].id}"
            f"?start=2026-01-01&end=2026-12-31&basis={basis}"
        ).status_code
        == 422
    )


def test_register_json_accepts_basis(owner_client, chart, paid):
    base = f"/payroll/api/v1/ledger/reports/account-register/{chart[A].id}"
    cash = owner_client.get(f"{base}?start=2026-02-01&end=2026-02-28&basis=cash").json()
    assert cash["basis"] == "cash" and cash["lines"][0]["credit"] == "300.00"
    default = owner_client.get(f"{base}?start=2026-02-01&end=2026-02-28").json()
    assert default["basis"] == "ledger" and default["lines"] == []


@pytest.mark.parametrize("client_name", ["owner_client", "viewer_client"])
def test_application_row_links_resolve_to_payment_and_invoice(request, client_name, chart, paid):
    client = request.getfixturevalue(client_name)
    invoice, payment = paid
    page = client.get(
        f"/accounts/{chart[A].id}/register?start=2026-02-01&end=2026-02-28&basis=cash"
    )
    assert page.status_code == 200
    links = [h for h in _hrefs(page.text) if h.startswith(("/payments/", "/invoices/"))]
    assert sorted(links) == sorted([f"/payments/{payment.id}", f"/invoices/{invoice.id}"])
    for link in links:
        assert client.get(link).status_code == 200


def test_cash_register_page_marks_void_mirror_rows(session, chart, make, owner_client):
    invoice = make.invoice()
    payment = make.pay(date(2026, 2, 5), "500.00", (invoice, "500.00"))
    make.void_payment(payment, date(2026, 3, 1))
    register = _register(session, chart[A], YEAR, CASH)
    assert [(ln.credit, ln.debit, ln.is_reversal) for ln in register.lines] == [
        (D("300.00"), D("0.00"), False),
        (D("0.00"), D("300.00"), True),
    ]
    assert register.closing_balance == D("0.00")
    page = owner_client.get(
        f"/accounts/{chart[A].id}/register?start=2026-01-01&end=2026-12-31&basis=cash"
    )
    assert "Reversal" in page.text


def test_cash_line_selection_refuses_ledger_entities(session):
    """Mixing JournalLine/JournalEntry columns into a cash query would cross join."""
    for bad in (JournalLine, JournalEntry, sa.func.sum(JournalLine.debit)):
        with pytest.raises(ValueError, match="row_source"):
            queries.line_selection(bad, basis=CASH)
    queries.line_selection(JournalLine, JournalEntry, basis=ACCRUAL)  # plain join: allowed
    stmt = queries.line_selection(*queries.row_source(CASH).columns(), basis=CASH)
    # The outer query joins only the cash subquery and account; journal_* sit inside it.
    [source] = stmt.get_final_froms()
    assert source.left.name == "cash_line" and source.right.name == "account"
