"""Issuing, replacing and voiding invoices: posting to the ledger."""

# ruff: noqa: F811  (the ``chart`` fixture is re-exported by conftest and used as an argument)

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text as sql_text
from sqlmodel import col, func, select

from payroll.audit.models import AuditLog
from payroll.auth.permissions import PermissionDeniedError
from payroll.ledger.accounts import AccountService
from payroll.ledger.exceptions import InvalidAccountError, PeriodLockedError
from payroll.ledger.models import Account, AccountType, JournalEntry, JournalLine, SystemRole
from payroll.receivables.exceptions import (
    ARNotConfiguredError,
    InvalidInvoiceError,
    InvoiceStateError,
)
from payroll.receivables.invoices import (
    InvoiceLineInput,
    InvoiceService,
    live_applied,
)
from payroll.receivables.models import (
    Invoice,
    Item,
    Payment,
    PaymentApplication,
    ReceivablesSettings,
)
from payroll.receivables.settings import ReceivablesSettingsService
from tests.conftest import make_user
from tests.ledger.conftest import chart  # noqa: F401

D = Decimal
DAY = date(2026, 3, 1)


@pytest.fixture(autouse=True)
def _committed_fixtures(session, owner_user, customer, item):
    """Commit the fixture rows: a service rollback must not wipe them mid-test."""
    session.commit()


def _line(item, qty="1", price=None, description="Work"):
    return InvoiceLineInput(
        item_id=item.id,
        description=description,
        qty=D(qty),
        unit_price=D(price) if price is not None else item.unit_price,
    )


def _draft(session, owner_user, customer, item, **kwargs):
    kwargs.setdefault("invoice_date", DAY)
    kwargs.setdefault("lines", [_line(item, "2")])
    return InvoiceService(session).create_draft(actor=owner_user, customer_id=customer.id, **kwargs)


def _issued(session, owner_user, customer, item, **kwargs):
    draft = _draft(session, owner_user, customer, item, **kwargs)
    return InvoiceService(session).issue(draft.id, actor=owner_user)


def _entry_lines(session, entry_id):
    stmt = (
        select(JournalLine)
        .where(JournalLine.entry_id == entry_id)
        .order_by(col(JournalLine.line_no))
    )
    return list(session.exec(stmt).all())


def _ar_balance(session, chart):
    return AccountService(session).balances().get(chart["Accounts Receivable"].id, D("0.00"))


def _set_lock(session, lock_date):
    session.execute(
        sql_text("UPDATE period_lock SET lock_date = :d WHERE id = 1"), {"d": lock_date}
    )
    session.commit()


def _apply(session, invoice, customer, chart, amount="10.00", status="posted"):
    payment = Payment(
        customer_id=customer.id,
        received_date=DAY,
        amount=D("100.00"),
        deposit_account_id=chart["Business Checking"].id,
        status=status,
    )
    session.add(payment)
    session.flush()
    session.add(
        PaymentApplication(
            payment_id=payment.id,
            invoice_id=invoice.id,
            amount=D(amount),
            recognized_date=DAY,
            ar_account_id=chart["Accounts Receivable"].id,
        )
    )
    session.commit()
    return payment


def _audit_actions(session):
    return [r.action for r in session.exec(select(AuditLog).order_by(col(AuditLog.id))).all()]


def test_issue_posts_dr_ar_cr_income_and_assigns_1001(session, owner_user, customer, item, chart):
    invoice = _issued(session, owner_user, customer, item)
    assert (invoice.status, invoice.number) == ("open", 1001)
    entry = session.get(JournalEntry, invoice.entry_id)
    assert (entry.entry_date, entry.source_type, entry.source_id) == (
        DAY,
        "invoice",
        str(invoice.id),
    )
    assert entry.memo == "Invoice #1001 Acme Corp"
    lines = _entry_lines(session, entry.id)
    assert [(ln.account_id, ln.debit, ln.credit) for ln in lines] == [
        (chart["Accounts Receivable"].id, D("300.00"), D("0.00")),
        (chart["Services Revenue"].id, D("0.00"), D("300.00")),
    ]
    assert lines[1].memo == "Work"
    assert _ar_balance(session, chart) == D("300.00")
    assert "invoice.issue" in _audit_actions(session)


