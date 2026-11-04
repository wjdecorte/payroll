"""Customer credit (as-of) and refunds: posting, validation, voids, audit."""

# ruff: noqa: F811  (the ``chart`` fixture is re-exported by conftest and used as an argument)

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text as sql_text
from sqlmodel import col, func, select

from payroll.audit.models import AuditLog
from payroll.auth.permissions import PermissionDeniedError
from payroll.ledger.accounts import AccountService
from payroll.ledger.exceptions import PeriodLockedError
from payroll.ledger.models import JournalEntry, JournalLine
from payroll.receivables.credits import RefundService, customer_credits
from payroll.receivables.exceptions import (
    ARNotConfiguredError,
    InvalidRefundError,
    OverApplicationError,
    PaymentStateError,
    RefundNotFoundError,
    RefundStateError,
)
from payroll.receivables.invoices import InvoiceLineInput, InvoiceService
from payroll.receivables.models import Customer, CustomerRefund, PaymentApplication
from payroll.receivables.payments import ApplicationInput, PaymentService
from tests.conftest import make_user
from tests.ledger.conftest import chart  # noqa: F401

D = Decimal
INVOICE_DAY = date(2026, 3, 1)
PAY_DAY = date(2026, 3, 10)
REFUND_DAY = date(2026, 3, 20)


@pytest.fixture(autouse=True)
def _committed_fixtures(session, owner_user, customer, item):
    """Commit the fixture rows: a service rollback must not wipe them mid-test."""
    session.commit()


def _pay(session, owner_user, customer, chart, amount="100.00", day=PAY_DAY, apply_to=None):
    apps = [ApplicationInput(i.id, D(a)) for i, a in (apply_to or [])]
    return PaymentService(session).record(
        actor=owner_user,
        customer_id=customer.id,
        received_date=day,
        amount=D(amount),
        deposit_account_id=chart["Undeposited Funds"].id,
        method="check",
        applications=apps,
    )


def _invoice(session, owner_user, customer, item, price="100.00", day=INVOICE_DAY):
    service = InvoiceService(session)
    draft = service.create_draft(
        actor=owner_user,
        customer_id=customer.id,
        invoice_date=day,
        lines=[InvoiceLineInput(item.id, "Work", D("1"), D(price))],
    )
    return service.issue(draft.id, actor=owner_user)


def _refund(session, owner_user, customer, chart, amount="40.00", day=REFUND_DAY, **kw):
    kw.setdefault("method", "check")
    kw.setdefault("bank_account_id", chart["Business Checking"].id)
    return RefundService(session).record(
        actor=owner_user, customer_id=customer.id, refund_date=day, amount=D(amount), **kw
    )


def _credit(session, customer, as_of=None):
    return customer_credits(session, as_of, customer.id).get(customer.id, D("0.00"))


def _lines(session, entry_id):
    stmt = (
        select(JournalLine)
        .where(JournalLine.entry_id == entry_id)
        .order_by(col(JournalLine.line_no))
    )
    return list(session.exec(stmt).all())


def _entry_count(session):
    return session.exec(select(func.count()).select_from(JournalEntry)).one()


def _audit(session, action):
    return session.exec(select(AuditLog).where(AuditLog.action == action)).all()


def test_credit_is_overpayment_minus_applications_minus_refunds(
    session, owner_user, customer, item, chart
):
    invoice = _invoice(session, owner_user, customer, item, "100.00")
    _pay(session, owner_user, customer, chart, "130.00", apply_to=[(invoice, "100.00")])
    assert _credit(session, customer) == D("30.00")
    assert customer_credits(session) == {customer.id: D("30.00")}
    _refund(session, owner_user, customer, chart, "12.50")
    assert _credit(session, customer) == D("17.50")
    _refund(session, owner_user, customer, chart, "17.50")
    # a zero balance is omitted
    assert customer_credits(session) == {}
    assert customer_credits(session, customer_id=customer.id) == {}


