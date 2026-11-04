"""Deposits under real concurrency: separate sessions, real COMMITs."""

import threading
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlmodel import Session, func, select

from payroll.auth.models import User
from payroll.exceptions import AppBaseError
from payroll.ledger.accounts import AccountService
from payroll.ledger.models import Account, AccountType, SystemRole
from payroll.migrations import alembic_runner
from payroll.receivables.deposits import DepositService
from payroll.receivables.models import Customer, Deposit, Payment
from payroll.receivables.payments import PaymentService
from tests.conftest import make_user

D = Decimal
DAY = date(2026, 4, 10)

ENGINE: list = [None]
IDS: list = [None]


@pytest.fixture
def dep_db(scratch_engine):
    """A migrated scratch database: owner, A/R, Undeposited Funds, a bank, a customer."""
    alembic_runner.upgrade()
    with Session(scratch_engine) as db:
        user = make_user(db)
        ar = Account(name="A/R", type=AccountType.ASSET, system_role=SystemRole.ACCOUNTS_RECEIVABLE)
        undeposited = Account(
            name="Undeposited", type=AccountType.ASSET, system_role=SystemRole.UNDEPOSITED_FUNDS
        )
        bank = Account(name="Bank", type=AccountType.ASSET)
        cust = Customer(name="Acme")
        db.add_all([ar, undeposited, bank, cust])
        db.flush()
        ids = {
            "user": user.id,
            "customer": cust.id,
            "undeposited": undeposited.id,
            "bank": bank.id,
        }
        db.commit()
    engine = sa.create_engine(scratch_engine.url, pool_size=10)
    yield engine, ids
    engine.dispose()


@pytest.fixture(autouse=True)
def _wire(dep_db):
    ENGINE[0], IDS[0] = dep_db
    yield
    ENGINE[0] = IDS[0] = None


def _payment(engine, ids, amount="50.00") -> int:
    with Session(engine) as db:
        return (
            PaymentService(db)
            .record(
                actor=db.get(User, ids["user"]),
                customer_id=ids["customer"],
                received_date=DAY,
                amount=D(amount),
                deposit_account_id=ids["undeposited"],
                method="check",
            )
            .id
        )


def _run(jobs) -> list[str]:
    barrier = threading.Barrier(len(jobs))
    outcome: list[str] = [""] * len(jobs)

    def runner(index, job):
        with Session(ENGINE[0]) as db:
            actor = db.get(User, IDS[0]["user"])
            barrier.wait(timeout=30)
            try:
                job(db, actor)
                outcome[index] = "ok"
            except AppBaseError as exc:
                outcome[index] = type(exc).__name__

    threads = [threading.Thread(target=runner, args=(i, j)) for i, j in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive(), "a worker is stuck (deadlock?)"
    assert "" not in outcome, outcome
    return outcome


def _make_deposit(ids, payment_ids):
    def job(db, actor):
        DepositService(db).create(
            actor=actor,
            deposit_date=DAY,
            bank_account_id=ids["bank"],
            payment_ids=payment_ids,
        )

    return job


@pytest.mark.parametrize("round_no", range(6))
def test_two_deposits_racing_for_one_payment_cannot_both_succeed(dep_db, round_no):
    engine, ids = dep_db
    payment_id = _payment(engine, ids)

    outcome = _run([_make_deposit(ids, [payment_id])] * 2)

    assert sorted(outcome) == ["DepositError", "ok"], outcome
    with Session(engine) as db:
        assert db.exec(select(func.count()).select_from(Deposit)).one() == 1
        deposit = db.exec(select(Deposit)).one()
        assert db.get(Payment, payment_id).deposit_id == deposit.id
        balances = AccountService(db).balances()
        assert balances.get(ids["bank"], D("0.00")) == D("50.00")
        assert balances.get(ids["undeposited"], D("0.00")) == D("0.00")


@pytest.mark.parametrize("round_no", range(6))
def test_overlapping_deposits_take_each_payment_at_most_once(dep_db, round_no):
    """Overlapping selections in opposite order: no deadlock, no payment taken twice."""
    engine, ids = dep_db
    a, b = _payment(engine, ids, "10.00"), _payment(engine, ids, "20.00")

    outcome = _run([_make_deposit(ids, [a, b]), _make_deposit(ids, [b, a])])

    assert sorted(outcome) == ["DepositError", "ok"], outcome
    with Session(engine) as db:
        assert db.exec(select(func.count()).select_from(Deposit)).one() == 1
        assert AccountService(db).balances().get(ids["bank"], D("0.00")) == D("30.00")


@pytest.mark.parametrize("round_no", range(10))
def test_payment_void_racing_deposit_create_never_leaves_void_payment_in_live_deposit(
    dep_db, round_no
):
    engine, ids = dep_db
    payment_id = _payment(engine, ids)

    def void_payment(db, actor):
        PaymentService(db).void(payment_id, actor=actor, void_date=DAY)

    outcome = _run([void_payment, _make_deposit(ids, [payment_id])])

    # Either order is valid: the void first (the deposit is then refused) or the deposit
    # first (the void is then refused). Both succeeding is the bug.
    assert outcome in (["ok", "DepositError"], ["DepositError", "ok"]), outcome
    with Session(engine) as db:
        payment = db.get(Payment, payment_id)
        live = db.exec(select(Deposit).where(Deposit.status == "posted")).all()
        in_live_deposit = payment.deposit_id is not None and any(
            d.id == payment.deposit_id for d in live
        )
        assert not (payment.status == "void" and in_live_deposit)
        balances = AccountService(db).balances()
        if payment.status == "void":
            assert live == [] and payment.deposit_id is None
            assert balances.get(ids["undeposited"], D("0.00")) == D("0.00")
            assert balances.get(ids["bank"], D("0.00")) == D("0.00")
        else:
            assert len(live) == 1 and payment.deposit_id == live[0].id
            assert balances.get(ids["bank"], D("0.00")) == D("50.00")


@pytest.mark.parametrize("round_no", range(6))
def test_deposit_void_racing_payment_void_is_consistent(dep_db, round_no):
    engine, ids = dep_db
    payment_id = _payment(engine, ids)
    with Session(engine) as db:
        deposit_id = (
            DepositService(db)
            .create(
                actor=db.get(User, ids["user"]),
                deposit_date=DAY,
                bank_account_id=ids["bank"],
                payment_ids=[payment_id],
            )
            .id
        )

    def void_payment(db, actor):
        PaymentService(db).void(payment_id, actor=actor, void_date=DAY)

    def void_deposit(db, actor):
        DepositService(db).void(deposit_id, actor=actor, void_date=DAY)

    outcome = _run([void_payment, void_deposit])

    assert outcome[1] == "ok", outcome  # the deposit void always succeeds
    assert outcome[0] in ("ok", "DepositError"), outcome  # the payment void may lose the race
    with Session(engine) as db:
        payment = db.get(Payment, payment_id)
        assert payment.deposit_id is None
        assert db.get(Deposit, deposit_id).status == "void"
        balances = AccountService(db).balances()
        assert balances.get(ids["bank"], D("0.00")) == D("0.00")
        expected = D("0.00") if payment.status == "void" else D("50.00")
        assert balances.get(ids["undeposited"], D("0.00")) == expected
