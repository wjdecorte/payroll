"""Customer payments: posting, applications, credits, voids and validation."""

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
from payroll.ledger.models import Account, AccountType, JournalEntry, JournalLine
from payroll.receivables.exceptions import (
    ARNotConfiguredError,
    DepositError,
    InvalidPaymentError,
    InvoiceStateError,
    OverApplicationError,
    PaymentStateError,
)
from payroll.receivables.invoices import InvoiceLineInput, InvoiceService, live_applied
from payroll.receivables.models import (
    ApplicationIncome,
    Customer,
    Deposit,
    Item,
    Payment,
    PaymentApplication,
)
from payroll.receivables.payments import (
    ApplicationInput,
    PaymentService,
    allocate_oldest_first,
    unapplied,
)
from tests.conftest import make_user
from tests.ledger.conftest import chart  # noqa: F401

D = Decimal
DAY = date(2026, 3, 1)


@pytest.fixture(autouse=True)
def _committed_fixtures(session, owner_user, customer, item):
    """Commit the fixture rows: a service rollback must not wipe them mid-test."""
    session.commit()


def _invoice(session, owner_user, customer, item, price="100.00", day=DAY, terms=None, qty="1"):
    service = InvoiceService(session)
    draft = service.create_draft(
        actor=owner_user,
        customer_id=customer.id,
        invoice_date=day,
        terms_days=terms,
        lines=[InvoiceLineInput(item.id, "Work", D(qty), D(price))],
    )
    return service.issue(draft.id, actor=owner_user)


def _record(session, owner_user, customer, chart, amount="100.00", **kwargs):
    kwargs.setdefault("received_date", DAY)
    kwargs.setdefault("method", "check")
    kwargs.setdefault("deposit_account_id", chart["Undeposited Funds"].id)
    return PaymentService(session).record(
        actor=owner_user, customer_id=customer.id, amount=D(amount), **kwargs
    )


def _lines(session, entry_id):
    stmt = (
        select(JournalLine)
        .where(JournalLine.entry_id == entry_id)
        .order_by(col(JournalLine.line_no))
    )
    return list(session.exec(stmt).all())


def _ar_balance(session, chart):
    return AccountService(session).balances().get(chart["Accounts Receivable"].id, D("0.00"))


def _entry_count(session):
    return session.exec(select(func.count()).select_from(JournalEntry)).one()


def _app_count(session):
    return session.exec(select(func.count()).select_from(PaymentApplication)).one()


def _set_lock(session, lock_date):
    session.execute(
        sql_text("UPDATE period_lock SET lock_date = :d WHERE id = 1"), {"d": lock_date}
    )
    session.commit()


def _balance(session, invoice):
    return invoice.total - live_applied(session, invoice.id)


def _apps(*pairs):
    return [ApplicationInput(i.id, D(a)) for i, a in pairs]


def test_record_posts_dr_deposit_cr_ar_two_lines_in_order(
    session, owner_user, customer, item, chart
):
    payment = _record(session, owner_user, customer, chart, "125.50", reference="1042")
    entry = session.get(JournalEntry, payment.entry_id)
    assert (entry.entry_date, entry.source_type, entry.source_id) == (
        DAY,
        "payment",
        str(payment.id),
    )
    assert entry.memo == "Payment check 1042 Acme Corp"
    lines = _lines(session, entry.id)
    assert [(ln.account_id, ln.debit, ln.credit) for ln in lines] == [
        (chart["Undeposited Funds"].id, D("125.50"), D("0.00")),
        (chart["Accounts Receivable"].id, D("0.00"), D("125.50")),
    ]
    assert (payment.status, payment.method, payment.reference) == ("posted", "check", "1042")


def test_record_to_undeposited_funds_and_straight_to_bank(
    session, owner_user, customer, item, chart
):
    one = _record(session, owner_user, customer, chart, "10.00")
    two = _record(
        session,
        owner_user,
        customer,
        chart,
        "20.00",
        deposit_account_id=chart["Business Checking"].id,
    )
    assert _lines(session, one.entry_id)[0].account_id == chart["Undeposited Funds"].id
    assert _lines(session, two.entry_id)[0].account_id == chart["Business Checking"].id
    assert two.deposit_account_id == chart["Business Checking"].id


