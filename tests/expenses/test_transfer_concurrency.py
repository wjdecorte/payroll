"""Transfers under real concurrency: separate sessions, real COMMITs."""

import threading
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlmodel import Session, func, select

from payroll.auth.models import User
from payroll.exceptions import AppBaseError
from payroll.expenses.models import Transfer
from payroll.expenses.transfers import TransferService
from payroll.ledger.models import Account, AccountType, JournalEntry
from payroll.migrations import alembic_runner
from tests.conftest import make_user

D = Decimal


@pytest.fixture
def tr_db(scratch_engine):
    alembic_runner.upgrade()
    with Session(scratch_engine) as db:
        user = make_user(db)
        bank = Account(name="Bank", type=AccountType.ASSET)
        savings = Account(name="Savings", type=AccountType.ASSET)
        db.add_all([bank, savings])
        db.flush()
        ids = {"user": user.id, "bank": bank.id, "savings": savings.id}
        db.commit()
    engine = sa.create_engine(scratch_engine.url, pool_size=10)
    yield engine, ids
    engine.dispose()


def _counts(engine) -> tuple[int, int]:
    with Session(engine) as db:
        return (
            db.exec(select(func.count()).select_from(Transfer)).one(),
            db.exec(select(func.count()).select_from(JournalEntry)).one(),
        )


def test_two_voids_racing_one_transfer_post_one_reversal(tr_db):
    """Force the overlap: the first void is held mid-flight until the second is lock-waiting."""
    engine, ids = tr_db
    with Session(engine) as db:
        transfer_id = (
            TransferService(db)
            .create(
                actor=db.get(User, ids["user"]),
                transfer_date=date(2026, 3, 1),
                from_account_id=ids["bank"],
                to_account_id=ids["savings"],
                amount=D("10.00"),
            )
            .id
        )

    outcome: dict[str, str] = {}

    def void(name: str, db: Session) -> None:
        try:
            TransferService(db).void(
                transfer_id, actor=db.get(User, ids["user"]), void_date=date(2026, 3, 5)
            )
            outcome[name] = "ok"
        except AppBaseError as exc:
            outcome[name] = type(exc).__name__

    # Session A takes the transfer row lock and holds it; B must queue behind it.
    holder = Session(engine)
    holder.execute(sa.text("SELECT id FROM transfer WHERE id = :i FOR UPDATE"), {"i": transfer_id})
    first = Session(engine)
    second = Session(engine)
    t1 = threading.Thread(target=void, args=("first", first))
    t2 = threading.Thread(target=void, args=("second", second))
    t1.start()
    _wait_for_waiters(engine, 1)
    t2.start()
    _wait_for_waiters(engine, 2)  # both voids are blocked on the same row: a real overlap
    assert t1.is_alive() and t2.is_alive()
    holder.rollback()
    holder.close()
    for t in (t1, t2):
        t.join(timeout=60)
        assert not t.is_alive()
    first.close()
    second.close()
    assert sorted(outcome.values()) == ["TransferError", "ok"], outcome
    assert _counts(engine) == (1, 2)  # the original and exactly one reversal


def _wait_for_waiters(engine, wanted: int, timeout: float = 10.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    with engine.connect() as conn:
        while time.monotonic() < deadline:
            waiting = conn.execute(
                sa.text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            ).scalar_one()
            if waiting >= wanted:
                return
            conn.rollback()
            time.sleep(0.02)
    raise AssertionError(f"fewer than {wanted} backends ever waited on a row lock")