def test_credit_per_customer_and_filter(session, owner_user, customer, chart):
    other = Customer(name="Other Co")
    session.add(other)
    session.commit()
    _pay(session, owner_user, customer, chart, "50.00")
    _pay(session, owner_user, other, chart, "20.00")
    assert customer_credits(session) == {customer.id: D("50.00"), other.id: D("20.00")}
    assert customer_credits(session, customer_id=other.id) == {other.id: D("20.00")}


def test_credit_as_of_dates(session, owner_user, customer, item, chart):
    invoice = _invoice(session, owner_user, customer, item, "100.00")
    payment = _pay(session, owner_user, customer, chart, "150.00", day=PAY_DAY)
    PaymentService(session).apply(
        payment.id,
        actor=owner_user,
        applications=[ApplicationInput(invoice.id, D("100.00"))],
    )
    assert _credit(session, customer, date(2026, 3, 9)) == D("0.00")  # before receipt
    assert customer_credits(session, date(2026, 3, 9)) == {}
    assert _credit(session, customer, PAY_DAY) == D("50.00")  # application recognized the same day
    # recognized = max(received, invoice date) = PAY_DAY, so it already counts on PAY_DAY
    assert _credit(session, customer, date(2026, 12, 31)) == D("50.00")


def test_credit_as_of_before_application_recognized(session, owner_user, customer, item, chart):
    # the invoice is dated after the payment, so the application is recognized later
    payment = _pay(session, owner_user, customer, chart, "150.00", day=date(2026, 2, 1))
    invoice = _invoice(session, owner_user, customer, item, "100.00")  # dated 2026-03-01
    PaymentService(session).apply(
        payment.id, actor=owner_user, applications=[ApplicationInput(invoice.id, D("100.00"))]
    )
    assert _credit(session, customer, date(2026, 2, 15)) == D("150.00")
    assert _credit(session, customer, date(2026, 2, 28)) == D("150.00")
    assert _credit(session, customer, INVOICE_DAY) == D("50.00")
    assert _credit(session, customer) == D("50.00")


def test_voided_payment_has_no_credit_but_counts_before_its_void_date(
    session, owner_user, customer, chart
):
    payment = _pay(session, owner_user, customer, chart, "80.00")
    PaymentService(session).void(payment.id, actor=owner_user, void_date=date(2026, 3, 15))
    assert _credit(session, customer) == D("0.00")
    assert customer_credits(session) == {}
    assert _credit(session, customer, date(2026, 3, 14)) == D("80.00")
    assert _credit(session, customer, date(2026, 3, 15)) == D("0.00")  # voided on that date
    assert _credit(session, customer, date(2026, 3, 9)) == D("0.00")  # not yet received


def test_voided_refund_counts_only_before_its_void_date(session, owner_user, customer, chart):
    _pay(session, owner_user, customer, chart, "100.00")
    refund = _refund(session, owner_user, customer, chart, "40.00", day=date(2026, 3, 12))
    RefundService(session).void(refund.id, actor=owner_user, void_date=date(2026, 3, 18))
    assert _credit(session, customer, date(2026, 3, 11)) == D("100.00")
    assert _credit(session, customer, date(2026, 3, 12)) == D("60.00")
    assert _credit(session, customer, date(2026, 3, 17)) == D("60.00")
    assert _credit(session, customer, date(2026, 3, 18)) == D("100.00")
    assert _credit(session, customer) == D("100.00")


