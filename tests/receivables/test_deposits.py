"""Bank deposits: posting, availability, validation, voids, audit."""

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
from payroll.ledger.models import Account, JournalEntry, JournalLine
from payroll.receivables.deposits import DepositService, available
from payroll.receivables.exceptions import DepositError, InvalidDepositError
from payroll.receivables.models import Deposit
from payroll.receivables.payments import PaymentService
from tests.conftest import make_user
from tests.ledger.conftest import chart  # noqa: F401

D = Decimal
DAY = date(2026, 3, 1)
LATER = date(2026, 3, 5)


@pytest.fixture(autouse=True)
def _committed_fixtures(session, owner_user, customer, chart):
    """Commit the fixture rows: a service rollback must not wipe them mid-test."""
    session.commit()


def _pay(session, owner_user, customer, chart, amount="100.00", account="Undeposited Funds", **kw):
    kw.setdefault("received_date", DAY)
    return PaymentService(session).record(
        actor=owner_user,
        customer_id=customer.id,
        amount=D(amount),
        deposit_account_id=chart[account].id,
        method="check",
        **kw,
    )


def _deposit(session, owner_user, chart, payments, day=LATER, bank="Business Checking", **kw):
    return DepositService(session).create(
        actor=owner_user,
        deposit_date=day,
        bank_account_id=chart[bank].id,
        payment_ids=[p.id for p in payments],
        **kw,
    )


def _balance(session, account):
    return AccountService(session).balances().get(account.id, D("0.00"))


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


def test_create_deposit_moves_undeposited_to_bank(session, owner_user, customer, chart):
    payment = _pay(session, owner_user, customer, chart, "125.50")
    undeposited, bank = chart["Undeposited Funds"], chart["Business Checking"]
    assert _balance(session, undeposited) == D("125.50")
    bank_before = _balance(session, bank)

    deposit = _deposit(session, owner_user, chart, [payment], memo="Friday run")

    entry = session.get(JournalEntry, deposit.entry_id)
    assert (entry.entry_date, entry.source_type, entry.source_id) == (
        LATER,
        "deposit",
        str(deposit.id),
    )
    assert [(ln.account_id, ln.debit, ln.credit) for ln in _lines(session, entry.id)] == [
        (bank.id, D("125.50"), D("0.00")),
        (undeposited.id, D("0.00"), D("125.50")),
    ]
    assert (deposit.status, deposit.total, deposit.bank_account_id) == (
        "posted",
        D("125.50"),
        bank.id,
    )
    assert _balance(session, undeposited) == D("0.00")
    assert _balance(session, bank) == bank_before + D("125.50")
    session.refresh(payment)
    assert payment.deposit_id == deposit.id


def test_deposit_groups_several_payments_one_entry(session, owner_user, customer, chart):
    payments = [
        _pay(session, owner_user, customer, chart, "10.00"),
        _pay(session, owner_user, customer, chart, "20.25"),
        _pay(session, owner_user, customer, chart, "30.50"),
    ]
    before = _entry_count(session)
    deposit = _deposit(session, owner_user, chart, payments)
    assert _entry_count(session) == before + 1
    assert deposit.total == D("60.75")
    assert [ln.debit for ln in _lines(session, deposit.entry_id)] == [D("60.75"), D("0.00")]
    assert {p.deposit_id for p in DepositService(session).payments(deposit.id)} == {deposit.id}
    assert len(DepositService(session).payments(deposit.id)) == 3
    assert _balance(session, chart["Undeposited Funds"]) == D("0.00")


def test_available_excludes_bank_direct_void_and_already_deposited_payments(
    session, owner_user, customer, chart
):
    later = _pay(session, owner_user, customer, chart, "1.00", received_date=LATER)
    oldest = _pay(session, owner_user, customer, chart, "2.00", received_date=date(2026, 2, 1))
    _pay(session, owner_user, customer, chart, "3.00", account="Business Checking")
    voided = _pay(session, owner_user, customer, chart, "4.00")
    PaymentService(session).void(voided.id, actor=owner_user, void_date=LATER)
    taken = _pay(session, owner_user, customer, chart, "5.00")
    _deposit(session, owner_user, chart, [taken])

    assert [p.id for p in available(session)] == [oldest.id, later.id]
    assert [p.id for p in DepositService(session).available()] == [oldest.id, later.id]


