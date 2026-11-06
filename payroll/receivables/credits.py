"""Customer credit (unapplied overpayments) and refunds of it.

Credit is never stored. For a customer it is the money received that has not been matched
to an invoice nor handed back: payments live as of a date, less their applications
recognized by then, less the refunds live as of then. A refund posts one journal entry
(debit A/R, credit the bank), so the credit that was sitting as a credit balance in A/R
leaves with the cash.

Locking order (matches PaymentService): the A/R role account (FOR SHARE), then the
customer's payment rows (FOR UPDATE, by id), then whatever posting locks. Holding the
payment rows is what stops two refunds from both spending the same credit, and keeps a
payment apply or void from changing the credit between the check and the post.
"""

import logging
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sqlmodel import Session, col, func, or_, select

from payroll import app_settings
from payroll.audit.services import record as record_audit
from payroll.auth.models import User
from payroll.auth.permissions import OWNER, ensure_role
from payroll.common.dates import is_supported_date, supported_range_text
from payroll.common.documents import (
    check_void_date,
    commit_or_rollback,
    locked_role_account,
    utcnow_naive,
    void_memo,
)
from payroll.ledger.models import Account, AccountType, JournalEntry, SourceType, SystemRole
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft
from payroll.receivables.exceptions import (
    ARNotConfiguredError,
    InvalidPaymentError,
    InvalidRefundError,
    RefundNotFoundError,
    RefundStateError,
)
from payroll.receivables.models import Customer, CustomerRefund, Payment, PaymentApplication

logger = logging.getLogger(f"{app_settings.logger_name}.receivables.credits")

MAX_REFERENCE = 100
MAX_MEMO = 500
ZERO = Decimal("0.00")


def _invalid(message: str) -> InvalidRefundError:
    return InvalidRefundError(message=message)


def payment_live_filter(as_of: date | None) -> list[Any]:
    """WHERE terms selecting payments live as of ``as_of`` (``None`` = posted now).

    A payment counts when received on or before ``as_of`` and not voided on or before it.
    Shared by customer credit and invoice balances so they can never disagree.
    """
    live = col(Payment.status) == "posted"
    if as_of is None:
        return [live]
    return [col(Payment.received_date) <= as_of, or_(live, col(Payment.void_date) > as_of)]


def customer_credits(
    session: Session, as_of: date | None = None, customer_id: int | None = None
) -> dict[int, Decimal]:
    """Unapplied credit per customer as of ``as_of`` (``None`` = now); zero balances omitted.

    Payments count when received on or before ``as_of`` and not voided on or before it;
    applications count when recognized on or before it (and their payment counts);
    refunds count when dated on or before it and not voided on or before it.
    """
    ref_live = col(CustomerRefund.status) == "posted"
    pay_filter = payment_live_filter(as_of)
    ref_filter = [ref_live]
    app_filter = []
    if as_of is not None:
        ref_filter = [
            col(CustomerRefund.refund_date) <= as_of,
            or_(ref_live, col(CustomerRefund.void_date) > as_of),
        ]
        app_filter = [col(PaymentApplication.recognized_date) <= as_of]
    if customer_id is not None:
        pay_filter.append(col(Payment.customer_id) == customer_id)
        ref_filter.append(col(CustomerRefund.customer_id) == customer_id)

    credit: dict[int, Decimal] = {}
    received = session.exec(
        select(Payment.customer_id, func.sum(Payment.amount))
        .where(*pay_filter)
        .group_by(col(Payment.customer_id))
    ).all()
    for cid, total in received:
        credit[cid] = credit.get(cid, ZERO) + Decimal(total)
    applied = session.exec(
        select(Payment.customer_id, func.sum(PaymentApplication.amount))
        .join(Payment, col(Payment.id) == col(PaymentApplication.payment_id))
        .where(*pay_filter, *app_filter)
        .group_by(col(Payment.customer_id))
    ).all()
    for cid, total in applied:
        credit[cid] = credit.get(cid, ZERO) - Decimal(total)
    refunded = session.exec(
        select(CustomerRefund.customer_id, func.sum(CustomerRefund.amount))
        .where(*ref_filter)
        .group_by(col(CustomerRefund.customer_id))
    ).all()
    for cid, total in refunded:
        credit[cid] = credit.get(cid, ZERO) - Decimal(total)
    return {cid: amount for cid, amount in credit.items() if amount != 0}