def test_issue_two_lines_same_income_account_makes_one_credit_line(
    session, owner_user, customer, item, chart
):
    invoice = _issued(
        session,
        owner_user,
        customer,
        item,
        lines=[_line(item, "1", "100.00", "First"), _line(item, "2", "50.00", "Second")],
    )
    lines = _entry_lines(session, invoice.entry_id)
    assert len(lines) == 2
    assert lines[1].credit == D("200.00")
    assert lines[1].memo == "First; Second"


def test_issue_lines_to_two_accounts_makes_two_credit_lines_summing_to_total(
    session, owner_user, customer, item, chart
):
    other_income = AccountService(session).create(
        actor=owner_user, name="Other Income", type=AccountType.INCOME, number="4500"
    )
    other = Item(name="Training", unit_price=D("40.00"), income_account_id=other_income.id)
    session.add(other)
    session.commit()
    invoice = _issued(
        session,
        owner_user,
        customer,
        item,
        lines=[_line(other, "1", "40.00", "Class"), _line(item, "1", "100.00", "Work")],
    )
    lines = _entry_lines(session, invoice.entry_id)
    assert len(lines) == 3
    credits = lines[1:]
    assert sum(ln.credit for ln in credits) == D("140.00") == lines[0].debit
    numbers = [session.get(Account, ln.account_id).number for ln in credits]
    assert numbers == sorted(n or "~" for n in numbers)
    assert {ln.account_id for ln in credits} == {
        other_income.id,
        chart["Services Revenue"].id,
    }


def test_issue_refreshes_income_account_snapshot_from_item(
    session, owner_user, customer, item, chart
):
    other_income = AccountService(session).create(
        actor=owner_user, name="Other Income", type=AccountType.INCOME
    )
    draft = _draft(session, owner_user, customer, item)
    item.income_account_id = other_income.id
    session.add(item)
    session.commit()
    invoice = InvoiceService(session).issue(draft.id, actor=owner_user)
    assert InvoiceService(session).lines(invoice.id)[0].income_account_id == other_income.id
    assert _entry_lines(session, invoice.entry_id)[1].account_id == other_income.id


def test_issue_rechecks_active_customer_and_item(session, owner_user, customer, item):
    draft = _draft(session, owner_user, customer, item)
    item.is_active = False
    session.add(item)
    session.commit()
    with pytest.raises(InvalidInvoiceError) as caught:
        InvoiceService(session).issue(draft.id, actor=owner_user)
    assert caught.value.message == "Line 1: the item is inactive"
    session.refresh(item)
    item.is_active = True
    customer.is_active = False
    session.add_all([item, customer])
    session.commit()
    with pytest.raises(InvalidInvoiceError) as caught:
        InvoiceService(session).issue(draft.id, actor=owner_user)
    assert caught.value.message == "The customer is inactive"
    assert session.get(Invoice, draft.id).status == "draft"


def test_second_issue_gets_1002_and_numbers_follow_next_number_setting(
    session, owner_user, customer, item
):
    first = _issued(session, owner_user, customer, item)
    second = _issued(session, owner_user, customer, item)
    assert (first.number, second.number) == (1001, 1002)
    ReceivablesSettingsService(session).set_next_invoice_number(1043, actor=owner_user)
    third = _issued(session, owner_user, customer, item)
    fourth = _issued(session, owner_user, customer, item)
    assert (third.number, fourth.number) == (1043, 1044)
    assert session.get(ReceivablesSettings, 1).next_invoice_no == 1045