def test_full_payment_applies_and_marks_paid(session, owner_user, customer, item, chart):
    invoice = _invoice(session, owner_user, customer, item)
    payment = _record(session, owner_user, customer, chart, applications=_apps((invoice, "100.00")))
    assert live_applied(session, invoice.id) == D("100.00")
    assert _balance(session, invoice) == D("0.00")
    assert unapplied(session, payment.id) == D("0.00")
    app = session.exec(select(PaymentApplication)).one()
    assert (app.payment_id, app.invoice_id, app.amount) == (payment.id, invoice.id, D("100.00"))
    assert app.ar_account_id == chart["Accounts Receivable"].id
    assert _ar_balance(session, chart) == D("0.00")  # invoice debit cancelled by payment credit


def test_partial_payment_leaves_balance(session, owner_user, customer, item, chart):
    invoice = _invoice(session, owner_user, customer, item)
    _record(session, owner_user, customer, chart, "40.00", applications=_apps((invoice, "40.00")))
    assert _balance(session, invoice) == D("60.00")
    second = _record(session, owner_user, customer, chart, "60.00")
    PaymentService(session).apply(
        second.id, actor=owner_user, applications=_apps((invoice, "60.00"))
    )
    assert _balance(session, invoice) == D("0.00")


def test_split_payment_across_two_invoices(session, owner_user, customer, item, chart):
    a = _invoice(session, owner_user, customer, item, "70.00")
    b = _invoice(session, owner_user, customer, item, "50.00")
    payment = _record(
        session,
        owner_user,
        customer,
        chart,
        "100.00",
        applications=_apps((a, "70.00"), (b, "30.00")),
    )
    assert (_balance(session, a), _balance(session, b)) == (D("0.00"), D("20.00"))
    assert unapplied(session, payment.id) == D("0.00")
    assert _app_count(session) == 2


def test_overpayment_leaves_unapplied_credit_in_ar(session, owner_user, customer, item, chart):
    invoice = _invoice(session, owner_user, customer, item)
    payment = _record(
        session, owner_user, customer, chart, "300.00", applications=_apps((invoice, "100.00"))
    )
    assert unapplied(session, payment.id) == D("200.00")
    assert _ar_balance(session, chart) == D("-200.00")  # 100 invoice - 300 payment


def test_apply_unapplied_credit_to_later_invoice_posts_nothing(
    session, owner_user, customer, item, chart
):
    payment = _record(session, owner_user, customer, chart, "100.00")
    later = _invoice(session, owner_user, customer, item, day=date(2026, 3, 20))
    entries = _entry_count(session)
    PaymentService(session).apply(
        payment.id, actor=owner_user, applications=_apps((later, "100.00"))
    )
    assert _entry_count(session) == entries
    app = session.exec(select(PaymentApplication)).one()
    assert app.recognized_date == date(2026, 3, 20)
    assert unapplied(session, payment.id) == D("0.00")


@pytest.fixture
def three_incomes(session, chart, customer):
    """Three income accounts (numbers 4001..4003) with one item each."""
    items = []
    for n in (1, 2, 3):
        account = Account(name=f"Income {n}", number=f"400{n}", type=AccountType.INCOME)
        session.add(account)
        session.flush()
        row = Item(name=f"Item {n}", unit_price=D("1.00"), income_account_id=account.id)
        session.add(row)
        session.flush()
        items.append(row)
    session.commit()
    return items


def _split(session, owner_user, customer, chart, items, prices, pay):
    service = InvoiceService(session)
    draft = service.create_draft(
        actor=owner_user,
        customer_id=customer.id,
        invoice_date=DAY,
        lines=[InvoiceLineInput(i.id, "x", D("1"), D(p)) for i, p in zip(items, prices)],
    )
    invoice = service.issue(draft.id, actor=owner_user)
    payment = _record(session, owner_user, customer, chart, pay, applications=_apps((invoice, pay)))
    app = session.exec(
        select(PaymentApplication).where(PaymentApplication.payment_id == payment.id)
    ).one()
    rows = session.exec(
        select(ApplicationIncome)
        .where(ApplicationIncome.application_id == app.id)
        .order_by(col(ApplicationIncome.account_id))
    ).all()
    return [r.amount for r in rows], app


