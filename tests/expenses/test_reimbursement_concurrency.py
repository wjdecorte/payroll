"""Reimbursements under real concurrency: separate sessions, real COMMITs."""

import threading
import time
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlmodel import Session, func, select

from payroll.auth.models import User
from payroll.exceptions import AppBaseError
from payroll.expenses import reimbursements as reimbursements_module
from payroll.expenses.expenses import ExpenseLineInput, ExpenseService
from payroll.expenses.models import Expense, Reimbursement
from payroll.expenses.reimbursements import ReimbursementService
from payroll.ledger.accounts import AccountService
from payroll.ledger.models import Account, AccountType, JournalEntry, SystemRole
from payroll.migrations import alembic_runner
from tests.conftest import make_user

D = Decimal
DAY = date(2026, 3, 1)
LATER = date(2026, 3, 5)


@pytest.fixture
def rb_db(scratch_engine):
    alembic_runner.upgrade()
    with Session(scratch_engine) as db:
        user = make_user(db)
        due = Account(
            name="Due to Owner", type=AccountType.LIABILITY, system_role=SystemRole.DUE_TO_OWNER
        )
        bank = Account(name="Bank", type=AccountType.ASSET)
        spare = Account(name="Other Liability", type=AccountType.LIABILITY)
        supplies = Account(name="Supplies", type=AccountType.EXPENSE)
        db.add_all([due, bank, spare, supplies])
        db.flush()
        ids = {
            "user": user.id,
            "due": due.id,
            "bank": bank.id,
            "spare": spare.id,
            "exp": supplies.id,
        }
        db.commit()
    engine = sa.create_engine(scratch_engine.url, pool_size=10)
    yield engine, ids
    engine.dispose()


def _wait_for_lock_waiter(engine, timeout: float = 10.0) -> None:
    """Block until some backend in the scratch database waits on a lock."""
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
    raise AssertionError("nothing ever waited on a lock")


def _expense(engine, ids, amount: str = "10.00") -> int:
    with Session(engine) as db:
        return (
            ExpenseService(db)
            .create(
                actor=db.get(User, ids["user"]),
                expense_date=DAY,
                lines=[ExpenseLineInput(ids["exp"], D(amount))],
                source="owner",
                paid_from_account_id=None,
                payee_text="Cash buy",
            )
            .id
        )


def _reimburse(db: Session, ids, expense_ids) -> Reimbursement:
    return ReimbursementService(db).create(
        actor=db.get(User, ids["user"]),
        reimbursement_date=LATER,
        paid_from_account_id=ids["bank"],
        expense_ids=list(expense_ids),
    )


def _counts(engine) -> dict[str, int]:
    with Session(engine) as db:
        return {
            "reimbursements": db.exec(select(func.count()).select_from(Reimbursement)).one(),
            "reimb_entries": db.exec(
                select(func.count())
                .select_from(JournalEntry)
                .where(JournalEntry.source_type == "reimbursement")
            ).one(),
            "linked": db.exec(
                select(func.count())
                .select_from(Expense)
                .where(Expense.reimbursement_id.is_not(None))  # type: ignore[union-attr]
            ).one(),
            "voided_expenses": db.exec(
                select(func.count()).select_from(Expense).where(Expense.status == "void")
            ).one(),
        }


