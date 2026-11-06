"""Transfers between company accounts: validation, posting, voids, audit."""

# ruff: noqa: F811  (fixtures re-exported by conftest are used as arguments)

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text as sql_text
from sqlmodel import func, select

from payroll.audit.models import AuditLog
from payroll.auth.permissions import PermissionDeniedError
from payroll.expenses.exceptions import InvalidTransferError, TransferError, TransferNotFoundError
from payroll.expenses.expenses import ExpenseLineInput, ExpenseService
from payroll.expenses.models import Transfer
from payroll.expenses.transfers import TransferService
from payroll.ledger.accounts import AccountService
from payroll.ledger.exceptions import PeriodLockedError
from payroll.ledger.models import AccountType, JournalEntry, SystemRole
from tests.ledger.conftest import chart  # noqa: F401

D = Decimal
DAY = date(2026, 3, 1)
LATER = date(2026, 3, 5)


@pytest.fixture(autouse=True)
def _committed_fixtures(session, owner_user, chart, expense_account, card, owner_role):
    """Commit the fixture rows: a service rollback must not wipe them mid-test."""
    session.commit()


@pytest.fixture
def bank(chart, owner_user, session):
    return AccountService(session).create(actor=owner_user, name="Checking", type=AccountType.ASSET)


@pytest.fixture
def savings(chart, owner_user, session):
    return AccountService(session).create(actor=owner_user, name="Savings", type=AccountType.ASSET)


def make(session, owner_user, src, dst, amount="50.00", **kw):
    kw.setdefault("transfer_date", DAY)
    return TransferService(session).create(
        actor=owner_user,
        from_account_id=src.id,
        to_account_id=dst.id,
        amount=D(amount),
        **kw,
    )


def entry_count(session):
    return session.exec(select(func.count()).select_from(JournalEntry)).one()


def transfer_count(session):
    return session.exec(select(func.count()).select_from(Transfer)).one()


def balance(session, account):
    row = session.execute(
        sql_text(
            "SELECT COALESCE(SUM(l.debit - l.credit), 0) FROM journal_line l "
            "JOIN journal_entry e ON e.id = l.entry_id WHERE l.account_id = :a"
        ),
        {"a": account.id},
    ).scalar_one()
    return D(row)


def rejected(session, owner_user, src, dst, fragment=None, error=InvalidTransferError, **kw):
    before = entry_count(session)
    with pytest.raises(error) as exc:
        make(session, owner_user, src, dst, **kw)
    if fragment:
        assert fragment in exc.value.message
    assert entry_count(session) == before
    assert transfer_count(session) == 0
    return exc.value


# ------------------------------------------------------------------ create


def test_create_posts_dr_to_cr_from(session, owner_user, bank, savings):
    transfer = make(session, owner_user, bank, savings, "125.50", memo="  move  ")
    assert (transfer.amount, transfer.status, transfer.memo) == (D("125.50"), "posted", "move")
    assert transfer.entry_id is not None
    assert balance(session, savings) == D("125.50")
    assert balance(session, bank) == D("-125.50")
    entry = session.get(JournalEntry, transfer.entry_id)
    assert entry.source_type == "transfer" and entry.source_id == str(transfer.id)
    assert entry.memo == "move" or "move" in entry.memo


def test_default_memo_is_readable(session, owner_user, bank, savings):
    transfer = make(session, owner_user, bank, savings)
    assert session.get(JournalEntry, transfer.entry_id).memo == "Transfer"


def test_create_audits(session, owner_user, bank, savings):
    transfer = make(session, owner_user, bank, savings)
    row = session.exec(select(AuditLog).where(AuditLog.action == "transfer.create")).one()
    assert row.entity_id == str(transfer.id)


def test_credit_card_payment_flow(session, owner_user, bank, card, expense_account):
    """Review Focus 4: pay a card expense by transfer; voiding it restores the credit."""
    ExpenseService(session).create(
        actor=owner_user,
        expense_date=DAY,
        lines=[ExpenseLineInput(expense_account.id, D("80.00"))],
        source="account",
        paid_from_account_id=card.id,
        payee_text="Shop",
    )
    assert balance(session, card) == D("-80.00")
    transfer = make(session, owner_user, bank, card, "80.00")
    assert balance(session, card) == D("0.00")
    TransferService(session).void(transfer.id, actor=owner_user, void_date=LATER)
    assert balance(session, card) == D("-80.00")


