"""Approve and void under real concurrency: separate sessions, real COMMITs.

A worker is held at a known point (just after its checks and its posting, before it
commits) and a second worker is started; a lock-wait probe on ``pg_stat_activity`` shows the
second one is blocked on the first, never racing past it, and the outcome is then asserted.
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
from payroll.payroll_run.models import PayrollRun
from payroll.payroll_run.posting import PayrollPostingService
from payroll.payroll_run.services import PayrollRunService
from tests.conftest import make_user
from tests.payroll_run.concurrency import (
    Hold,
    finish,
    outcome_of,
    run_thread,
    wait_for_lock_waiter,
)

D = Decimal
JAN_END = date(2026, 1, 31)
FEB_END = date(2026, 2, 28)


@pytest.fixture
def pr_db(scratch_engine):
    alembic_runner.upgrade()
    with Session(scratch_engine) as db:
        user = make_user(db)
        roles = {
            role: Account(name=f"Role {role.value}", type=ROLE_TYPES[role], system_role=role)
            for role in PAYROLL_ROLES
        }
        bank = Account(name="Bank", type=AccountType.ASSET)
        db.add_all([*roles.values(), bank])
        db.flush()
        ids = {"user": user.id, "bank": bank.id, "roles": {r: a.id for r, a in roles.items()}}
        db.commit()
    engine = sa.create_engine(scratch_engine.url, pool_size=10)
    yield engine, ids
    engine.dispose()


def add_draft(engine, period: str) -> int:
    """A draft that balances on its own: wages 100.00 against net pay 100.00."""
    with Session(engine) as db:
        run = PayrollRun(
            pay_period=period, gross_salary=D("100.00"), fica_wages=D("100.00"), net_pay=D("100.00")
        )
        db.add(run)
        db.commit()
        return run.id


def approve(engine, ids, run_id: int, day: date = JAN_END) -> PayrollRun:
    with Session(engine) as db:
        return PayrollPostingService(db).approve(
            run_id,
            actor=db.get(User, ids["user"]),
            pay_date=day,
            net_pay_source="account",
            net_pay_account_id=ids["bank"],
        )


def void(engine, ids, run_id: int, day: date = JAN_END) -> PayrollRun:
    with Session(engine) as db:
        return PayrollPostingService(db).void(
            run_id, actor=db.get(User, ids["user"]), void_date=day
        )


def statuses(engine) -> dict[str, str]:
    with Session(engine) as db:
        return dict(db.exec(select(PayrollRun.pay_period, PayrollRun.status)).all())


def entry_count(engine) -> int:
    with Session(engine) as db:
        return db.exec(select(func.count()).select_from(JournalEntry)).one()


# --------------------------------------------------------------------------- tax-year row lock


def test_approval_of_the_next_month_waits_for_the_earlier_approval_then_passes(pr_db, monkeypatch):
    """Feb must not judge Jan while Jan is mid-approval: it waits, then sees Jan approved."""
    engine, ids = pr_db
    jan, feb = add_draft(engine, "2026-01"), add_draft(engine, "2026-02")
    hold = Hold(monkeypatch, "payroll_run.approve", jan)
    results: dict = {}
    first = run_thread(results, "jan", lambda: approve(engine, ids, jan))
    assert hold.reached.wait(timeout=30)
    second = run_thread(results, "feb", lambda: approve(engine, ids, feb, FEB_END))
    wait_for_lock_waiter(engine)  # Feb is blocked on Jan's run row, not racing past it
    assert second.is_alive() and "feb" not in results
    hold.release.set()
    finish(first, second)

    assert results == {"jan": "ok", "feb": "ok"}
    assert statuses(engine) == {"2026-01": "approved", "2026-02": "approved"}
    assert entry_count(engine) == 2


def test_void_of_the_earlier_month_waits_for_the_later_approval_and_is_then_refused(
    pr_db, monkeypatch
):
    """Jan's void must see Feb's approval in flight, so it cannot void out of order."""
    engine, ids = pr_db
    jan, feb = add_draft(engine, "2026-01"), add_draft(engine, "2026-02")
    approve(engine, ids, jan)
    hold = Hold(monkeypatch, "payroll_run.approve", feb)
    results: dict = {}
    approver = run_thread(results, "feb", lambda: approve(engine, ids, feb, FEB_END))
    assert hold.reached.wait(timeout=30)
    voider = run_thread(results, "void", lambda: void(engine, ids, jan, FEB_END))
    wait_for_lock_waiter(engine)
    assert voider.is_alive() and "void" not in results
    hold.release.set()
    finish(approver, voider)

    assert results == {"feb": "ok", "void": "PayrollRunStateError"}
    assert statuses(engine) == {"2026-01": "approved", "2026-02": "approved"}
    assert entry_count(engine) == 2  # no reversal was posted


def test_approval_waits_for_the_earlier_void_in_flight(pr_db, monkeypatch):
    engine, ids = pr_db
    jan = add_draft(engine, "2026-01")
    approve(engine, ids, jan)
    hold = Hold(monkeypatch, "payroll_run.void", jan)
    results: dict = {}
    voider = run_thread(results, "void", lambda: void(engine, ids, jan))
    assert hold.reached.wait(timeout=30)
    feb = add_draft(engine, "2026-02")  # a later draft would block the void, so it arrives after
    approver = run_thread(results, "feb", lambda: approve(engine, ids, feb, FEB_END))
    wait_for_lock_waiter(engine)
    assert approver.is_alive() and "feb" not in results
    hold.release.set()
    finish(voider, approver)

    assert results == {"void": "ok", "feb": "ok"}  # an earlier void run does not block
    assert statuses(engine) == {"2026-01": "void", "2026-02": "approved"}


def test_two_approvals_of_one_run_post_one_entry(pr_db):
    engine, ids = pr_db
    jan = add_draft(engine, "2026-01")
    barrier = threading.Barrier(2)
    results: dict = {}

    def attempt(key: str) -> None:
        barrier.wait(timeout=30)
        results[key] = outcome_of(lambda: approve(engine, ids, jan))

    threads = [threading.Thread(target=attempt, args=(k,)) for k in ("a", "b")]
    for t in threads:
        t.start()
    finish(*threads)

    assert sorted(results.values()) == ["PayrollRunStateError", "ok"]
    assert entry_count(engine) == 1


def test_two_voids_of_one_run_post_one_reversal(pr_db):
    engine, ids = pr_db
    jan = add_draft(engine, "2026-01")
    approve(engine, ids, jan)
    barrier = threading.Barrier(2)
    results: dict = {}

    def attempt(key: str) -> None:
        barrier.wait(timeout=30)
        results[key] = outcome_of(lambda: void(engine, ids, jan))

    threads = [threading.Thread(target=attempt, args=(k,)) for k in ("a", "b")]
    for t in threads:
        t.start()
    finish(*threads)

    assert sorted(results.values()) == ["PayrollRunStateError", "ok"]
    assert entry_count(engine) == 2  # the original and exactly one reversal


def test_delete_of_a_draft_waits_for_its_approval_and_is_then_refused(pr_db, monkeypatch):
    engine, ids = pr_db
    jan = add_draft(engine, "2026-01")
    hold = Hold(monkeypatch, "payroll_run.approve", jan)
    results: dict = {}

    def delete() -> None:
        with Session(engine) as db:
            PayrollRunService(db).delete_run(jan, actor=db.get(User, ids["user"]))

    approver = run_thread(results, "approve", lambda: approve(engine, ids, jan))
    assert hold.reached.wait(timeout=30)
    deleter = run_thread(results, "delete", delete)
    wait_for_lock_waiter(engine)
    hold.release.set()
    finish(approver, deleter)

    assert results == {"approve": "ok", "delete": "PayrollRunStateError"}
    assert statuses(engine) == {"2026-01": "approved"}


# --------------------------------------------------------------------------- period lock


def set_lock(engine, day: date) -> None:
    with Session(engine) as db:
        db.execute(sa.text("UPDATE period_lock SET lock_date = :d WHERE id = 1"), {"d": day})
        db.commit()


@pytest.mark.parametrize(
    ("pay_date", "outcome"), [(date(2026, 1, 20), "PeriodLockedError"), (date(2026, 2, 1), "ok")]
)
def test_approval_waits_for_a_lock_date_change_in_flight(pr_db, pay_date, outcome):
    engine, ids = pr_db
    jan = add_draft(engine, "2026-01")
    results: dict = {}
    with Session(engine) as locker:
        locker.execute(
            sa.text("UPDATE period_lock SET lock_date = :d WHERE id = 1"), {"d": JAN_END}
        )
        worker = run_thread(results, "approve", lambda: approve(engine, ids, jan, pay_date))
        wait_for_lock_waiter(engine)
        assert worker.is_alive() and "approve" not in results
        locker.commit()
    finish(worker)

    assert results == {"approve": outcome}
    if outcome == "ok":
        assert (statuses(engine), entry_count(engine)) == ({"2026-01": "approved"}, 1)
    else:
        assert (statuses(engine), entry_count(engine)) == ({"2026-01": "draft"}, 0)


def test_lock_date_change_waits_for_an_approval_in_flight(pr_db, monkeypatch):
    engine, ids = pr_db
    jan, feb = add_draft(engine, "2026-01"), add_draft(engine, "2026-02")
    hold = Hold(monkeypatch, "payroll_run.approve", jan)
    results: dict = {}
    approver = run_thread(results, "approve", lambda: approve(engine, ids, jan, date(2026, 1, 20)))
    assert hold.reached.wait(timeout=30)
    setter = run_thread(results, "lock", lambda: set_lock(engine, JAN_END))
    wait_for_lock_waiter(engine)  # the lock change is stuck behind the approval
    assert setter.is_alive() and "lock" not in results
    hold.release.set()
    finish(approver, setter)

    assert results == {"approve": "ok", "lock": "ok"}  # committed before the lock took effect
    assert entry_count(engine) == 1
    # Afterwards the period is locked, and February is still open.
    assert outcome_of(lambda: approve(engine, ids, feb, FEB_END)) == "ok"
    assert statuses(engine) == {"2026-01": "approved", "2026-02": "approved"}