def min_credit_from(
    session: Session,
    customer_id: int,
    from_date: date,
    deltas: Sequence[tuple[date, Decimal]] = (),
) -> Decimal:
    """The lowest ``customer_credits`` balance for a customer on any date from ``from_date`` on.

    Credit only changes on event dates (a payment received or voided, an application
    recognized, a refund made or voided), so the minimum is found by evaluating
    ``customer_credits`` on ``from_date`` and on every later event date. ``deltas`` are
    hypothetical changes not yet stored: each ``(date, amount)`` lowers the credit by
    ``amount`` on and after ``date`` (negative amounts raise it). A document whose
    effective date is ``from_date`` is allowed only if this stays at or above zero.
    """
    dates: set[date] = {from_date}
    queries = (
        select(Payment.received_date).where(Payment.customer_id == customer_id),
        select(Payment.void_date).where(Payment.customer_id == customer_id),
        select(PaymentApplication.recognized_date)
        .join(Payment, col(Payment.id) == col(PaymentApplication.payment_id))
        .where(Payment.customer_id == customer_id),
        select(CustomerRefund.refund_date).where(CustomerRefund.customer_id == customer_id),
        select(CustomerRefund.void_date).where(CustomerRefund.customer_id == customer_id),
    )
    for query in queries:
        dates.update(d for d in session.exec(query).all() if d is not None and d >= from_date)
    dates.update(d for d, _ in deltas if d >= from_date)
    lowest: Decimal | None = None
    for day in sorted(dates):
        level = customer_credits(session, day, customer_id).get(customer_id, ZERO)
        level -= sum((a for d, a in deltas if d <= day), ZERO)
        if lowest is None or level < lowest:
            lowest = level
    return lowest  # type: ignore[return-value]  # dates always holds from_date