def test_refund_posts_dr_ar_cr_bank_and_reduces_credit(session, owner_user, customer, chart):
    _pay(session, owner_user, customer, chart, "100.00")
    ar, bank = chart["Accounts Receivable"], chart["Business Checking"]
    ar_before = AccountService(session).balances().get(ar.id, D("0.00"))
    bank_before = AccountService(session).balances().get(bank.id, D("0.00"))
    assert ar_before == D("-100.00")

    refund = _refund(session, owner_user, customer, chart, "40.00", reference="R-9", memo="oops")

    entry = session.get(JournalEntry, refund.entry_id)
    assert (entry.entry_date, entry.source_type, entry.source_id) == (
        REFUND_DAY,
        "refund",
        str(refund.id),
    )
    lines = _lines(session, entry.id)
    assert [(ln.account_id, ln.debit, ln.credit) for ln in lines] == [
        (ar.id, D("40.00"), D("0.00")),
        (bank.id, D("0.00"), D("40.00")),
    ]
    balances = AccountService(session).balances()
    assert balances[ar.id] == D("-60.00")  # the credit sitting in A/R shrank by the refund
    assert balances[bank.id] == bank_before - D("40.00")
    assert (refund.status, refund.method, refund.reference, refund.memo) == (
        "posted",
        "check",
        "R-9",
        "oops",
    )
    assert _credit(session, customer) == D("60.00")
    assert [r.id for r in RefundService(session).list(customer_id=customer.id)] == [refund.id]
    assert RefundService(session).get(refund.id).amount == D("40.00")


def test_refund_over_credit_rejected(session, owner_user, customer, chart):
    _pay(session, owner_user, customer, chart, "100.00")
    _refund(session, owner_user, customer, chart, "60.00")
    before = _entry_count(session)
    with pytest.raises(InvalidRefundError) as exc:
        _refund(session, owner_user, customer, chart, "40.01")
    assert "40.00" in exc.value.message
    assert _entry_count(session) == before
    _refund(session, owner_user, customer, chart, "40.00")  # exactly the rest is fine
    assert _credit(session, customer) == D("0.00")


def test_refund_when_no_credit_rejected(session, owner_user, customer, item, chart):
    with pytest.raises(InvalidRefundError) as exc:
        _refund(session, owner_user, customer, chart, "1.00")
    assert "no credit" in exc.value.message
    # a payment fully applied leaves no credit either
    invoice = _invoice(session, owner_user, customer, item, "100.00")
    _pay(session, owner_user, customer, chart, "100.00", apply_to=[(invoice, "100.00")])
    with pytest.raises(InvalidRefundError):
        _refund(session, owner_user, customer, chart, "0.01")
    assert session.exec(select(func.count()).select_from(CustomerRefund)).one() == 0


def test_refund_dated_before_receipt_rejected(session, owner_user, customer, chart):
    _pay(session, owner_user, customer, chart, "100.00", day=PAY_DAY)
    with pytest.raises(InvalidRefundError):
        _refund(session, owner_user, customer, chart, "10.00", day=date(2026, 3, 9))


def test_refund_dated_before_a_later_refund_cannot_overdraw(session, owner_user, customer, chart):
    _pay(session, owner_user, customer, chart, "100.00")
    _refund(session, owner_user, customer, chart, "100.00", day=date(2026, 3, 25))
    # as of 03-12 the whole 100.00 is still credit, but the later refund already spent it
    with pytest.raises(InvalidRefundError):
        _refund(session, owner_user, customer, chart, "50.00", day=date(2026, 3, 12))
    assert _credit(session, customer) == D("0.00")


@pytest.mark.parametrize(
    ("amount", "fragment"),
    [
        (D("0"), "between"),
        (D("-5.00"), "between"),
        (D("1000000000000.00"), "between"),
        (D("10.001"), "2 decimal"),
        (D("NaN"), "number"),
        (D("Infinity"), "number"),
        (12.5, "number"),
        ("12.50", "number"),
        (None, "number"),
    ],
)
def test_refund_rejects_bad_amounts_and_bad_bank(
    session, owner_user, customer, chart, amount, fragment
):
    _pay(session, owner_user, customer, chart, "100.00")
    with pytest.raises(InvalidRefundError) as exc:
        RefundService(session).record(
            actor=owner_user,
            customer_id=customer.id,
            refund_date=REFUND_DAY,
            amount=amount,
            bank_account_id=chart["Business Checking"].id,
            method="check",
        )
    assert fragment in exc.value.message
    assert session.exec(select(func.count()).select_from(CustomerRefund)).one() == 0


