"""Reimbursements: paying the owner back for expenses they paid personally.

A reimbursement settles a batch of open owner-paid expenses with one journal entry
(debit Due to Owner, credit the bank account, both for the total) and ``reimbursement_id``
set on each expense. Voiding reverses the entry and frees the expenses, which become
open again. Mutations are owner-only, audited, and commit through a wrapper that rolls
back on any error.

Locking order (spec sections 5 and 6): the Due to Owner role account (FOR SHARE), the
bank account (FOR SHARE), the expenses (FOR UPDATE, by id), then whatever posting locks.
``ExpenseService.void`` locks the expense row first, so locking the expenses here before
checking and setting ``reimbursement_id`` is what keeps a voided expense out of a live
reimbursement and keeps two reimbursements from taking the same expense. ``void`` locks
the reimbursement, then its expenses.
"""

import logging
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sqlmodel import Session, col, select

from payroll import app_settings
from payroll.audit.services import record as record_audit
from payroll.auth.models import User
from payroll.auth.permissions import OWNER, ensure_role
from payroll.common.dates import is_supported_date, supported_range_text
from payroll.common.documents import (
    MAX_MEMO,
    RESERVED_ROLES,
    check_void_date,
    clean_text,
    commit_or_rollback,
    is_int,
    lock_rows,
    locked_role_account,
    utcnow_naive,
    void_memo,
)
from payroll.expenses.exceptions import (
    InvalidReimbursementError,
    ReimbursementError,
    ReimbursementNotFoundError,
)
from payroll.expenses.models import Expense, Reimbursement
from payroll.ledger.models import Account, AccountType, JournalEntry, SourceType, SystemRole
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft

logger = logging.getLogger(f"{app_settings.logger_name}.expenses.reimbursements")

MAX_EXPENSES = 200
MAX_PAGE = 200
ZERO = Decimal("0.00")


def _invalid(message: str) -> InvalidReimbursementError:
    return InvalidReimbursementError(message=message)