def _run(worker_fns) -> None:
    threads = [threading.Thread(target=fn) for fn in worker_fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive(), "a worker is stuck (deadlock?)"


def test_two_sessions_reimbursing_one_expense_exactly_one_succeeds(rb_db, monkeypatch):
    """Review Focus 1: the loser waits on the winner's row lock, then sees the link."""
    engine, ids = rb_db
    expense_id = _expense(engine, ids)
    first_posted = threading.Event()
    release = threading.Event()
    real_audit = reimbursements_module.record_audit
    state = {"calls": 0}

    def paused_audit(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:  # the first reimburser holds the expense row, un-committed
            first_posted.set()
            assert release.wait(timeout=30)
        return real_audit(*args, **kwargs)

    monkeypatch.setattr(reimbursements_module, "record_audit", paused_audit)
    outcome: dict[str, object] = {}

    def run(name: str) -> None:
        with Session(engine) as db:
            try:
                outcome[name] = _reimburse(db, ids, [expense_id]).id
            except AppBaseError as exc:
                outcome[name] = type(exc).__name__

    first = threading.Thread(target=run, args=("a",))
    first.start()
    assert first_posted.wait(timeout=30)
    second = threading.Thread(target=run, args=("b",))
    second.start()
    _wait_for_lock_waiter(engine)  # the second is blocked on the expense row lock
    assert second.is_alive() and "b" not in outcome
    release.set()
    first.join(timeout=30)
    second.join(timeout=30)
    assert not first.is_alive() and not second.is_alive()

    assert isinstance(outcome["a"], int)
    assert outcome["b"] == "ReimbursementError"
    assert _counts(engine) == {
        "reimbursements": 1,
        "reimb_entries": 1,
        "linked": 1,
        "voided_expenses": 0,
    }


def test_reimbursement_racing_bind_role_never_strands_the_debt(rb_db, monkeypatch):
    """The rebind arrives while the reimburser holds the role row: it is refused."""
    engine, ids = rb_db
    expense_id = _expense(engine, ids)
    holding = threading.Event()
    release = threading.Event()
    real_audit = reimbursements_module.record_audit

    def paused_audit(*args, **kwargs):
        holding.set()  # role row held FOR SHARE, entry posted, not committed
        assert release.wait(timeout=30)
        return real_audit(*args, **kwargs)

    monkeypatch.setattr(reimbursements_module, "record_audit", paused_audit)
    outcome: dict[str, str] = {}

    def reimburse() -> None:
        with Session(engine) as db:
            outcome["reimburse"] = str(_reimburse(db, ids, [expense_id]).id)

    def rebind() -> None:
        with Session(engine) as db:
            actor = db.get(User, ids["user"])
            try:
                AccountService(db).bind_role(SystemRole.DUE_TO_OWNER, ids["spare"], actor=actor)
                outcome["rebind"] = "ok"
            except AppBaseError as exc:
                outcome["rebind"] = type(exc).__name__

    worker = threading.Thread(target=reimburse)
    worker.start()
    assert holding.wait(timeout=30)
    binder = threading.Thread(target=rebind)
    binder.start()
    _wait_for_lock_waiter(engine)  # the rebind waits for the reimbursement
    assert binder.is_alive()
    release.set()
    worker.join(timeout=30)
    binder.join(timeout=30)
    assert not worker.is_alive() and not binder.is_alive()

    assert outcome["rebind"] == "InvalidAccountError"  # frozen once the reimbursement committed
    with Session(engine) as db:
        bound = db.exec(
            select(Account.id).where(Account.system_role == SystemRole.DUE_TO_OWNER)
        ).one()
    assert bound == ids["due"]
    assert _counts(engine)["reimb_entries"] == 1


def test_expense_void_racing_reimbursement_one_wins(rb_db):
    """No expense ends up both voided and reimbursed, whoever gets the row lock first."""
    engine, ids = rb_db
    for _ in range(4):
        expense_id = _expense(engine, ids)
        barrier = threading.Barrier(2)
        outcome: dict[str, str] = {}

        def void(expense_id=expense_id, barrier=barrier, outcome=outcome) -> None:
            with Session(engine) as db:
                actor = db.get(User, ids["user"])
                barrier.wait(timeout=30)
                try:
                    ExpenseService(db).void(expense_id, actor=actor, void_date=LATER)
                    outcome["void"] = "ok"
                except AppBaseError as exc:
                    outcome["void"] = type(exc).__name__

        def reimburse(expense_id=expense_id, barrier=barrier, outcome=outcome) -> None:
            with Session(engine) as db:
                barrier.wait(timeout=30)
                try:
                    _reimburse(db, ids, [expense_id])
                    outcome["reimburse"] = "ok"
                except AppBaseError as exc:
                    outcome["reimburse"] = type(exc).__name__

        _run([void, reimburse])
        assert sorted(outcome.values()) in (["ExpenseError", "ok"], ["ReimbursementError", "ok"]), (
            outcome
        )
        with Session(engine) as db:
            row = db.get(Expense, expense_id)
            assert not (row.status == "void" and row.reimbursement_id is not None)
            assert (row.status == "void") != (row.reimbursement_id is not None)
            # clean up so the next round starts from a fresh, independent expense
            if row.reimbursement_id is not None:
                ReimbursementService(db).void(
                    row.reimbursement_id,
                    actor=db.get(User, ids["user"]),
                    void_date=date(2026, 3, 9),
                )
                db.refresh(row)
                assert row.reimbursement_id is None


def test_two_voids_racing_one_reimbursement_post_one_reversal(rb_db):
    engine, ids = rb_db
    expense_id = _expense(engine, ids)
    with Session(engine) as db:
        reim_id = _reimburse(db, ids, [expense_id]).id
    barrier = threading.Barrier(2)
    outcome: list[str] = ["", ""]

    def void(index: int) -> None:
        with Session(engine) as db:
            actor = db.get(User, ids["user"])
            barrier.wait(timeout=30)
            try:
                ReimbursementService(db).void(reim_id, actor=actor, void_date=date(2026, 3, 9))
                outcome[index] = "ok"
            except AppBaseError as exc:
                outcome[index] = type(exc).__name__

    _run([lambda i=i: void(i) for i in range(2)])
    assert sorted(outcome) == ["ReimbursementError", "ok"], outcome
    with Session(engine) as db:
        entries = db.exec(select(func.count()).select_from(JournalEntry)).one()
    assert entries == 3  # expense, reimbursement, exactly one reversal
    assert _counts(engine)["linked"] == 0