def test_refund_rejects_bad_bank_method_customer_and_text(session, owner_user, customer, chart):
    _pay(session, owner_user, customer, chart, "100.00")
    revenue = chart["Services Revenue"]
    inactive = chart["Business Checking"]
    cases = [
        ({"bank_account_id": revenue.id}, "bank (asset)"),
        ({"bank_account_id": chart["Accounts Receivable"].id}, "bank (asset)"),
        ({"bank_account_id": 999999}, "bank account"),
        ({"bank_account_id": None}, "bank account"),
        ({"method": "bitcoin"}, "method"),
        ({"method": None}, "method"),
        ({"reference": "x" * 101}, "Reference"),
        ({"memo": "x" * 501}, "Memo"),
        ({"day": date(1800, 1, 1)}, "date"),
    ]
    for override, fragment in cases:
        with pytest.raises(InvalidRefundError) as exc:
            _refund(session, owner_user, customer, chart, "10.00", **override)
        assert fragment in exc.value.message, (override, exc.value.message)
    with pytest.raises(InvalidRefundError) as exc:
        RefundService(session).record(
            actor=owner_user,
            customer_id=999999,
            refund_date=REFUND_DAY,
            amount=D("1.00"),
            bank_account_id=inactive.id,
            method="check",
        )
    assert "customer" in exc.value.message
    AccountService(session).set_active(inactive.id, False, actor=owner_user)
    session.commit()
    with pytest.raises(InvalidRefundError) as exc:
        _refund(session, owner_user, customer, chart, "10.00")
    assert "inactive" in exc.value.message
    assert session.exec(select(func.count()).select_from(CustomerRefund)).one() == 0


def test_refund_requires_ar_role(session, owner_user, customer, chart):
    _pay(session, owner_user, customer, chart, "100.00")
    session.execute(
        sql_text("UPDATE account SET system_role = NULL WHERE name = 'Accounts Receivable'")
    )
    session.commit()
    with pytest.raises(ARNotConfiguredError):
        _refund(session, owner_user, customer, chart, "10.00")


def test_void_refund_restores_credit(session, owner_user, customer, chart):
    _pay(session, owner_user, customer, chart, "100.00")
    refund = _refund(session, owner_user, customer, chart, "40.00")
    before = _entry_count(session)
    RefundService(session).void(refund.id, actor=owner_user, void_date=date(2026, 3, 22), memo="x")
    session.refresh(refund)
    assert (refund.status, refund.void_date) == ("void", date(2026, 3, 22))
    assert _credit(session, customer) == D("100.00")
    assert _entry_count(session) == before + 1
    reversal = session.exec(
        select(JournalEntry).where(JournalEntry.reverses_entry_id == refund.entry_id)
    ).one()
    assert (reversal.entry_date, reversal.source_type, reversal.source_id) == (
        date(2026, 3, 22),
        "refund",
        str(refund.id),
    )
    assert session.get(JournalEntry, refund.entry_id).status == "void"
    # the freed credit can be refunded again
    _refund(session, owner_user, customer, chart, "100.00", day=date(2026, 3, 22))
    with pytest.raises(RefundStateError):
        RefundService(session).void(refund.id, actor=owner_user, void_date=date(2026, 3, 23))
    with pytest.raises(RefundNotFoundError):
        RefundService(session).void(999999, actor=owner_user, void_date=date(2026, 3, 23))
    with pytest.raises(RefundNotFoundError):
        RefundService(session).get(999999)


def test_refund_in_locked_period_rejected(session, owner_user, customer, chart):
    _pay(session, owner_user, customer, chart, "100.00")
    refund = _refund(session, owner_user, customer, chart, "10.00", day=date(2026, 3, 12))
    session.execute(
        sql_text("UPDATE period_lock SET lock_date = :d WHERE id = 1"), {"d": date(2026, 3, 15)}
    )
    session.commit()
    before = _entry_count(session)
    with pytest.raises(PeriodLockedError):
        _refund(session, owner_user, customer, chart, "10.00", day=date(2026, 3, 14))
    with pytest.raises(PeriodLockedError):  # voiding a locked entry is refused too
        RefundService(session).void(refund.id, actor=owner_user, void_date=date(2026, 3, 20))
    assert _entry_count(session) == before
    assert session.exec(select(func.count()).select_from(CustomerRefund)).one() == 1
    session.refresh(refund)
    assert refund.status == "posted"
    _refund(session, owner_user, customer, chart, "10.00", day=date(2026, 3, 16))  # after the lock


