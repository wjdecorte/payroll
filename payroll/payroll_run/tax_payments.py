"""Tax payments: money paid to a tax agency against a payroll payable account.

A payment posts one journal entry: Dr the agency's payable role account, Cr the bank.
The amount may exceed the balance owed (the overpayment shows as a debit balance on the
payable). Voiding reverses the entry. Mutations are owner-only, audited, and commit through
a wrapper that rolls back on any error.

Locking order (spec section 6): the agency role account FOR SHARE, then the bank account
FOR SHARE, then (on void) the payment row FOR UPDATE, then whatever posting locks (period
lock FOR SHARE, entry counter). Two racing voids serialize on the payment row, so the
second sees it already void.
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
    lock_accounts,
    locked_role_account,
    utcnow_naive,
    void_memo,
)
from payroll.ledger.models import Account, JournalEntry, SourceType, SystemRole
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft

from .exceptions import (
    InvalidTaxPaymentError,
    PayrollRoleNotBoundError,
    TaxPaymentError,
    TaxPaymentNotFoundError,
)
from .ledger_models import TaxPayment

logger = logging.getLogger(f"{app_settings.logger_name}.payroll_run.tax_payments")

MAX_PAGE = 200
AGENCY_ROLES: dict[str, SystemRole] = {
    "irs_941": SystemRole.PAYROLL_FEDERAL_PAYABLE,
    "irs_futa": SystemRole.PAYROLL_FUTA_PAYABLE,
    "ga_dor": SystemRole.PAYROLL_GA_WH_PAYABLE,
    "ga_dol": SystemRole.PAYROLL_SUTA_PAYABLE,
}


def _invalid(message: str) -> InvalidTaxPaymentError:
    return InvalidTaxPaymentError(message=message)


def _unbound(role: SystemRole) -> PayrollRoleNotBoundError:
    return PayrollRoleNotBoundError(
        message=f"The account role {role.value} is not bound; bind it in Settings > Ledger first"
    )


def _check_agency(agency: object) -> str:
    if not isinstance(agency, str) or agency not in AGENCY_ROLES:
        raise _invalid("Choose the agency being paid: " + ", ".join(AGENCY_ROLES))
    return agency


class TaxPaymentService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ reads

    def get(self, payment_id: int) -> TaxPayment:
        payment = self.session.get(TaxPayment, payment_id) if is_int(payment_id) else None
        if payment is None:
            raise TaxPaymentNotFoundError()
        return payment

    def list(self, agency: str | None = None, limit: int = 50, offset: int = 0) -> list[TaxPayment]:
        if agency is not None:
            _check_agency(agency)
        if not is_int(limit) or not 1 <= limit <= MAX_PAGE:
            raise _invalid(f"The page size must be between 1 and {MAX_PAGE}")
        if not is_int(offset) or offset < 0:
            raise _invalid("The offset cannot be negative")
        stmt = select(TaxPayment)
        if agency is not None:
            stmt = stmt.where(TaxPayment.agency == agency)
        stmt = (
            stmt.order_by(col(TaxPayment.payment_date).desc(), col(TaxPayment.id).desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.exec(stmt).all())

    # -------------------------------------------------------------- mutations

    def create(
        self,
        *,
        actor: User,
        payment_date: date,
        agency: str,
        bank_account_id: int,
        amount: Decimal,
        memo: str | None = None,
    ) -> TaxPayment:
        ensure_role(actor, OWNER)

        def work() -> TaxPayment:
            # (1) validate the inputs that need no database access
            if not isinstance(payment_date, date) or not is_supported_date(payment_date):
                raise _invalid(f"The payment date must be within {supported_range_text()}")
            clean_agency = _check_agency(agency)
            if not is_int(bank_account_id):
                raise _invalid("Choose the bank account the payment is made from")
            clean_amount = checked_amount(amount, "The amount", InvalidTaxPaymentError)
            clean_memo = clean_text(memo, "Memo", MAX_MEMO, InvalidTaxPaymentError)

            # (2) lock the payable account, then the bank account, FOR SHARE, and check them
            role = AGENCY_ROLES[clean_agency]
            payable = locked_role_account(self.session, role, lambda: _unbound(role))
            bank = self._check_bank(
                lock_accounts(self.session, {bank_account_id}).get(bank_account_id), payable
            )

            # (3) write the document, then post
            payment = TaxPayment(
                payment_date=payment_date,
                agency=clean_agency,
                bank_account_id=bank.id,  # type: ignore[arg-type]
                amount=clean_amount,
                memo=clean_memo,
            )
            self.session.add(payment)
            self.session.flush()
            entry = self._post_entry(payment, payable, actor)
            payment.entry_id = entry.id
            self.session.add(payment)
            self.session.flush()
            record_audit(
                self.session,
                action="tax_payment.create",
                user_id=actor.id,
                entity="tax_payment",
                entity_id=str(payment.id),
                after=self._snapshot(payment) | {"entry_id": entry.id},
            )
            return payment

        payment = commit_or_rollback(self.session, work)
        logger.info("Created tax payment id=%s", payment.id)
        return payment

    def void(
        self, payment_id: int, *, actor: User, void_date: date, memo: str | None = None
    ) -> TaxPayment:
        ensure_role(actor, OWNER)

        def work() -> TaxPayment:
            if not is_int(payment_id):
                raise _invalid("Choose a tax payment to void")
            clean_memo = clean_text(memo, "Memo", MAX_MEMO, InvalidTaxPaymentError)
            # The agency and bank are immutable, so read them unlocked to lock in order.
            peek = self.session.get(TaxPayment, payment_id)
            if peek is None:
                raise TaxPaymentNotFoundError()
            role = AGENCY_ROLES[peek.agency]
            locked_role_account(self.session, role, lambda: _unbound(role))
            lock_accounts(self.session, {peek.bank_account_id})
            payment = self.session.exec(
                select(TaxPayment)
                .where(TaxPayment.id == payment_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).first()
            if payment is None:
                raise TaxPaymentNotFoundError()
            if payment.status != "posted":
                raise TaxPaymentError(message="The tax payment is already void")
            check_void_date(void_date, payment.payment_date, "payment", InvalidTaxPaymentError)
            before = self._snapshot(payment)
            reversal = LedgerPostingService(self.session).void(
                payment.entry_id,  # type: ignore[arg-type]
                entry_date=void_date,
                actor=actor,
                memo=void_memo(
                    clean_memo, f"Void of tax payment {payment.payment_date.isoformat()}"
                ),
            )
            payment.status = "void"
            payment.void_date = void_date
            payment.void_entry_id = reversal.id
            payment.modify_date = utcnow_naive()
            self.session.add(payment)
            self.session.flush()
            record_audit(
                self.session,
                action="tax_payment.void",
                user_id=actor.id,
                entity="tax_payment",
                entity_id=str(payment.id),
                before=before,
                after=self._snapshot(payment)
                | {"void_date": void_date.isoformat(), "reversal_entry_id": reversal.id},
            )
            return payment

        return commit_or_rollback(self.session, work)

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _check_bank(account: Account | None, payable: Account) -> Account:
        if account is None:
            raise _invalid("The bank account was not found")
        if account.type not in PAYMENT_TYPES or account.system_role in RESERVED_ROLES:
            raise _invalid(f"{account.name} cannot pay a tax; use a bank, cash or card account")
        if account.id == payable.id:
            raise _invalid("Choose a bank account other than the payable account being paid")
        if not account.is_active:
            raise _invalid(f"{account.name} is inactive")
        return account

    def _post_entry(self, payment: TaxPayment, payable: Account, actor: User) -> JournalEntry:
        """Dr the agency's payable account, Cr the bank."""
        return LedgerPostingService(
            self.session
        ).post(
            EntryDraft(
                payment.payment_date,
                [
                    LineDraft(payable.id, debit=payment.amount),  # type: ignore[arg-type]
                    LineDraft(payment.bank_account_id, credit=payment.amount),
                ],
                memo=(payment.memo or f"Tax payment {payment.agency}")[:MAX_MEMO],
                source_type=SourceType.TAX_PAYMENT,
                source_id=str(payment.id),
            ),
            actor=actor,
        )

    @staticmethod
    def _snapshot(payment: TaxPayment) -> dict[str, Any]:
        return {
            "payment_date": payment.payment_date.isoformat(),
            "agency": payment.agency,
            "bank_account_id": payment.bank_account_id,
            "amount": str(payment.amount),
            "memo": payment.memo,
            "status": payment.status,
        }