def test_application_income_split_sums_exactly(session, owner_user, customer, chart, three_incomes):
    slices, app = _split(
        session, owner_user, customer, chart, three_incomes, ["33.34", "33.33", "33.33"], "99.99"
    )
    # true shares of 99.99: 33.3367 / 33.3267 / 33.3267; rounding gives 100.00, one cent too
    # much, which is taken back from the account rounded up the most
    assert sum(slices) == D("99.99") and len(slices) == 3
    assert slices == [D("33.33"), D("33.33"), D("33.33")]

    equal, _ = _split(
        session, owner_user, customer, chart, three_incomes, ["33.33", "33.33", "33.33"], "33.33"
    )
    assert equal == [D("11.11")] * 3

    tiny, tiny_app = _split(
        session, owner_user, customer, chart, three_incomes, ["33.33", "33.33", "33.33"], "0.01"
    )
    assert tiny == [D("0.01")]  # only the last account takes it; no zero rows


def test_recognized_date_is_later_of_payment_and_invoice_date(
    session, owner_user, customer, item, chart
):
    early = _invoice(session, owner_user, customer, item, day=date(2026, 3, 1))
    late = _invoice(session, owner_user, customer, item, day=date(2026, 3, 25))
    _record(
        session,
        owner_user,
        customer,
        chart,
        "200.00",
        received_date=date(2026, 3, 10),
        applications=_apps((early, "100.00"), (late, "100.00")),
    )
    dates = {
        a.invoice_id: a.recognized_date for a in session.exec(select(PaymentApplication)).all()
    }
    assert dates == {early.id: date(2026, 3, 10), late.id: date(2026, 3, 25)}


def test_application_in_locked_period_rejected_and_nothing_written(
    session, owner_user, customer, item, chart
):
    old = _invoice(session, owner_user, customer, item, day=date(2026, 2, 1))
    payment = _record(
        session, owner_user, customer, chart, "100.00", received_date=date(2026, 4, 5)
    )
    # an application recognized on 2026-04-05 is fine until the lock reaches it
    _set_lock(session, date(2026, 4, 5))
    payments = session.exec(select(func.count()).select_from(Payment)).one()
    entries = _entry_count(session)
    with pytest.raises(PeriodLockedError) as err:
        PaymentService(session).apply(
            payment.id, actor=owner_user, applications=_apps((old, "50.00"))
        )
    assert "2026-04-05" in err.value.message
    assert _app_count(session) == 0
    # recording with an application in the locked period writes nothing either
    with pytest.raises(PeriodLockedError):
        _record(
            session,
            owner_user,
            customer,
            chart,
            "100.00",
            received_date=date(2026, 4, 5),
            applications=_apps((old, "50.00")),
        )
    assert session.exec(select(func.count()).select_from(Payment)).one() == payments
    assert _entry_count(session) == entries
    assert _app_count(session) == 0


def test_locked_invoice_date_blocks_even_when_payment_date_is_open(
    session, owner_user, customer, item, chart
):
    """recognized_date is the later date, so a locked payment date is judged by the invoice."""
    invoice = _invoice(session, owner_user, customer, item, day=date(2026, 3, 1))
    payment = _record(
        session, owner_user, customer, chart, "100.00", received_date=date(2026, 3, 2)
    )
    _set_lock(session, date(2026, 3, 2))
    with pytest.raises(PeriodLockedError):
        PaymentService(session).apply(
            payment.id, actor=owner_user, applications=_apps((invoice, "10.00"))
        )


def test_rejects_over_application_to_invoice_and_beyond_payment_amount(
    session, owner_user, customer, item, chart
):
    invoice = _invoice(session, owner_user, customer, item)
    with pytest.raises(OverApplicationError):
        _record(
            session, owner_user, customer, chart, "500.00", applications=_apps((invoice, "100.01"))
        )
    with pytest.raises(OverApplicationError):
        _record(
            session, owner_user, customer, chart, "50.00", applications=_apps((invoice, "50.01"))
        )
    assert _app_count(session) == 0
    payment = _record(
        session, owner_user, customer, chart, "80.00", applications=_apps((invoice, "60.00"))
    )
    other = _record(session, owner_user, customer, chart, "80.00")
    # only 40.00 of the invoice is left; and 20.00 of the first payment
    with pytest.raises(OverApplicationError):
        PaymentService(session).apply(
            other.id, actor=owner_user, applications=_apps((invoice, "40.01"))
        )
    with pytest.raises(OverApplicationError):
        PaymentService(session).apply(
            payment.id, actor=owner_user, applications=_apps((invoice, "20.01"))
        )
    assert live_applied(session, invoice.id) == D("60.00")


