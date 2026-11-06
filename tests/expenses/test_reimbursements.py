"""Reimbursements: total, posting, validation, voids, audit."""

# ruff: noqa: F811  (fixtures re-exported by conftest are used as arguments)

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlmodel import func, select

from payroll.audit.models import AuditLog
from payroll.auth.permissions import PermissionDeniedError
from payroll.expenses.exceptions import (
    ExpenseError,
    InvalidReimbursementError,
    ReimbursementError,
    ReimbursementNotFoundError,
)
from payroll.expenses.expenses import ExpenseLineInput, ExpenseService
from payroll.expenses.models import Expense, Reimbursement
from payroll.expenses.reimbursements import ReimbursementService
from payroll.ledger.accounts import AccountService
from payroll.ledger.exceptions import PeriodLockedError
from payroll.ledger.models import AccountType, JournalEntry, JournalLine, SourceType, SystemRole
from tests.conftest import make_user
from tests.ledger.conftest import chart  # noqa: F401

D = Decimal
DAY = date(2026, 3, 1)
LATER = date(2026, 3, 5)
LATEST = date(2026, 3, 9)


@pytest.fixture(autouse=True)
def _committed_fixtures(session, owner_user, chart, expense_account, card, owner_role):
    """Commit the fixture rows: a service rollback must not wipe them mid-test."""
    session.commit()


def owner_expense(session, owner_user, account, amount="10.00", day=DAY, **kw):
    return ExpenseService(session).create(
        actor=owner_user,
        expense_date=day,
        lines=[ExpenseLineInput(account.id, D(amount))],
        source="owner",
        paid_from_account_id=None,
        payee_text="Cash buy",
        **kw,
    )


def reimburse(session, owner_user, chart, expenses, day=LATER, **kw):
    kw.setdefault("paid_from_account_id", chart["Business Checking"].id)
    return ReimbursementService(session).create(
        actor=owner_user,
        reimbursement_date=day,
        expense_ids=[e.id for e in expenses],
        **kw,
    )


def entry_count(session):
    return session.exec(select(func.count()).select_from(JournalEntry)).one()


def reim_count(session):
    return session.exec(select(func.count()).select_from(Reimbursement)).one()


def links(session):
    return dict(session.exec(select(Expense.id, Expense.reimbursement_id)).all())


def rejected(
    session, owner_user, chart, expenses, fragment=None, error=InvalidReimbursementError, **kw
):
    before = (entry_count(session), reim_count(session), links(session))
    with pytest.raises(error) as exc:
        reimburse(session, owner_user, chart, expenses, **kw)
    if fragment:
        assert fragment in exc.value.message
    assert (entry_count(session), reim_count(session), links(session)) == before
    return exc.value


def lines_of(session, entry_id):
    rows = session.exec(
        select(JournalLine).where(JournalLine.entry_id == entry_id).order_by(JournalLine.line_no)
    ).all()
    return [(r.account_id, r.debit, r.credit) for r in rows]


# ------------------------------------------------------------------ create


def test_total_is_the_sum_and_the_entry_is_due_to_owner_against_the_bank(
    session, owner_user, chart, expense_account, owner_role
):
    expenses = [
        owner_expense(session, owner_user, expense_account, "12.34"),
        owner_expense(session, owner_user, expense_account, "100.00", day=LATER),
        owner_expense(session, owner_user, expense_account, "0.66"),
    ]
    reim = reimburse(session, owner_user, chart, expenses, memo="  March  ")
    bank = chart["Business Checking"]
    assert reim.total == D("113.00")
    assert (reim.status, reim.memo, reim.date) == ("posted", "March", LATER)
    assert reim.paid_from_account_id == bank.id
    assert lines_of(session, reim.entry_id) == [
        (owner_role.id, D("113.00"), D("0.00")),
        (bank.id, D("0.00"), D("113.00")),
    ]
    entry = session.get(JournalEntry, reim.entry_id)
    assert (entry.source_type, entry.source_id) == (SourceType.REIMBURSEMENT, str(reim.id))
    assert set(links(session).values()) == {reim.id}
    service = ReimbursementService(session)
    # oldest first, then by id
    assert [e.id for e in service.expenses(reim.id)] == [
        expenses[0].id,
        expenses[2].id,
        expenses[1].id,
    ]
    assert service.get(reim.id).id == reim.id
    assert [r.id for r in service.list()] == [reim.id]


def test_create_is_audited(session, owner_user, chart, expense_account):
    expense = owner_expense(session, owner_user, expense_account)
    reim = reimburse(session, owner_user, chart, [expense])
    row = session.exec(select(AuditLog).where(AuditLog.action == "reimbursement.create")).one()
    assert row.entity_id == str(reim.id)
    assert row.after["expense_ids"] == [expense.id]
    assert row.after["total"] == "10.00"


