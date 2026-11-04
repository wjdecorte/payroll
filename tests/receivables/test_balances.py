"""Derived invoice state (paid, balance, status) and the A/R aging report."""

# ruff: noqa: F811  (the ``chart`` fixture is re-exported by conftest and used as an argument)

from datetime import date, timedelta
from decimal import Decimal

import pytest

from payroll.ledger.models import SourceType
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft
from payroll.receivables.balances import ar_aging, invoice_states
from payroll.receivables.credits import RefundService
from payroll.receivables.deposits import DepositService
from payroll.receivables.invoices import InvoiceLineInput, InvoiceService
from payroll.receivables.models import Customer
from payroll.receivables.payments import ApplicationInput, PaymentService
from payroll.reports.periods import InvalidPeriodError
from tests.ledger.conftest import chart  # noqa: F401

D = Decimal
DAY = date(2026, 3, 1)


@pytest.fixture(autouse=True)
def _committed_fixtures(session, owner_user, customer, item, chart):
    """Commit the fixture rows: a service rollback must not wipe them mid-test."""
    session.commit()


def _invoice(session, owner_user, customer, item, price="100.00", day=DAY, terms=30):
    service = InvoiceService(session)
    draft = service.create_draft(
        actor=owner_user,
        customer_id=customer.id,
        invoice_date=day,
        terms_days=terms,
        lines=[InvoiceLineInput(item.id, "Work", D("1"), D(price))],
    )
    return service.issue(draft.id, actor=owner_user)


def _pay(session, owner_user, customer, chart, amount="100.00", day=DAY, apply_to=()):
    return PaymentService(session).record(
        actor=owner_user,
        customer_id=customer.id,
        received_date=day,
        amount=D(amount),
        deposit_account_id=chart["Undeposited Funds"].id,
        method="check",
        applications=[ApplicationInput(i.id, D(a)) for i, a in apply_to],
    )


def _state(session, invoice, as_of):
    (found,) = [s for s in invoice_states(session, as_of) if s.invoice.id == invoice.id]
    return found


def test_status_open_partial_paid_overdue_void_draft(session, owner_user, customer, item, chart):
    open_inv = _invoice(session, owner_user, customer, item, "100.00", DAY, terms=60)
    partial = _invoice(session, owner_user, customer, item, "100.00", DAY, terms=60)
    paid = _invoice(session, owner_user, customer, item, "100.00", DAY, terms=60)
    overdue = _invoice(session, owner_user, customer, item, "100.00", DAY, terms=10)
    voided = _invoice(session, owner_user, customer, item, "100.00", DAY, terms=60)
    InvoiceService(session).void(voided.id, actor=owner_user, void_date=date(2026, 3, 2))
    draft = InvoiceService(session).create_draft(
        actor=owner_user,
        customer_id=customer.id,
        invoice_date=DAY,
        lines=[InvoiceLineInput(item.id, "Work", D("1"), D("100.00"))],
    )
    _pay(session, owner_user, customer, chart, "40.00", apply_to=[(partial, "40.00")])
    _pay(session, owner_user, customer, chart, "100.00", apply_to=[(paid, "100.00")])
    as_of = date(2026, 3, 20)

    states = {s.invoice.id: s for s in invoice_states(session, as_of, include_inactive=True)}
    assert (states[open_inv.id].status, states[open_inv.id].paid) == ("open", D("0.00"))
    assert (states[partial.id].status, states[partial.id].paid, states[partial.id].balance) == (
        "partial",
        D("40.00"),
        D("60.00"),
    )
    assert (states[paid.id].status, states[paid.id].balance) == ("paid", D("0.00"))
    assert (states[overdue.id].status, states[overdue.id].days_overdue) == ("overdue", 9)
    assert states[voided.id].status == "void"
    assert states[draft.id].status == "draft"
    # aging view: neither the draft nor the voided invoice exists
    assert {s.invoice.id for s in invoice_states(session, as_of)} == {
        open_inv.id,
        partial.id,
        paid.id,
        overdue.id,
    }


def test_partial_and_overdue_precedence(session, owner_user, customer, item, chart):
    inv = _invoice(session, owner_user, customer, item, "100.00", DAY, terms=10)
    _pay(session, owner_user, customer, chart, "40.00", apply_to=[(inv, "40.00")])
    assert _state(session, inv, DAY + timedelta(days=10)).status == "partial"
    late = _state(session, inv, DAY + timedelta(days=11))
    assert (late.status, late.paid, late.balance) == ("overdue", D("40.00"), D("60.00"))


