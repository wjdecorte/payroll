"""Tests for the ledger setup CLI (payroll.ledger.cli)."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func
from sqlmodel import col, select

from payroll import app_settings
from payroll.expenses.models import Attachment, Expense, Reimbursement, Transfer, Vendor
from payroll.expenses.owner import owner_summary
from payroll.ledger import cli
from payroll.ledger.accounts import AccountService
from payroll.ledger.models import Account, JournalEntry, SourceType
from payroll.ledger.period_lock import PeriodLockService
from payroll.payroll_run.ledger_models import TaxPayment
from payroll.payroll_run.liability import payroll_liability_report
from payroll.payroll_run.models import PayrollRun
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
SETTLEMENT_MEMO = "Owner paid the June net pay owed"  # the payroll section's journal


@pytest.fixture(autouse=True)
def _attachment_dir(tmp_path, monkeypatch):
    """Demo receipts go to a scratch directory, never the configured one."""
    monkeypatch.setattr(app_settings, "attachment_dir", str(tmp_path / "attachments"))
    return tmp_path / "attachments"


def _entries(session) -> int:
    return session.exec(select(func.count()).select_from(JournalEntry)).one()


def _manual_count(session) -> int:
    return session.exec(
        select(func.count())
        .select_from(JournalEntry)
        .where(
            col(JournalEntry.source_type).in_((SourceType.MANUAL, SourceType.OPENING_BALANCE)),
            col(JournalEntry.memo) != SETTLEMENT_MEMO,
        )
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
        e
        for e in entries
        if e.source_type in (SourceType.MANUAL, SourceType.OPENING_BALANCE)
        and e.memo != SETTLEMENT_MEMO
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
        SourceType.EXPENSE,
        SourceType.REIMBURSEMENT,
        SourceType.TRANSFER,
        SourceType.PAYROLL_RUN,
        SourceType.TAX_PAYMENT,
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


EXPENSE_TABLES = (Vendor, Expense, Reimbursement, Transfer, Attachment)


def _expense_counts(session) -> list[int]:
    return [session.exec(select(func.count()).select_from(t)).one() for t in EXPENSE_TABLES]


def test_seed_demo_adds_expenses_that_reconcile(session, owner_user, capsys, _attachment_dir):
    assert _run(session, "seed-demo", "--email", owner_user.email) == 0
    out = capsys.readouterr().out
    assert "Expenses: 2 vendors, 4 expenses, 1 reimbursement, 1 card payment, 1 receipt." in out
    assert _expense_counts(session) == [2, 4, 1, 1, 1]

    summary = owner_summary(session, END)
    assert summary.matches and summary.owed_to_me == D("67.15")
    assert len(summary.open_expenses) == 1
    split = session.exec(select(Expense).where(Expense.memo == "Office order")).one()
    receipt = session.exec(select(Attachment)).one()
    assert receipt.owner_id == split.id and receipt.content_type == "application/pdf"
    assert (_attachment_dir / receipt.stored_name).is_file()

    reports = ReportService(session)
    assert reports.trial_balance(END).balanced
    aging = ar_aging(session, END)
    assert aging.difference == 0 and all(i.ok for i in aging.invariants)
    names = {n.full_name for n in AccountService(session).tree()}
    assert "Business Visa" in names


def test_seed_demo_twice_leaves_the_expense_rows_unchanged(session, owner_user, capsys):
    assert _run(session, "seed-demo", "--email", owner_user.email) == 0
    before = _expense_counts(session)
    accounts = len(AccountService(session).list(include_inactive=True))
    capsys.readouterr()
    assert _run(session, "seed-demo", "--email", owner_user.email) == 1  # refused
    assert _expense_counts(session) == before
    assert _run(session, "seed-demo", "--email", owner_user.email, "--force") == 0
    assert "Expenses not added" in capsys.readouterr().out
    assert _expense_counts(session) == before
    assert len(AccountService(session).list(include_inactive=True)) == accounts
    assert owner_summary(session, END).matches


def test_seed_demo_skips_the_receipt_when_the_directory_is_not_writable(
    session, owner_user, capsys, tmp_path, monkeypatch
):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    monkeypatch.setattr(app_settings, "attachment_dir", str(blocker / "attachments"))
    assert _run(session, "seed-demo", "--email", owner_user.email) == 0
    out = capsys.readouterr().out
    assert "Receipt skipped" in out and "0 receipt" in out
    assert _expense_counts(session) == [2, 4, 1, 1, 0]
    assert owner_summary(session, END).matches


def _payroll_counts(session) -> list[int]:
    return [
        session.exec(select(func.count()).select_from(t)).one() for t in (PayrollRun, TaxPayment)
    ]


def test_seed_demo_adds_a_year_of_payroll_that_reconciles(session, owner_user, capsys):
    assert _run(session, "seed-demo", "--email", owner_user.email) == 0
    out = capsys.readouterr().out
    assert "Payroll: 12 runs (1 voided and approved again, 1 paid to Due to Owner)" in out

    runs = session.exec(select(PayrollRun).order_by(col(PayrollRun.id))).all()
    live = [r for r in runs if r.status == "approved"]
    assert [r.pay_period for r in live] == [f"2026-{m:02d}" for m in range(1, 13)]
    assert [r.pay_period for r in runs if r.status == "void"] == ["2026-03"]
    assert len(runs) == 13
    # The chain is unbroken, including across the voided and re-approved month.
    assert live[0].ytd_fica_before == 0
    for before, after in zip(live, live[1:], strict=False):
        assert after.ytd_fica_before == before.ytd_fica_after
    january = live[0]
    assert january.health_insurance == D("500.00") and january.hsa_contribution == D("300.00")
    assert all(r.health_insurance == 0 and r.hsa_contribution == 0 for r in live[1:])
    assert [r.pay_period for r in live if r.net_pay_source == "owner"] == ["2026-06"]
    assert all(r.net_pay_source == "account" for r in live if r.pay_period != "2026-06")

    assert session.exec(select(func.count()).select_from(TaxPayment)).one() > 10
    report = payroll_liability_report(session, END)
    for agency in report.agencies:
        assert agency.other == 0, agency.agency
        assert agency.balance == sum((p.unpaid for p in agency.periods), D("0.00"))
    assert any(p.unpaid > 0 for a in report.agencies for p in a.periods)  # something is owed

    reports = ReportService(session)
    assert reports.trial_balance(END).balanced
    aging = ar_aging(session, END)
    assert aging.difference == 0 and all(i.ok for i in aging.invariants)
    summary = owner_summary(session, END)
    assert summary.matches and summary.owed_to_me == D("67.15")
    year = Period(date(2026, 1, 1), END, "2026")
    for basis in (Basis.CASH, Basis.ACCRUAL):
        assert all(i.ok for i in reports.profit_and_loss(year, basis=basis).invariants)
        assert all(i.ok for i in reports.balance_sheet(END, basis=basis).invariants)


def test_seed_demo_twice_leaves_the_payroll_unchanged(session, owner_user, capsys):
    assert _run(session, "seed-demo", "--email", owner_user.email) == 0
    before = _payroll_counts(session)
    entries = _entries(session)
    capsys.readouterr()
    assert _run(session, "seed-demo", "--email", owner_user.email) == 1  # refused
    assert _payroll_counts(session) == before
    assert _run(session, "seed-demo", "--email", owner_user.email, "--force") == 0
    assert "Payroll not added" in capsys.readouterr().out
    assert _payroll_counts(session) == before
    assert _entries(session) > entries  # --force still adds the year's manual entries
    assert all(a.other == 0 for a in payroll_liability_report(session, END).agencies)
