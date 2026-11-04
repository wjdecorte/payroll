"""Issuing invoices under real concurrency: separate sessions, real COMMITs."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlmodel import Session, col, select

from payroll.auth.models import User
from payroll.ledger.exceptions import PeriodLockedError
from payroll.ledger.models import Account, AccountType, JournalEntry, SystemRole
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