def test_rejects_other_customers_invoice_draft_invoice_void_invoice(
    session, owner_user, customer, item, chart
):
    stranger = Customer(name="Other Co")
    session.add(stranger)
    session.commit()
    foreign = _invoice(session, owner_user, stranger, item)
    draft = InvoiceService(session).create_draft(
        actor=owner_user,
        customer_id=customer.id,
        invoice_date=DAY,
        lines=[InvoiceLineInput(item.id, "x", D("1"), D("10.00"))],
    )
    voided = _invoice(session, owner_user, customer, item)
    InvoiceService(session).void(voided.id, actor=owner_user, void_date=DAY)
    with pytest.raises(InvalidPaymentError) as err:
        _record(session, owner_user, customer, chart, applications=_apps((foreign, "10.00")))
    assert "different customer" in err.value.message
    for bad in (draft, voided):
        with pytest.raises(InvoiceStateError):
            _record(session, owner_user, customer, chart, applications=_apps((bad, "10.00")))
    assert _app_count(session) == 0


@pytest.mark.parametrize(
    "amount",
    [
        D("0"),
        D("0.00"),
        D("-1.00"),
        D("1.001"),
        D("NaN"),
        D("Infinity"),
        D("1000000000000.00"),
        D("1E+30"),
        "10.00",
        10.0,
        None,
    ],
)
def test_rejects_zero_negative_three_decimal_nan_huge_amounts(
    session, owner_user, customer, item, chart, amount
):
    with pytest.raises(InvalidPaymentError):
        PaymentService(session).record(
            actor=owner_user,
            customer_id=customer.id,
            received_date=DAY,
            amount=amount,
            deposit_account_id=chart["Undeposited Funds"].id,
            method="check",
        )
    invoice = _invoice(session, owner_user, customer, item)
    payment = _record(session, owner_user, customer, chart, "100.00")
    with pytest.raises(InvalidPaymentError):
        PaymentService(session).apply(
            payment.id, actor=owner_user, applications=[ApplicationInput(invoice.id, amount)]
        )
    assert _app_count(session) == 0


def test_rejects_bad_fields_and_duplicate_invoice_lines(session, owner_user, customer, item, chart):
    invoice = _invoice(session, owner_user, customer, item)
    with pytest.raises(InvalidPaymentError):
        _record(session, owner_user, customer, chart, method="bitcoin")
    with pytest.raises(InvalidPaymentError):
        _record(session, owner_user, customer, chart, reference="x" * 101)
    with pytest.raises(InvalidPaymentError):
        _record(session, owner_user, customer, chart, memo="x" * 501)
    with pytest.raises(InvalidPaymentError):
        _record(session, owner_user, customer, chart, received_date=date(1800, 1, 1))
    with pytest.raises(InvalidPaymentError):
        _record(
            session,
            owner_user,
            customer,
            chart,
            applications=_apps((invoice, "5.00"), (invoice, "5.00")),
        )
    payment = _record(session, owner_user, customer, chart, applications=_apps((invoice, "5.00")))
    with pytest.raises(InvalidPaymentError):  # one row per (payment, invoice)
        PaymentService(session).apply(
            payment.id, actor=owner_user, applications=_apps((invoice, "5.00"))
        )
    with pytest.raises(InvalidPaymentError):
        PaymentService(session).apply(payment.id, actor=owner_user, applications=[])
    customer.is_active = False
    session.add(customer)
    session.commit()
    with pytest.raises(InvalidPaymentError):
        _record(session, owner_user, customer, chart)


def test_rejects_non_asset_and_inactive_deposit_account(session, owner_user, customer, item, chart):
    for name in ("Services Revenue", "Accounts Receivable"):
        with pytest.raises(InvalidPaymentError):
            _record(session, owner_user, customer, chart, deposit_account_id=chart[name].id)
    with pytest.raises(InvalidPaymentError):
        _record(session, owner_user, customer, chart, deposit_account_id=999999)
    chart["Business Checking"].is_active = False
    session.add(chart["Business Checking"])
    session.commit()
    with pytest.raises(InvalidPaymentError):
        _record(
            session, owner_user, customer, chart, deposit_account_id=chart["Business Checking"].id
        )
    assert session.exec(select(func.count()).select_from(Payment)).one() == 0