def test_issue_requires_ar_role_bound(session, owner_user, customer, item, chart):
    draft = _draft(session, owner_user, customer, item)
    AccountService(session).bind_role(SystemRole.ACCOUNTS_RECEIVABLE, None, actor=owner_user)
    with pytest.raises(ARNotConfiguredError) as caught:
        InvoiceService(session).issue(draft.id, actor=owner_user)
    assert caught.value.message == "Bind Accounts receivable in Settings > Ledger first"
    assert session.get(Invoice, draft.id).status == "draft"


def test_issue_in_locked_period_rejected_and_burns_no_number(session, owner_user, customer, item):
    _set_lock(session, date(2026, 3, 31))
    draft = _draft(session, owner_user, customer, item)
    with pytest.raises(PeriodLockedError):
        InvoiceService(session).issue(draft.id, actor=owner_user)
    invoice = session.get(Invoice, draft.id)
    assert (invoice.status, invoice.number, invoice.entry_id) == ("draft", None, None)
    assert session.get(ReceivablesSettings, 1).next_invoice_no == 1001
    later = _draft(session, owner_user, customer, item, invoice_date=date(2026, 4, 2))
    assert InvoiceService(session).issue(later.id, actor=owner_user).number == 1001


def test_issue_twice_rejected(session, owner_user, customer, item):
    invoice = _issued(session, owner_user, customer, item)
    with pytest.raises(InvoiceStateError):
        InvoiceService(session).issue(invoice.id, actor=owner_user)
    assert session.get(ReceivablesSettings, 1).next_invoice_no == 1002


def test_replace_reverses_old_entry_and_posts_new_same_number(
    session, owner_user, customer, item, chart
):
    invoice = _issued(session, owner_user, customer, item)
    old_entry_id = invoice.entry_id
    replaced = InvoiceService(session).replace(
        invoice.id,
        actor=owner_user,
        customer_id=customer.id,
        invoice_date=DAY,
        lines=[_line(item, "1", "75.00", "Smaller")],
    )
    assert (replaced.number, replaced.status, replaced.total) == (1001, "open", D("75.00"))
    assert replaced.entry_id != old_entry_id
    old = session.get(JournalEntry, old_entry_id)
    assert old.status == "void"
    entries = list(
        session.exec(select(JournalEntry).where(JournalEntry.source_id == str(invoice.id))).all()
    )
    assert len(entries) == 3
    reversal = next(e for e in entries if e.reverses_entry_id == old_entry_id)
    assert reversal.entry_date == DAY
    live = [e for e in entries if e.status == "posted" and e.reverses_entry_id is None]
    assert [e.id for e in live] == [replaced.entry_id]
    assert _ar_balance(session, chart) == D("75.00")
    assert [ln.description for ln in InvoiceService(session).lines(invoice.id)] == ["Smaller"]
    assert "invoice.replace" in _audit_actions(session)


def test_replace_with_new_date_moves_income(session, owner_user, customer, item, chart):
    invoice = _issued(session, owner_user, customer, item)
    replaced = InvoiceService(session).replace(
        invoice.id,
        actor=owner_user,
        customer_id=customer.id,
        invoice_date=date(2026, 5, 10),
        lines=[_line(item, "2")],
    )
    assert session.get(JournalEntry, replaced.entry_id).entry_date == date(2026, 5, 10)
    assert replaced.due_date == date(2026, 6, 9)
    assert _ar_balance(session, chart) == D("300.00")


def test_replace_refused_with_live_application(session, owner_user, customer, item, chart):
    invoice = _issued(session, owner_user, customer, item)
    _apply(session, invoice, customer, chart)
    assert live_applied(session, invoice.id) == D("10.00")
    with pytest.raises(InvoiceStateError):
        InvoiceService(session).replace(
            invoice.id,
            actor=owner_user,
            customer_id=customer.id,
            invoice_date=DAY,
            lines=[_line(item, "1")],
        )
    assert session.get(Invoice, invoice.id).total == D("300.00")