def test_rejects_payment_dated_after_deposit_date(session, owner_user, customer, chart):
    payment = _pay(session, owner_user, customer, chart, received_date=LATER)
    before = _entry_count(session)
    with pytest.raises(InvalidDepositError) as exc:
        _deposit(session, owner_user, chart, [payment], day=DAY)
    assert "after the deposit date" in exc.value.message
    assert _entry_count(session) == before
    session.refresh(payment)
    assert payment.deposit_id is None
    # the same date is fine
    assert _deposit(session, owner_user, chart, [payment], day=LATER).total == D("100.00")


def test_rejects_same_payment_in_two_deposits_even_concurrently(
    session, owner_user, customer, chart
):
    """Sequential case here; the real-thread race is in test_deposit_concurrency.py."""
    payment = _pay(session, owner_user, customer, chart)
    first = _deposit(session, owner_user, chart, [payment])
    with pytest.raises(DepositError) as exc:
        _deposit(session, owner_user, chart, [payment])
    assert "already in a deposit" in exc.value.message
    assert session.exec(select(func.count()).select_from(Deposit)).one() == 1
    session.refresh(payment)
    assert payment.deposit_id == first.id


def test_rejects_void_and_bank_direct_payments(session, owner_user, customer, chart):
    voided = _pay(session, owner_user, customer, chart)
    PaymentService(session).void(voided.id, actor=owner_user, void_date=DAY)
    direct = _pay(session, owner_user, customer, chart, account="Business Checking")
    with pytest.raises(DepositError) as exc:
        _deposit(session, owner_user, chart, [voided])
    assert "void" in exc.value.message
    with pytest.raises(InvalidDepositError) as exc2:
        _deposit(session, owner_user, chart, [direct])
    assert "Undeposited Funds" in exc2.value.message
    with pytest.raises(InvalidDepositError):
        DepositService(session).create(
            actor=owner_user,
            deposit_date=LATER,
            bank_account_id=chart["Business Checking"].id,
            payment_ids=[999_999],
        )


def test_rejects_bank_equal_to_undeposited_funds_or_inactive_or_non_asset(
    session, owner_user, customer, chart
):
    payment = _pay(session, owner_user, customer, chart)
    for name in ("Undeposited Funds", "Accounts Receivable", "Services Revenue"):
        with pytest.raises(InvalidDepositError):
            _deposit(session, owner_user, chart, [payment], bank=name)
    bank = session.get(Account, chart["Business Checking"].id)
    bank.is_active = False
    session.add(bank)
    session.commit()
    with pytest.raises(InvalidDepositError) as exc:
        _deposit(session, owner_user, chart, [payment])
    assert "inactive" in exc.value.message
    with pytest.raises(InvalidDepositError):
        DepositService(session).create(
            actor=owner_user,
            deposit_date=LATER,
            bank_account_id=999_999,
            payment_ids=[payment.id],
        )
    session.refresh(payment)
    assert payment.deposit_id is None


def test_rejects_empty_selection_and_over_200(session, owner_user, customer, chart):
    with pytest.raises(InvalidDepositError):
        _deposit(session, owner_user, chart, [])
    one = _pay(session, owner_user, customer, chart)
    with pytest.raises(InvalidDepositError):
        DepositService(session).create(
            actor=owner_user,
            deposit_date=LATER,
            bank_account_id=chart["Business Checking"].id,
            payment_ids=[one.id, one.id],
        )
    with pytest.raises(InvalidDepositError) as exc:
        DepositService(session).create(
            actor=owner_user,
            deposit_date=LATER,
            bank_account_id=chart["Business Checking"].id,
            payment_ids=list(range(1, 202)),
        )
    assert "at most 200" in exc.value.message


def test_rejects_bad_date_and_long_memo(session, owner_user, customer, chart):
    payment = _pay(session, owner_user, customer, chart)
    with pytest.raises(InvalidDepositError):
        _deposit(session, owner_user, chart, [payment], day=date(1800, 1, 1))
    with pytest.raises(InvalidDepositError):
        _deposit(session, owner_user, chart, [payment], memo="x" * 501)


def test_void_deposit_reverses_and_frees_payments(session, owner_user, customer, chart):
    payments = [
        _pay(session, owner_user, customer, chart, "10.00"),
        _pay(session, owner_user, customer, chart, "15.00"),
    ]
    undeposited, bank = chart["Undeposited Funds"], chart["Business Checking"]
    bank_before = _balance(session, bank)
    deposit = _deposit(session, owner_user, chart, payments)
    voided_on = date(2026, 3, 9)

    result = DepositService(session).void(deposit.id, actor=owner_user, void_date=voided_on)

    assert (result.status, result.void_date) == ("void", voided_on)
    assert _balance(session, undeposited) == D("25.00")
    assert _balance(session, bank) == bank_before
    original = session.get(JournalEntry, deposit.entry_id)
    assert original.status == "void"
    reversal = session.exec(
        select(JournalEntry).where(JournalEntry.reverses_entry_id == original.id)
    ).one()
    assert (reversal.entry_date, reversal.source_type, reversal.source_id) == (
        voided_on,
        "deposit",
        str(deposit.id),
    )
    for p in payments:
        session.refresh(p)
        assert p.deposit_id is None
    assert {p.id for p in available(session)} == {p.id for p in payments}
    # freed payments can be deposited again, and can now be voided
    again = _deposit(session, owner_user, chart, payments)
    assert again.id != deposit.id
    assert _balance(session, undeposited) == D("0.00")


