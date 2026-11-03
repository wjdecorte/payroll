"""
Ledger setup CLI: ``python -m payroll.ledger.cli <command>``.

Commands (no password is ever prompted: the actor is the owner found by email):

- ``seed-chart --email OWNER``: create the default chart of accounts and bind the
  system roles. Idempotent; prints how many accounts were created.
- ``seed-demo --email OWNER [--force]``: post a fictitious 2026 year (about forty
  balanced journal entries, with reversals and a void) for exploring the reports.
  Refuses (exit 1) when any journal entry already exists unless ``--force``; even
  then it only adds entries and never touches existing ones. It also seeds the
  default chart first (idempotent), since the entries need those accounts.

When run as a program the CLI first applies pending migrations, like
``payroll.auth.cli``. Exit codes: 0 ok, 1 error (message on stderr), 2 usage error.
"""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from alembic.util.exc import CommandError
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, func, select

from payroll import app_settings
from payroll.auth.models import User, UserRole
from payroll.exceptions import AppBaseError
from payroll.ledger.accounts import AccountService
from payroll.ledger.default_chart import seed_default_chart
from payroll.ledger.models import JournalEntry, SourceType
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft
from payroll.reports.statements import ReportService

DEMO_YEAR = 2026


class CliError(Exception):
    """A user-facing error: shown on stderr, exit code 1."""


def _owner(db: Session, email: str) -> User:
    user = db.exec(select(User).where(User.email == email.strip().lower())).first()
    if user is None:
        raise CliError(f"No user with email {email.strip().lower()}.")
    if user.role != UserRole.OWNER or not user.is_active:
        raise CliError("The ledger CLI must be run as an active owner.")
    return user


def _entry_count(db: Session) -> int:
    return db.exec(select(func.count()).select_from(JournalEntry)).one()


# ----------------------------------------------------------------------
# Demo year
# ----------------------------------------------------------------------

D = Decimal
CHECKING = "Business Checking"
REVENUE = "Services Revenue"
WAGES = "Payroll Expenses:Wages"
TAXES = "Payroll Expenses:Taxes"
OPENING_EQUITY = "Opening Balance Equity"

# Monthly services revenue received into checking (January..December).
REVENUE_BY_MONTH = [
    "8500.00",
    "9200.00",
    "10150.00",
    "9800.00",
    "11000.00",
    "10400.00",
    "9600.00",
    "11250.00",
    "12000.00",
    "11800.00",
    "12500.00",
    "13400.00",
]


@dataclass(frozen=True)
class DemoSummary:
    entries: int
    reversals: int
    voided: int
    accounts_created: int
    balanced: bool
    total_debits: Decimal


def _simple(
    posting: LedgerPostingService,
    actor: User,
    ids: dict[str, int],
    when: date,
    debit: str,
    credit: str,
    amount: str,
    memo: str,
    source_type: SourceType = SourceType.MANUAL,
) -> JournalEntry:
    value = D(amount)
    return posting.post(
        EntryDraft(
            entry_date=when,
            memo=memo,
            source_type=source_type,
            lines=[
                LineDraft(ids[debit], debit=value),
                LineDraft(ids[credit], credit=value),
            ],
        ),
        actor=actor,
    )