def test_ids_may_be_given_in_any_order(session, owner_user, chart, expense_account):
    a = owner_expense(session, owner_user, expense_account, "1.00")
    b = owner_expense(session, owner_user, expense_account, "2.00")
    reim = reimburse(session, owner_user, chart, [b, a])
    assert reim.total == D("3.00")


def test_already_reimbursed_is_refused(session, owner_user, chart, expense_account):
    expense = owner_expense(session, owner_user, expense_account)
    other = owner_expense(session, owner_user, expense_account)
    first = reimburse(session, owner_user, chart, [expense])
    exc = rejected(
        session, owner_user, chart, [expense, other], "already reimbursed", ReimbursementError
    )
    assert exc.http_code == 409
    assert links(session)[other.id] is None
    assert links(session)[expense.id] == first.id


def test_void_expense_is_refused(session, owner_user, chart, expense_account):
    expense = owner_expense(session, owner_user, expense_account)
    ExpenseService(session).void(expense.id, actor=owner_user, void_date=LATER)
    rejected(session, owner_user, chart, [expense], "void", ReimbursementError)


def test_account_paid_expense_is_refused(session, owner_user, chart, expense_account, card):
    expense = ExpenseService(session).create(
        actor=owner_user,
        expense_date=DAY,
        lines=[ExpenseLineInput(expense_account.id, D("5.00"))],
        source="account",
        paid_from_account_id=card.id,
        payee_text="Shop",
    )
    rejected(session, owner_user, chart, [expense], "not paid by the owner", ReimbursementError)


def test_missing_and_duplicate_and_bad_ids_are_refused(session, owner_user, chart, expense_account):
    expense = owner_expense(session, owner_user, expense_account)
    svc = ReimbursementService(session)
    bank = chart["Business Checking"].id
    before = (entry_count(session), reim_count(session), links(session))
    for ids, fragment in [
        ([expense.id, expense.id + 999], "not found"),
        ([expense.id, expense.id], "only once"),
        ([], "at least one"),
        (list(range(1, 202)), "at most 200"),
        ([expense.id, True], "valid expenses"),
        ([expense.id, "7"], "valid expenses"),
        ([expense.id, 1.5], "valid expenses"),
    ]:
        with pytest.raises(InvalidReimbursementError) as exc:
            svc.create(
                actor=owner_user,
                reimbursement_date=LATER,
                paid_from_account_id=bank,
                expense_ids=ids,
            )
        assert fragment in exc.value.message, ids
    assert (entry_count(session), reim_count(session), links(session)) == before


def test_date_before_an_expense_is_refused(session, owner_user, chart, expense_account):
    a = owner_expense(session, owner_user, expense_account, day=DAY)
    b = owner_expense(session, owner_user, expense_account, day=LATER)
    rejected(session, owner_user, chart, [a, b], "before", day=date(2026, 3, 4))
    assert reimburse(session, owner_user, chart, [a, b], day=LATER).date == LATER  # same day ok


def test_unsupported_and_non_date_values_are_refused(session, owner_user, chart, expense_account):
    expense = owner_expense(session, owner_user, expense_account)
    for bad in (date(1800, 1, 1), "2026-03-05", None):
        rejected(session, owner_user, chart, [expense], "date", day=bad)


def test_bank_account_rules(session, owner_user, chart, expense_account, card):
    expense = owner_expense(session, owner_user, expense_account)
    ar = chart["Accounts Receivable (A/R)"] if "Accounts Receivable (A/R)" in chart else None
    for account_id, fragment in [
        (card.id, "bank"),  # a liability
        (expense_account.id, "bank"),
        (AccountService(session).account_by_role(SystemRole.DUE_TO_OWNER).id, "bank"),
        (999999, "not found"),
        (None, "Choose"),
        (True, "Choose"),
    ]:
        rejected(session, owner_user, chart, [expense], fragment, paid_from_account_id=account_id)
    if ar is not None:
        rejected(session, owner_user, chart, [expense], "bank", paid_from_account_id=ar.id)


def test_inactive_bank_account_is_refused(session, owner_user, chart, expense_account):
    expense = owner_expense(session, owner_user, expense_account)
    savings = AccountService(session).create(
        actor=owner_user, name="Old Savings", type=AccountType.ASSET
    )
    AccountService(session).set_active(savings.id, False, actor=owner_user)
    rejected(session, owner_user, chart, [expense], "inactive", paid_from_account_id=savings.id)


def test_memo_rules(session, owner_user, chart, expense_account):
    expense = owner_expense(session, owner_user, expense_account)
    rejected(session, owner_user, chart, [expense], "at most 500", memo="x" * 501)
    rejected(session, owner_user, chart, [expense], "control", memo="a\x00b")
    rejected(session, owner_user, chart, [expense], "text", memo=5)


def test_unbound_role_is_refused(session, owner_user, chart, expense_account, owner_role):
    expense = owner_expense(session, owner_user, expense_account)
    session.execute(
        text("UPDATE account SET system_role = NULL WHERE id = :i"),
        {"i": owner_role.id},
    )
    session.commit()
    rejected(session, owner_user, chart, [expense], "Bind Due to Owner in Settings > Ledger first")


