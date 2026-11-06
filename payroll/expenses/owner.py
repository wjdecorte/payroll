"""The owner balance: what the company owes the owner, against the open owner-paid expenses.

``owed_to_me`` is the Due to Owner ledger balance (credit minus debit); the open owner-paid
expenses are the documents that balance should be made of. They agree unless something
else (a manual journal) touched the Due to Owner account, so both are reported and
``matches`` says whether they agree.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import exists
from sqlmodel import Session, col, func, or_, select

from payroll.audit.models import AuditLog
from payroll.expenses.models import Expense, Reimbursement
from payroll.ledger.models import Account, JournalEntry, JournalLine, SystemRole

ZERO = Decimal("0.00")


@dataclass
class OwnerSummary:
    owed_to_me: Decimal
    open_expense_total: Decimal
    open_expenses: list[Expense]
    matches: bool


def _owed(session: Session, as_of: date | None) -> Decimal:
    account_id = session.exec(
        select(Account.id).where(Account.system_role == SystemRole.DUE_TO_OWNER)
    ).first()
    if account_id is None:
        return ZERO
    stmt = (
        select(func.coalesce(func.sum(JournalLine.credit - JournalLine.debit), 0))
        .join(JournalEntry, col(JournalEntry.id) == col(JournalLine.entry_id))
        .where(JournalLine.account_id == account_id)
    )
    if as_of is not None:
        stmt = stmt.where(JournalEntry.entry_date <= as_of)
    return Decimal(session.exec(stmt).one()).quantize(ZERO)


def _reimbursed_then_freed(session: Session, as_of: date) -> set[int]:
    """Expenses whose reimbursement was in force on ``as_of`` but was voided afterwards.

    Voiding clears the link, so the freed expense ids are read back from the audit row
    the void wrote.
    """
    voided = session.exec(
        select(Reimbursement.id).where(
            Reimbursement.status == "void",
            Reimbursement.date <= as_of,
            col(Reimbursement.void_date) > as_of,
        )
    ).all()
    if not voided:
        return set()
    rows = session.exec(
        select(AuditLog.after).where(
            AuditLog.action == "reimbursement.void",
            col(AuditLog.entity_id).in_([str(i) for i in voided]),
        )
    ).all()
    return {int(i) for after in rows for i in (after or {}).get("freed_expense_ids", [])}


def open_expenses(session: Session, as_of: date | None = None) -> list[Expense]:
    """Owner-paid expenses that were posted and not yet reimbursed on ``as_of`` (default now)."""
    stmt = select(Expense).where(Expense.source == "owner")
    reimbursed_ids: set[int] = set()
    if as_of is None:
        stmt = stmt.where(Expense.status == "posted", col(Expense.reimbursement_id).is_(None))
    else:
        reimbursed_by_then = exists().where(
            col(Reimbursement.id) == col(Expense.reimbursement_id),
            col(Reimbursement.date) <= as_of,
        )
        stmt = stmt.where(
            Expense.date <= as_of,
            or_(Expense.status == "posted", col(Expense.void_date) > as_of),
            ~reimbursed_by_then,
        )
        reimbursed_ids = _reimbursed_then_freed(session, as_of)
    rows = session.exec(stmt.order_by(col(Expense.date), col(Expense.id))).all()
    return [row for row in rows if row.id not in reimbursed_ids]


def owner_summary(session: Session, as_of: date | None = None) -> OwnerSummary:
    owed = _owed(session, as_of)
    expenses = open_expenses(session, as_of)
    total = sum((e.total for e in expenses), ZERO)
    return OwnerSummary(owed, total, expenses, owed == total)
