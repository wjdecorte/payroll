"""
Database-level ledger integrity: constraints and triggers.

The ``session`` fixture wraps each test in a rolled-back outer transaction, so a
real COMMIT never happens. Deferred constraint triggers fire at COMMIT; the tests
force them with ``SET CONSTRAINTS ALL IMMEDIATE``, which runs the pending checks
exactly as COMMIT would.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlmodel import Session, text

from payroll.ledger.models import (
    Account,
    AccountType,
    JournalEntry,
    JournalLine,
    SourceType,
    SystemRole,
)
from tests.conftest import make_user

D = Decimal


def _account(session: Session, name: str = "Cash", **kw) -> Account:
    kw.setdefault("type", AccountType.ASSET)
    acct = Account(name=name, **kw)
    session.add(acct)
    session.flush()
    return acct


def _entry(session: Session, user_id: int, entry_no: int = 1, **kw) -> JournalEntry:
    kw.setdefault("source_type", SourceType.MANUAL)
    entry = JournalEntry(entry_no=entry_no, entry_date=date(2026, 1, 15), posted_by=user_id, **kw)
    session.add(entry)
    session.flush()
    return entry


def _line(session: Session, entry: JournalEntry, account: Account, n: int, debit, credit):
    line = JournalLine(
        entry_id=entry.id, line_no=n, account_id=account.id, debit=D(debit), credit=D(credit)
    )
    session.add(line)
    session.flush()
    return line


def _commit(session: Session) -> None:
    session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    session.execute(text("SET CONSTRAINTS ALL DEFERRED"))


@pytest.fixture
def user(session):
    return make_user(session)


@pytest.fixture
def entry(session, user):
    return _entry(session, user.id)


@pytest.fixture
def cash(session):
    return _account(session, "Cash")


@pytest.fixture
def sales(session):
    return _account(session, "Sales", type=AccountType.INCOME)


@pytest.fixture
def posted(session, entry, cash, sales):
    _line(session, entry, cash, 1, "10.00", "0.00")
    _line(session, entry, sales, 2, "0.00", "10.00")
    _commit(session)
    return entry


def test_line_with_both_sides_is_rejected(session, entry, cash):
    with pytest.raises(IntegrityError):
        _line(session, entry, cash, 1, "5.00", "5.00")


def test_line_with_neither_side_is_rejected(session, entry, cash):
    with pytest.raises(IntegrityError):
        _line(session, entry, cash, 1, "0.00", "0.00")


def test_unbalanced_entry_fails_at_commit(session, entry, cash, sales):
    _line(session, entry, cash, 1, "10.00", "0.00")
    _line(session, entry, sales, 2, "0.00", "9.99")
    with pytest.raises(DBAPIError, match="unbalanced"):
        _commit(session)


def test_single_line_entry_fails_at_commit(session, entry, cash):
    _line(session, entry, cash, 1, "10.00", "0.00")
    with pytest.raises(DBAPIError, match="at least 2 lines"):
        _commit(session)


def test_balanced_entry_commits(session, posted):
    total = session.execute(
        text("SELECT sum(debit) - sum(credit) FROM journal_line WHERE entry_id = :e"),
        {"e": posted.id},
    ).scalar_one()
    assert total == D("0.00")


def test_line_update_and_delete_rejected(session, posted):
    with pytest.raises(DBAPIError, match="append-only"):
        session.execute(
            text("UPDATE journal_line SET memo = 'x' WHERE entry_id = :e"), {"e": posted.id}
        )
    session.rollback()


def test_line_delete_rejected(session, posted):
    with pytest.raises(DBAPIError, match="append-only"):
        session.execute(text("DELETE FROM journal_line WHERE entry_id = :e"), {"e": posted.id})


def test_line_truncate_rejected(session, posted):
    with pytest.raises(DBAPIError, match="append-only"):
        session.execute(text("TRUNCATE journal_line"))


def test_entry_update_other_than_void_rejected(session, posted):
    with pytest.raises(DBAPIError, match="immutable: UPDATE"):
        session.execute(
            text("UPDATE journal_entry SET memo = 'changed' WHERE id = :e"), {"e": posted.id}
        )


def test_entry_status_posted_to_void_allowed(session, posted):
    session.execute(
        text("UPDATE journal_entry SET status = 'void' WHERE id = :e"), {"e": posted.id}
    )
    status = session.execute(
        text("SELECT status FROM journal_entry WHERE id = :e"), {"e": posted.id}
    ).scalar_one()
    assert status == "void"


def test_void_with_other_change_rejected(session, posted):
    with pytest.raises(DBAPIError, match="immutable: UPDATE"):
        session.execute(
            text("UPDATE journal_entry SET status = 'void', memo = 'x' WHERE id = :e"),
            {"e": posted.id},
        )


def test_entry_void_back_to_posted_rejected(session, posted):
    session.execute(
        text("UPDATE journal_entry SET status = 'void' WHERE id = :e"), {"e": posted.id}
    )
    with pytest.raises(DBAPIError, match="immutable: UPDATE"):
        session.execute(
            text("UPDATE journal_entry SET status = 'posted' WHERE id = :e"), {"e": posted.id}
        )


def test_entry_delete_rejected(session, posted):
    with pytest.raises(DBAPIError, match="immutable: DELETE"):
        session.execute(text("DELETE FROM journal_entry WHERE id = :e"), {"e": posted.id})


def test_entry_truncate_rejected(session, posted):
    with pytest.raises(DBAPIError, match="immutable: TRUNCATE"):
        session.execute(text("TRUNCATE journal_entry CASCADE"))


def test_duplicate_reversal_rejected(session, user, entry):
    _entry(session, user.id, entry_no=2, reverses_entry_id=entry.id)
    with pytest.raises(IntegrityError):
        _entry(session, user.id, entry_no=3, reverses_entry_id=entry.id)


def test_duplicate_source_entry_no_rejected(session, user, entry):
    with pytest.raises(IntegrityError):
        _entry(session, user.id, entry_no=entry.entry_no)


def test_invalid_status_rejected(session, user):
    with pytest.raises(IntegrityError):
        _entry(session, user.id, status="draft")


def test_account_name_unique_per_parent_case_insensitively(session, cash):
    with pytest.raises(IntegrityError):
        _account(session, "CASH")


def test_same_name_allowed_under_different_parent(session, cash):
    _account(session, "Cash", parent_id=cash.id)


def test_account_number_unique_when_present_but_nulls_repeat(session):
    _account(session, "A", number=None)
    _account(session, "B", number=None)
    _account(session, "C", number="1000")
    with pytest.raises(IntegrityError):
        _account(session, "D", number="1000")


def test_system_role_unique(session):
    _account(session, "AR", system_role=SystemRole.ACCOUNTS_RECEIVABLE)
    _account(session, "Other", system_role=None)
    with pytest.raises(IntegrityError):
        _account(session, "AR2", system_role=SystemRole.ACCOUNTS_RECEIVABLE)


def test_account_with_lines_cannot_be_deleted(session, posted, cash):
    with pytest.raises(IntegrityError):
        session.execute(text("DELETE FROM account WHERE id = :a"), {"a": cash.id})


def test_period_lock_row_seeded(session):
    row = session.execute(text("SELECT id, lock_date FROM period_lock")).all()
    assert row == [(1, None)]


def test_entry_without_lines_fails_at_commit(session, entry):
    with pytest.raises(DBAPIError, match="at least 2 lines"):
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


def test_void_through_orm_with_modify_date_succeeds(session, posted):
    posted.status = "void"
    posted.modify_date = datetime.utcnow()
    session.flush()
    session.refresh(posted)
    assert posted.status == "void"


def test_orm_memo_change_rejected(session, posted):
    posted.memo = "changed"
    with pytest.raises(DBAPIError, match="immutable: UPDATE"):
        session.flush()
