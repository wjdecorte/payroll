"""
Ledger rules that only show across real, committed transactions.

The shared ``session`` fixture never commits, so these tests use ``scratch_engine``
(a throwaway database that is dropped afterwards) and genuine COMMITs.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session

from payroll.ledger.models import Account, AccountType, JournalEntry, JournalLine, SourceType
from payroll.migrations import alembic_runner
from tests.conftest import make_user


def _post(db: Session, user_id: int, accounts, entry_no: int, **kw) -> JournalEntry:
    entry = JournalEntry(
        entry_no=entry_no,
        source_type=SourceType.MANUAL,
        entry_date=date(2026, 1, 15),
        posted_by=user_id,
        **kw,
    )
    db.add(entry)
    db.flush()
    cash, sales = accounts
    db.add(JournalLine(entry_id=entry.id, line_no=1, account_id=cash, debit=Decimal("5.00")))
    db.add(JournalLine(entry_id=entry.id, line_no=2, account_id=sales, credit=Decimal("5.00")))
    return entry


@pytest.fixture
def ledger_db(scratch_engine):
    alembic_runner.upgrade()
    with Session(scratch_engine) as db:
        user = make_user(db)
        cash = Account(name="Cash", type=AccountType.ASSET)
        sales = Account(name="Sales", type=AccountType.INCOME)
        db.add_all([cash, sales])
        db.flush()
        ids = (user.id, (cash.id, sales.id))
        db.commit()
    return scratch_engine, ids


def test_lines_cannot_be_appended_to_a_committed_entry(ledger_db):
    engine, (user_id, accounts) = ledger_db
    with Session(engine) as db:
        entry = _post(db, user_id, accounts, 1)
        db.commit()
        entry_id = entry.id

    with Session(engine) as db:
        cash, sales = accounts
        db.add(JournalLine(entry_id=entry_id, line_no=3, account_id=cash, debit=Decimal("1.00")))
        db.add(JournalLine(entry_id=entry_id, line_no=4, account_id=sales, credit=Decimal("1.00")))
        with pytest.raises(DBAPIError, match="can only be added in the transaction"):
            db.commit()


def test_lines_cannot_be_appended_to_a_void_entry(ledger_db):
    engine, (user_id, accounts) = ledger_db
    with Session(engine) as db:
        entry = _post(db, user_id, accounts, 1)
        db.commit()
        entry.status = "void"
        db.commit()
        entry_id = entry.id

    with Session(engine) as db:
        cash, sales = accounts
        db.add(JournalLine(entry_id=entry_id, line_no=3, account_id=cash, debit=Decimal("1.00")))
        db.add(JournalLine(entry_id=entry_id, line_no=4, account_id=sales, credit=Decimal("1.00")))
        with pytest.raises(DBAPIError, match="can only be added in the transaction"):
            db.commit()


def test_entry_and_lines_in_one_transaction_commit(ledger_db):
    engine, (user_id, accounts) = ledger_db
    with Session(engine) as db:
        _post(db, user_id, accounts, 1)
        db.commit()
        assert db.query(JournalLine).count() == 2


def test_entry_and_lines_inside_a_savepoint_commit(ledger_db):
    engine, (user_id, accounts) = ledger_db
    with Session(engine) as db:
        with db.begin_nested():
            _post(db, user_id, accounts, 1)
        db.commit()
        assert db.query(JournalLine).count() == 2


def test_void_original_and_post_reversal_in_one_transaction(ledger_db):
    engine, (user_id, accounts) = ledger_db
    with Session(engine) as db:
        original = _post(db, user_id, accounts, 1)
        db.commit()
        original_id = original.id

    with Session(engine) as db:
        original = db.get(JournalEntry, original_id)
        original.status = "void"
        reversal = JournalEntry(
            entry_no=2,
            source_type=SourceType.MANUAL,
            entry_date=date(2026, 1, 16),
            posted_by=user_id,
            reverses_entry_id=original_id,
        )
        db.add(reversal)
        db.flush()
        cash, sales = accounts
        db.add(
            JournalLine(entry_id=reversal.id, line_no=1, account_id=sales, debit=Decimal("5.00"))
        )
        db.add(
            JournalLine(entry_id=reversal.id, line_no=2, account_id=cash, credit=Decimal("5.00"))
        )
        db.commit()
        assert db.query(JournalLine).count() == 4
