"""Tax payment voids under real concurrency: separate sessions, real COMMITs.

One void is held just after it posts, before it commits; the second is started and a
lock-wait probe on ``pg_stat_activity`` shows it is blocked on the first (the payment row)
rather than racing past it. Exactly one reversal results.
"""

import threading
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlmodel import Session, func, select

from payroll.auth.models import User
from payroll.ledger.accounts import PAYROLL_ROLES, ROLE_TYPES
from payroll.ledger.models import Account, AccountType, JournalEntry
from payroll.migrations import alembic_runner
from payroll.payroll_run import tax_payments as tax_payments_module
from payroll.payroll_run.ledger_models import TaxPayment
from payroll.payroll_run.tax_payments import TaxPaymentService
from tests.conftest import make_user
from tests.payroll_run.concurrency import Hold, finish, run_thread, wait_for_lock_waiter

D = Decimal
DAY = date(2026, 3, 1)
LATER = date(2026, 3, 5)


@pytest.fixture
def tp_db(scratch_engine):
    alembic_runner.upgrade()
    with Session(scratch_engine) as db:
        user = make_user(db)
        roles = [
            Account(name=f"Role {role.value}", type=ROLE_TYPES[role], system_role=role)
            for role in PAYROLL_ROLES
        ]
        bank = Account(name="Bank", type=AccountType.ASSET)
        db.add_all([*roles, bank])
        db.flush()
        ids = {"user": user.id, "bank": bank.id}
        db.commit()
    engine = sa.create_engine(scratch_engine.url, pool_size=10)
    yield engine, ids
    engine.dispose()


def _create(engine, ids) -> int:
    with Session(engine) as db:
        return (
            TaxPaymentService(db)
            .create(
                actor=db.get(User, ids["user"]),
                payment_date=DAY,
                agency="irs_941",
                bank_account_id=ids["bank"],
                amount=D("10.00"),
            )
            .id
        )


def _void(engine, ids, payment_id: int) -> None:
    with Session(engine) as db:
        TaxPaymentService(db).void(payment_id, actor=db.get(User, ids["user"]), void_date=LATER)


def _counts(engine) -> tuple[int, int]:
    with Session(engine) as db:
        payments = db.exec(
            select(func.count()).select_from(TaxPayment).where(TaxPayment.status == "void")
        ).one()
        entries = db.exec(select(func.count()).select_from(JournalEntry)).one()
        return payments, entries


def test_two_voids_racing_post_one_reversal(tp_db, monkeypatch):
    engine, ids = tp_db
    payment_id = _create(engine, ids)
    hold = Hold(monkeypatch, "tax_payment.void", payment_id, module=tax_payments_module)
    results: dict = {}

    first = run_thread(results, "first", lambda: _void(engine, ids, payment_id))
    assert hold.reached.wait(timeout=30)  # the first void holds the locks, posted, uncommitted
    second = run_thread(results, "second", lambda: _void(engine, ids, payment_id))
    wait_for_lock_waiter(engine)  # the second is blocked on the payment row, not racing past it
    assert "second" not in results
    hold.release.set()
    finish(first, second)

    assert sorted(results.values()) == ["TaxPaymentError", "ok"], results
    assert results["first"] == "ok"
    assert _counts(engine) == (1, 2)  # the original and exactly one reversal


def test_unheld_simultaneous_voids_post_one_reversal(tp_db):
    engine, ids = tp_db
    payment_id = _create(engine, ids)
    barrier = threading.Barrier(2)
    results: dict = {}

    def attempt(key: str) -> None:
        barrier.wait(timeout=30)
        run_thread(results, key, lambda: _void(engine, ids, payment_id)).join(timeout=30)

    threads = [threading.Thread(target=attempt, args=(k,)) for k in ("a", "b")]
    for t in threads:
        t.start()
    finish(*threads)
    assert sorted(results.values()) == ["TaxPaymentError", "ok"], results
    assert _counts(engine) == (1, 2)