def test_payment_in_live_deposit_cannot_be_voided_until_deposit_voided(
    session, owner_user, customer, chart
):
    payment = _pay(session, owner_user, customer, chart)
    deposit = _deposit(session, owner_user, chart, [payment])
    with pytest.raises(DepositError):
        PaymentService(session).void(payment.id, actor=owner_user, void_date=LATER)
    DepositService(session).void(deposit.id, actor=owner_user, void_date=LATER)
    PaymentService(session).void(payment.id, actor=owner_user, void_date=LATER)
    session.refresh(payment)
    assert payment.status == "void"


def test_void_deposit_in_locked_period_rejected(session, owner_user, customer, chart):
    payment = _pay(session, owner_user, customer, chart)
    deposit = _deposit(session, owner_user, chart, [payment])
    session.execute(
        sql_text("UPDATE period_lock SET lock_date = :d WHERE id = 1"), {"d": date(2026, 3, 31)}
    )
    session.commit()
    before = _entry_count(session)
    # a void dated in the open period is still refused: the original entry is locked
    with pytest.raises(PeriodLockedError):
        DepositService(session).void(deposit.id, actor=owner_user, void_date=date(2026, 4, 2))
    with pytest.raises(PeriodLockedError):
        DepositService(session).void(deposit.id, actor=owner_user, void_date=date(2026, 3, 10))
    assert _entry_count(session) == before
    session.refresh(deposit)
    session.refresh(payment)
    assert deposit.status == "posted" and payment.deposit_id == deposit.id


def test_void_twice_rejected(session, owner_user, customer, chart):
    payment = _pay(session, owner_user, customer, chart)
    deposit = _deposit(session, owner_user, chart, [payment])
    DepositService(session).void(deposit.id, actor=owner_user, void_date=LATER)
    before = _entry_count(session)
    with pytest.raises(DepositError) as exc:
        DepositService(session).void(deposit.id, actor=owner_user, void_date=LATER)
    assert "already void" in exc.value.message
    assert _entry_count(session) == before


def test_get_and_list(session, owner_user, customer, chart):
    one = _deposit(session, owner_user, chart, [_pay(session, owner_user, customer, chart)])
    two = _deposit(
        session,
        owner_user,
        chart,
        [_pay(session, owner_user, customer, chart)],
        day=date(2026, 3, 8),
    )
    service = DepositService(session)
    assert service.get(one.id).id == one.id
    assert [d.id for d in service.list()] == [two.id, one.id]
    assert [d.id for d in service.list(limit=1, offset=1)] == [one.id]
    with pytest.raises(Exception) as exc:
        service.get(999_999)
    assert exc.value.http_code == 404


def test_owner_only_and_audited(session, owner_user, customer, chart):
    payment = _pay(session, owner_user, customer, chart)
    for role in ("viewer", "accountant"):
        other = make_user(session, email=f"{role}@example.com", role=role)
        session.commit()
        with pytest.raises(PermissionDeniedError):
            _deposit(session, other, chart, [payment])
    assert session.exec(select(func.count()).select_from(Deposit)).one() == 0

    deposit = _deposit(session, owner_user, chart, [payment])
    other = make_user(session, email="viewer2@example.com", role="viewer")
    session.commit()
    with pytest.raises(PermissionDeniedError):
        DepositService(session).void(deposit.id, actor=other, void_date=LATER)
    session.refresh(deposit)
    assert deposit.status == "posted"

    DepositService(session).void(deposit.id, actor=owner_user, void_date=LATER)
    created = _audit(session, "deposit.create")
    voided = _audit(session, "deposit.void")
    assert len(created) == 1 and len(voided) == 1
    assert created[0].entity_id == str(deposit.id) == voided[0].entity_id
    assert created[0].user_id == owner_user.id
    assert created[0].after["payment_ids"] == [payment.id]
    assert voided[0].after["freed_payment_ids"] == [payment.id]
    assert voided[0].before["status"] == "posted"
