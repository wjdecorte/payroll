"""Bank deposits of undeposited customer payments.

A deposit moves a batch of payments that were received into Undeposited Funds into a
bank account: one journal entry (debit the bank, credit Undeposited Funds, both for the
total) and ``deposit_id`` set on each payment. Voiding reverses the entry and frees the
payments. Mutations are owner-only, audited, and commit through a wrapper that rolls
back on any error.

Locking order: the Undeposited Funds role account (FOR SHARE), the payments (FOR UPDATE,
by id), then whatever posting locks. ``PaymentService.void`` reads ``deposit_id`` under
the payment's row lock, so locking the payments here before checking and setting
``deposit_id`` is what keeps a voided payment out of a live deposit and keeps two
deposits from taking the same payment. ``void`` locks the deposit, then its payments.
"""

import logging
from collections.abc import Callable, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sqlmodel import Session, col, select

from payroll import app_settings
from payroll.audit.services import record as record_audit
from payroll.auth.models import User
from payroll.auth.permissions import OWNER, ensure_role
from payroll.common.dates import is_supported_date, supported_range_text
from payroll.ledger.models import Account, AccountType, JournalEntry, SourceType, SystemRole
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft
from payroll.receivables._common import check_void_date, utcnow_naive
from payroll.receivables.exceptions import (
    DepositError,
    DepositNotFoundError,
    InvalidDepositError,
)
from payroll.receivables.models import Deposit, Payment

logger = logging.getLogger(f"{app_settings.logger_name}.receivables.deposits")

MAX_MEMO = 500
MAX_PAYMENTS = 200
MAX_TOTAL = Decimal("999999999999.99")
ZERO = Decimal("0.00")


def _invalid(message: str) -> InvalidDepositError:
    return InvalidDepositError(message=message)


def available(session: Session) -> list[Payment]:
    """Posted payments received into Undeposited Funds and not in a live deposit, oldest first."""
    undeposited_id = session.exec(
        select(Account.id).where(Account.system_role == SystemRole.UNDEPOSITED_FUNDS)
    ).first()
    if undeposited_id is None:
        return []
    stmt = (
        select(Payment)
        .where(
            Payment.deposit_account_id == undeposited_id,
            Payment.status == "posted",
            col(Payment.deposit_id).is_(None),
        )
        .order_by(col(Payment.received_date), col(Payment.id))
    )
    return list(session.exec(stmt).all())


