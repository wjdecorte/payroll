"""Payments and applications under real concurrency: separate sessions, real COMMITs."""

import threading
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlmodel import Session, col, func, select

from payroll.auth.models import User
from payroll.exceptions import AppBaseError
from payroll.ledger.accounts import AccountService
from payroll.ledger.models import Account, AccountType, SystemRole
from payroll.migrations import alembic_runner
from payroll.receivables.invoices import InvoiceLineInput, InvoiceService, live_applied
from payroll.receivables.models import Customer, Invoice, Item, Payment, PaymentApplication
from payroll.receivables.payments import ApplicationInput, PaymentService
from tests.conftest import make_user

D = Decimal
DAY = date(2026, 4, 10)


@pytest.fixture
def pay_db(scratch_engine):
    """A migrated scratch database with an owner, A/R, Undeposited Funds, a customer, an item."""
    alembic_runner.upgrade()
    with Session(scratch_engine) as db:
        user = make_user(db)
        ar = Account(name="A/R", type=AccountType.ASSET, system_role=SystemRole.ACCOUNTS_RECEIVABLE)
        undeposited = Account(
            name="Undeposited", type=AccountType.ASSET, system_role=SystemRole.UNDEPOSITED_FUNDS
        )
        income = Account(name="Sales", type=AccountType.INCOME)
        cust = Customer(name="Acme")
        db.add_all([ar, undeposited, income, cust])
        db.flush()
        item = Item(name="Work", unit_price=D("10.00"), income_account_id=income.id)
        db.add(item)
        db.flush()
        ids = {
            "user": user.id,
            "customer": cust.id,
            "item": item.id,
            "undeposited": undeposited.id,
            "ar": ar.id,
        }
        db.commit()
    engine = sa.create_engine(scratch_engine.url, pool_size=10)
    yield engine, ids
    engine.dispose()


def _invoice(engine, ids, price="100.00") -> int:
    with Session(engine) as db:
        actor = db.get(User, ids["user"])
        service = InvoiceService(db)
        line = InvoiceLineInput(ids["item"], "Work", D("1"), D(price))
        draft = service.create_draft(
            actor=actor, customer_id=ids["customer"], invoice_date=DAY, lines=[line]
        )
        return service.issue(draft.id, actor=actor).id


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


def _run(jobs) -> list[str]:
    """Run each job in its own thread and session, released together by a barrier."""
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


ENGINE: list = [None]
IDS: list = [None]


@pytest.fixture(autouse=True)
def _wire(pay_db):
    ENGINE[0], IDS[0] = pay_db
    yield
    ENGINE[0] = IDS[0] = None


def _applied_total(engine) -> Decimal:
    with Session(engine) as db:
        return Decimal(db.exec(select(func.coalesce(func.sum(PaymentApplication.amount), 0))).one())


@pytest.mark.parametrize("round_no", range(3))
@pytest.mark.parametrize("amount", ["50.00", "60.00"])
def test_two_payments_racing_for_one_invoice_cannot_exceed_total(pay_db, amount, round_no):
    """record()+apply() racing. The payment entry counter serializes the two records, so this
    does not exercise the invoice row lock (see the apply-vs-apply test below)."""
    engine, ids = pay_db
    invoice_id = _invoice(engine, ids)

    def record(db, actor):
        PaymentService(db).record(
            actor=actor,
            customer_id=ids["customer"],
            received_date=DAY,
            amount=D(amount),
            deposit_account_id=ids["undeposited"],
            method="ach",
            applications=[ApplicationInput(invoice_id, D(amount))],
        )

    outcome = _run([record, record])
    wins = outcome.count("ok")
    assert wins == (2 if amount == "50.00" else 1), outcome
    assert all(o in ("ok", "OverApplicationError") for o in outcome), outcome
    with Session(engine) as db:
        applied = live_applied(db, invoice_id)
        assert applied == D(amount) * wins <= D("100.00")
        assert db.exec(select(func.count()).select_from(Payment)).one() == wins
        # the ledger and the subledger agree: A/R = invoice - payments recorded
        ar_balance = AccountService(db).balances().get(ids["ar"], D("0.00"))
        assert ar_balance == D("100.00") - D(amount) * wins
        assert ar_balance == db.get(Invoice, invoice_id).total - applied


