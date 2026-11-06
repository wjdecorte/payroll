"""Transfers: money moved between two company accounts (bank to bank, a card payment).

A transfer posts one journal entry: Dr the destination account, Cr the source account.
Voiding reverses the entry. Mutations are owner-only, audited, and commit through a
wrapper that rolls back on any error.

Locking order (spec section 6): both accounts FOR SHARE in id order, then whatever
posting locks (period lock FOR SHARE, entry counter). ``void`` locks the transfer row
FOR UPDATE first, so two racing voids serialize and the second sees it already void.
"""

import logging
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
    PAYMENT_TYPES,
    RESERVED_ROLES,
    check_void_date,
    checked_amount,
    clean_text,
    commit_or_rollback,
    is_int,
    utcnow_naive,
    void_memo,
)
from payroll.expenses.exceptions import InvalidTransferError, TransferError, TransferNotFoundError
from payroll.expenses.models import Transfer
from payroll.ledger.models import Account, JournalEntry, SourceType
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft

logger = logging.getLogger(f"{app_settings.logger_name}.expenses.transfers")

MAX_PAGE = 200


def _invalid(message: str) -> InvalidTransferError:
    return InvalidTransferError(message=message)


class TransferService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ reads

    def get(self, transfer_id: int) -> Transfer:
        transfer = self.session.get(Transfer, transfer_id) if is_int(transfer_id) else None
        if transfer is None:
            raise TransferNotFoundError()
        return transfer

    def list(self, limit: int = 100, offset: int = 0) -> list[Transfer]:
        if not is_int(limit) or not 1 <= limit <= MAX_PAGE:
            raise _invalid(f"The page size must be between 1 and {MAX_PAGE}")
        if not is_int(offset) or offset < 0:
            raise _invalid("The offset cannot be negative")
        stmt = (
            select(Transfer)
            .order_by(col(Transfer.date).desc(), col(Transfer.id).desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.exec(stmt).all())

    # -------------------------------------------------------------- mutations

    def create(
        self,
        *,
        actor: User,
        transfer_date: date,
        from_account_id: int,
        to_account_id: int,
        amount: Decimal,
        memo: str | None = None,
    ) -> Transfer:
        ensure_role(actor, OWNER)

        def work() -> Transfer:
            # (1) validate the inputs that need no database access
            if not isinstance(transfer_date, date) or not is_supported_date(transfer_date):
                raise _invalid(f"The transfer date must be within {supported_range_text()}")
            if not is_int(from_account_id) or not is_int(to_account_id):
                raise _invalid("Choose the accounts to move money from and to")
            if from_account_id == to_account_id:
                raise _invalid("Choose two different accounts")
            clean_amount = checked_amount(amount, "The amount", InvalidTransferError)
            clean_memo = clean_text(memo, "Memo", MAX_MEMO, InvalidTransferError)

            # (2) lock both accounts FOR SHARE in id order, then check them
            accounts = self._lock_accounts({from_account_id, to_account_id})
            source = self._check_account(accounts.get(from_account_id), "from")
            target = self._check_account(accounts.get(to_account_id), "to")

            # (3) write the document, then post
            transfer = Transfer(
                date=transfer_date,
                from_account_id=source.id,  # type: ignore[arg-type]
                to_account_id=target.id,  # type: ignore[arg-type]
                amount=clean_amount,
                memo=clean_memo,
            )
            self.session.add(transfer)
            self.session.flush()
            entry = self._post_entry(transfer, actor)
            transfer.entry_id = entry.id
            self.session.add(transfer)
            self.session.flush()
            record_audit(
                self.session,
                action="transfer.create",
                user_id=actor.id,
                entity="transfer",
                entity_id=str(transfer.id),
                after=self._snapshot(transfer) | {"entry_id": entry.id},
            )
            return transfer

        transfer = commit_or_rollback(self.session, work)
        logger.info("Created transfer id=%s", transfer.id)
        return transfer

    def void(
        self, transfer_id: int, *, actor: User, void_date: date, memo: str | None = None
    ) -> Transfer:
        ensure_role(actor, OWNER)

        def work() -> Transfer:
            if not is_int(transfer_id):
                raise _invalid("Choose a transfer to void")
            clean_memo = clean_text(memo, "Memo", MAX_MEMO, InvalidTransferError)
            transfer = self.session.exec(
                select(Transfer)
                .where(Transfer.id == transfer_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).first()
            if transfer is None:
                raise TransferNotFoundError()
            if transfer.status != "posted":
                raise TransferError(message="The transfer is already void")
            check_void_date(void_date, transfer.date, "transfer", InvalidTransferError)
            before = self._snapshot(transfer)
            reversal = LedgerPostingService(self.session).void(
                transfer.entry_id,  # type: ignore[arg-type]
                entry_date=void_date,
                actor=actor,
                memo=void_memo(clean_memo, f"Void of transfer {transfer.date.isoformat()}"),
            )
            transfer.status = "void"
            transfer.void_date = void_date
            transfer.void_entry_id = reversal.id
            transfer.modify_date = utcnow_naive()
            self.session.add(transfer)
            self.session.flush()
            record_audit(
                self.session,
                action="transfer.void",
                user_id=actor.id,
                entity="transfer",
                entity_id=str(transfer.id),
                before=before,
                after=self._snapshot(transfer)
                | {"void_date": void_date.isoformat(), "reversal_entry_id": reversal.id},
            )
            return transfer

        return commit_or_rollback(self.session, work)

    # ---------------------------------------------------------------- helpers

    def _lock_accounts(self, ids: set[int]) -> dict[int, Account]:
        rows = self.session.exec(
            select(Account)
            .where(col(Account.id).in_(sorted(ids)))
            .order_by(col(Account.id))
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        ).all()
        return {row.id: row for row in rows}  # type: ignore[misc]

    @staticmethod
    def _check_account(account: Account | None, end: str) -> Account:
        if account is None:
            raise _invalid(f"The account to transfer {end} was not found")
        if account.type not in PAYMENT_TYPES or account.system_role in RESERVED_ROLES:
            raise _invalid(
                f"{account.name} cannot take part in a transfer; use a bank, cash or card account"
            )
        if not account.is_active:
            raise _invalid(f"{account.name} is inactive")
        return account

    def _post_entry(self, transfer: Transfer, actor: User) -> JournalEntry:
        """Dr the destination account, Cr the source account."""
        return LedgerPostingService(self.session).post(
            EntryDraft(
                transfer.date,
                [
                    LineDraft(transfer.to_account_id, debit=transfer.amount),
                    LineDraft(transfer.from_account_id, credit=transfer.amount),
                ],
                memo=(transfer.memo or "Transfer")[:MAX_MEMO],
                source_type=SourceType.TRANSFER,
                source_id=str(transfer.id),
            ),
            actor=actor,
        )

    @staticmethod
    def _snapshot(transfer: Transfer) -> dict[str, Any]:
        return {
            "date": transfer.date.isoformat(),
            "from_account_id": transfer.from_account_id,
            "to_account_id": transfer.to_account_id,
            "amount": str(transfer.amount),
            "memo": transfer.memo,
            "status": transfer.status,
        }
