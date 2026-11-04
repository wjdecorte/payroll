"""
Application limit trigger under real concurrency: two connections, real COMMITs.

Both transactions insert an application (each holds FOR KEY SHARE on the payment and
invoice rows through the FK checks), then both commit. Exactly one must win; the loser
must fail with the trigger's over-application error, never a deadlock.
"""

import threading
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session

from payroll.ledger.models import Account, AccountType
from payroll.migrations import alembic_runner
from payroll.receivables.models import Customer, Invoice, Payment

D = Decimal
JAN = date(2026, 1, 15)
INSERT = (
    "INSERT INTO payment_application "
    "(create_date, modify_date, payment_id, invoice_id, amount, recognized_date, ar_account_id)"
    " VALUES (now(), now(), :p, :i, :a, '2026-01-15', :ar)"
)


@pytest.fixture
def db(scratch_engine):
    alembic_runner.upgrade()
    with Session(scratch_engine) as s:
        ar = Account(name="A/R", type=AccountType.ASSET)
        cash = Account(name="Cash", type=AccountType.ASSET)
        cust = Customer(name="Acme")
        s.add_all([ar, cash, cust])
        s.flush()

        def invoice(total):
            row = Invoice(customer_id=cust.id, invoice_date=JAN, due_date=JAN, total=D(total))
            s.add(row)
            s.flush()
            return row.id

        def payment(amount):
            row = Payment(
                customer_id=cust.id, received_date=JAN, amount=D(amount), deposit_account_id=cash.id
            )
            s.add(row)
            s.flush()
            return row.id

        ids = {
            "ar": ar.id,
            "pay100": payment("100.00"),
            "pay70a": payment("70.00"),
            "pay70b": payment("70.00"),
            "inv80a": invoice("80.00"),
            "inv80b": invoice("80.00"),
            "inv100": invoice("100.00"),
        }
        s.commit()
    return scratch_engine, ids


def _race(engine, ids, first: tuple[str, str], second: tuple[str, str]):
    """Both insert (no commit), then commit: the first in a thread, the second after it waits."""
    a, b = engine.connect(), engine.connect()
    try:
        ta, tb = a.begin(), b.begin()
        for conn, (pay, inv) in ((a, first), (b, second)):
            conn.execute(
                sa.text(INSERT),
                {"p": ids[pay], "i": ids[inv], "a": D("60.00"), "ar": ids["ar"]},
            )
        outcome: dict[str, BaseException | None] = {}

        def commit_a():
            try:
                ta.commit()
                outcome["a"] = None
            except BaseException as exc:  # noqa: BLE001
                outcome["a"] = exc

        thread = threading.Thread(target=commit_a)
        thread.start()
        thread.join(timeout=1.5)  # a's trigger now waits (or has already failed)
        try:
            tb.commit()
            outcome["b"] = None
        except BaseException as exc:  # noqa: BLE001
            outcome["b"] = exc
        thread.join(timeout=30)
        assert not thread.is_alive()
        return outcome
    finally:
        a.close()
        b.close()


def _assert_one_winner(outcome):
    errors = [e for e in outcome.values() if e is not None]
    assert len(errors) == 1, outcome
    err = errors[0]
    assert isinstance(err, DBAPIError)
    assert "deadlock" not in str(err).lower()
    assert "over-applied" in str(err)


def test_concurrent_over_application_of_a_payment_fails_cleanly(db):
    engine, ids = db
    # pay100 applied 60 to each of two invoices: 60 fits, 120 does not.
    outcome = _race(engine, ids, ("pay100", "inv80a"), ("pay100", "inv80b"))
    _assert_one_winner(outcome)
    with engine.connect() as conn:
        total = conn.execute(sa.text("SELECT coalesce(sum(amount),0) FROM payment_application"))
        assert total.scalar_one() == D("60.00")


def test_concurrent_over_application_of_an_invoice_fails_cleanly(db):
    engine, ids = db
    # inv100 receives 60 from each of two payments: 60 fits, 120 does not.
    outcome = _race(engine, ids, ("pay70a", "inv100"), ("pay70b", "inv100"))
    _assert_one_winner(outcome)
    with engine.connect() as conn:
        total = conn.execute(sa.text("SELECT coalesce(sum(amount),0) FROM payment_application"))
        assert total.scalar_one() == D("60.00")