def test_due_today_is_not_overdue_but_next_day_is(session, owner_user, customer, item):
    inv = _invoice(session, owner_user, customer, item, terms=30)
    due = inv.due_date
    today = _state(session, inv, due)
    assert (today.status, today.days_overdue) == ("open", 0)
    tomorrow = _state(session, inv, due + timedelta(days=1))
    assert (tomorrow.status, tomorrow.days_overdue) == ("overdue", 1)


def test_status_as_of_before_invoice_date_excludes_invoice(session, owner_user, customer, item):
    inv = _invoice(session, owner_user, customer, item)
    assert [s.invoice.id for s in invoice_states(session, DAY - timedelta(days=1))] == []
    assert _state(session, inv, DAY).status == "open"


def test_status_as_of_before_payment_date_shows_open(session, owner_user, customer, item, chart):
    inv = _invoice(session, owner_user, customer, item)
    _pay(session, owner_user, customer, chart, day=date(2026, 3, 10), apply_to=[(inv, "100.00")])
    assert _state(session, inv, date(2026, 3, 9)).status == "open"
    assert _state(session, inv, date(2026, 3, 9)).paid == D("0.00")
    assert _state(session, inv, date(2026, 3, 10)).status == "paid"


def test_voided_invoice_excluded_after_void_date_and_included_before(
    session, owner_user, customer, item
):
    inv = _invoice(session, owner_user, customer, item)
    InvoiceService(session).void(inv.id, actor=owner_user, void_date=date(2026, 3, 5))
    assert [s.invoice.id for s in invoice_states(session, date(2026, 3, 4))] == [inv.id]
    assert _state(session, inv, date(2026, 3, 4)).status == "open"
    assert invoice_states(session, date(2026, 3, 5)) == []
    assert invoice_states(session, date(2026, 3, 6)) == []


def test_voided_payment_stops_counting_on_its_void_date(session, owner_user, customer, item, chart):
    inv = _invoice(session, owner_user, customer, item)
    pay = _pay(session, owner_user, customer, chart, apply_to=[(inv, "100.00")])
    PaymentService(session).void(pay.id, actor=owner_user, void_date=date(2026, 3, 8))
    assert _state(session, inv, date(2026, 3, 7)).status == "paid"
    on_void_day = _state(session, inv, date(2026, 3, 8))
    assert (on_void_day.status, on_void_day.paid) == ("open", D("0.00"))
    assert _state(session, inv, date(2026, 3, 20)).balance == D("100.00")


def test_recognized_date_after_as_of_is_not_counted(session, owner_user, customer, item, chart):
    # payment dated before the invoice: recognized on the invoice date
    pay = _pay(session, owner_user, customer, chart, day=date(2026, 2, 20))
    inv = _invoice(session, owner_user, customer, item, day=date(2026, 3, 1))
    PaymentService(session).apply(
        pay.id, actor=owner_user, applications=[ApplicationInput(inv.id, D("100.00"))]
    )
    assert _state(session, inv, date(2026, 3, 1)).status == "paid"
    assert [s for s in invoice_states(session, date(2026, 2, 25))] == []


@pytest.mark.parametrize(
    ("days_late", "bucket"),
    [(0, "current"), (1, "d1_30"), (30, "d1_30"), (31, "d31_60"), (60, "d31_60"),
     (61, "d61_90"), (90, "d61_90"), (91, "d91_plus")],
)  # fmt: skip
def test_buckets_boundaries(session, owner_user, customer, item, days_late, bucket):
    inv = _invoice(session, owner_user, customer, item, "100.00", DAY, terms=10)
    report = ar_aging(session, inv.due_date + timedelta(days=days_late))
    (row,) = report.rows
    expected = {"current": 0, "d1_30": 0, "d31_60": 0, "d61_90": 0, "d91_plus": 0}
    expected[bucket] = D("100.00")
    got = {k: getattr(row, k) for k in expected}
    assert got == {k: (v or D("0.00")) for k, v in expected.items()}
    assert row.total == D("100.00")
    assert report.totals.total == D("100.00")


def test_aging_rows_totals_and_open_invoice_detail(session, owner_user, customer, item, chart):
    other = Customer(name="Zed LLC")
    session.add(other)
    session.commit()
    a1 = _invoice(session, owner_user, customer, item, "100.00", DAY, terms=10)
    a2 = _invoice(session, owner_user, customer, item, "50.00", DAY, terms=60)
    z1 = _invoice(session, owner_user, other, item, "70.00", DAY, terms=10)
    _pay(session, owner_user, customer, chart, "30.00", apply_to=[(a1, "30.00")])
    report = ar_aging(session, date(2026, 3, 25))
    by_name = {r.customer_name: r for r in report.rows}
    assert [r.customer_name for r in report.rows] == ["Acme Corp", "Zed LLC"]
    assert (by_name["Acme Corp"].d1_30, by_name["Acme Corp"].current) == (D("70.00"), D("50.00"))
    assert by_name["Zed LLC"].d1_30 == D("70.00")
    assert report.totals.total == D("190.00")
    assert report.totals.d1_30 == D("140.00")
    assert {s.invoice.id for s in report.invoices} == {a1.id, a2.id, z1.id}
    assert all(s.balance > 0 for s in report.invoices)