def test_replace_allowed_when_only_a_voided_payment_was_applied(
    session, owner_user, customer, item, chart
):
    invoice = _issued(session, owner_user, customer, item)
    _apply(session, invoice, customer, chart, status="void")
    assert live_applied(session, invoice.id) == D("0")
    replaced = InvoiceService(session).replace(
        invoice.id,
        actor=owner_user,
        customer_id=customer.id,
        invoice_date=DAY,
        lines=[_line(item, "1")],
    )
    assert replaced.total == D("150.00")


def test_replace_requires_open_invoice(session, owner_user, customer, item):
    draft = _draft(session, owner_user, customer, item)
    with pytest.raises(InvoiceStateError):
        InvoiceService(session).replace(
            draft.id,
            actor=owner_user,
            customer_id=customer.id,
            invoice_date=DAY,
            lines=[_line(item)],
        )


def test_void_posts_reversal_on_given_date_and_marks_void(
    session, owner_user, customer, item, chart
):
    invoice = _issued(session, owner_user, customer, item)
    voided = InvoiceService(session).void(
        invoice.id, actor=owner_user, void_date=date(2026, 3, 20), memo="Entered twice"
    )
    assert (voided.status, voided.void_date, voided.number) == ("void", date(2026, 3, 20), 1001)
    assert session.get(JournalEntry, invoice.entry_id).status == "void"
    reversal = session.exec(
        select(JournalEntry).where(JournalEntry.reverses_entry_id == invoice.entry_id)
    ).one()
    assert (reversal.entry_date, reversal.memo) == (date(2026, 3, 20), "Entered twice")
    assert _ar_balance(session, chart) == D("0.00")
    assert "invoice.void" in _audit_actions(session)


def test_void_refused_with_live_application(session, owner_user, customer, item, chart):
    invoice = _issued(session, owner_user, customer, item)
    _apply(session, invoice, customer, chart)
    with pytest.raises(InvoiceStateError):
        InvoiceService(session).void(invoice.id, actor=owner_user, void_date=DAY)
    assert session.get(Invoice, invoice.id).status == "open"


def test_void_in_locked_period_rejected(session, owner_user, customer, item):
    invoice = _issued(session, owner_user, customer, item)
    _set_lock(session, date(2026, 3, 31))
    with pytest.raises(PeriodLockedError):
        InvoiceService(session).void(invoice.id, actor=owner_user, void_date=date(2026, 4, 5))
    assert session.get(Invoice, invoice.id).status == "open"


def test_void_date_in_locked_period_rejected(session, owner_user, customer, item):
    invoice = _issued(session, owner_user, customer, item, invoice_date=date(2026, 4, 2))
    _set_lock(session, date(2026, 3, 31))
    with pytest.raises(PeriodLockedError):
        InvoiceService(session).void(invoice.id, actor=owner_user, void_date=date(2026, 3, 15))
    assert session.get(Invoice, invoice.id).status == "open"


def test_void_twice_rejected(session, owner_user, customer, item):
    invoice = _issued(session, owner_user, customer, item)
    InvoiceService(session).void(invoice.id, actor=owner_user, void_date=DAY)
    with pytest.raises(InvoiceStateError):
        InvoiceService(session).void(invoice.id, actor=owner_user, void_date=DAY)


def test_voided_number_is_not_reused(session, owner_user, customer, item):
    first = _issued(session, owner_user, customer, item)
    InvoiceService(session).void(first.id, actor=owner_user, void_date=DAY)
    assert _issued(session, owner_user, customer, item).number == 1002