@pytest.mark.parametrize("role", [SystemRole.ACCOUNTS_RECEIVABLE, SystemRole.UNDEPOSITED_FUNDS])
@pytest.mark.parametrize("end", ["from", "to"])
def test_bound_role_accounts_refused(session, owner_user, bank, role, end):
    bound = AccountService(session).account_by_role(role)
    assert bound is not None
    session.commit()
    pair = (bound, bank) if end == "from" else (bank, bound)
    rejected(session, owner_user, *pair)


@pytest.mark.parametrize("end", ["from", "to"])
def test_due_to_owner_refused(session, owner_user, bank, owner_role, end):
    pair = (owner_role, bank) if end == "from" else (bank, owner_role)
    rejected(session, owner_user, *pair)


def test_equal_accounts_refused(session, owner_user, bank):
    rejected(session, owner_user, bank, bank, "different")


@pytest.mark.parametrize("end", ["from", "to"])
def test_revenue_account_refused(session, owner_user, bank, chart, end):
    revenue = AccountService(session).create(
        actor=owner_user, name="Sales Test", type=AccountType.INCOME
    )
    session.commit()
    pair = (revenue, bank) if end == "from" else (bank, revenue)
    rejected(session, owner_user, *pair)


@pytest.mark.parametrize("end", ["from", "to"])
def test_inactive_account_refused(session, owner_user, bank, savings, end):
    target = bank if end == "from" else savings
    AccountService(session).set_active(target.id, False, actor=owner_user)
    session.commit()
    rejected(session, owner_user, bank, savings, "inactive")


def test_missing_account_refused(session, owner_user, bank):
    with pytest.raises(InvalidTransferError):
        TransferService(session).create(
            actor=owner_user,
            transfer_date=DAY,
            from_account_id=bank.id,
            to_account_id=999999,
            amount=D("1.00"),
        )
    assert transfer_count(session) == 0


@pytest.mark.parametrize(
    ("amount", "fragment"),
    [
        (D("0.00"), "between"),
        (D("-1.00"), "between"),
        (D("1.005"), "two decimal"),
        (D("1E+30"), "between"),
        (D("100000000.01"), "between"),
        (D("NaN"), "number"),
        (D("Infinity"), "number"),
        (50, "number"),
        (50.0, "number"),
        ("50.00", "number"),
        (True, "number"),
        (None, "number"),
    ],
)
def test_bad_amounts_refused(session, owner_user, bank, savings, amount, fragment):
    before = entry_count(session)
    with pytest.raises(InvalidTransferError) as exc:
        TransferService(session).create(
            actor=owner_user,
            transfer_date=DAY,
            from_account_id=bank.id,
            to_account_id=savings.id,
            amount=amount,
        )
    assert fragment in exc.value.message
    assert entry_count(session) == before and transfer_count(session) == 0


def test_max_amount_accepted(session, owner_user, bank, savings):
    assert make(session, owner_user, bank, savings, "100000000.00").amount == D("100000000.00")


@pytest.mark.parametrize("bad", ["1", 1.5, True, None, [1]])
def test_non_int_account_ids_refused(session, owner_user, bank, bad):
    for kwargs in (
        {"from_account_id": bad, "to_account_id": bank.id},
        {"from_account_id": bank.id, "to_account_id": bad},
    ):
        with pytest.raises(InvalidTransferError):
            TransferService(session).create(
                actor=owner_user, transfer_date=DAY, amount=D("1.00"), **kwargs
            )
    assert transfer_count(session) == 0


@pytest.mark.parametrize(
    "bad", [None, "2026-03-01", 20260301, date(1899, 12, 31), date(9999, 12, 31)]
)
def test_unsupported_dates_refused(session, owner_user, bank, savings, bad):
    rejected(session, owner_user, bank, savings, transfer_date=bad)


def test_memo_validation(session, owner_user, bank, savings):
    rejected(session, owner_user, bank, savings, "Memo", memo="x" * 501)
    rejected(session, owner_user, bank, savings, "control", memo="a\x00b")
    rejected(session, owner_user, bank, savings, "text", memo=5)


def test_lock_date_refused(session, owner_user, bank, savings):
    session.execute(sql_text("UPDATE period_lock SET lock_date = :d WHERE id = 1"), {"d": LATER})
    session.commit()
    rejected(session, owner_user, bank, savings, error=PeriodLockedError)