def test_refund_entries_are_numbered_independently_of_payments(
    session, owner_user, customer, chart
):
    payments = [_pay(session, owner_user, customer, chart, "50.00") for _ in range(3)]
    first = _refund(session, owner_user, customer, chart, "10.00")
    second = _refund(session, owner_user, customer, chart, "10.00")
    numbers = [session.get(JournalEntry, r.entry_id).entry_no for r in (first, second)]
    assert numbers == [1, 2]
    assert [session.get(JournalEntry, p.entry_id).entry_no for p in payments] == [1, 2, 3]


def test_owner_only_and_audited(session, owner_user, customer, chart):
    _pay(session, owner_user, customer, chart, "100.00")
    for role in ("viewer", "accountant"):
        other = make_user(session, email=f"{role}@example.com", role=role)
        session.commit()
        with pytest.raises(PermissionDeniedError):
            _refund(session, other, customer, chart)
    assert session.exec(select(func.count()).select_from(CustomerRefund)).one() == 0

    refund = _refund(session, owner_user, customer, chart, "25.00", method="ach")
    other = make_user(session, email="viewer2@example.com", role="viewer")
    session.commit()
    with pytest.raises(PermissionDeniedError):
        RefundService(session).void(refund.id, actor=other, void_date=REFUND_DAY)
    session.refresh(refund)
    assert refund.status == "posted"

    RefundService(session).void(refund.id, actor=owner_user, void_date=REFUND_DAY)
    recorded = _audit(session, "refund.record")
    voided = _audit(session, "refund.void")
    assert len(recorded) == 1 and len(voided) == 1
    assert recorded[0].entity_id == str(refund.id) == voided[0].entity_id
    assert recorded[0].user_id == owner_user.id
    assert recorded[0].after["amount"] == "25.00"
    assert recorded[0].after["entry_id"] == refund.entry_id
    assert voided[0].before["status"] == "posted" and voided[0].after["status"] == "void"


def test_failed_refund_writes_no_audit_row(session, owner_user, customer, chart):
    with pytest.raises(InvalidRefundError):
        _refund(session, owner_user, customer, chart, "5.00")
    assert _audit(session, "refund.record") == []


# --- interplay with the payment service: credit can never go negative -------------------


def test_apply_cannot_spend_refunded_credit(session, owner_user, customer, item, chart):
    invoice = _invoice(session, owner_user, customer, item, "100.00")
    payment = _pay(session, owner_user, customer, chart, "100.00")
    _refund(session, owner_user, customer, chart, "60.00")
    with pytest.raises(OverApplicationError) as exc:
        PaymentService(session).apply(
            payment.id,
            actor=owner_user,
            applications=[ApplicationInput(invoice.id, D("50.00"))],
        )
    assert "40.00" in exc.value.message
    PaymentService(session).apply(
        payment.id, actor=owner_user, applications=[ApplicationInput(invoice.id, D("40.00"))]
    )
    assert _credit(session, customer) == D("0.00")


def test_void_payment_refused_while_its_credit_was_refunded(session, owner_user, customer, chart):
    payment = _pay(session, owner_user, customer, chart, "100.00")
    refund = _refund(session, owner_user, customer, chart, "30.00")
    with pytest.raises(PaymentStateError):
        PaymentService(session).void(payment.id, actor=owner_user, void_date=REFUND_DAY)
    session.refresh(payment)
    assert payment.status == "posted"
    RefundService(session).void(refund.id, actor=owner_user, void_date=REFUND_DAY)
    PaymentService(session).void(payment.id, actor=owner_user, void_date=REFUND_DAY)
    assert _credit(session, customer) == D("0.00")


# --- back-dated documents may not make credit negative on ANY date -----------------------