def test_record_needs_ar_role(session, owner_user, customer, item, chart):
    ar = chart["Accounts Receivable"]
    ar.system_role = None
    session.add(ar)
    session.commit()
    with pytest.raises(ARNotConfiguredError):
        _record(session, owner_user, customer, chart)


def test_void_payment_reverses_entry_releases_balance_and_keeps_applications(
    session, owner_user, customer, item, chart
):
    invoice = _invoice(session, owner_user, customer, item)
    payment = _record(session, owner_user, customer, chart, applications=_apps((invoice, "100.00")))
    assert _balance(session, invoice) == D("0.00")
    voided = PaymentService(session).void(
        payment.id, actor=owner_user, void_date=date(2026, 3, 9), memo="Bounced"
    )
    assert (voided.status, voided.void_date) == ("void", date(2026, 3, 9))
    assert _balance(session, invoice) == D("100.00")
    assert unapplied(session, payment.id) == D("0.00")
    assert _app_count(session) == 1  # history stays
    assert _ar_balance(session, chart) == D("100.00")  # the invoice is owed again
    original = session.get(JournalEntry, payment.entry_id)
    assert original.status == "void"
    reversal = session.exec(
        select(JournalEntry).where(JournalEntry.reverses_entry_id == original.id)
    ).one()
    assert (reversal.entry_date, reversal.source_type, reversal.source_id) == (
        date(2026, 3, 9),
        "payment",
        str(payment.id),
    )
    # the invoice can now be voided (no live application)
    InvoiceService(session).void(invoice.id, actor=owner_user, void_date=date(2026, 3, 10))
    with pytest.raises(PaymentStateError):
        PaymentService(session).apply(
            payment.id, actor=owner_user, applications=_apps((invoice, "1.00"))
        )


def test_void_refused_when_deposited(session, owner_user, customer, item, chart):
    payment = _record(session, owner_user, customer, chart)
    deposit = Deposit(
        deposit_date=DAY, bank_account_id=chart["Business Checking"].id, total=D("100.00")
    )
    session.add(deposit)
    session.flush()
    payment.deposit_id = deposit.id
    session.add(payment)
    session.commit()
    with pytest.raises(DepositError) as err:
        PaymentService(session).void(payment.id, actor=owner_user, void_date=DAY)
    assert err.value.message == "Void the deposit first"
    deposit.status = "void"
    session.add(deposit)
    session.commit()
    assert (
        PaymentService(session).void(payment.id, actor=owner_user, void_date=DAY).status == "void"
    )


def test_void_in_locked_period_rejected(session, owner_user, customer, item, chart):
    payment = _record(session, owner_user, customer, chart, received_date=date(2026, 1, 10))
    _set_lock(session, date(2026, 3, 15))
    with pytest.raises(PeriodLockedError):
        PaymentService(session).void(payment.id, actor=owner_user, void_date=date(2026, 3, 20))
    _set_lock(session, date(2026, 2, 1))
    with pytest.raises(PeriodLockedError):
        PaymentService(session).void(payment.id, actor=owner_user, void_date=date(2026, 1, 20))
    session.refresh(payment)
    assert payment.status == "posted"


def test_void_twice_rejected(session, owner_user, customer, item, chart):
    payment = _record(session, owner_user, customer, chart)
    service = PaymentService(session)
    service.void(payment.id, actor=owner_user, void_date=DAY)
    with pytest.raises(PaymentStateError):
        service.void(payment.id, actor=owner_user, void_date=DAY)
    assert _entry_count(session) == 2  # original + one reversal


