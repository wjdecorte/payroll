"""Tests for the ledger setup CLI (payroll.ledger.cli)."""

from datetime import date

import pytest
from sqlalchemy import func
from sqlmodel import select

from payroll.ledger import cli
from payroll.ledger.accounts import AccountService
from payroll.ledger.models import Account, JournalEntry, SourceType
from payroll.ledger.period_lock import PeriodLockService
from payroll.reports.periods import Period
from payroll.reports.statements import ReportService

END = date(2026, 12, 31)


def _entries(session) -> int:
    return session.exec(select(func.count()).select_from(JournalEntry)).one()


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
    count = _entries(session)
    assert 38 <= count <= 50
    assert f"Posted {count} demo journal entries" in out

    entries = session.exec(select(JournalEntry)).all()
    assert {e.source_type for e in entries} == {SourceType.MANUAL, SourceType.OPENING_BALANCE}
    assert sum(1 for e in entries if e.status == "void") == 1
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
    assert _run(session, "seed-demo", "--email", owner_user.email, "--force") == 0
    after = session.exec(select(JournalEntry.id, JournalEntry.status, JournalEntry.memo)).all()
    assert set(before) <= set(after)
    assert len(after) == 2 * len(before)


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
        assert 38 <= _entries(db) <= 50