def _counts(session):
    session.expire_all()
    return (
        _entry_count(session),
        session.exec(select(func.count()).select_from(PaymentApplication)).one(),
        session.exec(select(func.count()).select_from(CustomerRefund)).one(),
    )


def test_backdated_refund_refused_when_a_later_application_would_go_negative(
    session, owner_user, customer, item, chart
):
    p1 = _pay(session, owner_user, customer, chart, "100.00", day=date(2026, 3, 10))
    invoice = _invoice(session, owner_user, customer, item, day=date(2026, 4, 1))
    PaymentService(session).apply(
        p1.id, actor=owner_user, applications=[ApplicationInput(invoice.id, D("100.00"))]
    )
    _pay(session, owner_user, customer, chart, "100.00", day=date(2026, 5, 1))
    # credit: 100 on 03-10, 0 from 04-01 (applied), 100 from 05-01; "now" and 03-15 both show 100
    assert _credit(session, customer, date(2026, 3, 15)) == D("100.00")
    assert _credit(session, customer) == D("100.00")
    before = _counts(session)
    with pytest.raises(InvalidRefundError):
        _refund(session, owner_user, customer, chart, "100.00", day=date(2026, 3, 15))
    assert _counts(session) == before
    # a refund that fits under the lowest point is fine
    _refund(session, owner_user, customer, chart, "100.00", day=date(2026, 5, 2))


def test_backdated_refund_allowed_when_credit_never_dips(session, owner_user, customer, chart):
    _pay(session, owner_user, customer, chart, "100.00", day=date(2026, 3, 10))
    _pay(session, owner_user, customer, chart, "100.00", day=date(2026, 5, 1))
    _refund(session, owner_user, customer, chart, "100.00", day=date(2026, 3, 15))
    assert _credit(session, customer, date(2026, 3, 15)) == D("0.00")
    assert _credit(session, customer) == D("100.00")


@pytest.fixture
def refunded_then_topped_up(session, owner_user, customer, chart):
    """P1 100 on 03-10, a refund of 100 on 03-20, P2 100 on 04-01 (credit 100, 0, 100)."""
    p1 = _pay(session, owner_user, customer, chart, "100.00", day=date(2026, 3, 10))
    _refund(session, owner_user, customer, chart, "100.00", day=date(2026, 3, 20))
    _pay(session, owner_user, customer, chart, "100.00", day=date(2026, 4, 1))
    return p1


def test_backdated_application_refused_when_past_credit_would_go_negative(
    session, owner_user, customer, item, chart, refunded_then_topped_up
):
    p1 = refunded_then_topped_up
    early = _invoice(session, owner_user, customer, item, day=date(2026, 3, 1))  # recognized 03-10
    before = _counts(session)
    with pytest.raises(OverApplicationError):
        PaymentService(session).apply(
            p1.id, actor=owner_user, applications=[ApplicationInput(early.id, D("100.00"))]
        )
    assert _counts(session) == before
    # an invoice dated after P2 arrived: recognized 04-05, credit never dips below zero
    late = _invoice(session, owner_user, customer, item, day=date(2026, 4, 5))
    PaymentService(session).apply(
        p1.id, actor=owner_user, applications=[ApplicationInput(late.id, D("100.00"))]
    )
    assert _credit(session, customer) == D("0.00")
    assert _credit(session, customer, date(2026, 3, 25)) == D("0.00")


def test_backdated_payment_void_refused_when_past_credit_would_go_negative(
    session, owner_user, customer, chart, refunded_then_topped_up
):
    p1 = refunded_then_topped_up
    before = _counts(session)
    with pytest.raises(PaymentStateError):
        PaymentService(session).void(p1.id, actor=owner_user, void_date=date(2026, 3, 15))
    assert _counts(session) == before
    session.refresh(p1)
    assert p1.status == "posted"
    # voided once P2 has arrived the credit never dips below zero
    PaymentService(session).void(p1.id, actor=owner_user, void_date=date(2026, 4, 5))
    assert _credit(session, customer) == D("0.00")
    assert _credit(session, customer, date(2026, 3, 25)) == D("0.00")
