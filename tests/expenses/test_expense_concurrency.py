"""Expenses under real concurrency: separate sessions, real COMMITs."""

import threading
import time
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlmodel import Session, func, select

from payroll.auth.models import User
from payroll.exceptions import AppBaseError
from payroll.expenses import expenses as expenses_module
from payroll.expenses.expenses import ExpenseLineInput, ExpenseService
from payroll.expenses.models import Expense
from payroll.ledger.models import Account, AccountType, JournalEntry
from payroll.migrations import alembic_runner
from tests.conftest import make_user

D = Decimal
LOCK = date(2026, 3, 31)


@pytest.fixture
def exp_db(scratch_engine):
    alembic_runner.upgrade()
    with Session(scratch_engine) as db:
        user = make_user(db)
        bank = Account(name="Bank", type=AccountType.ASSET)
        supplies = Account(name="Supplies", type=AccountType.EXPENSE)
        db.add_all([bank, supplies])
        db.flush()
        ids = {"user": user.id, "bank": bank.id, "exp": supplies.id}
        db.commit()
    engine = sa.create_engine(scratch_engine.url, pool_size=10)
    yield engine, ids
    engine.dispose()


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
    raise AssertionError("nothing ever waited on a row lock")


def _create(engine, ids, day: date) -> Expense:
    with Session(engine) as db:
        return ExpenseService(db).create(
            actor=db.get(User, ids["user"]),
            expense_date=day,
            lines=[ExpenseLineInput(ids["exp"], D("10.00"))],
            source="account",
            paid_from_account_id=ids["bank"],
            payee_text="Shop",
        )


def _counts(engine) -> tuple[int, int]:
    with Session(engine) as db:
        return (
            db.exec(select(func.count()).select_from(Expense)).one(),
            db.exec(select(func.count()).select_from(JournalEntry)).one(),
        )


@pytest.mark.parametrize(
    ("day", "outcome"), [(date(2026, 3, 15), "locked"), (date(2026, 4, 1), "posted")]
)
def test_create_waits_for_a_lock_date_change_in_flight(exp_db, day, outcome):
    """The lock date moves while the create is waiting: dates inside it are refused."""
    engine, ids = exp_db
    result: dict[str, object] = {}

    def create() -> None:
        try:
            result["value"] = _create(engine, ids, day).id
        except AppBaseError as exc:
            result["value"] = exc

    with Session(engine) as locker:
        locker.execute(sa.text("UPDATE period_lock SET lock_date = :d WHERE id = 1"), {"d": LOCK})
        worker = threading.Thread(target=create)
        worker.start()
        _wait_for_lock_waiter(engine)
        assert worker.is_alive() and "value" not in result
        locker.commit()
    worker.join(timeout=30)
    assert not worker.is_alive()

    if outcome == "locked":
        assert type(result["value"]).__name__ == "PeriodLockedError"
        assert _counts(engine) == (0, 0)  # no half-written document or entry survives
    else:
        assert isinstance(result["value"], int)
        assert _counts(engine) == (1, 1)


def test_lock_date_change_waits_for_a_create_in_flight(exp_db, monkeypatch):
    """A create past its period-lock read holds it; the lock change waits, then cannot undo it.

    Afterwards the books are locked, and a further expense inside the period is refused.
    """
    engine, ids = exp_db
    posted = threading.Event()
    release = threading.Event()
    real_audit = expenses_module.record_audit

    def paused_audit(*args, **kwargs):
        posted.set()  # the entry is posted, the period lock row is held FOR SHARE
        assert release.wait(timeout=30)
        return real_audit(*args, **kwargs)

    monkeypatch.setattr(expenses_module, "record_audit", paused_audit)
    created: dict[str, object] = {}

    def create() -> None:
        created["id"] = _create(engine, ids, date(2026, 3, 15)).id

    worker = threading.Thread(target=create)
    worker.start()
    assert posted.wait(timeout=30)

    def set_lock() -> None:
        with Session(engine) as db:
            db.execute(sa.text("UPDATE period_lock SET lock_date = :d WHERE id = 1"), {"d": LOCK})
            db.commit()

    setter = threading.Thread(target=set_lock)
    setter.start()
    _wait_for_lock_waiter(engine)  # the lock change is stuck behind the create
    assert setter.is_alive()
    release.set()
    worker.join(timeout=30)
    setter.join(timeout=30)
    assert not worker.is_alive() and not setter.is_alive()

    assert _counts(engine) == (1, 1)  # committed before the lock took effect
    with pytest.raises(AppBaseError) as exc:
        _create(engine, ids, date(2026, 3, 16))
    assert type(exc.value).__name__ == "PeriodLockedError"
    assert _counts(engine) == (1, 1)


def test_two_voids_racing_one_expense_post_one_reversal(exp_db):
    engine, ids = exp_db
    expense_id = _create(engine, ids, date(2026, 3, 1)).id
    barrier = threading.Barrier(2)
    outcome: list[str] = ["", ""]

    def void(index: int) -> None:
        with Session(engine) as db:
            actor = db.get(User, ids["user"])
            barrier.wait(timeout=30)
            try:
                ExpenseService(db).void(expense_id, actor=actor, void_date=date(2026, 3, 5))
                outcome[index] = "ok"
            except AppBaseError as exc:
                outcome[index] = type(exc).__name__

    threads = [threading.Thread(target=void, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive()
    assert sorted(outcome) == ["ExpenseError", "ok"], outcome
    assert _counts(engine) == (1, 2)  # the original and exactly one reversal