def test_unbind_or_rebind_ar_role_refused_after_issue_but_allowed_before(
    session, owner_user, customer, item, chart
):
    accounts = AccountService(session)
    spare = accounts.create(
        actor=owner_user, name="Other Receivables", type=chart["Accounts Receivable"].type
    )
    accounts.bind_role(SystemRole.ACCOUNTS_RECEIVABLE, spare.id, actor=owner_user)
    accounts.bind_role(
        SystemRole.ACCOUNTS_RECEIVABLE, chart["Accounts Receivable"].id, actor=owner_user
    )
    _issued(session, owner_user, customer, item)
    message = "Accounts receivable cannot change after invoices have been issued"
    for target in (spare.id, None):
        with pytest.raises(InvalidAccountError) as caught:
            accounts.bind_role(SystemRole.ACCOUNTS_RECEIVABLE, target, actor=owner_user)
        assert caught.value.message == message
    # re-binding the same account and other roles stay allowed
    accounts.bind_role(
        SystemRole.ACCOUNTS_RECEIVABLE, chart["Accounts Receivable"].id, actor=owner_user
    )
    accounts.bind_role(SystemRole.UNDEPOSITED_FUNDS, None, actor=owner_user)
    assert (
        accounts.account_by_role(SystemRole.ACCOUNTS_RECEIVABLE).id
        == chart["Accounts Receivable"].id
    )


def test_voided_invoice_still_freezes_ar_role(session, owner_user, customer, item):
    invoice = _issued(session, owner_user, customer, item)
    InvoiceService(session).void(invoice.id, actor=owner_user, void_date=DAY)
    with pytest.raises(InvalidAccountError):
        AccountService(session).bind_role(SystemRole.ACCOUNTS_RECEIVABLE, None, actor=owner_user)


def test_owner_only_and_audited(session, owner_user, customer, item):
    viewer = make_user(session, role="viewer", email="viewer@example.com")
    session.commit()
    draft = _draft(session, owner_user, customer, item)
    service = InvoiceService(session)
    with pytest.raises(PermissionDeniedError):
        service.issue(draft.id, actor=viewer)
    issued = service.issue(draft.id, actor=owner_user)
    with pytest.raises(PermissionDeniedError):
        service.replace(
            issued.id,
            actor=viewer,
            customer_id=customer.id,
            invoice_date=DAY,
            lines=[_line(item)],
        )
    with pytest.raises(PermissionDeniedError):
        service.void(issued.id, actor=viewer, void_date=DAY)
    assert session.get(Invoice, issued.id).status == "open"
    service.replace(
        issued.id, actor=owner_user, customer_id=customer.id, invoice_date=DAY, lines=[_line(item)]
    )
    service.void(issued.id, actor=owner_user, void_date=DAY)
    rows = session.exec(select(AuditLog).where(col(AuditLog.entity) == "invoice")).all()
    by_action = {r.action: r for r in rows}
    assert {"invoice.create", "invoice.issue", "invoice.replace", "invoice.void"} <= set(by_action)
    assert all(r.user_id == owner_user.id for r in rows)
    assert session.exec(select(func.count()).select_from(JournalEntry)).one() == 4


def test_issue_and_replace_skip_zero_amount_income_account(
    session, owner_user, customer, item, chart
):
    free_income = AccountService(session).create(
        actor=owner_user, name="Free Income", type=AccountType.INCOME, number="4600"
    )
    free = Item(name="Freebie", unit_price=D("0.00"), income_account_id=free_income.id)
    session.add(free)
    session.commit()
    lines = [_line(free, "1", "0.00", "Gift"), _line(item, "1", "100.00", "Work")]
    invoice = _issued(session, owner_user, customer, item, lines=lines)
    entry_lines = _entry_lines(session, invoice.entry_id)
    assert [ln.account_id for ln in entry_lines] == [
        chart["Accounts Receivable"].id,
        chart["Services Revenue"].id,
    ]
    replaced = InvoiceService(session).replace(
        invoice.id,
        actor=owner_user,
        customer_id=customer.id,
        invoice_date=DAY,
        lines=[_line(free, "1", "0.00", "Gift"), _line(item, "1", "60.00", "Work")],
    )
    assert [ln.credit for ln in _entry_lines(session, replaced.entry_id)] == [D("0.00"), D("60.00")]
    assert _ar_balance(session, chart) == D("60.00")