def test_locked_period_leaves_nothing_behind(session, owner_user, chart, expense_account):
    expense = owner_expense(session, owner_user, expense_account)
    session.execute(text("UPDATE period_lock SET lock_date = :d WHERE id = 1"), {"d": LATER})
    session.commit()
    rejected(session, owner_user, chart, [expense], error=PeriodLockedError, day=LATER)


def test_only_the_owner_may_mutate(session, owner_user, chart, expense_account):
    others = [
        make_user(session, email=f"{role}@example.com", role=role)
        for role in ("viewer", "accountant")
    ]
    expense = owner_expense(session, owner_user, expense_account)
    session.commit()
    for other in others:
        before = (entry_count(session), reim_count(session), links(session))
        with pytest.raises(PermissionDeniedError):
            reimburse(session, other, chart, [expense])
        assert (entry_count(session), reim_count(session), links(session)) == before
    reim = reimburse(session, owner_user, chart, [expense])
    for other in others:
        with pytest.raises(PermissionDeniedError):
            ReimbursementService(session).void(reim.id, actor=other, void_date=LATEST)
    session.refresh(reim)
    assert reim.status == "posted"


def test_list_paging_and_get_not_found(session, owner_user, chart, expense_account):
    expenses = [owner_expense(session, owner_user, expense_account) for _ in range(3)]
    reims = [reimburse(session, owner_user, chart, [e], day=LATER) for e in expenses]
    svc = ReimbursementService(session)
    assert [r.id for r in svc.list()] == [r.id for r in reversed(reims)]
    assert [r.id for r in svc.list(limit=1, offset=1)] == [reims[1].id]
    for limit, offset in [(0, 0), (201, 0), (10, -1), (True, 0), ("5", 0)]:
        with pytest.raises(InvalidReimbursementError):
            svc.list(limit=limit, offset=offset)
    with pytest.raises(ReimbursementNotFoundError):
        svc.get(999999)
    with pytest.raises(ReimbursementNotFoundError):
        svc.expenses(999999)


# -------------------------------------------------------------------- void


def test_void_blocked_on_expense_then_reimbursement_void_reopens(
    session, owner_user, chart, expense_account, owner_role
):
    """Review Focus 2."""
    expenses = [
        owner_expense(session, owner_user, expense_account, "10.00"),
        owner_expense(session, owner_user, expense_account, "20.50"),
    ]
    first = reimburse(session, owner_user, chart, expenses)
    with pytest.raises(ExpenseError) as exc:
        ExpenseService(session).void(expenses[0].id, actor=owner_user, void_date=LATEST)
    assert "void the reimbursement first" in exc.value.message

    svc = ReimbursementService(session)
    entries_before = entry_count(session)
    voided = svc.void(first.id, actor=owner_user, void_date=LATEST, memo="  ")
    assert (voided.status, voided.void_date) == ("void", LATEST)
    assert voided.void_entry_id is not None
    assert entry_count(session) == entries_before + 1
    reversal = session.get(JournalEntry, voided.void_entry_id)
    assert reversal.memo == f"Void of reimbursement {LATER.isoformat()}"
    assert set(links(session).values()) == {None}  # freed
    assert svc.expenses(first.id) == []
    with pytest.raises(ReimbursementError) as again:
        svc.void(first.id, actor=owner_user, void_date=LATEST)
    assert "already void" in again.value.message
    assert entry_count(session) == entries_before + 1

    second = reimburse(session, owner_user, chart, expenses, day=LATEST)
    assert (second.total, second.id != first.id) == (first.total, True)
    assert lines_of(session, second.entry_id) == [
        (a, d, c) for a, d, c in lines_of(session, first.entry_id)
    ]
    assert set(links(session).values()) == {second.id}


def test_void_date_before_the_reimbursement_is_refused(session, owner_user, chart, expense_account):
    reim = reimburse(
        session, owner_user, chart, [owner_expense(session, owner_user, expense_account)]
    )
    before = (entry_count(session), links(session))
    with pytest.raises(InvalidReimbursementError):
        ReimbursementService(session).void(reim.id, actor=owner_user, void_date=DAY)
    assert (entry_count(session), links(session)) == before
    session.refresh(reim)
    assert reim.status == "posted"


def test_void_missing_is_not_found(session, owner_user):
    with pytest.raises(ReimbursementNotFoundError):
        ReimbursementService(session).void(999999, actor=owner_user, void_date=LATEST)


def test_void_is_audited(session, owner_user, chart, expense_account):
    expense = owner_expense(session, owner_user, expense_account)
    reim = reimburse(session, owner_user, chart, [expense])
    ReimbursementService(session).void(reim.id, actor=owner_user, void_date=LATEST)
    row = session.exec(select(AuditLog).where(AuditLog.action == "reimbursement.void")).one()
    assert row.entity_id == str(reim.id)
    assert row.after["freed_expense_ids"] == [expense.id]
