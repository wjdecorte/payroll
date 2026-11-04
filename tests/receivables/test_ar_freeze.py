"""The A/R role freezes once any invoice, payment or refund entry exists (void ones too)."""

# ruff: noqa: F811  (the ``chart`` fixture is re-exported by conftest and used as an argument)

from datetime import date
from decimal import Decimal

import pytest

from payroll.ledger.accounts import AccountService
from payroll.ledger.exceptions import InvalidAccountError
from payroll.ledger.models import SystemRole
from payroll.receivables.credits import RefundService
from payroll.receivables.invoices import InvoiceLineInput, InvoiceService
from payroll.receivables.payments import PaymentService
from tests.ledger.conftest import chart  # noqa: F401

D = Decimal
DAY = date(2026, 3, 10)
MESSAGE = "Accounts receivable cannot change after invoices, payments or refunds have been recorded"


@pytest.fixture(autouse=True)
def _committed_fixtures(session, owner_user, customer, item):
    """Commit the fixture rows: a service rollback must not wipe them mid-test."""
    session.commit()


@pytest.fixture
def spare(session, owner_user, chart):
    return AccountService(session).create(
        actor=owner_user, name="Other Receivables", type=chart["Accounts Receivable"].type
    )


def _pay(session, owner_user, customer, chart, amount="500.00"):
    return PaymentService(session).record(
        actor=owner_user,
        customer_id=customer.id,
        received_date=DAY,
        amount=D(amount),
        deposit_account_id=chart["Undeposited Funds"].id,
        method="check",
    )


def _assert_frozen(session, owner_user, chart, spare):
    accounts = AccountService(session)
    for target in (spare.id, None):
        with pytest.raises(InvalidAccountError) as caught:
            accounts.bind_role(SystemRole.ACCOUNTS_RECEIVABLE, target, actor=owner_user)
        assert caught.value.message == MESSAGE
    assert (
        accounts.account_by_role(SystemRole.ACCOUNTS_RECEIVABLE).id
        == chart["Accounts Receivable"].id
    )
    # the same account and other roles stay allowed
    accounts.bind_role(
        SystemRole.ACCOUNTS_RECEIVABLE, chart["Accounts Receivable"].id, actor=owner_user
    )
    accounts.bind_role(SystemRole.UNDEPOSITED_FUNDS, None, actor=owner_user)


def test_without_documents_the_ar_role_can_move_unbind_and_bind_first_time(
    session, owner_user, chart, spare
):
    accounts = AccountService(session)
    accounts.bind_role(SystemRole.ACCOUNTS_RECEIVABLE, spare.id, actor=owner_user)
    accounts.bind_role(SystemRole.ACCOUNTS_RECEIVABLE, None, actor=owner_user)
    assert accounts.account_by_role(SystemRole.ACCOUNTS_RECEIVABLE) is None
    # first-time binding (no current A/R) is allowed
    accounts.bind_role(
        SystemRole.ACCOUNTS_RECEIVABLE, chart["Accounts Receivable"].id, actor=owner_user
    )
    assert (
        accounts.account_by_role(SystemRole.ACCOUNTS_RECEIVABLE).id
        == chart["Accounts Receivable"].id
    )


def test_a_payment_with_no_invoice_freezes_the_role(session, owner_user, customer, chart, spare):
    _pay(session, owner_user, customer, chart)
    _assert_frozen(session, owner_user, chart, spare)


def test_a_refund_freezes_the_role(session, owner_user, customer, chart, spare):
    _pay(session, owner_user, customer, chart)
    RefundService(session).record(
        actor=owner_user,
        customer_id=customer.id,
        refund_date=DAY,
        amount=D("100.00"),
        bank_account_id=chart["Business Checking"].id,
        method="check",
    )
    _assert_frozen(session, owner_user, chart, spare)


def test_a_voided_payment_still_freezes_the_role(session, owner_user, customer, chart, spare):
    payment = _pay(session, owner_user, customer, chart)
    PaymentService(session).void(payment.id, actor=owner_user, void_date=DAY)
    _assert_frozen(session, owner_user, chart, spare)


def test_a_voided_invoice_still_freezes_the_role(session, owner_user, customer, item, chart, spare):
    service = InvoiceService(session)
    draft = service.create_draft(
        actor=owner_user,
        customer_id=customer.id,
        invoice_date=DAY,
        lines=[InvoiceLineInput(item.id, "Work", D("1"), D("10.00"))],
    )
    invoice = service.issue(draft.id, actor=owner_user)
    service.void(invoice.id, actor=owner_user, void_date=DAY)
    _assert_frozen(session, owner_user, chart, spare)


def test_a_draft_invoice_does_not_freeze_the_role(
    session, owner_user, customer, item, chart, spare
):
    InvoiceService(session).create_draft(
        actor=owner_user,
        customer_id=customer.id,
        invoice_date=DAY,
        lines=[InvoiceLineInput(item.id, "Work", D("1"), D("10.00"))],
    )
    AccountService(session).bind_role(SystemRole.ACCOUNTS_RECEIVABLE, spare.id, actor=owner_user)
