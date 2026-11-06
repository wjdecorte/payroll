"""The owner-reimbursement report: what the company owes the owner, as of a date.

The owed figure, the open owner-paid expenses and the mismatch flag all come from
:func:`payroll.expenses.owner.owner_summary` (with its as-of handling), so the report and
the ``/owner`` page can never disagree. On top of that it lists the reimbursements dated
in a range, voided ones included and marked.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlmodel import Session, col, func, select

from payroll.audit.models import AuditLog
from payroll.expenses.models import Expense, Reimbursement, Vendor
from payroll.expenses.owner import owner_summary
from payroll.ledger.models import Account


@dataclass(frozen=True)
class OpenExpenseRow:
    expense_id: int
    date: date
    payee: str
    memo: str
    total: Decimal


@dataclass(frozen=True)
class ReimbursementRow:
    id: int
    date: date
    bank_account: str
    total: Decimal
    status: str
    void_date: date | None
    expense_count: int


@dataclass(frozen=True)
class OwnerReport:
    as_of: date
    date_from: date
    date_to: date
    owed_to_me: Decimal
    open_expense_total: Decimal
    matches: bool
    open_expenses: list[OpenExpenseRow]
    reimbursements: list[ReimbursementRow]


def _vendor_names(session: Session, expenses: list[Expense]) -> dict[int, str]:
    ids = {e.vendor_id for e in expenses if e.vendor_id is not None}
    if not ids:
        return {}
    rows = session.exec(select(Vendor.id, Vendor.name).where(col(Vendor.id).in_(ids))).all()
    return {vendor_id: name for vendor_id, name in rows}


def _expense_counts(session: Session, reimbursements: list[Reimbursement]) -> dict[int, int]:
    """Expenses each reimbursement covered: linked ones, or those freed when it was voided."""
    posted = [r.id for r in reimbursements if r.status != "void"]
    counts: dict[int, int] = {}
    if posted:
        rows = session.exec(
            select(Expense.reimbursement_id, func.count())
            .where(col(Expense.reimbursement_id).in_(posted))
            .group_by(col(Expense.reimbursement_id))
        ).all()
        counts = {rid: n for rid, n in rows if rid is not None}
    voided = [str(r.id) for r in reimbursements if r.status == "void"]
    if voided:
        audit = session.exec(
            select(AuditLog.entity_id, AuditLog.after).where(
                AuditLog.action == "reimbursement.void", col(AuditLog.entity_id).in_(voided)
            )
        ).all()
        for entity_id, after in audit:
            counts[int(entity_id)] = len((after or {}).get("freed_expense_ids", []))
    return counts


def owner_reimbursement_report(
    session: Session, as_of: date, date_from: date, date_to: date
) -> OwnerReport:
    summary = owner_summary(session, as_of)
    vendors = _vendor_names(session, summary.open_expenses)
    open_rows = [
        OpenExpenseRow(
            expense_id=e.id,  # type: ignore[arg-type]
            date=e.date,
            payee=vendors.get(e.vendor_id, "") if e.vendor_id is not None else (e.payee_text or ""),
            memo=e.memo or "",
            total=e.total,
        )
        for e in summary.open_expenses
    ]
    reimbursements = list(
        session.exec(
            select(Reimbursement)
            .where(Reimbursement.date >= date_from, Reimbursement.date <= date_to)
            .order_by(col(Reimbursement.date), col(Reimbursement.id))
        ).all()
    )
    banks = {
        account_id: name
        for account_id, name in session.exec(
            select(Account.id, Account.name).where(
                col(Account.id).in_({r.paid_from_account_id for r in reimbursements})
            )
        ).all()
    }
    counts = _expense_counts(session, reimbursements)
    return OwnerReport(
        as_of=as_of,
        date_from=date_from,
        date_to=date_to,
        owed_to_me=summary.owed_to_me,
        open_expense_total=summary.open_expense_total,
        matches=summary.matches,
        open_expenses=open_rows,
        reimbursements=[
            ReimbursementRow(
                id=r.id,  # type: ignore[arg-type]
                date=r.date,
                bank_account=banks.get(r.paid_from_account_id, ""),
                total=r.total,
                status=r.status,
                void_date=r.void_date,
                expense_count=counts.get(r.id, 0),  # type: ignore[arg-type]
            )
            for r in reimbursements
        ],
    )