class RefundService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ reads

    def get(self, refund_id: int) -> CustomerRefund:
        refund = self.session.get(CustomerRefund, refund_id)
        if refund is None:
            raise RefundNotFoundError()
        return refund

    def list(
        self, customer_id: int | None = None, limit: int = 100, offset: int = 0
    ) -> list[CustomerRefund]:
        stmt = select(CustomerRefund)
        if customer_id is not None:
            stmt = stmt.where(CustomerRefund.customer_id == customer_id)
        stmt = (
            stmt.order_by(col(CustomerRefund.refund_date).desc(), col(CustomerRefund.id).desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.exec(stmt).all())

    # -------------------------------------------------------------- mutations

    def record(
        self,
        *,
        actor: User,
        customer_id: int,
        refund_date: date,
        amount: Decimal,
        bank_account_id: int,
        method: str,
        reference: str | None = None,
        memo: str | None = None,
    ) -> CustomerRefund:
        ensure_role(actor, OWNER)
        # imported here: payments.py imports this module (min_credit_from)
        from payroll.receivables.payments import METHODS, check_amount

        def work() -> CustomerRefund:
            ar = locked_role_account(
                self.session,
                SystemRole.ACCOUNTS_RECEIVABLE,
                lambda: ARNotConfiguredError(
                    message="Bind Accounts receivable in Settings > Ledger first"
                ),
            )
            customer = (
                self.session.get(Customer, customer_id) if isinstance(customer_id, int) else None
            )
            if customer is None:
                raise _invalid("Choose a customer")
            if not isinstance(refund_date, date) or not is_supported_date(refund_date):
                raise _invalid(f"The refund date must be within {supported_range_text()}")
            try:
                amount_ok = check_amount(amount, "Refund amount")
            except InvalidPaymentError as exc:
                raise _invalid(exc.message) from exc
            if method not in METHODS:
                raise _invalid("Choose a refund method")
            clean_reference = (reference or "").strip()
            if len(clean_reference) > MAX_REFERENCE:
                raise _invalid(f"Reference must be at most {MAX_REFERENCE} characters")
            clean_memo = (memo or "").strip()
            if len(clean_memo) > MAX_MEMO:
                raise _invalid(f"Memo must be at most {MAX_MEMO} characters")
            bank = self._bank(bank_account_id, ar)

            self._lock_customer_payments(customer.id)  # type: ignore[arg-type]
            available = min_credit_from(self.session, customer.id, refund_date)  # type: ignore[arg-type]
            if available <= 0:
                raise _invalid("The customer has no credit to refund")
            if amount_ok > available:
                raise _invalid(f"Only {available} of credit is available to refund")

            refund = CustomerRefund(
                customer_id=customer.id,  # type: ignore[arg-type]
                refund_date=refund_date,
                method=method,
                reference=clean_reference or None,
                memo=clean_memo or None,
                amount=amount_ok,
                bank_account_id=bank.id,  # type: ignore[arg-type]
            )
            self.session.add(refund)
            self.session.flush()
            entry = self._post_entry(refund, customer, bank, ar, actor)
            refund.entry_id = entry.id
            self.session.add(refund)
            self.session.flush()
            record_audit(
                self.session,
                action="refund.record",
                user_id=actor.id,
                entity="refund",
                entity_id=str(refund.id),
                after=self._snapshot(refund) | {"entry_id": entry.id},
            )
            return refund

        refund = commit_or_rollback(self.session, work)
        logger.info("Recorded refund id=%s", refund.id)
        return refund

    def void(
        self, refund_id: int, *, actor: User, void_date: date, memo: str | None = None
    ) -> CustomerRefund:
        ensure_role(actor, OWNER)

        def work() -> CustomerRefund:
            refund = self.session.exec(
                select(CustomerRefund)
                .where(CustomerRefund.id == refund_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).first()
            if refund is None:
                raise RefundNotFoundError()
            if refund.status != "posted":
                raise RefundStateError(message="The refund is already void")
            check_void_date(void_date, refund.refund_date, "refund", InvalidRefundError)
            before = self._snapshot(refund)
            reversal = LedgerPostingService(self.session).void(
                refund.entry_id,  # type: ignore[arg-type]
                entry_date=void_date,
                actor=actor,
                memo=void_memo(memo, self._void_default_memo(refund)),
            )
            refund.status = "void"
            refund.void_date = void_date
            refund.modify_date = utcnow_naive()
            self.session.add(refund)
            self.session.flush()
            record_audit(
                self.session,
                action="refund.void",
                user_id=actor.id,
                entity="refund",
                entity_id=str(refund.id),
                before=before,
                after=self._snapshot(refund)
                | {"void_date": void_date.isoformat(), "reversal_entry_id": reversal.id},
            )
            return refund

        return commit_or_rollback(self.session, work)

    # ---------------------------------------------------------------- helpers

    def _void_default_memo(self, refund: CustomerRefund) -> str:
        customer = self.session.get(Customer, refund.customer_id)
        name = f" - {customer.name}" if customer is not None else ""
        return f"Void of refund {refund.refund_date.isoformat()}{name}"

    def _lock_customer_payments(self, customer_id: int) -> None:
        """FOR UPDATE on the customer's payments in id order (same order as other services)."""
        self.session.exec(
            select(Payment)
            .where(Payment.customer_id == customer_id)
            .order_by(col(Payment.id))
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()

    def _bank(self, account_id: int, ar: Account) -> Account:
        account = self.session.get(Account, account_id) if isinstance(account_id, int) else None
        if account is None:
            raise _invalid("Choose the bank account the refund was paid from")
        if account.type != AccountType.ASSET or account.id == ar.id:
            raise _invalid("Pay the refund from a bank (asset) account")
        if not account.is_active:
            raise _invalid("The bank account is inactive")
        return account

    def _post_entry(
        self,
        refund: CustomerRefund,
        customer: Customer,
        bank: Account,
        ar: Account,
        actor: User,
    ) -> JournalEntry:
        """Dr A/R, Cr the bank, both for the full refund amount."""
        words = ["Refund", refund.method, refund.reference, customer.name]
        memo = " ".join(w for w in words if w)[:MAX_MEMO]
        return LedgerPostingService(self.session).post(
            EntryDraft(
                refund.refund_date,
                [
                    LineDraft(ar.id, debit=refund.amount),  # type: ignore[arg-type]
                    LineDraft(bank.id, credit=refund.amount),  # type: ignore[arg-type]
                ],
                memo=memo,
                source_type=SourceType.REFUND,
                source_id=str(refund.id),
            ),
            actor=actor,
        )

    @staticmethod
    def _snapshot(refund: CustomerRefund) -> dict[str, Any]:
        return {
            "customer_id": refund.customer_id,
            "refund_date": refund.refund_date.isoformat(),
            "method": refund.method,
            "reference": refund.reference,
            "amount": str(refund.amount),
            "bank_account_id": refund.bank_account_id,
            "status": refund.status,
        }