def test_customer_with_only_credit_shows_credit_and_zero_invoices(
    session, owner_user, customer, chart
):
    _pay(session, owner_user, customer, chart, "75.00")
    report = ar_aging(session, date(2026, 3, 10))
    (row,) = report.rows
    assert (row.customer_name, row.total, row.credit) == ("Acme Corp", D("0.00"), D("75.00"))
    assert report.totals.credit == D("75.00")
    assert report.invoices == []
    assert report.ledger_ar_balance == D("-75.00")
    assert report.difference == D("0.00") and all(i.ok for i in report.invariants)


def test_subledger_matches_ledger_for_invoices_payments_refunds_and_deposits(
    session, owner_user, customer, item, chart
):
    i1 = _invoice(session, owner_user, customer, item, "100.00", DAY)
    i2 = _invoice(session, owner_user, customer, item, "250.00", DAY)
    p1 = _pay(session, owner_user, customer, chart, "100.00", apply_to=[(i1, "100.00")])
    _pay(session, owner_user, customer, chart, "300.00", apply_to=[(i2, "100.00")])
    DepositService(session).create(
        actor=owner_user,
        deposit_date=date(2026, 3, 5),
        bank_account_id=chart["Business Checking"].id,
        payment_ids=[p1.id],
    )
    RefundService(session).record(
        actor=owner_user,
        customer_id=customer.id,
        refund_date=date(2026, 3, 6),
        amount=D("50.00"),
        bank_account_id=chart["Business Checking"].id,
        method="check",
    )
    report = ar_aging(session, date(2026, 3, 31))
    # invoices 350 - applied 200 = 150 owed; credit 300 - 100 - 50 = 150
    assert report.totals.total == D("150.00") and report.totals.credit == D("150.00")
    assert report.ledger_ar_balance == D("0.00")
    assert report.difference == D("0.00")
    (inv,) = report.invariants
    assert (inv.name, inv.ok) == ("ar_subledger_matches_ledger", True)


def test_manual_journal_to_ar_reported_as_difference_not_raised(
    session, owner_user, customer, item, chart
):
    _invoice(session, owner_user, customer, item, "100.00", DAY)
    LedgerPostingService(session).post(
        EntryDraft(
            date(2026, 3, 2),
            [
                LineDraft(chart["Accounts Receivable"].id, debit=D("25.00")),
                LineDraft(chart["Services Revenue"].id, credit=D("25.00")),
            ],
            memo="manual adjustment",
            source_type=SourceType.MANUAL,
        ),
        actor=owner_user,
    )
    session.commit()
    report = ar_aging(session, date(2026, 3, 31))
    assert report.ledger_ar_balance == D("125.00")
    assert report.difference == D("-25.00")
    (inv,) = report.invariants
    assert inv.ok is False and "25.00" in inv.detail


def test_empty_books_aging_is_zero_and_ok(session):
    report = ar_aging(session, date(2026, 3, 31))
    assert report.rows == [] and report.invoices == []
    assert report.totals.total == D("0.00") and report.totals.credit == D("0.00")
    assert report.ledger_ar_balance == D("0.00") and report.difference == D("0.00")
    assert [i.ok for i in report.invariants] == [True]


def test_aging_as_of_in_the_past_matches_ledger_as_of_then(
    session, owner_user, customer, item, chart
):
    inv = _invoice(session, owner_user, customer, item, "100.00", DAY)
    pay = _pay(session, owner_user, customer, chart, "100.00", day=date(2026, 3, 10),
               apply_to=[(inv, "100.00")])  # fmt: skip
    PaymentService(session).void(pay.id, actor=owner_user, void_date=date(2026, 3, 20))
    expectations = {
        date(2026, 2, 28): (D("0.00"), D("0.00")),
        date(2026, 3, 5): (D("100.00"), D("100.00")),
        date(2026, 3, 15): (D("0.00"), D("0.00")),
        date(2026, 3, 25): (D("100.00"), D("100.00")),
    }
    for as_of, (owed, ledger) in expectations.items():
        report = ar_aging(session, as_of)
        assert (report.totals.total, report.ledger_ar_balance) == (owed, ledger), as_of
        assert report.difference == D("0.00"), as_of


def test_unsupported_as_of_is_refused(session):
    with pytest.raises(InvalidPeriodError):
        ar_aging(session, date(1800, 1, 1))
