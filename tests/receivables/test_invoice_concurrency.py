"""Issuing invoices under real concurrency: separate sessions, real COMMITs."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlmodel import Session, col, select

from payroll.auth.models import User
from payroll.exceptions import AppBaseError
from payroll.ledger.accounts import AccountService
from payroll.ledger.exceptions import PeriodLockedError
from payroll.ledger.models import (
    Account,
    AccountType,
    JournalEntry,
    JournalLine,
    SourceType,
    SystemRole,
)
from payroll.migrations import alembic_runner
from payroll.receivables.invoices import InvoiceLineInput, InvoiceService
from payroll.receivables.models import Customer, Invoice, Item, ReceivablesSettings
from tests.conftest import make_user

D = Decimal
THREADS = 10
OPEN_DAY = date(2026, 4, 10)
LOCKED_DAY = date(2026, 3, 10)


@pytest.fixture
def invoice_db(scratch_engine):
    """A migrated scratch database with an owner, the chart basics and a locked March."""
    alembic_runner.upgrade()
    with Session(scratch_engine) as db:
        user = make_user(db)
        ar = Account(name="A/R", type=AccountType.ASSET, system_role=SystemRole.ACCOUNTS_RECEIVABLE)
        income = Account(name="Sales", type=AccountType.INCOME)
        cust = Customer(name="Acme")
        db.add_all([ar, income, cust])
        db.flush()
        item = Item(name="Work", unit_price=D("10.00"), income_account_id=income.id)
        db.add(item)
        db.flush()
        ids = (user.id, cust.id, item.id)
        db.execute(sa.text("UPDATE period_lock SET lock_date = '2026-03-31' WHERE id = 1"))
        db.commit()
    engine = sa.create_engine(scratch_engine.url, pool_size=THREADS + 5)
    yield engine, ids
    engine.dispose()


def _drafts(engine, ids, days: list[date]) -> list[int]:
    user_id, cust_id, item_id = ids
    out = []
    with Session(engine) as db:
        service = InvoiceService(db)
        actor = db.get(User, user_id)
        for day in days:
            line = InvoiceLineInput(item_id, "Work", D("1"), D("10.00"))
            invoice = service.create_draft(
                actor=actor, customer_id=cust_id, invoice_date=day, lines=[line]
            )
            out.append(invoice.id)
    return out


def _issue_all(engine, ids, invoice_ids: list[int]) -> list[str]:
    barrier = threading.Barrier(len(invoice_ids))

    def work(invoice_id: int) -> str:
        with Session(engine) as db:
            actor = db.get(User, ids[0])
            barrier.wait(timeout=30)
            try:
                InvoiceService(db).issue(invoice_id, actor=actor)
            except PeriodLockedError:
                return "locked"
            return "ok"

    with ThreadPoolExecutor(max_workers=len(invoice_ids)) as pool:
        return list(pool.map(work, invoice_ids))


def _numbers(engine) -> list[int]:
    with Session(engine) as db:
        stmt = (
            select(Invoice.number).where(col(Invoice.number).is_not(None)).order_by(Invoice.number)
        )
        return list(db.exec(stmt).all())


def test_parallel_issues_get_unique_contiguous_numbers(invoice_db):
    engine, ids = invoice_db
    invoice_ids = _drafts(engine, ids, [OPEN_DAY] * THREADS)
    assert _issue_all(engine, ids, invoice_ids) == ["ok"] * THREADS
    assert _numbers(engine) == list(range(1001, 1001 + THREADS))
    with Session(engine) as db:
        assert db.get(ReceivablesSettings, 1).next_invoice_no == 1001 + THREADS
        live = db.exec(select(sa.func.count()).select_from(JournalEntry)).one()
        assert live == THREADS


def test_failed_issues_leave_no_gaps(invoice_db):
    engine, ids = invoice_db
    days = [OPEN_DAY, LOCKED_DAY] * (THREADS // 2)
    invoice_ids = _drafts(engine, ids, days)
    results = _issue_all(engine, ids, invoice_ids)
    assert results.count("locked") == THREADS // 2
    good = results.count("ok")
    assert _numbers(engine) == list(range(1001, 1001 + good))
    with Session(engine) as db:
        assert db.get(ReceivablesSettings, 1).next_invoice_no == 1001 + good
        statuses = [db.get(Invoice, i).status for i in invoice_ids]
    assert statuses == ["open" if r == "ok" else "draft" for r in results]


def _ar_id(engine) -> int:
    with Session(engine) as db:
        return db.exec(
            select(Account.id).where(Account.system_role == SystemRole.ACCOUNTS_RECEIVABLE)
        ).one()


def test_rebind_waits_for_in_flight_issue(invoice_db):
    """A session holding the A/R row FOR SHARE (as an issue does) blocks a rebind."""
    engine, ids = invoice_db
    old_ar = _ar_id(engine)
    with Session(engine) as db:
        new_ar = Account(name="A/R 2", type=AccountType.ASSET)
        db.add(new_ar)
        db.commit()
        new_id = new_ar.id
    holder = Session(engine)
    holder.exec(select(Account).where(Account.id == old_ar).with_for_update(read=True)).one()
    done = threading.Event()

    def rebind():
        with Session(engine) as db:
            AccountService(db).bind_role(
                SystemRole.ACCOUNTS_RECEIVABLE, new_id, actor=db.get(User, ids[0])
            )
        done.set()

    thread = threading.Thread(target=rebind)
    thread.start()
    assert not done.wait(1.0), "rebind did not wait for the in-flight issue"
    holder.rollback()
    thread.join(timeout=30)
    assert done.is_set()
    assert _ar_id(engine) == new_id
    holder.close()


def _race_issue_and_rebind(engine, ids, *, issue_delay=0.0, rebind_delay=0.0):
    """Issue one draft while another session rebinds A/R; returns what happened.

    A delay holds one side back after the barrier so a test can force an interleaving.
    """
    with Session(engine) as db:
        new_ar = Account(name="A/R 2", type=AccountType.ASSET)
        db.add(new_ar)
        db.commit()
        new_id = new_ar.id
    (invoice_id,) = _drafts(engine, ids, [OPEN_DAY])
    barrier = threading.Barrier(2)
    outcome: dict[str, str] = {}

    def issue():
        with Session(engine) as db:
            actor = db.get(User, ids[0])
            barrier.wait(timeout=30)
            time.sleep(issue_delay)
            try:
                InvoiceService(db).issue(invoice_id, actor=actor)
                outcome["issue"] = "ok"
            except AppBaseError as exc:
                outcome["issue"] = type(exc).__name__

    def rebind():
        with Session(engine) as db:
            actor = db.get(User, ids[0])
            barrier.wait(timeout=30)
            time.sleep(rebind_delay)
            try:
                AccountService(db).bind_role(SystemRole.ACCOUNTS_RECEIVABLE, new_id, actor=actor)
                outcome["rebind"] = "ok"
            except AppBaseError as exc:
                outcome["rebind"] = type(exc).__name__

    threads = [threading.Thread(target=issue), threading.Thread(target=rebind)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert set(outcome) == {"issue", "rebind"}
    return invoice_id, new_id, outcome


def _assert_issue_rebind_invariant(engine, invoice_id, old_ar, new_id, outcome):
    """Whatever the interleaving: an invoice is only ever posted to the account that holds
    the A/R role at the end, and a refused/failed side wrote nothing."""
    current_ar = _ar_id(engine)
    with Session(engine) as db:
        invoice = db.get(Invoice, invoice_id)
        if invoice.status == "open":
            assert outcome["issue"] == "ok"
            debit_account = db.exec(
                select(JournalLine.account_id).where(
                    JournalLine.entry_id == invoice.entry_id, JournalLine.debit > 0
                )
            ).one()
            assert debit_account == current_ar
            if outcome["rebind"] == "ok":
                assert current_ar == new_id == debit_account  # rebind first, issue posted to it
            else:
                assert outcome["rebind"] == "InvalidAccountError"
                assert current_ar == old_ar == debit_account  # issue first, rebind refused
        else:
            assert outcome["issue"] != "ok"
            assert invoice.entry_id is None
            assert outcome["rebind"] == "ok"
            assert current_ar == new_id
        entries = db.exec(
            select(JournalEntry).where(JournalEntry.source_type == SourceType.INVOICE)
        ).all()
        assert len(entries) == (1 if invoice.status == "open" else 0)


@pytest.mark.parametrize("round_no", range(5))
def test_issue_racing_rebind_never_posts_to_a_non_ar_account(invoice_db, round_no):
    engine, ids = invoice_db
    old_ar = _ar_id(engine)
    invoice_id, new_id, outcome = _race_issue_and_rebind(engine, ids)
    _assert_issue_rebind_invariant(engine, invoice_id, old_ar, new_id, outcome)


def test_issue_wins_the_race_when_the_rebind_is_held_back(invoice_db):
    engine, ids = invoice_db
    old_ar = _ar_id(engine)
    invoice_id, new_id, outcome = _race_issue_and_rebind(engine, ids, rebind_delay=1.0)
    assert outcome == {"issue": "ok", "rebind": "InvalidAccountError"}  # the forced order
    _assert_issue_rebind_invariant(engine, invoice_id, old_ar, new_id, outcome)


def test_rebind_wins_the_race_when_the_issue_is_held_back(invoice_db):
    engine, ids = invoice_db
    old_ar = _ar_id(engine)
    invoice_id, new_id, outcome = _race_issue_and_rebind(engine, ids, issue_delay=1.0)
    assert outcome == {"issue": "ok", "rebind": "ok"}  # the forced order
    assert _ar_id(engine) == new_id != old_ar
    _assert_issue_rebind_invariant(engine, invoice_id, old_ar, new_id, outcome)
