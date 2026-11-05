"""Tests for the ledger setup CLI (payroll.ledger.cli)."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func
from sqlmodel import col, select

from payroll.ledger import cli
from payroll.ledger.accounts import AccountService
from payroll.ledger.models import Account, JournalEntry, SourceType
from payroll.ledger.period_lock import PeriodLockService
from payroll.receivables.balances import ar_aging, invoice_states
from payroll.receivables.models import (
    Customer,
    CustomerRefund,
    Deposit,
    Invoice,
    Item,
    Payment,
)
from payroll.reports.basis import Basis
from payroll.reports.periods import Period
from payroll.reports.statements import ReportService

END = date(2026, 12, 31)
D = Decimal


def _entries(session) -> int:
    return session.exec(select(func.count()).select_from(JournalEntry)).one()


def _manual_count(session) -> int:
    return session.exec(
        select(func.count())
        .select_from(JournalEntry)
        .where(col(JournalEntry.source_type).in_((SourceType.MANUAL, SourceType.OPENING_BALANCE)))
    ).one()


def _run(session, *argv: str) -> int:
    return cli.main(list(argv), db=session)


def test_seed_chart_twice_creates_nothing_the_second_time(session, owner_user, capsys):
    assert _run(session, "seed-chart", "--email", owner_user.email) == 0
    first = AccountService(session).list(include_inactive=True)
    assert len(first) > 10
    assert f"Created {len(first)} account(s)" in capsys.readouterr().out
    assert _run(session, "seed-chart", "--email", owner_user.email.upper()) == 0
    assert "Created 0 account(s)" in capsys.readouterr().out
    assert len(AccountService(session).list(include_inactive=True)) == len(first)


@pytest.mark.parametrize("role", ["accountant", "viewer"])
def test_non_owner_is_refused(session, accountant_user, viewer_user, role, capsys):
    user = accountant_user if role == "accountant" else viewer_user
    assert _run(session, "seed-chart", "--email", user.email) == 1
    assert "owner" in capsys.readouterr().err
    assert session.exec(select(func.count()).select_from(Account)).one() == 0


def test_unknown_email_and_inactive_owner_are_refused(session, owner_user, capsys):
    assert _run(session, "seed-chart", "--email", "nobody@example.com") == 1
    assert "No user" in capsys.readouterr().err
    owner_user.is_active = False
    session.flush()
    assert _run(session, "seed-chart", "--email", owner_user.email) == 1


def test_seed_demo_posts_a_balanced_year(session, owner_user, capsys):
    assert _run(session, "seed-demo", "--email", owner_user.email) == 0
    out = capsys.readouterr().out
    assert "balanced" in out and "NOT" not in out
    entries = session.exec(select(JournalEntry)).all()
    manual = [
        e for e in entries if e.source_type in (SourceType.MANUAL, SourceType.OPENING_BALANCE)
    ]
    assert 38 <= len(manual) <= 50
    assert f"Posted {len(manual)} demo journal entries" in out
    assert {e.source_type for e in entries} == {
        SourceType.MANUAL,
        SourceType.OPENING_BALANCE,
        SourceType.INVOICE,
        SourceType.PAYMENT,
        SourceType.DEPOSIT,
        SourceType.REFUND,
    }
    assert sum(1 for e in manual if e.status == "void") == 1
    assert all(date(2026, 1, 1) <= e.entry_date <= END for e in entries)

    reports = ReportService(session)
    trial = reports.trial_balance(END)
    assert trial.balanced and trial.total_debit == trial.total_credit > 0
    year = Period(date(2026, 1, 1), END, "2026")
    assert all(i.ok for i in reports.profit_and_loss(year).invariants)
    assert all(i.ok for i in reports.balance_sheet(END).invariants)


def test_seed_demo_refuses_when_entries_exist(session, owner_user, capsys):
    assert _run(session, "seed-demo", "--email", owner_user.email) == 0
    capsys.readouterr()
    before = _entries(session)
    assert _run(session, "seed-demo", "--email", owner_user.email) == 1
    assert "already has" in capsys.readouterr().err
    assert _entries(session) == before


def test_seed_demo_force_only_adds(session, owner_user, capsys):
    assert _run(session, "seed-demo", "--email", owner_user.email) == 0
    before = session.exec(select(JournalEntry.id, JournalEntry.status, JournalEntry.memo)).all()
    manual_before = _manual_count(session)
    assert _run(session, "seed-demo", "--email", owner_user.email, "--force") == 0
    after = session.exec(select(JournalEntry.id, JournalEntry.status, JournalEntry.memo)).all()
    assert set(before) <= set(after)
    # The journal year is added again; the receivables (customers exist) are not.
    assert "Receivables not added" in capsys.readouterr().out
    assert session.exec(select(func.count()).select_from(Customer)).one() == 4
    assert session.exec(select(func.count()).select_from(Invoice)).one() == 7
    assert len(after) > len(before)
    assert _manual_count(session) == 2 * manual_before


def test_seed_demo_failure_posts_nothing(session, owner_user, capsys):
    """A period lock covering the demo year makes the post fail; nothing partial remains."""
    from payroll.auth.security import hash_password

    owner_user.password_hash = hash_password("Correct-Horse-Battery-9!")
    PeriodLockService(session).set(
        date(2026, 6, 30), password="Correct-Horse-Battery-9!", actor=owner_user
    )
    assert _run(session, "seed-demo", "--email", owner_user.email) == 1
    assert "Could not post the demo year" in capsys.readouterr().err
    assert _entries(session) == 0


def test_main_without_injected_session_migrates_and_commits(scratch_engine, capsys):
    from sqlmodel import Session

    from payroll.migrations import alembic_runner
    from tests.conftest import make_user

    alembic_runner.upgrade()
    with Session(scratch_engine) as db:
        make_user(db, email="cli.owner@example.com")
        db.commit()
    assert cli.main(["seed-chart", "--email", "cli.owner@example.com"]) == 0
    assert cli.main(["seed-demo", "--email", "cli.owner@example.com"]) == 0
    assert cli.main(["seed-demo", "--email", "cli.owner@example.com"]) == 1
    capsys.readouterr()
    with Session(scratch_engine) as db:
        assert 38 <= _manual_count(db) <= 50
        assert _entries(db) > _manual_count(db)  # the receivables documents posted too


def test_seed_demo_adds_receivables_that_reconcile(session, owner_user, capsys):
    assert _run(session, "seed-demo", "--email", owner_user.email) == 0
    assert "Receivables: 4 customers, 3 items, 7 invoices, 4 payments, 1 deposit, 1 refund" in (
        capsys.readouterr().out
    )
    assert session.exec(select(func.count()).select_from(Customer)).one() == 4
    assert session.exec(select(func.count()).select_from(Item)).one() == 3
    assert session.exec(select(func.count()).select_from(Payment)).one() == 4
    assert session.exec(select(func.count()).select_from(Deposit)).one() == 1
    assert session.exec(select(func.count()).select_from(CustomerRefund)).one() == 1

    # Every status the app shows is present at year end.
    states = invoice_states(session, END, include_inactive=True)
    assert {s.status for s in states} == {"open", "partial", "paid", "overdue", "void"}
    assert len(states) == 7

    aging = ar_aging(session, END)
    assert aging.difference == 0 and all(i.ok for i in aging.invariants)
    assert aging.totals.total > 0 and aging.totals.credit == D(
        "300.00"
    )  # Delta's unrefunded credit

    reports = ReportService(session)
    year = Period(date(2026, 1, 1), END, "2026")
    for basis in (Basis.CASH, Basis.ACCRUAL):
        assert all(i.ok for i in reports.profit_and_loss(year, basis=basis).invariants)
        assert all(i.ok for i in reports.balance_sheet(END, basis=basis).invariants)
    assert reports.trial_balance(END).balanced


def test_seed_demo_twice_refuses_the_second_time_and_adds_no_receivables(
    session, owner_user, capsys
):
    assert _run(session, "seed-demo", "--email", owner_user.email) == 0
    capsys.readouterr()
    before = session.exec(select(func.count()).select_from(Invoice)).one()
    assert before == 7
    assert _run(session, "seed-demo", "--email", owner_user.email) == 1
    assert "already has" in capsys.readouterr().err
    assert session.exec(select(func.count()).select_from(Invoice)).one() == before
    assert session.exec(select(func.count()).select_from(Customer)).one() == 4
