"""Expenses: one bill or receipt split over expense lines, paid from an account or by the owner.

An expense posts one journal entry: a debit per line to its expense account and a credit
for the total to either the payment account (``source = 'account'``) or the Due to Owner
role account (``source = 'owner'``, a debt the company owes the owner until reimbursed).
Voiding reverses the entry. Mutations are owner-only, audited, and commit through a
wrapper that rolls back on any error.

Locking order (spec section 6): the Due to Owner role account (FOR SHARE, owner source
only), the vendor row (FOR SHARE), the payment and line accounts (FOR SHARE, by id), then
whatever posting locks (period lock FOR SHARE, entry counter). ``void`` locks the expense
row FOR UPDATE first, so a racing reimbursement (which also locks it) serializes with it.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import exists
from sqlmodel import Session, col, func, select

from payroll import app_settings
from payroll.audit.services import record as record_audit
from payroll.auth.models import User
from payroll.auth.permissions import OWNER, ensure_role
from payroll.common.dates import is_supported_date, supported_range_text
from payroll.common.documents import (
    MAX_DOCUMENT_AMOUNT,
    MAX_MEMO,
    PAYMENT_TYPES,
    RESERVED_ROLES,
    check_void_date,
    checked_amount,
    clean_text,
    commit_or_rollback,
    is_int,
    locked_role_account,
    utcnow_naive,
    void_memo,
)
from payroll.expenses.exceptions import ExpenseError, ExpenseNotFoundError, InvalidExpenseError
from payroll.expenses.models import Expense, ExpenseLine, Vendor
from payroll.ledger.models import Account, AccountType, JournalEntry, SourceType, SystemRole
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft

logger = logging.getLogger(f"{app_settings.logger_name}.expenses.expenses")

MAX_LINES = 100
MAX_PAYEE = 200
MAX_AMOUNT = MAX_DOCUMENT_AMOUNT
LINE_TYPES = frozenset({AccountType.EXPENSE, AccountType.COGS, AccountType.OTHER_EXPENSE})
STATUSES = frozenset({"posted", "void"})
MAX_PAGE = 200


@dataclass(frozen=True)
class ExpenseLineInput:
    account_id: int
    amount: Decimal
    memo: str | None = None


def _invalid(message: str) -> InvalidExpenseError:
    return InvalidExpenseError(message=message)


def _clean_text(value: str | None, label: str, limit: int) -> str | None:
    return clean_text(value, label, limit, InvalidExpenseError)


class ExpenseService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ reads

    def get(self, expense_id: int) -> Expense:
        expense = self.session.get(Expense, expense_id)
        if expense is None:
            raise ExpenseNotFoundError()
        return expense

    def lines(self, expense_id: int) -> list[ExpenseLine]:
        stmt = (
            select(ExpenseLine)
            .where(ExpenseLine.expense_id == expense_id)
            .order_by(col(ExpenseLine.line_no))
        )
        return list(self.session.exec(stmt).all())

    def _filtered(
        self,
        stmt: Any,
        *,
        date_from: date | None,
        date_to: date | None,
        vendor_id: int | None,
        account_id: int | None,
        status: str | None,
        open_owner_paid: bool,
    ) -> Any:
        if status is not None and status not in STATUSES:
            raise _invalid("Status must be posted or void")
        if date_from is not None:
            stmt = stmt.where(col(Expense.date) >= date_from)
        if date_to is not None:
            stmt = stmt.where(col(Expense.date) <= date_to)
        if vendor_id is not None:
            stmt = stmt.where(Expense.vendor_id == vendor_id)
        if account_id is not None:
            # a line on the account, or the account the expense was paid from
            on_line = exists().where(
                col(ExpenseLine.expense_id) == col(Expense.id),
                col(ExpenseLine.account_id) == account_id,
            )
            stmt = stmt.where(on_line | (col(Expense.paid_from_account_id) == account_id))
        if status is not None:
            stmt = stmt.where(Expense.status == status)
        if open_owner_paid:
            stmt = stmt.where(
                Expense.source == "owner",
                Expense.status == "posted",
                col(Expense.reimbursement_id).is_(None),
            )
        return stmt

    def list(
        self,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
        vendor_id: int | None = None,
        account_id: int | None = None,
        status: str | None = None,
        open_owner_paid: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Expense]:
        if not is_int(limit) or not 1 <= limit <= MAX_PAGE:
            raise _invalid(f"The page size must be between 1 and {MAX_PAGE}")
        if not is_int(offset) or offset < 0:
            raise _invalid("The offset cannot be negative")
        stmt = self._filtered(
            select(Expense),
            date_from=date_from,
            date_to=date_to,
            vendor_id=vendor_id,
            account_id=account_id,
            status=status,
            open_owner_paid=open_owner_paid,
        )
        stmt = (
            stmt.order_by(col(Expense.date).desc(), col(Expense.id).desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.exec(stmt).all())

    def count(
        self,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
        vendor_id: int | None = None,
        account_id: int | None = None,
        status: str | None = None,
        open_owner_paid: bool = False,
    ) -> int:
        stmt = self._filtered(
            select(func.count()).select_from(Expense),
            date_from=date_from,
            date_to=date_to,
            vendor_id=vendor_id,
            account_id=account_id,
            status=status,
            open_owner_paid=open_owner_paid,
        )
        return self.session.exec(stmt).one()

    # -------------------------------------------------------------- mutations

    def create(
        self,
        *,
        actor: User,
        expense_date: date,
        lines: Sequence[ExpenseLineInput],
        source: Literal["account", "owner"],
        paid_from_account_id: int | None,
        vendor_id: int | None = None,
        payee_text: str | None = None,
        memo: str | None = None,
    ) -> Expense:
        ensure_role(actor, OWNER)

        def work() -> Expense:
            # (1) validate the inputs that need no database access
            if not isinstance(expense_date, date) or not is_supported_date(expense_date):
                raise _invalid(f"The expense date must be within {supported_range_text()}")
            if source not in ("account", "owner"):
                raise _invalid(
                    "Choose whether the expense was paid from an account or by the owner"
                )
            clean_memo = _clean_text(memo, "Memo", MAX_MEMO)
            payee = _clean_text(payee_text, "Payee", MAX_PAYEE)
            if (vendor_id is None) == (payee is None):
                raise _invalid("Give either a vendor or a payee, not both and not neither")
            inputs = self._check_lines(lines)
            total = sum((line.amount for line in inputs), Decimal("0.00"))
            if total > MAX_AMOUNT:
                raise _invalid("The expense total cannot exceed 100,000,000.00")
            if source == "owner" and paid_from_account_id is not None:
                raise _invalid("An owner-paid expense has no payment account")
            if source == "account" and not is_int(paid_from_account_id):
                raise _invalid("Choose the account the expense was paid from")

            # (2) locks: role account, vendor, then accounts by id
            due_to_owner: Account | None = None
            if source == "owner":
                due_to_owner = locked_role_account(
                    self.session,
                    SystemRole.DUE_TO_OWNER,
                    lambda: _invalid("Bind Due to Owner in Settings > Ledger first"),
                )
            if vendor_id is not None:
                self._check_vendor(vendor_id)
            account_ids = {line.account_id for line in inputs}
            if source == "account":
                account_ids.add(paid_from_account_id)  # type: ignore[arg-type]
            accounts = self._lock_accounts(account_ids)
            for line in inputs:
                self._check_line_account(accounts.get(line.account_id))
            payment: Account | None = None
            if source == "account":
                payment = accounts.get(paid_from_account_id)  # type: ignore[arg-type]
                self._check_payment_account(payment)

            # (3) write the document, then post
            expense = Expense(
                date=expense_date,
                vendor_id=vendor_id,
                payee_text=payee,
                memo=clean_memo,
                source=source,
                paid_from_account_id=payment.id if payment else None,
                total=total,
            )
            self.session.add(expense)
            self.session.flush()
            for number, line in enumerate(inputs, start=1):
                self.session.add(
                    ExpenseLine(
                        expense_id=expense.id,  # type: ignore[arg-type]
                        line_no=number,
                        account_id=line.account_id,
                        memo=line.memo,
                        amount=line.amount,
                    )
                )
            self.session.flush()
            credit_account = payment or due_to_owner
            entry = self._post_entry(expense, inputs, credit_account, actor)  # type: ignore[arg-type]
            expense.entry_id = entry.id
            self.session.add(expense)
            self.session.flush()
            record_audit(
                self.session,
                action="expense.create",
                user_id=actor.id,
                entity="expense",
                entity_id=str(expense.id),
                after=self._snapshot(expense)
                | {
                    "entry_id": entry.id,
                    "lines": [
                        {"account_id": ln.account_id, "amount": str(ln.amount)} for ln in inputs
                    ],
                },
            )
            return expense

        expense = commit_or_rollback(self.session, work)
        logger.info("Created expense id=%s", expense.id)
        return expense

    def void(
        self, expense_id: int, *, actor: User, void_date: date, memo: str | None = None
    ) -> Expense:
        ensure_role(actor, OWNER)

        def work() -> Expense:
            expense = self.session.exec(
                select(Expense)
                .where(Expense.id == expense_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).first()
            if expense is None:
                raise ExpenseNotFoundError()
            if expense.status != "posted":
                raise ExpenseError(message="The expense is already void")
            if expense.reimbursement_id is not None:
                raise ExpenseError(
                    message="The expense has been reimbursed; void the reimbursement first"
                )
            check_void_date(void_date, expense.date, "expense", InvalidExpenseError)
            before = self._snapshot(expense)
            reversal = LedgerPostingService(self.session).void(
                expense.entry_id,  # type: ignore[arg-type]
                entry_date=void_date,
                actor=actor,
                memo=void_memo(memo, f"Void of expense {expense.date.isoformat()}"),
            )
            expense.status = "void"
            expense.void_date = void_date
            expense.void_entry_id = reversal.id
            expense.modify_date = utcnow_naive()
            self.session.add(expense)
            self.session.flush()
            record_audit(
                self.session,
                action="expense.void",
                user_id=actor.id,
                entity="expense",
                entity_id=str(expense.id),
                before=before,
                after=self._snapshot(expense)
                | {"void_date": void_date.isoformat(), "reversal_entry_id": reversal.id},
            )
            return expense

        return commit_or_rollback(self.session, work)

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _check_lines(lines: Sequence[ExpenseLineInput]) -> list[ExpenseLineInput]:
        items = list(lines)
        if not items:
            raise _invalid("Add at least one expense line")
        if len(items) > MAX_LINES:
            raise _invalid(f"An expense can have at most {MAX_LINES} lines")
        for line in items:
            if not isinstance(line, ExpenseLineInput) or not is_int(line.account_id):
                raise _invalid("Choose an account for every line")
            checked_amount(line.amount, "A line amount", InvalidExpenseError)
        return [
            ExpenseLineInput(
                line.account_id,
                checked_amount(line.amount, "A line amount", InvalidExpenseError),
                _clean_text(line.memo, "Line memo", MAX_MEMO),
            )
            for line in items
        ]

    def _check_vendor(self, vendor_id: int) -> None:
        vendor = (
            self.session.exec(
                select(Vendor)
                .where(Vendor.id == vendor_id)
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            ).first()
            if is_int(vendor_id)
            else None
        )
        if vendor is None:
            raise _invalid("The vendor was not found")
        if not vendor.active:
            raise _invalid("The vendor is inactive")

    def _lock_accounts(self, ids: set[int]) -> dict[int, Account]:
        if any(not is_int(i) for i in ids):
            raise _invalid("Choose valid accounts")
        rows = self.session.exec(
            select(Account)
            .where(col(Account.id).in_(sorted(ids)))
            .order_by(col(Account.id))
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        ).all()
        return {row.id: row for row in rows}  # type: ignore[misc]

    @staticmethod
    def _check_line_account(account: Account | None) -> None:
        if account is None:
            raise _invalid("An expense line account was not found")
        if account.type not in LINE_TYPES:
            raise _invalid(
                f"{account.name} is not an expense account; expense lines take expense, "
                "cost of goods sold or other expense accounts"
            )
        if not account.is_active:
            raise _invalid(f"{account.name} is inactive")

    @staticmethod
    def _check_payment_account(account: Account | None) -> None:
        if account is None:
            raise _invalid("The account the expense was paid from was not found")
        if account.type not in PAYMENT_TYPES or account.system_role in RESERVED_ROLES:
            raise _invalid(
                f"{account.name} cannot pay an expense; use a bank, cash or card account"
            )
        if not account.is_active:
            raise _invalid(f"{account.name} is inactive")

    def _post_entry(
        self,
        expense: Expense,
        lines: Sequence[ExpenseLineInput],
        credit_account: Account,
        actor: User,
    ) -> JournalEntry:
        """Dr each line's account, Cr the payment account or Due to Owner for the total."""
        who = f" to {expense.payee_text}" if expense.payee_text else ""
        memo = (f"Expense {expense.memo}" if expense.memo else f"Expense{who}")[:MAX_MEMO]
        drafts = [LineDraft(ln.account_id, debit=ln.amount, memo=ln.memo) for ln in lines]
        drafts.append(LineDraft(credit_account.id, credit=expense.total))  # type: ignore[arg-type]
        return LedgerPostingService(self.session).post(
            EntryDraft(
                expense.date,
                drafts,
                memo=memo,
                source_type=SourceType.EXPENSE,
                source_id=str(expense.id),
            ),
            actor=actor,
        )

    @staticmethod
    def _snapshot(expense: Expense) -> dict[str, Any]:
        return {
            "date": expense.date.isoformat(),
            "source": expense.source,
            "paid_from_account_id": expense.paid_from_account_id,
            "vendor_id": expense.vendor_id,
            "payee_text": expense.payee_text,
            "total": str(expense.total),
            "status": expense.status,
            "reimbursement_id": expense.reimbursement_id,
        }