def test_allocate_oldest_first(session, owner_user, customer, item, chart):
    late = _invoice(session, owner_user, customer, item, "50.00", day=date(2026, 3, 10))
    early = _invoice(session, owner_user, customer, item, "30.00", day=date(2026, 2, 1))
    mid = _invoice(session, owner_user, customer, item, "40.00", day=date(2026, 3, 1))
    _record(session, owner_user, customer, chart, "10.00", applications=_apps((early, "10.00")))
    plan = allocate_oldest_first(session, customer.id, D("70.00"))
    # early has 20.00 left, then mid 40.00, then late takes the last 10.00
    assert plan == [
        ApplicationInput(early.id, D("20.00")),
        ApplicationInput(mid.id, D("40.00")),
        ApplicationInput(late.id, D("10.00")),
    ]
    assert allocate_oldest_first(session, customer.id, D("500.00")) == [
        ApplicationInput(early.id, D("20.00")),
        ApplicationInput(mid.id, D("40.00")),
        ApplicationInput(late.id, D("50.00")),
    ]  # the 390.00 left over stays unapplied
    assert allocate_oldest_first(session, customer.id + 99, D("5.00")) == []
    assert allocate_oldest_first(session, customer.id, D("70.00"), exclude={early.id}) == [
        ApplicationInput(mid.id, D("40.00")),
        ApplicationInput(late.id, D("30.00")),
    ]


def test_owner_only_and_audited(session, owner_user, customer, item, chart):
    viewer = make_user(session, role="viewer", email="v@example.com")
    accountant = make_user(session, role="accountant", email="a@example.com")
    session.commit()
    invoice = _invoice(session, owner_user, customer, item)
    for user in (viewer, accountant):
        with pytest.raises(PermissionDeniedError):
            _record(session, user, customer, chart)
    payment = _record(session, owner_user, customer, chart, "100.00")
    for user in (viewer, accountant):
        with pytest.raises(PermissionDeniedError):
            PaymentService(session).apply(
                payment.id, actor=user, applications=_apps((invoice, "1.00"))
            )
        with pytest.raises(PermissionDeniedError):
            PaymentService(session).void(payment.id, actor=user, void_date=DAY)
    assert _app_count(session) == 0
    PaymentService(session).apply(
        payment.id, actor=owner_user, applications=_apps((invoice, "25.00"))
    )
    PaymentService(session).void(payment.id, actor=owner_user, void_date=DAY)
    actions = [
        r.action
        for r in session.exec(select(AuditLog).order_by(col(AuditLog.id))).all()
        if r.action.startswith("payment.")
    ]
    assert actions == ["payment.record", "payment.apply", "payment.void"]
    record = session.exec(select(AuditLog).where(AuditLog.action == "payment.apply")).one()
    assert record.entity_id == str(payment.id)


def test_listing_filters_by_customer(session, owner_user, customer, item, chart):
    other = Customer(name="Zed")
    session.add(other)
    session.commit()
    mine = _record(session, owner_user, customer, chart)
    theirs = _record(session, owner_user, other, chart)
    service = PaymentService(session)
    assert {p.id for p in service.list()} == {mine.id, theirs.id}
    assert [p.id for p in service.list(customer_id=other.id)] == [theirs.id]
    assert service.list(limit=1, offset=1)[0].id == mine.id
    assert [a.invoice_id for a in service.applications(mine.id)] == []


# ---------------------------------------------------------------------------
# Cumulative income split: per-account totals stay exact across installments
# ---------------------------------------------------------------------------


@pytest.fixture
def make_incomes(session, chart):
    """Factory: ``n`` income accounts (numbers 4101..) each with one item."""

    def build(n):
        items = []
        for k in range(1, n + 1):
            account = Account(name=f"Cum Income {k}", number=f"410{k}", type=AccountType.INCOME)
            session.add(account)
            session.flush()
            row = Item(name=f"Cum Item {k}", unit_price=D("1.00"), income_account_id=account.id)
            session.add(row)
            session.flush()
            items.append(row)
        session.commit()
        return items

    return build


def _issue(session, owner_user, customer, items, prices):
    service = InvoiceService(session)
    draft = service.create_draft(
        actor=owner_user,
        customer_id=customer.id,
        invoice_date=DAY,
        lines=[InvoiceLineInput(i.id, "x", D("1"), D(p)) for i, p in zip(items, prices)],
    )
    return service.issue(draft.id, actor=owner_user)


def _pay_parts(session, owner_user, customer, chart, invoice, parts):
    for part in parts:
        _record(session, owner_user, customer, chart, part, applications=_apps((invoice, part)))


