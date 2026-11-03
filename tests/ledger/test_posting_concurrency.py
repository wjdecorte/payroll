"""
Posting under real concurrency: separate sessions on separate threads, real COMMITs.

Uses ``scratch_engine`` (a throwaway, migrated database) because the shared
``session`` fixture never commits and runs on a single connection.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlmodel import Session, select

from payroll.auth.models import User
from payroll.ledger.exceptions import (
    AlreadyReversedError,
    InactiveAccountError,
    PeriodLockedError,
    UnbalancedEntryError,
)
from payroll.ledger.models import Account, AccountType, EntryCounter, JournalEntry, SourceType
from payroll.ledger.services import EntryDraft, JournalService, LedgerPostingService, LineDraft
from payroll.migrations import alembic_runner
from tests.conftest import make_user

THREADS = 20


@pytest.fixture
def ledger_db(scratch_engine):
    """A migrated scratch database with an owner and two accounts, plus a big-pool engine."""
    alembic_runner.upgrade()
    with Session(scratch_engine) as db:
        user = make_user(db)
        cash = Account(name="Cash", type=AccountType.ASSET)
        sales = Account(name="Sales", type=AccountType.INCOME)
        db.add_all([cash, sales])
        db.flush()
        ids = (user.id, cash.id, sales.id)
        db.commit()
    engine = sa.create_engine(scratch_engine.url, pool_size=THREADS + 5)
    yield engine, ids
    engine.dispose()


def _draft(cash: int, sales: int, credit: str = "10.00", day: date = date(2026, 1, 15)):
    lines = [LineDraft(cash, debit=Decimal("10.00")), LineDraft(sales, credit=Decimal(credit))]
    return EntryDraft(day, lines)


def _committed_numbers(engine) -> list[int]:
    with Session(engine) as db:
        stmt = select(JournalEntry.entry_no).order_by(JournalEntry.entry_no)
        return list(db.exec(stmt).all())


def _run_parallel(engine, ids, kinds: list[str]) -> list[str]:
    """Run one posting thread per kind, all released together by a barrier.

    ``ok`` commits a valid entry, ``bad`` attempts an unbalanced one, ``rollback``
    posts a valid entry and then rolls the transaction back.
    """
    user_id, cash, sales = ids
    barrier = threading.Barrier(len(kinds))

    def work(kind: str) -> str:
        with Session(engine) as db:
            actor = db.get(User, user_id)
            barrier.wait(timeout=30)
            try:
                if kind == "bad":
                    LedgerPostingService(db).post(_draft(cash, sales, "9.99"), actor=actor)
                elif kind == "rollback":
                    LedgerPostingService(db).post(_draft(cash, sales), actor=actor)
                    db.rollback()
                else:
                    JournalService(db).create_manual(_draft(cash, sales), actor=actor)
            except UnbalancedEntryError:
                return "rejected"
            return kind

    with ThreadPoolExecutor(max_workers=len(kinds)) as pool:
        return list(pool.map(work, kinds))


def test_parallel_posts_get_contiguous_unique_numbers(ledger_db):
    engine, ids = ledger_db
    results = _run_parallel(engine, ids, ["ok"] * THREADS)
    assert results == ["ok"] * THREADS
    assert _committed_numbers(engine) == list(range(1, THREADS + 1))
    with Session(engine) as db:
        assert db.get(EntryCounter, SourceType.MANUAL).last_no == THREADS


def test_parallel_posts_with_failures_leave_no_gaps(ledger_db):
    engine, ids = ledger_db
    kinds = ["ok", "bad", "rollback", "ok"] * (THREADS // 4)
    results = _run_parallel(engine, ids, kinds)
    assert results.count("rejected") == THREADS // 4
    committed = kinds.count("ok")
    assert _committed_numbers(engine) == list(range(1, committed + 1))
    # the next post continues the sequence
    user_id, cash, sales = ids
    with Session(engine) as db:
        entry = JournalService(db).create_manual(_draft(cash, sales), actor=db.get(User, user_id))
        assert entry.entry_no == committed + 1


def _wait_for_lock_waiter(engine, timeout: float = 10.0) -> None:
    """Block until some backend in the scratch database waits on a row lock."""
    deadline = time.monotonic() + timeout
    with engine.connect() as conn:
        while time.monotonic() < deadline:
            waiting = conn.execute(
                sa.text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            ).scalar_one()
            if waiting:
                return
            conn.rollback()
            time.sleep(0.02)
    raise AssertionError("the post never waited for the period lock row")


@pytest.mark.parametrize(
    ("day", "outcome"),
    [(date(2026, 3, 15), "locked"), (date(2026, 4, 1), "posted")],
)
def test_post_waits_for_lock_date_change(ledger_db, day, outcome):
    engine, (user_id, cash, sales) = ledger_db
    result: dict[str, object] = {}

    def post() -> None:
        with Session(engine) as db:
            try:
                entry = JournalService(db).create_manual(
                    _draft(cash, sales, day=day), actor=db.get(User, user_id)
                )
                result["value"] = entry.entry_no
            except PeriodLockedError as exc:
                result["value"] = exc

    with Session(engine) as locker:
        # Hold the lock row's update (as the Task 4 setter will) without committing.
        locker.execute(sa.text("UPDATE period_lock SET lock_date = '2026-03-31' WHERE id = 1"))
        poster = threading.Thread(target=post)
        poster.start()
        _wait_for_lock_waiter(engine)
        assert poster.is_alive() and "value" not in result
        locker.commit()
    poster.join(timeout=30)
    assert not poster.is_alive()

    if outcome == "locked":
        assert isinstance(result["value"], PeriodLockedError)
        assert _committed_numbers(engine) == []
    else:
        assert result["value"] == 1
        assert _committed_numbers(engine) == [1]


def _assert_unlocked(engine, entry_id: int, account_ids: tuple[int, ...]) -> None:
    """Another session can lock every row a failed post touched, without waiting."""
    with engine.connect() as other:
        for sql, params in [
            ("SELECT id FROM journal_entry WHERE id = :id FOR UPDATE NOWAIT", {"id": entry_id}),
            ("SELECT id FROM period_lock WHERE id = 1 FOR UPDATE NOWAIT", {}),
            (
                "SELECT id FROM account WHERE id = ANY(:ids) FOR UPDATE NOWAIT",
                {"ids": list(account_ids)},
            ),
            ("SELECT source_type FROM entry_counter FOR UPDATE NOWAIT", {}),
        ]:
            other.execute(sa.text(sql), params)  # raises LockNotAvailable if still held
        other.rollback()


@pytest.mark.parametrize(
    ("failure", "method"),
    [
        (failure, method)
        for failure in ("locked_period", "inactive_account", "already_reversed")
        for method in ("create_manual", "reverse_manual", "void_manual")
        # creating an entry cannot hit an existing reversal
        if (failure, method) != ("already_reversed", "create_manual")
    ],
)
def test_failed_manual_call_releases_its_locks(ledger_db, method, failure):
    engine, (user_id, cash, sales) = ledger_db
    with Session(engine) as setup:
        journals = JournalService(setup)
        actor = setup.get(User, user_id)
        entry_id = journals.create_manual(_draft(cash, sales, day=date(2026, 4, 2)), actor=actor).id
        if failure == "already_reversed":
            journals.reverse_manual(entry_id, entry_date=date(2026, 4, 3), actor=actor)
        elif failure == "locked_period":
            setup.execute(sa.text("UPDATE period_lock SET lock_date = '2026-03-31' WHERE id = 1"))
        else:
            setup.execute(
                sa.text("UPDATE account SET is_active = false WHERE id = :id"), {"id": sales}
            )
        setup.commit()

    expected = {
        "locked_period": PeriodLockedError,
        "inactive_account": InactiveAccountError,
        "already_reversed": AlreadyReversedError,
    }[failure]
    day = date(2026, 3, 31) if failure == "locked_period" else date(2026, 4, 5)
    db = Session(engine)  # deliberately neither closed nor rolled back before the check
    try:
        journals = JournalService(db)
        actor = db.get(User, user_id)
        with pytest.raises(expected):
            if method == "create_manual":
                journals.create_manual(_draft(cash, sales, day=day), actor=actor)
            else:
                getattr(journals, method)(entry_id, entry_date=day, actor=actor)
        _assert_unlocked(engine, entry_id, (cash, sales))
    finally:
        db.close()