def test_same_payment_applied_twice_concurrently_cannot_exceed_amount(pay_db):
    engine, ids = pay_db
    first, second = _invoice(engine, ids), _invoice(engine, ids)
    payment_id = _payment(engine, ids, "100.00")

    def apply_to(invoice_id):
        def job(db, actor):
            PaymentService(db).apply(
                payment_id, actor=actor, applications=[ApplicationInput(invoice_id, D("60.00"))]
            )

        return job

    outcome = _run([apply_to(first), apply_to(second)])
    assert sorted(outcome) == ["OverApplicationError", "ok"], outcome
    assert _applied_total(engine) == D("60.00")


def test_same_payment_applied_twice_to_one_invoice_is_refused_once(pay_db):
    engine, ids = pay_db
    invoice_id = _invoice(engine, ids)
    payment_id = _payment(engine, ids, "100.00")

    def job(db, actor):
        PaymentService(db).apply(
            payment_id, actor=actor, applications=[ApplicationInput(invoice_id, D("30.00"))]
        )

    outcome = _run([job, job])
    assert sorted(outcome) == ["InvalidPaymentError", "ok"], outcome
    assert _applied_total(engine) == D("30.00")


@pytest.mark.parametrize("action", ["void", "replace"])
@pytest.mark.parametrize("round_no", range(6))
def test_application_racing_invoice_void_or_replace_never_leaves_both(pay_db, action, round_no):
    engine, ids = pay_db
    invoice_id = _invoice(engine, ids)
    payment_id = _payment(engine, ids, "100.00")
    with Session(engine) as db:
        entry_before = db.get(Invoice, invoice_id).entry_id

    def apply(db, actor):
        PaymentService(db).apply(
            payment_id, actor=actor, applications=[ApplicationInput(invoice_id, D("40.00"))]
        )

    def change(db, actor):
        service = InvoiceService(db)
        if action == "void":
            service.void(invoice_id, actor=actor, void_date=DAY)
        else:
            line = InvoiceLineInput(ids["item"], "Smaller", D("1"), D("20.00"))
            service.replace(
                invoice_id,
                actor=actor,
                customer_id=ids["customer"],
                invoice_date=DAY,
                lines=[line],
            )

    outcome = _run([apply, change])
    # a replace that wins shrinks the invoice to 20.00, so the 40.00 application is then
    # refused as an over-application; after a void it is refused as a non-open invoice
    assert outcome[0] in ("ok", "InvoiceStateError", "OverApplicationError"), outcome
    assert outcome[1] in ("ok", "InvoiceStateError"), outcome
    with Session(engine) as db:
        invoice = db.get(Invoice, invoice_id)
        applied = live_applied(db, invoice_id)
        if applied > 0:
            # the application won: the invoice was left exactly as it was
            assert outcome == ["ok", "InvoiceStateError"], outcome
            assert (invoice.status, invoice.total, invoice.entry_id) == (
                "open",
                D("100.00"),
                entry_before,
            )
        else:
            # the change won: the application was refused because the invoice is gone
            assert outcome[0] != "ok" and outcome[1] == "ok", outcome
            assert db.exec(select(func.count()).select_from(PaymentApplication)).one() == 0
            if action == "void":
                assert invoice.status == "void"
            else:
                assert invoice.total == D("20.00") and invoice.entry_id != entry_before
        rows = db.exec(select(col(PaymentApplication.invoice_id))).all()
        assert (len(rows) == 1) == (applied > 0)


@pytest.mark.parametrize("round_no", range(6))
def test_two_recorded_payments_applied_concurrently_cannot_exceed_invoice(pay_db, round_no):
    """Two existing payments apply() to one invoice at once: only the invoice lock serializes them."""
    engine, ids = pay_db
    invoice_id = _invoice(engine, ids)
    first, second = _payment(engine, ids, "60.00"), _payment(engine, ids, "60.00")

    def apply_from(payment_id):
        def job(db, actor):
            PaymentService(db).apply(
                payment_id, actor=actor, applications=[ApplicationInput(invoice_id, D("60.00"))]
            )

        return job

    outcome = _run([apply_from(first), apply_from(second)])
    assert sorted(outcome) == ["OverApplicationError", "ok"], outcome
    assert _applied_total(engine) == D("60.00")
    with Session(engine) as db:
        assert live_applied(db, invoice_id) == D("60.00") <= db.get(Invoice, invoice_id).total
        ar_balance = AccountService(db).balances().get(ids["ar"], D("0.00"))
        assert ar_balance == D("100.00") - D("120.00")  # invoice less both payments recorded
