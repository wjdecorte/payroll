"""Void / replace reversal memos name the document, never the journal entry number."""

# ruff: noqa: F811  (the ``chart`` fixture is re-exported by conftest and used as an argument)

from datetime import date
from decimal import Decimal

import pytest
from sqlmodel import select

from payroll.ledger.models import JournalEntry
from payroll.receivables.credits import RefundService
from payroll.receivables.deposits import DepositService
from payroll.receivables.invoices import InvoiceLineInput, InvoiceService
from payroll.receivables.payments import PaymentService
from tests.ledger.conftest import chart  # noqa: F401

D = Decimal
DAY = date(2026, 3, 1)
LATER = date(2026, 3, 20)


@pytest.fixture(autouse=True)
def _committed_fixtures(session, owner_user, customer, item, chart):
    """Commit the fixture rows: a service rollback must not wipe them mid-test."""
    session.commit()


def _reversals(session, original_entry_id):
    return session.exec(
        select(JournalEntry).where(JournalEntry.reverses_entry_id == original_entry_id)
    ).all()


def _invoice(session, owner_user, customer, item):
    service = InvoiceService(session)
    draft = service.create_draft(
        actor=owner_user,
        customer_id=customer.id,
        invoice_date=DAY,
        lines=[InvoiceLineInput(item.id, "Work", D("1"), D("100.00"))],
    )
    return service.issue(draft.id, actor=owner_user)


def _payment(session, owner_user, customer, chart, amount="100.00"):
    return PaymentService(session).record(
        actor=owner_user,
        customer_id=customer.id,
        received_date=DAY,
        amount=D(amount),
        deposit_account_id=chart["Undeposited Funds"].id,
        method="check",
    )


def test_invoice_void_memo_uses_the_invoice_number_not_the_entry_number(
    session, owner_user, customer, item
):
    invoice = _invoice(session, owner_user, customer, item)
    entry_id = invoice.entry_id
    InvoiceService(session).void(invoice.id, actor=owner_user, void_date=LATER)
    (reversal,) = _reversals(session, entry_id)
    assert reversal.memo == f"Void of invoice #{invoice.number}"


def test_caller_memo_wins_on_every_void(session, owner_user, customer, item, chart):
    invoice = _invoice(session, owner_user, customer, item)
    payment = _payment(session, owner_user, customer, chart)
    InvoiceService(session).void(invoice.id, actor=owner_user, void_date=LATER, memo="Duplicate")
    PaymentService(session).void(payment.id, actor=owner_user, void_date=LATER, memo="Bounced")
    assert _reversals(session, invoice.entry_id)[0].memo == "Duplicate"
    assert _reversals(session, payment.entry_id)[0].memo == "Bounced"


def test_invoice_replace_reversal_memo(session, owner_user, customer, item):
    invoice = _invoice(session, owner_user, customer, item)
    old_entry = invoice.entry_id
    InvoiceService(session).replace(
        invoice.id,
        actor=owner_user,
        customer_id=customer.id,
        invoice_date=DAY,
        lines=[InvoiceLineInput(item.id, "Work", D("1"), D("120.00"))],
    )
    (reversal,) = _reversals(session, old_entry)
    assert reversal.memo == f"Replaced invoice #{invoice.number}"


def test_payment_void_memo(session, owner_user, customer, chart):
    payment = _payment(session, owner_user, customer, chart)
    PaymentService(session).void(payment.id, actor=owner_user, void_date=LATER)
    (reversal,) = _reversals(session, payment.entry_id)
    assert reversal.memo == f"Void of payment 2026-03-01 - {customer.name}"


def test_deposit_void_memo(session, owner_user, customer, chart):
    payment = _payment(session, owner_user, customer, chart)
    deposit = DepositService(session).create(
        actor=owner_user,
        deposit_date=date(2026, 3, 5),
        bank_account_id=chart["Business Checking"].id,
        payment_ids=[payment.id],
    )
    DepositService(session).void(deposit.id, actor=owner_user, void_date=LATER)
    (reversal,) = _reversals(session, deposit.entry_id)
    assert reversal.memo == "Void of deposit 2026-03-05"


def test_refund_void_memo(session, owner_user, customer, chart):
    _payment(session, owner_user, customer, chart)
    refund = RefundService(session).record(
        actor=owner_user,
        customer_id=customer.id,
        refund_date=date(2026, 3, 10),
        amount=D("40.00"),
        bank_account_id=chart["Business Checking"].id,
        method="check",
    )
    RefundService(session).void(refund.id, actor=owner_user, void_date=LATER)
    (reversal,) = _reversals(session, refund.entry_id)
    assert reversal.memo == f"Void of refund 2026-03-10 - {customer.name}"


def test_default_memo_stays_within_the_limit(session, owner_user, customer, chart):
    customer.name = "N" * 200
    session.add(customer)
    session.commit()
    payment = _payment(session, owner_user, customer, chart)
    PaymentService(session).void(payment.id, actor=owner_user, void_date=LATER)
    (reversal,) = _reversals(session, payment.entry_id)
    assert reversal.memo.startswith("Void of payment 2026-03-01 - NNN")
    assert len(reversal.memo) <= 500