def _post_demo_year(db: Session, actor: User, accounts_created: int) -> DemoSummary:
    ids = {n.full_name: n.account.id for n in AccountService(db).tree(include_inactive=True)}
    posting = LedgerPostingService(db)

    def simple(when: date, debit: str, credit: str, amount: str, memo: str, **kwargs):
        return _simple(posting, actor, ids, when, debit, credit, amount, memo, **kwargs)

    posted = 0
    simple(
        date(DEMO_YEAR, 1, 2),
        CHECKING,
        OPENING_EQUITY,
        "25000.00",
        "Opening balance",
        source_type=SourceType.OPENING_BALANCE,
    )
    posted += 1

    for month, amount in enumerate(REVENUE_BY_MONTH, start=1):
        simple(
            date(DEMO_YEAR, month, 15),
            CHECKING,
            REVENUE,
            amount,
            f"Consulting revenue received, {date(DEMO_YEAR, month, 1):%B}",
        )
        wages, taxes = D("4800.00"), D("367.20")
        posting.post(
            EntryDraft(
                entry_date=date(DEMO_YEAR, month, 28),
                memo=f"Owner payroll, {date(DEMO_YEAR, month, 1):%B}",
                lines=[
                    LineDraft(ids[WAGES], debit=wages),
                    LineDraft(ids[TAXES], debit=taxes),
                    LineDraft(ids[CHECKING], credit=wages + taxes),
                ],
            ),
            actor=actor,
        )
        posted += 2

    recurring = [
        (3, 5, "Software and Subscriptions", "389.00", "Accounting and CRM software, Q1"),
        (6, 5, "Software and Subscriptions", "389.00", "Accounting and CRM software, Q2"),
        (9, 5, "Software and Subscriptions", "389.00", "Accounting and CRM software, Q3"),
        (12, 5, "Software and Subscriptions", "389.00", "Accounting and CRM software, Q4"),
        (3, 20, "Professional Fees", "850.00", "Tax preparation retainer"),
        (9, 20, "Professional Fees", "425.00", "Legal review of client agreement"),
        (2, 9, "Office Expenses", "142.37", "Printer paper and toner"),
        (5, 12, "Office Expenses", "219.99", "Desk chair"),
        (10, 7, "Office Expenses", "63.40", "Postage and shipping"),
        (4, 22, "Travel", "612.80", "Flight to client site"),
        (8, 18, "Travel", "287.45", "Mileage and parking"),
        (5, 3, "Meals", "74.10", "Client lunch"),
        (11, 14, "Meals", "58.25", "Client dinner"),
        (6, 30, "Bank Charges", "45.00", "Quarterly account fees"),
    ]
    made: dict[str, JournalEntry] = {}
    for month, day, account, amount, memo in recurring:
        made[memo] = simple(date(DEMO_YEAR, month, day), account, CHECKING, amount, memo)
        posted += 1

    for month, memo in (
        (6, "Shareholder distribution, mid-year"),
        (12, "Shareholder distribution, year end"),
    ):
        simple(date(DEMO_YEAR, month, 30), "Shareholder Distributions", CHECKING, "6000.00", memo)
        posted += 1

    # Two posted-in-error entries reversed, and one wrong entry voided.
    reversals = 0
    for memo, day in (
        ("Desk chair", date(DEMO_YEAR, 5, 14)),
        ("Flight to client site", date(DEMO_YEAR, 4, 25)),
    ):
        posting.reverse(made[memo].id, entry_date=day, actor=actor)
        reversals += 1
    posting.void(made["Client dinner"].id, entry_date=date(DEMO_YEAR, 11, 15), actor=actor)
    posted += reversals + 1

    trial = ReportService(db).trial_balance(date(DEMO_YEAR, 12, 31))
    return DemoSummary(
        entries=posted,
        reversals=reversals + 1,
        voided=1,
        accounts_created=accounts_created,
        balanced=trial.balanced,
        total_debits=trial.total_debit,
    )


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m payroll.ledger.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    chart = sub.add_parser("seed-chart", help="create the default chart of accounts")
    chart.add_argument("--email", required=True, help="an owner's email")

    demo = sub.add_parser("seed-demo", help="post a fictitious 2026 year for exploring reports")
    demo.add_argument("--email", required=True, help="an owner's email")
    demo.add_argument(
        "--force", action="store_true", help="add the demo year although entries already exist"
    )
    return parser


def _run(args: argparse.Namespace, db: Session) -> str:
    actor = _owner(db, args.email)
    if args.command == "seed-chart":
        created = seed_default_chart(db, actor=actor)
        return f"Created {created} account(s); the default chart is in place."
    existing = _entry_count(db)
    if existing and not args.force:
        raise CliError(
            f"The ledger already has {existing} journal entries; "
            "pass --force to add the demo year anyway (existing entries are never changed)."
        )
    try:
        accounts_created = seed_default_chart(db, actor=actor)  # commits its own work
        with db.begin_nested():  # all or nothing: a failure undoes the entries posted so far
            summary = _post_demo_year(db, actor, accounts_created)
    except AppBaseError as exc:
        raise CliError(f"Could not post the demo year: {exc.message}") from exc
    return (
        f"Posted {summary.entries} demo journal entries for {DEMO_YEAR} "
        f"({summary.reversals} reversals, {summary.voided} voided); "
        f"created {summary.accounts_created} account(s). "
        f"Trial balance at {DEMO_YEAR}-12-31: debits {summary.total_debits}, "
        f"{'balanced' if summary.balanced else 'NOT balanced'}."
    )


def main(argv: Sequence[str] | None = None, db: Session | None = None) -> int:
    """
    Run one command. ``db`` is for tests; otherwise migrations are applied and a
    session on its own engine is used. The transaction is committed only on success.
    """
    args = _build_parser().parse_args(argv)
    try:
        if db is not None:
            message = _run(args, db)
            db.commit()
        else:
            from payroll.migrations import alembic_runner

            alembic_runner.upgrade()
            engine = create_engine(app_settings.database_url)
            try:
                with Session(engine) as session:
                    message = _run(args, session)
                    session.commit()
            finally:
                engine.dispose()
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (SQLAlchemyError, CommandError) as exc:
        if db is not None:
            db.rollback()
        print(f"error: Database error: {exc}; nothing was changed.", file=sys.stderr)
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
