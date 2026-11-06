"""The journal entry an expense posts."""

# ruff: noqa: F811  (fixtures re-exported by conftest are used as arguments)

from datetime import date
from decimal import Decimal

import pytest
from sqlmodel import col, select

from payroll.expenses.expenses import ExpenseLineInput, ExpenseService
from payroll.ledger.accounts import AccountService
from payroll.ledger.models import AccountType, JournalEntry, JournalLine
from tests.ledger.conftest import chart  # noqa: F401

D = Decimal
DAY = date(2026, 3, 1)


@pytest.fixture(autouse=True)
def _committed_fixtures(session, owner_user, chart, vendor, expense_account, card, owner_role):
    session.commit()


def _entry_lines(session, entry_id):
    stmt = (
        select(JournalLine)
        .where(JournalLine.entry_id == entry_id)
        .order_by(col(JournalLine.line_no))
    )
    return [(ln.account_id, ln.debit, ln.credit, ln.memo) for ln in session.exec(stmt).all()]


def _three_lines(session, owner_user, expense_account):
    cogs = AccountService(session).create(
        actor=owner_user, name="Parts Test", type=AccountType.COGS
    )
    other = AccountService(session).create(
        actor=owner_user, name="Fees Test", type=AccountType.OTHER_EXPENSE
    )
    session.commit()
    return (
        cogs,
        other,
        [
            ExpenseLineInput(expense_account.id, D("10.00"), "paper"),
            ExpenseLineInput(cogs.id, D("20.50")),
            ExpenseLineInput(other.id, D("0.25"), "fee"),
        ],
    )


def test_three_line_split_from_a_bank_account(session, owner_user, expense_account, chart):
    cogs, other, lines = _three_lines(session, owner_user, expense_account)
    bank = chart["Business Checking"]
    expense = ExpenseService(session).create(
        actor=owner_user,
        expense_date=DAY,
        lines=lines,
        source="account",
        paid_from_account_id=bank.id,
        payee_text="Hardware Co",
        memo="monthly",
    )
    entry = session.get(JournalEntry, expense.entry_id)
    assert (entry.entry_date, entry.source_type, entry.source_id) == (
        DAY,
        "expense",
        str(expense.id),
    )
    assert entry.memo == "Expense monthly"
    assert _entry_lines(session, entry.id) == [
        (expense_account.id, D("10.00"), D("0.00"), "paper"),
        (cogs.id, D("20.50"), D("0.00"), None),
        (other.id, D("0.25"), D("0.00"), "fee"),
        (bank.id, D("0.00"), D("30.75"), None),
    ]
    assert expense.total == D("30.75")


def test_three_line_split_owner_paid_credits_due_to_owner(
    session, owner_user, expense_account, owner_role
):
    cogs, other, lines = _three_lines(session, owner_user, expense_account)
    expense = ExpenseService(session).create(
        actor=owner_user,
        expense_date=DAY,
        lines=lines,
        source="owner",
        paid_from_account_id=None,
        payee_text="Hardware Co",
    )
    rows = _entry_lines(session, expense.entry_id)
    assert rows[-1] == (owner_role.id, D("0.00"), D("30.75"), None)
    assert sum(r[1] for r in rows) == sum(r[2] for r in rows) == D("30.75")
    assert expense.paid_from_account_id is None
    assert AccountService(session).balances()[owner_role.id] == D(
        "-30.75"
    )  # balances are debit minus credit


def test_liability_card_source_increases_the_card_balance(
    session, owner_user, expense_account, card
):
    ExpenseService(session).create(
        actor=owner_user,
        expense_date=DAY,
        source="account",
        paid_from_account_id=card.id,
        lines=[ExpenseLineInput(expense_account.id, D("40.00"))],
        payee_text="Shop",
    )
    assert AccountService(session).balances()[card.id] == D("-40.00")
    assert AccountService(session).balances()[expense_account.id] == D("40.00")


def test_default_memo_names_the_payee(session, owner_user, expense_account, card):
    expense = ExpenseService(session).create(
        actor=owner_user,
        expense_date=DAY,
        source="account",
        paid_from_account_id=card.id,
        lines=[ExpenseLineInput(expense_account.id, D("1.00"))],
        payee_text="Shop",
    )
    assert session.get(JournalEntry, expense.entry_id).memo == "Expense to Shop"