class ReimbursementService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ reads

    def get(self, reimbursement_id: int) -> Reimbursement:
        reimbursement = self.session.get(Reimbursement, reimbursement_id)
        if reimbursement is None:
            raise ReimbursementNotFoundError()
        return reimbursement

    def expenses(self, reimbursement_id: int) -> list[Expense]:
        """The expenses currently linked to the reimbursement (none once it is void)."""
        self.get(reimbursement_id)
        stmt = (
            select(Expense)
            .where(Expense.reimbursement_id == reimbursement_id)
            .order_by(col(Expense.date), col(Expense.id))
        )
        return list(self.session.exec(stmt).all())

    def list(self, limit: int = 100, offset: int = 0) -> list[Reimbursement]:
        if not is_int(limit) or not 1 <= limit <= MAX_PAGE:
            raise _invalid(f"The page size must be between 1 and {MAX_PAGE}")
        if not is_int(offset) or offset < 0:
            raise _invalid("The offset cannot be negative")
        stmt = (
            select(Reimbursement)
            .order_by(col(Reimbursement.date).desc(), col(Reimbursement.id).desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.exec(stmt).all())

    # -------------------------------------------------------------- mutations

    def create(
        self,
        *,
        actor: User,
        reimbursement_date: date,
        paid_from_account_id: int,
        expense_ids: Sequence[int],
        memo: str | None = None,
    ) -> Reimbursement:
        ensure_role(actor, OWNER)

        def work() -> Reimbursement:
            # (1) validate the inputs that need no database access
            if not isinstance(reimbursement_date, date) or not is_supported_date(
                reimbursement_date
            ):
                raise _invalid(f"The reimbursement date must be within {supported_range_text()}")
            clean_memo = clean_text(memo, "Memo", MAX_MEMO, InvalidReimbursementError)
            ids = self._check_ids(expense_ids)
            if not is_int(paid_from_account_id):
                raise _invalid("Choose the bank account the reimbursement is paid from")

            # (2) locks: role account, bank account, then the expenses by id
            due_to_owner = locked_role_account(
                self.session,
                SystemRole.DUE_TO_OWNER,
                lambda: _invalid("Bind Due to Owner in Settings > Ledger first"),
            )
            bank = self._lock_bank(paid_from_account_id)
            expenses = lock_rows(
                self.session, Expense, ids, lambda: _invalid("A selected expense was not found")
            )
            total = ZERO
            for expense in expenses:
                label = f"The expense from {expense.date.isoformat()} for {expense.total}"
                if expense.source != "owner":
                    raise ReimbursementError(message=f"{label} was not paid by the owner")
                if expense.status != "posted":
                    raise ReimbursementError(message=f"{label} is void")
                if expense.reimbursement_id is not None:
                    raise ReimbursementError(message=f"{label} is already reimbursed")
                if expense.date > reimbursement_date:
                    raise _invalid(
                        f"The reimbursement date cannot be before the date of {label[4:]}"
                    )
                total += expense.total

            # (3) write the document, link the expenses, then post
            reimbursement = Reimbursement(
                date=reimbursement_date,
                paid_from_account_id=bank.id,  # type: ignore[arg-type]
                memo=clean_memo,
                total=total,
            )
            self.session.add(reimbursement)
            self.session.flush()
            entry = self._post_entry(reimbursement, bank, due_to_owner, actor)
            reimbursement.entry_id = entry.id
            self.session.add(reimbursement)
            for expense in expenses:
                expense.reimbursement_id = reimbursement.id
                expense.modify_date = utcnow_naive()
                self.session.add(expense)
            self.session.flush()
            record_audit(
                self.session,
                action="reimbursement.create",
                user_id=actor.id,
                entity="reimbursement",
                entity_id=str(reimbursement.id),
                after=self._snapshot(reimbursement)
                | {"entry_id": entry.id, "expense_ids": [e.id for e in expenses]},
            )
            return reimbursement

        reimbursement = commit_or_rollback(self.session, work)
        logger.info("Created reimbursement id=%s", reimbursement.id)
        return reimbursement

    def void(
        self,
        reimbursement_id: int,
        *,
        actor: User,
        void_date: date,
        memo: str | None = None,
    ) -> Reimbursement:
        ensure_role(actor, OWNER)

        def work() -> Reimbursement:
            clean_memo = clean_text(memo, "Memo", MAX_MEMO, InvalidReimbursementError)
            reimbursement = self.session.exec(
                select(Reimbursement)
                .where(Reimbursement.id == reimbursement_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).first()
            if reimbursement is None:
                raise ReimbursementNotFoundError()
            if reimbursement.status != "posted":
                raise ReimbursementError(message="The reimbursement is already void")
            check_void_date(
                void_date, reimbursement.date, "reimbursement", InvalidReimbursementError
            )
            expenses = list(
                self.session.exec(
                    select(Expense)
                    .where(Expense.reimbursement_id == reimbursement.id)
                    .order_by(col(Expense.id))
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).all()
            )
            before = self._snapshot(reimbursement)
            reversal = LedgerPostingService(self.session).void(
                reimbursement.entry_id,  # type: ignore[arg-type]
                entry_date=void_date,
                actor=actor,
                memo=void_memo(
                    clean_memo, f"Void of reimbursement {reimbursement.date.isoformat()}"
                ),
            )
            reimbursement.status = "void"
            reimbursement.void_date = void_date
            reimbursement.void_entry_id = reversal.id
            reimbursement.modify_date = utcnow_naive()
            self.session.add(reimbursement)
            # The database lets a link be cleared only once its reimbursement is void, so
            # the void must be flushed before the expenses are freed.
            self.session.flush()
            for expense in expenses:
                expense.reimbursement_id = None
                expense.modify_date = utcnow_naive()
                self.session.add(expense)
            self.session.flush()
            record_audit(
                self.session,
                action="reimbursement.void",
                user_id=actor.id,
                entity="reimbursement",
                entity_id=str(reimbursement.id),
                before=before,
                after=self._snapshot(reimbursement)
                | {
                    "void_date": void_date.isoformat(),
                    "reversal_entry_id": reversal.id,
                    "freed_expense_ids": [e.id for e in expenses],
                },
            )
            return reimbursement

        return commit_or_rollback(self.session, work)

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _check_ids(expense_ids: Sequence[int]) -> list[int]:
        ids = list(expense_ids)
        if not ids:
            raise _invalid("Choose at least one expense to reimburse")
        if len(ids) > MAX_EXPENSES:
            raise _invalid(f"A reimbursement can cover at most {MAX_EXPENSES} expenses")
        if any(not is_int(i) for i in ids):
            raise _invalid("Choose valid expenses")
        if len(set(ids)) != len(ids):
            raise _invalid("An expense can be listed only once")
        return sorted(ids)

    def _lock_bank(self, account_id: int) -> Account:
        account = self.session.exec(
            select(Account)
            .where(Account.id == account_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        ).first()
        if account is None:
            raise _invalid("The bank account was not found")
        if account.type != AccountType.ASSET or account.system_role in RESERVED_ROLES:
            raise _invalid(f"{account.name} cannot pay a reimbursement; use a bank or cash account")
        if not account.is_active:
            raise _invalid(f"{account.name} is inactive")
        return account

    def _post_entry(
        self, reimbursement: Reimbursement, bank: Account, due_to_owner: Account, actor: User
    ) -> JournalEntry:
        """Dr Due to Owner, Cr the bank account, both for the reimbursement total."""
        memo = (f"Reimbursement {reimbursement.memo}" if reimbursement.memo else "Reimbursement")[
            :MAX_MEMO
        ]
        return LedgerPostingService(self.session).post(
            EntryDraft(
                reimbursement.date,
                [
                    LineDraft(due_to_owner.id, debit=reimbursement.total),  # type: ignore[arg-type]
                    LineDraft(bank.id, credit=reimbursement.total),  # type: ignore[arg-type]
                ],
                memo=memo,
                source_type=SourceType.REIMBURSEMENT,
                source_id=str(reimbursement.id),
            ),
            actor=actor,
        )

    @staticmethod
    def _snapshot(reimbursement: Reimbursement) -> dict[str, Any]:
        return {
            "date": reimbursement.date.isoformat(),
            "paid_from_account_id": reimbursement.paid_from_account_id,
            "total": str(reimbursement.total),
            "memo": reimbursement.memo,
            "status": reimbursement.status,
        }