def _live_slices(session, invoice_id):
    """account_id -> sum of stored slices of live applications; also checks every slice > 0
    and that every application's slices sum to the application amount."""
    totals: dict[int, Decimal] = {}
    for app in session.exec(
        select(PaymentApplication).where(PaymentApplication.invoice_id == invoice_id)
    ).all():
        rows = session.exec(
            select(ApplicationIncome).where(ApplicationIncome.application_id == app.id)
        ).all()
        assert all(r.amount > 0 for r in rows)
        assert sum((r.amount for r in rows), D("0.00")) == app.amount
        if session.get(Payment, app.payment_id).status == "posted":
            for r in rows:
                totals[r.account_id] = totals.get(r.account_id, D("0.00")) + r.amount
    return totals


def _expected(items, prices):
    return {i.income_account_id: D(p) for i, p in zip(items, prices)}


@pytest.mark.parametrize(
    "parts",
    [
        ["33.33", "33.33", "33.34"],
        ["10.00", "10.00", "80.00"],
        ["80.00", "10.00", "10.00"],
        ["0.01"] * 7 + ["99.93"],
        ["0.01", "99.99"],
        ["99.99", "0.01"],
        ["25.00"] * 4,
        ["0.03", "0.07", "49.90", "50.00"],
    ],
)
def test_installments_give_exact_per_account_totals(
    session, owner_user, customer, chart, make_incomes, parts
):
    items = make_incomes(3)
    prices = ["50.00", "30.00", "20.00"]
    assert sum(D(p) for p in parts) == D("100.00")
    invoice = _issue(session, owner_user, customer, items, prices)
    _pay_parts(session, owner_user, customer, chart, invoice, parts)
    assert _live_slices(session, invoice.id) == _expected(items, prices)


@pytest.mark.parametrize("seed", range(25))
def test_random_installment_partitions_are_exact(
    session, owner_user, customer, chart, make_incomes, seed
):
    import random

    rng = random.Random(seed)
    n = rng.choice([3, 4])
    items = make_incomes(n)
    cents = [rng.randint(1, 4000) for _ in range(n)]
    cents[-1] += 1  # odd totals such as 100.01
    prices = [f"{c / 100:.2f}" for c in cents]
    total = sum(cents)
    cuts = sorted(rng.sample(range(1, total), rng.randint(1, min(9, total - 1))))
    parts = [f"{(b - a) / 100:.2f}" for a, b in zip([0, *cuts], [*cuts, total])]
    invoice = _issue(session, owner_user, customer, items, prices)
    assert invoice.total == D(total) / 100
    _pay_parts(session, owner_user, customer, chart, invoice, parts)
    assert _balance(session, invoice) == D("0.00")
    assert _live_slices(session, invoice.id) == _expected(items, prices)


def test_void_then_repay_ends_with_exact_live_and_cash_basis_totals(
    session, owner_user, customer, chart, make_incomes
):
    from payroll.reports import queries
    from payroll.reports.basis import Basis

    items = make_incomes(3)
    prices = ["50.00", "30.00", "20.00"]
    invoice = _issue(session, owner_user, customer, items, prices)
    keep = _record(
        session, owner_user, customer, chart, "33.33", applications=_apps((invoice, "33.33"))
    )
    bad = _record(
        session, owner_user, customer, chart, "33.33", applications=_apps((invoice, "33.33"))
    )
    PaymentService(session).void(bad.id, actor=owner_user, void_date=DAY)
    _pay_parts(session, owner_user, customer, chart, invoice, ["0.01", "33.33", "33.33"])
    assert keep.status == "posted" and _balance(session, invoice) == D("0.00")
    assert _live_slices(session, invoice.id) == _expected(items, prices)
    totals = {
        t.account.id: t.credit - t.debit
        for t in queries.account_totals(
            session, basis=Basis.CASH, account_types=[AccountType.INCOME], end=date(2026, 12, 31)
        )
    }
    assert totals == _expected(items, prices)


def _pay_checked(session, owner_user, customer, chart, invoice, items, prices, parts):
    """Pay in installments; per application: 0 < slice <= remaining cap, slices sum to amount."""
    expected = _expected(items, prices)
    for part in parts:
        caps = {
            a: t - _live_slices(session, invoice.id).get(a, D("0.00")) for a, t in expected.items()
        }
        payment = _record(
            session, owner_user, customer, chart, part, applications=_apps((invoice, part))
        )
        app = session.exec(
            select(PaymentApplication).where(PaymentApplication.payment_id == payment.id)
        ).one()
        rows = session.exec(
            select(ApplicationIncome).where(ApplicationIncome.application_id == app.id)
        ).all()
        assert sum((r.amount for r in rows), D("0.00")) == D(part)
        for r in rows:
            assert D("0.00") < r.amount <= caps[r.account_id]
    return payment