@pytest.mark.parametrize("who", ["accountant_user", "viewer_user"])
def test_non_owner_refused_create_and_void(session, owner_user, bank, savings, request, who):
    actor = request.getfixturevalue(who)
    session.commit()
    with pytest.raises(PermissionDeniedError):
        TransferService(session).create(
            actor=actor,
            transfer_date=DAY,
            from_account_id=bank.id,
            to_account_id=savings.id,
            amount=D("1.00"),
        )
    assert transfer_count(session) == 0
    transfer = make(session, owner_user, bank, savings)
    with pytest.raises(PermissionDeniedError):
        TransferService(session).void(transfer.id, actor=actor, void_date=LATER)
    assert session.get(Transfer, transfer.id).status == "posted"


# -------------------------------------------------------------------- void


def test_void_reverses_and_audits(session, owner_user, bank, savings):
    transfer = make(session, owner_user, bank, savings)
    voided = TransferService(session).void(transfer.id, actor=owner_user, void_date=LATER)
    assert (voided.status, voided.void_date) == ("void", LATER)
    assert voided.void_entry_id is not None
    assert balance(session, savings) == D("0.00") and balance(session, bank) == D("0.00")
    reversal = session.get(JournalEntry, voided.void_entry_id)
    assert reversal.entry_date == LATER and "2026-03-01" in reversal.memo
    assert "transfer" in reversal.memo
    assert session.exec(select(AuditLog).where(AuditLog.action == "transfer.void")).one()


def test_void_custom_memo(session, owner_user, bank, savings):
    transfer = make(session, owner_user, bank, savings)
    voided = TransferService(session).void(
        transfer.id, actor=owner_user, void_date=LATER, memo="oops"
    )
    assert session.get(JournalEntry, voided.void_entry_id).memo == "oops"


def test_void_date_before_transfer_refused(session, owner_user, bank, savings):
    transfer = make(session, owner_user, bank, savings)
    entries = entry_count(session)
    for bad in (date(2026, 2, 28), None, "2026-03-05"):
        with pytest.raises(InvalidTransferError):
            TransferService(session).void(transfer.id, actor=owner_user, void_date=bad)
    assert entry_count(session) == entries
    assert session.get(Transfer, transfer.id).status == "posted"


def test_void_on_lock_date_refused(session, owner_user, bank, savings):
    transfer = make(session, owner_user, bank, savings)
    session.execute(sql_text("UPDATE period_lock SET lock_date = :d WHERE id = 1"), {"d": LATER})
    session.commit()
    entries = entry_count(session)
    with pytest.raises(PeriodLockedError):
        TransferService(session).void(transfer.id, actor=owner_user, void_date=LATER)
    assert entry_count(session) == entries
    assert session.get(Transfer, transfer.id).status == "posted"


def test_second_void_refused(session, owner_user, bank, savings):
    transfer = make(session, owner_user, bank, savings)
    TransferService(session).void(transfer.id, actor=owner_user, void_date=LATER)
    entries = entry_count(session)
    with pytest.raises(TransferError):
        TransferService(session).void(transfer.id, actor=owner_user, void_date=LATER)
    assert entry_count(session) == entries


def test_void_missing_transfer(session, owner_user):
    with pytest.raises(TransferNotFoundError):
        TransferService(session).void(999999, actor=owner_user, void_date=LATER)


@pytest.mark.parametrize("bad", ["1", 1.5, True, None])
def test_void_non_int_id_refused(session, owner_user, bad):
    with pytest.raises((InvalidTransferError, TransferNotFoundError)):
        TransferService(session).void(bad, actor=owner_user, void_date=LATER)


# ------------------------------------------------------------------- reads


def test_get_and_list(session, owner_user, bank, savings):
    first = make(session, owner_user, bank, savings, "1.00")
    second = make(session, owner_user, bank, savings, "2.00", transfer_date=LATER)
    service = TransferService(session)
    assert service.get(first.id).amount == D("1.00")
    with pytest.raises(TransferNotFoundError):
        service.get(999999)
    assert [t.id for t in service.list()] == [second.id, first.id]
    assert [t.id for t in service.list(limit=1, offset=1)] == [first.id]


@pytest.mark.parametrize(
    ("limit", "offset"), [(0, 0), (201, 0), (-1, 0), (10, -1), (True, 0), ("5", 0), (5, None)]
)
def test_list_validates_paging(session, limit, offset):
    with pytest.raises(InvalidTransferError):
        TransferService(session).list(limit=limit, offset=offset)