class DepositService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ reads

    def get(self, deposit_id: int) -> Deposit:
        deposit = self.session.get(Deposit, deposit_id)
        if deposit is None:
            raise DepositNotFoundError()
        return deposit

    def list(self, limit: int = 100, offset: int = 0) -> list[Deposit]:
        stmt = (
            select(Deposit)
            .order_by(col(Deposit.deposit_date).desc(), col(Deposit.id).desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.exec(stmt).all())

    def available(self) -> list[Payment]:
        return available(self.session)

    def payments(self, deposit_id: int) -> list[Payment]:
        stmt = (
            select(Payment)
            .where(Payment.deposit_id == deposit_id)
            .order_by(col(Payment.received_date), col(Payment.id))
        )
        return list(self.session.exec(stmt).all())

    # -------------------------------------------------------------- mutations

    def create(
        self,
        *,
        actor: User,
        deposit_date: date,
        bank_account_id: int,
        payment_ids: Sequence[int],
        memo: str | None = None,
    ) -> Deposit:
        ensure_role(actor, OWNER)

        def work() -> Deposit:
            undeposited = self._locked_undeposited()
            if not isinstance(deposit_date, date) or not is_supported_date(deposit_date):
                raise _invalid(f"The deposit date must be within {supported_range_text()}")
            clean_memo = (memo or "").strip()
            if len(clean_memo) > MAX_MEMO:
                raise _invalid(f"Memo must be at most {MAX_MEMO} characters")
            ids = self._check_ids(payment_ids)
            bank = self._bank(bank_account_id, undeposited)
            payments = self._lock_payments(ids)
            total = ZERO
            for payment in payments:
                label = f"The payment from {payment.received_date.isoformat()} for {payment.amount}"
                if payment.status != "posted":
                    raise DepositError(message=f"{label} is void")
                if payment.deposit_account_id != undeposited.id:
                    raise _invalid(f"{label} was not received into Undeposited Funds")
                if payment.deposit_id is not None:
                    raise DepositError(message=f"{label} is already in a deposit")
                if payment.received_date > deposit_date:
                    raise _invalid(f"{label} was received after the deposit date")
                total += payment.amount
            if total <= 0 or total > MAX_TOTAL:
                raise _invalid("The deposit total must be between 0.01 and 999,999,999,999.99")

            deposit = Deposit(
                deposit_date=deposit_date,
                bank_account_id=bank.id,  # type: ignore[arg-type]
                memo=clean_memo or None,
                total=total,
            )
            self.session.add(deposit)
            self.session.flush()
            entry = self._post_entry(deposit, bank, undeposited, actor)
            deposit.entry_id = entry.id
            self.session.add(deposit)
            for payment in payments:
                payment.deposit_id = deposit.id
                payment.modify_date = utcnow_naive()
                self.session.add(payment)
            self.session.flush()
            record_audit(
                self.session,
                action="deposit.create",
                user_id=actor.id,
                entity="deposit",
                entity_id=str(deposit.id),
                after=self._snapshot(deposit)
                | {"entry_id": entry.id, "payment_ids": [p.id for p in payments]},
            )
            return deposit

        deposit = self._commit(work)
        logger.info("Created deposit id=%s", deposit.id)
        return deposit

    def void(
        self, deposit_id: int, *, actor: User, void_date: date, memo: str | None = None
    ) -> Deposit:
        ensure_role(actor, OWNER)

        def work() -> Deposit:
            deposit = self.session.exec(
                select(Deposit)
                .where(Deposit.id == deposit_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).first()
            if deposit is None:
                raise DepositNotFoundError()
            if deposit.status != "posted":
                raise DepositError(message="The deposit is already void")
            check_void_date(void_date, deposit.deposit_date, "deposit", InvalidDepositError)
            payments = list(
                self.session.exec(
                    select(Payment)
                    .where(Payment.deposit_id == deposit.id)
                    .order_by(col(Payment.id))
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).all()
            )
            before = self._snapshot(deposit)
            reversal = LedgerPostingService(self.session).void(
                deposit.entry_id,  # type: ignore[arg-type]
                entry_date=void_date,
                actor=actor,
                memo=memo,
            )
            deposit.status = "void"
            deposit.void_date = void_date
            deposit.modify_date = utcnow_naive()
            self.session.add(deposit)
            for payment in payments:
                payment.deposit_id = None
                payment.modify_date = utcnow_naive()
                self.session.add(payment)
            self.session.flush()
            record_audit(
                self.session,
                action="deposit.void",
                user_id=actor.id,
                entity="deposit",
                entity_id=str(deposit.id),
                before=before,
                after=self._snapshot(deposit)
                | {
                    "void_date": void_date.isoformat(),
                    "reversal_entry_id": reversal.id,
                    "freed_payment_ids": [p.id for p in payments],
                },
            )
            return deposit

        return self._commit(work)

    # ---------------------------------------------------------------- helpers

    def _locked_undeposited(self) -> Account:
        """The Undeposited Funds account, FOR SHARE so a rebind waits for this transaction."""
        account = self.session.exec(
            select(Account)
            .where(Account.system_role == SystemRole.UNDEPOSITED_FUNDS)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        ).first()
        if account is None or account.system_role != SystemRole.UNDEPOSITED_FUNDS:
            raise _invalid("Bind Undeposited Funds in Settings > Ledger first")
        return account

    @staticmethod
    def _check_ids(payment_ids: Sequence[int]) -> list[int]:
        ids = list(payment_ids)
        if not ids:
            raise _invalid("Choose at least one payment to deposit")
        if len(ids) > MAX_PAYMENTS:
            raise _invalid(f"A deposit can hold at most {MAX_PAYMENTS} payments")
        if any(not isinstance(i, int) or isinstance(i, bool) for i in ids):
            raise _invalid("Choose valid payments")
        if len(set(ids)) != len(ids):
            raise _invalid("A payment can be listed only once")
        return sorted(ids)

    def _bank(self, account_id: int, undeposited: Account) -> Account:
        account = self.session.get(Account, account_id) if isinstance(account_id, int) else None
        if account is None:
            raise _invalid("Choose the bank account to deposit to")
        if (
            account.type != AccountType.ASSET
            or account.id == undeposited.id
            or account.system_role == SystemRole.ACCOUNTS_RECEIVABLE
        ):
            raise _invalid("Deposit to a bank (asset) account other than Undeposited Funds")
        if not account.is_active:
            raise _invalid("The bank account is inactive")
        return account

    def _lock_payments(self, ids: list[int]) -> list[Payment]:
        payments = list(
            self.session.exec(
                select(Payment)
                .where(col(Payment.id).in_(ids))
                .order_by(col(Payment.id))
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
        )
        if len(payments) != len(ids):
            raise _invalid("A selected payment was not found")
        return payments

    def _post_entry(
        self, deposit: Deposit, bank: Account, undeposited: Account, actor: User
    ) -> JournalEntry:
        """Dr the bank, Cr Undeposited Funds, both for the deposit total."""
        memo = (f"Deposit {deposit.memo}" if deposit.memo else "Deposit")[:MAX_MEMO]
        return LedgerPostingService(self.session).post(
            EntryDraft(
                deposit.deposit_date,
                [
                    LineDraft(bank.id, debit=deposit.total),  # type: ignore[arg-type]
                    LineDraft(undeposited.id, credit=deposit.total),  # type: ignore[arg-type]
                ],
                memo=memo,
                source_type=SourceType.DEPOSIT,
                source_id=str(deposit.id),
            ),
            actor=actor,
        )

    def _commit(self, work: Callable[[], Deposit]) -> Deposit:
        """Run ``work`` and commit; on any error roll back at once (frees row locks)."""
        try:
            result = work()
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return result

    @staticmethod
    def _snapshot(deposit: Deposit) -> dict[str, Any]:
        return {
            "deposit_date": deposit.deposit_date.isoformat(),
            "bank_account_id": deposit.bank_account_id,
            "total": str(deposit.total),
            "status": deposit.status,
        }