def _cash_income(session):
    from payroll.reports import queries
    from payroll.reports.basis import Basis

    return {
        t.account.id: t.credit - t.debit
        for t in queries.account_totals(
            session, basis=Basis.CASH, account_types=[AccountType.INCOME], end=date(2026, 12, 31)
        )
    }


@pytest.mark.parametrize(
    ("prices", "parts"),
    [
        (["30.00", "30.00", "30.00", "10.01"], ["99.99", "0.02"]),
        (["33.33", "33.33", "33.33", "0.02"], ["37.53", "37.53", "24.95"]),
    ],
)
def test_four_account_counterexamples_end_exact(
    session, owner_user, customer, chart, make_incomes, prices, parts
):
    items = make_incomes(4)
    invoice = _issue(session, owner_user, customer, items, prices)
    _pay_checked(session, owner_user, customer, chart, invoice, items, prices, parts)
    assert _balance(session, invoice) == D("0.00")
    assert _live_slices(session, invoice.id) == _expected(items, prices)
    assert _cash_income(session) == _expected(items, prices)


@pytest.mark.parametrize("seed", range(40))
def test_random_four_and_five_accounts_with_tiny_lines_are_exact(
    session, owner_user, customer, chart, make_incomes, seed
):
    import random

    rng = random.Random(1000 + seed)
    n = rng.choice([4, 5])
    cents = [rng.randint(1, 3) if rng.random() < 0.5 else rng.randint(100, 4000) for _ in range(n)]
    cents[rng.choice([n - 1, rng.randrange(n)])] = rng.randint(1, 3)
    items = make_incomes(n)
    prices = [f"{c / 100:.2f}" for c in cents]
    total = sum(cents)
    cuts = sorted(rng.sample(range(1, total), rng.randint(1, min(7, total - 1))))
    parts = [f"{(b - a) / 100:.2f}" for a, b in zip([0, *cuts], [*cuts, total])]
    invoice = _issue(session, owner_user, customer, items, prices)
    _pay_checked(session, owner_user, customer, chart, invoice, items, prices, parts)
    assert _balance(session, invoice) == D("0.00")
    assert _live_slices(session, invoice.id) == _expected(items, prices)


def test_four_account_void_then_repay_ends_exact(
    session, owner_user, customer, chart, make_incomes
):
    items = make_incomes(4)
    prices = ["30.00", "30.00", "30.00", "10.01"]
    invoice = _issue(session, owner_user, customer, items, prices)
    _record(session, owner_user, customer, chart, "50.00", applications=_apps((invoice, "50.00")))
    bad = _record(
        session, owner_user, customer, chart, "49.99", applications=_apps((invoice, "49.99"))
    )
    PaymentService(session).void(bad.id, actor=owner_user, void_date=DAY)
    _pay_checked(session, owner_user, customer, chart, invoice, items, prices, ["0.02", "49.99"])
    assert _balance(session, invoice) == D("0.00")
    assert _live_slices(session, invoice.id) == _expected(items, prices)
    assert _cash_income(session) == _expected(items, prices)


def test_void_before_the_payment_date_refused_and_on_the_date_allowed(
    session, owner_user, customer, item, chart
):
    payment = _record(session, owner_user, customer, chart)  # received 2026-03-01
    entries = session.exec(select(func.count()).select_from(JournalEntry)).one()
    with pytest.raises(InvalidPaymentError) as exc:
        PaymentService(session).void(payment.id, actor=owner_user, void_date=date(2026, 2, 28))
    assert exc.value.message == "The void date cannot be before the payment date (2026-03-01)"
    session.refresh(payment)
    assert payment.status == "posted" and payment.void_date is None
    assert session.exec(select(func.count()).select_from(JournalEntry)).one() == entries
    PaymentService(session).void(payment.id, actor=owner_user, void_date=DAY)
    session.refresh(payment)
    assert payment.status == "void" and payment.void_date == DAY
