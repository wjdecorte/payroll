"""
Ledger setup CLI: ``python -m payroll.ledger.cli <command>``.

Commands (no password is ever prompted: the actor is the owner found by email):

- ``seed-chart --email OWNER``: create the default chart of accounts and bind the
  system roles. Idempotent; prints how many accounts were created.
- ``seed-demo --email OWNER [--force]``: post a fictitious 2026 year (about forty
  balanced journal entries, with reversals and a void) for exploring the reports.
  Refuses (exit 1) when any journal entry already exists unless ``--force``; even
  then it only adds entries and never touches existing ones. It also seeds the
  default chart first (idempotent), since the entries need those accounts. It also adds
  receivables: four customers, three items, invoices (open, partial, paid, overdue, void),
  payments (to Undeposited Funds and straight to the bank, one overpayment), a deposit and a
  refund, so the aging page and every report on both bases have something to show.
  Finally it adds expenses (skipped when vendors already exist): two vendors, a split expense
  paid from the bank, two owner-paid expenses with one reimbursed, a credit card expense and a
  bank-to-card payment transfer, and a tiny PDF receipt stored under the attachment directory
  (skipped with a notice when that directory is not writable), so the Due to Owner balance,
  the owner report and the expense pages have something to show.

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
from payroll.expenses.attachments import AttachmentService
from payroll.expenses.expenses import ExpenseLineInput, ExpenseService
from payroll.expenses.models import Vendor
from payroll.expenses.reimbursements import ReimbursementService
from payroll.expenses.transfers import TransferService
from payroll.expenses.vendors import VendorService
from payroll.ledger.accounts import AccountService
from payroll.ledger.default_chart import seed_default_chart
from payroll.ledger.models import AccountType, JournalEntry, SourceType
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft
from payroll.receivables.credits import RefundService
from payroll.receivables.customers import CustomerService
from payroll.receivables.deposits import DepositService
from payroll.receivables.invoices import InvoiceLineInput, InvoiceService
from payroll.receivables.items import ItemService
from payroll.receivables.models import Customer
from payroll.receivables.payments import ApplicationInput, PaymentService
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


@dataclass(frozen=True)
class ReceivablesSummary:
    customers: int
    items: int
    invoices: int
    payments: int
    deposits: int
    refunds: int


def _post_demo_receivables(db: Session, actor: User) -> ReceivablesSummary:
    """
    Receivables for the demo year, built through the real services (each commits itself).

    At 2026-12-31 the invoices are: two paid (Acme in January and June), one overdue (Cedar,
    unpaid since March), one void (Delta, April), one paid after an overpayment (Delta, July),
    one partial (Birch, December) and one open (Acme, due the day after year end).
    """
    accounts = AccountService(db)
    ids = {n.full_name: n.account.id for n in accounts.tree(include_inactive=True)}
    if "Training Revenue" not in ids:
        ids["Training Revenue"] = accounts.create(
            actor=actor, name="Training Revenue", type=AccountType.INCOME, number="4100"
        ).id
    customers = CustomerService(db)
    acme, birch, cedar, delta = (
        customers.create(actor=actor, name=name, email=email, terms_days=terms)
        for name, email, terms in (
            ("Acme Corp", "ap@acme.example", 30),
            ("Birch Design", "billing@birch.example", 30),
            ("Cedar Health", "payables@cedar.example", 45),
            ("Delta Logistics", "ap@delta.example", 30),
        )
    )
    item_service = ItemService(db)
    consulting, retainer, workshop = (
        item_service.create(
            actor=actor, name=name, income_account_id=ids[account], unit_price=D(price)
        )
        for name, account, price in (
            ("Consulting (hourly)", "Services Revenue", "150.00"),
            ("Monthly Retainer", "Services Revenue", "2500.00"),
            ("Training Workshop", "Training Revenue", "1200.00"),
        )
    )
    invoices = InvoiceService(db)
    payments = PaymentService(db)
    bank, undeposited = ids["Business Checking"], ids["Undeposited Funds"]

    def invoice(customer, when: date, *lines: tuple, void_on: date | None = None):
        draft = invoices.create_draft(
            actor=actor,
            customer_id=customer.id,
            invoice_date=when,
            lines=[
                InvoiceLineInput(item.id, item.name, D(qty), item.unit_price) for item, qty in lines
            ],
        )
        issued = invoices.issue(draft.id, actor=actor)
        if void_on is not None:
            invoices.void(issued.id, actor=actor, void_date=void_on, memo="Entered in error")
        return issued

    def pay(customer, when: date, amount: str, account: int, method: str, *applied):
        return payments.record(
            actor=actor,
            customer_id=customer.id,
            received_date=when,
            amount=D(amount),
            deposit_account_id=account,
            method=method,
            applications=[ApplicationInput(inv.id, D(value)) for inv, value in applied],
        )

    inv1 = invoice(acme, date(DEMO_YEAR, 1, 5), (consulting, "20"))  # 3,000.00
    pay1 = pay(acme, date(DEMO_YEAR, 2, 3), "3000.00", undeposited, "check", (inv1, "3000.00"))
    DepositService(db).create(
        actor=actor,
        deposit_date=date(DEMO_YEAR, 2, 5),
        bank_account_id=bank,
        payment_ids=[pay1.id],
        memo="February deposit",
    )
    invoice(cedar, date(DEMO_YEAR, 3, 15), (consulting, "10"), (workshop, "1"))  # overdue
    invoice(delta, date(DEMO_YEAR, 4, 2), (consulting, "8.5"), void_on=date(DEMO_YEAR, 4, 20))
    inv4 = invoice(acme, date(DEMO_YEAR, 6, 1), (consulting, "5"))  # 750.00
    pay(acme, date(DEMO_YEAR, 6, 20), "750.00", bank, "ach", (inv4, "750.00"))
    inv_delta = invoice(delta, date(DEMO_YEAR, 7, 1), (retainer, "1"))  # 2,500.00
    # An overpayment: 500.00 stays as customer credit, of which 200.00 is refunded.
    pay(delta, date(DEMO_YEAR, 7, 20), "3000.00", bank, "ach", (inv_delta, "2500.00"))
    RefundService(db).record(
        actor=actor,
        customer_id=delta.id,
        refund_date=date(DEMO_YEAR, 8, 5),
        amount=D("200.00"),
        bank_account_id=bank,
        method="check",
        memo="Refund of part of the overpayment",
    )
    invoice(acme, date(DEMO_YEAR, 12, 1), (retainer, "1"))  # open, not yet due
    inv6 = invoice(birch, date(DEMO_YEAR, 12, 10), (retainer, "1"), (workshop, "1"))  # 3,700.00
    pay(birch, date(DEMO_YEAR, 12, 15), "1500.00", undeposited, "check", (inv6, "1500.00"))
    return ReceivablesSummary(customers=4, items=3, invoices=7, payments=4, deposits=1, refunds=1)


@dataclass(frozen=True)
class ExpensesSummary:
    vendors: int
    expenses: int
    reimbursements: int
    transfers: int
    receipts: int
    receipt_notice: str | None = None


# A minimal valid one-page PDF; the demo receipt is this placeholder.
DEMO_RECEIPT_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 100]>>endobj\n"
    b"trailer<</Root 1 0 R/Size 4>>\n%%EOF\n"
)


def _post_demo_expenses(db: Session, actor: User) -> ExpensesSummary:
    """
    Expenses for the demo year, built through the real services (each commits itself).

    Idempotent per name: vendors and the credit card account are looked up by name before
    being created. At 2026-12-31 the owner is owed 67.15 (one of the two owner-paid expenses
    is reimbursed) and the card balance is 112.60 after a partial payment.
    """
    accounts = AccountService(db)
    ids = {n.full_name: n.account.id for n in accounts.tree(include_inactive=True)}
    if "Business Visa" not in ids:
        ids["Business Visa"] = accounts.create(
            actor=actor, name="Business Visa", type=AccountType.LIABILITY
        ).id
    vendor_service = VendorService(db)
    vendors: dict[str, Vendor] = {v.name: v for v in vendor_service.list(include_inactive=True)}
    for name, note in (
        ("Office Supply Co", "Paper, toner and small equipment"),
        ("City Utilities", "Electricity and water"),
    ):
        if name not in vendors:
            vendors[name] = vendor_service.create(actor=actor, name=name, note=note)
    expenses = ExpenseService(db)

    def line(account: str, amount: str, memo: str | None = None) -> ExpenseLineInput:
        return ExpenseLineInput(ids[account], D(amount), memo)

    split = expenses.create(
        actor=actor,
        expense_date=date(DEMO_YEAR, 10, 3),
        vendor_id=vendors["Office Supply Co"].id,
        source="account",
        paid_from_account_id=ids[CHECKING],
        lines=[
            line("Office Expenses", "86.40", "Paper and toner"),
            line("Software and Subscriptions", "49.00", "Design software seat"),
            line("Other Expenses", "12.35", "Delivery"),
        ],
        memo="Office order",
    )
    utilities = expenses.create(
        actor=actor,
        expense_date=date(DEMO_YEAR, 11, 3),
        vendor_id=vendors["City Utilities"].id,
        source="owner",
        paid_from_account_id=None,
        lines=[line("Other Expenses", "184.22", "Home office utilities")],
        memo="Paid personally by the owner",
    )
    expenses.create(
        actor=actor,
        expense_date=date(DEMO_YEAR, 11, 10),
        vendor_id=vendors["Office Supply Co"].id,
        source="owner",
        paid_from_account_id=None,
        lines=[line("Office Expenses", "67.15", "Printer cartridges")],
        memo="Paid personally by the owner",
    )
    ReimbursementService(db).create(
        actor=actor,
        reimbursement_date=date(DEMO_YEAR, 11, 20),
        paid_from_account_id=ids[CHECKING],
        expense_ids=[utilities.id],
        memo="Reimbursed from checking",
    )
    expenses.create(
        actor=actor,
        expense_date=date(DEMO_YEAR, 11, 12),
        payee_text="Regional Airline",
        source="account",
        paid_from_account_id=ids["Business Visa"],
        lines=[line("Travel", "412.60", "Flight to client site")],
        memo="Charged to the company card",
    )
    TransferService(db).create(
        actor=actor,
        transfer_date=date(DEMO_YEAR, 12, 5),
        from_account_id=ids[CHECKING],
        to_account_id=ids["Business Visa"],
        amount=D("300.00"),
        memo="Card payment",
    )

    receipts, notice = 0, None
    try:
        AttachmentService(db).add(
            actor=actor,
            owner_type="expense",
            owner_id=split.id,
            filename="office-order-receipt.pdf",
            data=DEMO_RECEIPT_PDF,
        )
        receipts = 1
    except OSError as exc:
        db.rollback()
        notice = f"Receipt skipped: cannot write to {app_settings.attachment_dir} ({exc.strerror or exc})."
    return ExpensesSummary(
        vendors=2,
        expenses=4,
        reimbursements=1,
        transfers=1,
        receipts=receipts,
        receipt_notice=notice,
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
    db.commit()  # the receivables services commit as they go; keep the year already posted
    if db.exec(select(func.count()).select_from(Customer)).one():
        receivables = "Receivables not added: customers already exist."
    else:
        try:
            rec = _post_demo_receivables(db, actor)
        except AppBaseError as exc:
            raise CliError(f"Posted the demo year but not the receivables: {exc.message}") from exc
        receivables = (
            f"Receivables: {rec.customers} customers, {rec.items} items, {rec.invoices} invoices, "
            f"{rec.payments} payments, {rec.deposits} deposit, {rec.refunds} refund."
        )
    if db.exec(select(func.count()).select_from(Vendor)).one():
        expenses = "Expenses not added: vendors already exist."
    else:
        try:
            exp = _post_demo_expenses(db, actor)
        except AppBaseError as exc:
            raise CliError(f"Posted the demo year but not the expenses: {exc.message}") from exc
        expenses = (
            f"Expenses: {exp.vendors} vendors, {exp.expenses} expenses, "
            f"{exp.reimbursements} reimbursement, {exp.transfers} card payment, "
            f"{exp.receipts} receipt."
        )
        if exp.receipt_notice:
            expenses += f" {exp.receipt_notice}"
    return (
        f"Posted {summary.entries} demo journal entries for {DEMO_YEAR} "
        f"({summary.reversals} reversals, {summary.voided} voided); "
        f"created {summary.accounts_created} account(s). "
        f"Trial balance at {DEMO_YEAR}-12-31: debits {summary.total_debits}, "
        f"{'balanced' if summary.balanced else 'NOT balanced'}. "
        f"{receivables} {expenses}"
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
