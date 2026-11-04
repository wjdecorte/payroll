"""Refunds under real concurrency: separate sessions, real COMMITs."""

import threading
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlmodel import Session

from payroll.auth.models import User
from payroll.exceptions import AppBaseError
from payroll.ledger.models import Account, AccountType, SystemRole
from payroll.migrations import alembic_runner
from payroll.receivables.credits import RefundService, customer_credits
from payroll.receivables.invoices import InvoiceLineInput, InvoiceService
from payroll.receivables.models import Customer, Item
from payroll.receivables.payments import ApplicationInput, PaymentService
from tests.conftest import make_user

D = Decimal
DAY = date(2026, 4, 10)


@pytest.fixture
def refund_db(scratch_engine):
    alembic_runner.upgrade()
    with Session(scratch_engine) as db:
        user = make_user(db)
        ar = Account(name="A/R", type=AccountType.ASSET, system_role=SystemRole.ACCOUNTS_RECEIVABLE)
        undeposited = Account(
            name="Undeposited", type=AccountType.ASSET, system_role=SystemRole.UNDEPOSITED_FUNDS
        )
        bank = Account(name="Bank", type=AccountType.ASSET)
        income = Account(name="Sales", type=AccountType.INCOME)
        cust = Customer(name="Acme")
        db.add_all([ar, undeposited, bank, income, cust])
        db.flush()
        item = Item(name="Work", unit_price=D("10.00"), income_account_id=income.id)
        db.add(item)
        db.flush()
        ids = {
            "user": user.id,
            "customer": cust.id,
            "item": item.id,
            "undeposited": undeposited.id,
            "bank": bank.id,
        }
        db.commit()
    engine = sa.create_engine(scratch_engine.url, pool_size=10)
    yield engine, ids
    engine.dispose()


def _payment(engine, ids, amount: str) -> int:
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


def _invoice(engine, ids, price="100.00") -> int:
    with Session(engine) as db:
        actor = db.get(User, ids["user"])
        service = InvoiceService(db)
        line = InvoiceLineInput(ids["item"], "Work", D("1"), D(price))
        draft = service.create_draft(
            actor=actor, customer_id=ids["customer"], invoice_date=DAY, lines=[line]
        )
        return service.issue(draft.id, actor=actor).id


def _refund_job(ids, amount: str):
    def job(db, actor):
        RefundService(db).record(
            actor=actor,
            customer_id=ids["customer"],
            refund_date=DAY,
            amount=D(amount),
            bank_account_id=ids["bank"],
            method="check",
        )

    return job


def _run(engine, ids, jobs) -> list[str]:
    barrier = threading.Barrier(len(jobs))
    outcome: list[str] = [""] * len(jobs)

    def runner(index, job):
        with Session(engine) as db:
            actor = db.get(User, ids["user"])
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


def _credit(engine, ids) -> Decimal:
    with Session(engine) as db:
        return customer_credits(db, None, ids["customer"]).get(ids["customer"], D("0.00"))


@pytest.mark.parametrize("round_no", range(4))
def test_concurrent_refunds_cannot_exceed_credit(refund_db, round_no):
    engine, ids = refund_db
    _payment(engine, ids, "100.00")
    # four refunds of 40.00 against 100.00 of credit: at most two fit, whatever the order
    outcome = _run(engine, ids, [_refund_job(ids, "40.00")] * 4)
    assert all(o in ("ok", "InvalidRefundError") for o in outcome), outcome
    wins = outcome.count("ok")
    assert wins == 2, outcome
    assert _credit(engine, ids) == D("100.00") - D("40.00") * wins >= 0


@pytest.mark.parametrize("round_no", range(4))
def test_refund_racing_apply_never_overspends_credit(refund_db, round_no):
    engine, ids = refund_db
    invoice_id = _invoice(engine, ids)
    payment_id = _payment(engine, ids, "100.00")

    def apply(db, actor):
        PaymentService(db).apply(
            payment_id, actor=actor, applications=[ApplicationInput(invoice_id, D("60.00"))]
        )

    outcome = _run(engine, ids, [apply, _refund_job(ids, "60.00")])
    assert sorted(outcome) == ["InvalidRefundError", "ok"] or sorted(outcome) == [
        "OverApplicationError",
        "ok",
    ], outcome
    assert _credit(engine, ids) == D("40.00")


@pytest.mark.parametrize("round_no", range(4))
def test_refund_racing_payment_void_never_leaves_negative_credit(refund_db, round_no):
    engine, ids = refund_db
    payment_id = _payment(engine, ids, "100.00")

    def void(db, actor):
        PaymentService(db).void(payment_id, actor=actor, void_date=DAY)

    outcome = _run(engine, ids, [void, _refund_job(ids, "60.00")])
    # either the void wins (the refund then finds no credit) or the refund wins
    # (the void is then refused): never both
    assert sorted(outcome) in (["InvalidRefundError", "ok"], ["PaymentStateError", "ok"]), outcome
    assert _credit(engine, ids) in (D("0.00"), D("40.00"))
