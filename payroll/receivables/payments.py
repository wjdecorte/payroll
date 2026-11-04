"""Customer payments and their application to invoices.

Recording a payment posts one journal entry (debit the deposit account, credit A/R).
Applying a payment to invoices posts nothing: the money simply stays a credit on A/R
until it is matched to an invoice, and each application stores the cash-basis income
split that Task 9's reports read. Applications are append-only; a misapplied payment is
fixed by voiding it and recording it again. Mutations are owner-only, audited, and
commit through a wrapper that rolls back on any error.

Locking order (so racing writers wait instead of deadlocking): A/R role account
(FOR SHARE), the payment (FOR UPDATE), the target invoices (FOR UPDATE, by id), then the
period lock (FOR SHARE). Locking the invoice before inserting an application is what
makes ``InvoiceService.void`` / ``replace`` (which look for live applications under the
same invoice lock) race-free.
"""

import logging
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlmodel import Session, col, func, select

from payroll import app_settings
from payroll.audit.services import record as record_audit
from payroll.auth.models import User
from payroll.auth.permissions import OWNER, ensure_role
from payroll.common.dates import is_supported_date, supported_range_text
from payroll.common.money import round_money
from payroll.ledger.exceptions import PeriodLockedError
from payroll.ledger.models import (
    Account,
    AccountType,
    JournalEntry,
    PeriodLock,
    SourceType,
    SystemRole,
)
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft
from payroll.receivables._common import utcnow_naive
from payroll.receivables.credits import min_credit_from
from payroll.receivables.exceptions import (
    ARNotConfiguredError,
    DepositError,
    InvalidPaymentError,
    InvoiceNotFoundError,
    InvoiceStateError,
    OverApplicationError,
    PaymentNotFoundError,
    PaymentStateError,
)
from payroll.receivables.invoices import live_applied
from payroll.receivables.models import (
    ApplicationIncome,
    Customer,
    Deposit,
    Invoice,
    InvoiceLine,
    Payment,
    PaymentApplication,
)

logger = logging.getLogger(f"{app_settings.logger_name}.receivables.payments")

METHODS = ("check", "ach", "cash", "card", "other")
MAX_REFERENCE = 100
MAX_MEMO = 500
MAX_AMOUNT = Decimal("999999999999.99")
CENT = Decimal("0.01")
ZERO = Decimal("0.00")


@dataclass(frozen=True)
class ApplicationInput:
    invoice_id: int
    amount: Decimal


def _invalid(message: str) -> InvalidPaymentError:
    return InvalidPaymentError(message=message)


def check_amount(value: object, label: str = "Amount") -> Decimal:
    """A finite Decimal from 0.01 to 999,999,999,999.99 with at most two decimal places."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise _invalid(f"{label} must be a number")
    if value <= 0 or value > MAX_AMOUNT:
        raise _invalid(f"{label} must be between 0.01 and 999,999,999,999.99")
    try:
        exact = value == value.quantize(CENT)
    except InvalidOperation:  # pragma: no cover - the range check above prevents it
        exact = False
    if not exact:
        raise _invalid(f"{label} can have at most 2 decimal places")
    return value.quantize(CENT)


def unapplied(session: Session, payment_id: int) -> Decimal:
    """What is left of a payment after its applications; 0 for a void payment."""
    payment = session.get(Payment, payment_id)
    if payment is None or payment.status != "posted":
        return ZERO
    return payment.amount - _applied_by_payment(session, payment_id)


def _applied_by_payment(session: Session, payment_id: int) -> Decimal:
    stmt = select(func.coalesce(func.sum(PaymentApplication.amount), 0)).where(
        PaymentApplication.payment_id == payment_id
    )
    return Decimal(session.exec(stmt).one())


def allocate_oldest_first(
    session: Session, customer_id: int, amount: Decimal, exclude: Collection[int] = ()
) -> list[ApplicationInput]:
    """Spread ``amount`` over the customer's open invoices, oldest due date first.

    Each invoice takes up to its live balance; whatever is left over stays unapplied.
    ``exclude`` skips invoices (a payment can apply to an invoice only once).
    """
    stmt = (
        select(Invoice)
        .where(Invoice.customer_id == customer_id, Invoice.status == "open")
        .order_by(col(Invoice.due_date), col(Invoice.number), col(Invoice.id))
    )
    remaining = amount
    out: list[ApplicationInput] = []
    for invoice in session.exec(stmt).all():
        if remaining <= 0:
            break
        if invoice.id in exclude:
            continue
        balance = invoice.total - live_applied(session, invoice.id)  # type: ignore[arg-type]
        take = min(balance, remaining)
        if take > 0:
            out.append(ApplicationInput(invoice.id, take))  # type: ignore[arg-type]
            remaining -= take
    return out


class PaymentService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ reads

    def get(self, payment_id: int) -> Payment:
        payment = self.session.get(Payment, payment_id)
        if payment is None:
            raise PaymentNotFoundError()
        return payment

    def list(
        self, customer_id: int | None = None, limit: int = 100, offset: int = 0
    ) -> list[Payment]:
        stmt = select(Payment)
        if customer_id is not None:
            stmt = stmt.where(Payment.customer_id == customer_id)
        stmt = (
            stmt.order_by(col(Payment.received_date).desc(), col(Payment.id).desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.exec(stmt).all())

    def applications(self, payment_id: int) -> list[PaymentApplication]:
        stmt = (
            select(PaymentApplication)
            .where(PaymentApplication.payment_id == payment_id)
            .order_by(col(PaymentApplication.id))
        )
        return list(self.session.exec(stmt).all())

    # -------------------------------------------------------------- mutations

    def record(
        self,
        *,
        actor: User,
        customer_id: int,
        received_date: date,
        amount: Decimal,
        deposit_account_id: int,
        method: str,
        reference: str | None = None,
        memo: str | None = None,
        applications: Sequence[ApplicationInput] = (),
    ) -> Payment:
        ensure_role(actor, OWNER)

        def work() -> Payment:
            ar = self._locked_ar()
            customer = (
                self.session.get(Customer, customer_id) if isinstance(customer_id, int) else None
            )
            if customer is None:
                raise _invalid("Choose a customer")
            if not customer.is_active:
                raise _invalid("The customer is inactive")
            if not isinstance(received_date, date) or not is_supported_date(received_date):
                raise _invalid(f"The received date must be within {supported_range_text()}")
            amount_ok = check_amount(amount)
            if method not in METHODS:
                raise _invalid("Choose a payment method")
            clean_reference = (reference or "").strip()
            if len(clean_reference) > MAX_REFERENCE:
                raise _invalid(f"Reference must be at most {MAX_REFERENCE} characters")
            clean_memo = (memo or "").strip()
            if len(clean_memo) > MAX_MEMO:
                raise _invalid(f"Memo must be at most {MAX_MEMO} characters")
            deposit = self._deposit_account(deposit_account_id, ar)
            checked = self._check_inputs(applications)

            payment = Payment(
                customer_id=customer.id,  # type: ignore[arg-type]
                received_date=received_date,
                method=method,
                reference=clean_reference or None,
                memo=clean_memo or None,
                amount=amount_ok,
                deposit_account_id=deposit.id,  # type: ignore[arg-type]
            )
            self.session.add(payment)
            self.session.flush()
            entry = self._post_entry(payment, customer, deposit, ar, actor)
            payment.entry_id = entry.id
            self.session.add(payment)
            self.session.flush()
            applied = self._apply(payment, ar, checked)
            record_audit(
                self.session,
                action="payment.record",
                user_id=actor.id,
                entity="payment",
                entity_id=str(payment.id),
                after=self._snapshot(payment) | {"entry_id": entry.id, "applications": applied},
            )
            return payment

        payment = self._commit(work)
        logger.info("Recorded payment id=%s", payment.id)
        return payment

    def apply(
        self, payment_id: int, *, actor: User, applications: Sequence[ApplicationInput]
    ) -> Payment:
        ensure_role(actor, OWNER)

        def work() -> Payment:
            ar = self._locked_ar()
            if ar is None:
                raise ARNotConfiguredError(
                    message="Bind Accounts receivable in Settings > Ledger first"
                )
            checked = self._check_inputs(applications)
            if not checked:
                raise _invalid("Enter an amount to apply")
            payment = self._locked_payment(payment_id)
            if payment.status != "posted":
                raise PaymentStateError(message="A void payment cannot be applied")
            applied = self._apply(payment, ar, checked)
            record_audit(
                self.session,
                action="payment.apply",
                user_id=actor.id,
                entity="payment",
                entity_id=str(payment.id),
                after={"applications": applied},
            )
            return payment

        return self._commit(work)

    def void(
        self, payment_id: int, *, actor: User, void_date: date, memo: str | None = None
    ) -> Payment:
        ensure_role(actor, OWNER)

        def work() -> Payment:
            payment = self._locked_payment(payment_id)
            if payment.status != "posted":
                raise PaymentStateError(message="The payment is already void")
            if payment.deposit_id is not None:
                deposit = self.session.get(Deposit, payment.deposit_id)
                if deposit is not None and deposit.status == "posted":
                    raise DepositError(message="Void the deposit first")
            if self._void_makes_credit_negative(payment, void_date):
                raise PaymentStateError(
                    message="Part of this payment was refunded; void the refund first"
                )
            before = self._snapshot(payment)
            reversal = LedgerPostingService(self.session).void(
                payment.entry_id,  # type: ignore[arg-type]
                entry_date=void_date,
                actor=actor,
                memo=memo,
            )
            payment.status = "void"
            payment.void_date = void_date
            payment.modify_date = utcnow_naive()
            self.session.add(payment)
            self.session.flush()
            record_audit(
                self.session,
                action="payment.void",
                user_id=actor.id,
                entity="payment",
                entity_id=str(payment.id),
                before=before,
                after=self._snapshot(payment)
                | {"void_date": void_date.isoformat(), "reversal_entry_id": reversal.id},
            )
            return payment

        return self._commit(work)

    # ---------------------------------------------------------------- helpers

    def _locked_ar(self) -> Account:
        """The A/R account, held FOR SHARE so a rebind waits until this transaction ends."""
        ar = self.session.exec(
            select(Account)
            .where(Account.system_role == SystemRole.ACCOUNTS_RECEIVABLE)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        ).first()
        if ar is None or ar.system_role != SystemRole.ACCOUNTS_RECEIVABLE:
            raise ARNotConfiguredError(
                message="Bind Accounts receivable in Settings > Ledger first"
            )
        return ar

    def _locked_payment(self, payment_id: int) -> Payment:
        """The payment FOR UPDATE, after every payment of its customer (FOR UPDATE, by id).

        Customer refunds spend credit across all of a customer's payments, so an apply or
        void must hold them all to check the customer's credit consistently. Every writer
        takes these rows in id order, so they wait for each other instead of deadlocking.
        """
        customer_id = self.session.exec(
            select(Payment.customer_id).where(Payment.id == payment_id)
        ).first()
        if customer_id is None:
            raise PaymentNotFoundError()
        locked = self.session.exec(
            select(Payment)
            .where(Payment.customer_id == customer_id)
            .order_by(col(Payment.id))
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        payment = next((p for p in locked if p.id == payment_id), None)
        if payment is None:  # pragma: no cover - payments are never deleted
            raise PaymentNotFoundError()
        return payment

    def _void_makes_credit_negative(self, payment: Payment, void_date: date) -> bool:
        """Would voiding on ``void_date`` push the customer's credit below zero on any date?

        From the void date on the payment no longer counts, so credit loses its amount less
        the applications recognized by then (those that come later stop counting at
        their own recognized dates).
        """
        start = max(void_date, payment.received_date)
        deltas = [(start, payment.amount)]
        for app in self.applications(payment.id):  # type: ignore[arg-type]
            deltas.append((max(app.recognized_date, start), -app.amount))
        return min_credit_from(self.session, payment.customer_id, start, deltas) < 0

    def _deposit_account(self, account_id: int, ar: Account) -> Account:
        account = self.session.get(Account, account_id) if isinstance(account_id, int) else None
        if account is None:
            raise _invalid("Choose where the money was deposited")
        if account.type != AccountType.ASSET or account.id == ar.id:
            raise _invalid("Deposit to Undeposited Funds or a bank (asset) account")
        if not account.is_active:
            raise _invalid("The deposit account is inactive")
        return account

    @staticmethod
    def _check_inputs(applications: Sequence[ApplicationInput]) -> list[ApplicationInput]:
        seen: set[int] = set()
        out: list[ApplicationInput] = []
        for item in applications:
            if not isinstance(item.invoice_id, int) or isinstance(item.invoice_id, bool):
                raise _invalid("Choose an invoice to apply to")
            if item.invoice_id in seen:
                raise _invalid("An invoice can be listed only once per payment")
            seen.add(item.invoice_id)
            out.append(
                ApplicationInput(item.invoice_id, check_amount(item.amount, "Applied amount"))
            )
        return sorted(out, key=lambda a: a.invoice_id)

    def _apply(
        self, payment: Payment, ar: Account, items: list[ApplicationInput]
    ) -> list[dict[str, Any]]:
        """Insert the applications (payment already locked); returns audit rows."""
        if not items:
            return []
        invoices = {
            inv.id: inv
            for inv in self.session.exec(
                select(Invoice)
                .where(col(Invoice.id).in_([i.invoice_id for i in items]))
                .order_by(col(Invoice.id))
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
        }
        lock_date = self.session.exec(
            select(PeriodLock.lock_date).where(PeriodLock.id == 1).with_for_update(read=True)
        ).one_or_none()
        available = payment.amount - _applied_by_payment(self.session, payment.id)  # type: ignore[arg-type]
        wanted = sum((i.amount for i in items), ZERO)
        if wanted > available:
            raise OverApplicationError(message=f"Only {available} of this payment is left to apply")
        deltas = [
            (max(payment.received_date, invoices[i.invoice_id].invoice_date), i.amount)
            for i in items
            if i.invoice_id in invoices
        ]
        if deltas:
            from_date = min(d for d, _ in deltas)
            headroom = min_credit_from(self.session, payment.customer_id, from_date)
            if min_credit_from(self.session, payment.customer_id, from_date, deltas) < 0:
                raise OverApplicationError(
                    message=f"Only {max(headroom, ZERO)} of credit is left to apply "
                    "(the rest was refunded or applied on earlier dates)"
                )
        existing = set(
            self.session.exec(
                select(PaymentApplication.invoice_id).where(
                    PaymentApplication.payment_id == payment.id
                )
            ).all()
        )
        audit: list[dict[str, Any]] = []
        for item in items:
            invoice = invoices.get(item.invoice_id)
            if invoice is None:
                raise InvoiceNotFoundError()
            label = f"Invoice {invoice.number}" if invoice.number is not None else "The invoice"
            if invoice.status != "open":
                raise InvoiceStateError(message=f"{label} is not open")
            if invoice.customer_id != payment.customer_id:
                raise _invalid(f"{label} belongs to a different customer")
            if invoice.id in existing:
                raise _invalid(f"This payment is already applied to {label.lower()}")
            balance = invoice.total - live_applied(self.session, invoice.id)  # type: ignore[arg-type]
            if item.amount > balance:
                raise OverApplicationError(message=f"{label} has only {balance} left to pay")
            recognized = max(payment.received_date, invoice.invoice_date)
            if lock_date is not None and recognized <= lock_date:
                raise PeriodLockedError(
                    message=f"The books are locked through {lock_date.isoformat()}; "
                    f"{recognized.isoformat()} is in a locked period"
                )
            application = PaymentApplication(
                payment_id=payment.id,  # type: ignore[arg-type]
                invoice_id=invoice.id,  # type: ignore[arg-type]
                amount=item.amount,
                recognized_date=recognized,
                ar_account_id=ar.id,  # type: ignore[arg-type]
            )
            self.session.add(application)
            self.session.flush()
            for account_id, slice_amount in self._income_split(invoice, item.amount):
                self.session.add(
                    ApplicationIncome(
                        application_id=application.id,  # type: ignore[arg-type]
                        account_id=account_id,
                        amount=slice_amount,
                    )
                )
            audit.append(
                {
                    "invoice_id": invoice.id,
                    "amount": str(item.amount),
                    "recognized_date": recognized.isoformat(),
                }
            )
        self.session.flush()
        return audit

    def _income_split(self, invoice: Invoice, amount: Decimal) -> list[tuple[int, Decimal]]:
        """Split ``amount`` over the invoice's income accounts, cumulatively and capped.

        ``prior`` is what live (posted-payment) applications already cover and ``prior_A`` the
        slices they stored for account ``A``; ``cap_A = total_A - prior_A`` is what ``A`` can
        still take. Each slice starts at ``round_money((prior + amount) x total_A / G) -
        prior_A`` limited to ``0..cap_A``. The cents that rounding leaves over (or adds) are
        then handed out (or taken back) so the slices sum to ``amount`` exactly, never going
        outside ``0..cap_A``: extra cents go to the accounts with the largest rounding
        shortfall first, take-backs from the smallest. Since the caps sum to at least
        ``amount``, there is always room, and a payment that clears the invoice fills every
        cap, so each account's live slices end exactly at its invoice total, for any number of
        accounts and any installment sizes. Zero slices are not stored.
        """
        totals: dict[int, Decimal] = {}
        for line in self.session.exec(
            select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id)
        ).all():
            totals[line.income_account_id] = totals.get(line.income_account_id, ZERO) + line.amount
        totals = {aid: t for aid, t in totals.items() if t > 0}
        accounts = {
            a.id: a
            for a in self.session.exec(select(Account).where(col(Account.id).in_(list(totals))))
        }
        ordered = sorted(
            totals,
            key=lambda aid: (accounts[aid].number is None, accounts[aid].number or "", aid),
        )
        grand = sum(totals.values(), ZERO)
        # the application row for ``amount`` is already flushed (its slices are not yet)
        prior = live_applied(self.session, invoice.id) - amount  # type: ignore[arg-type]
        prior_by_account = {
            aid: Decimal(total)
            for aid, total in self.session.exec(
                select(ApplicationIncome.account_id, func.sum(ApplicationIncome.amount))
                .join(
                    PaymentApplication,
                    col(PaymentApplication.id) == col(ApplicationIncome.application_id),
                )
                .join(Payment, col(Payment.id) == col(PaymentApplication.payment_id))
                .where(PaymentApplication.invoice_id == invoice.id, Payment.status == "posted")
                .group_by(col(ApplicationIncome.account_id))
            ).all()
        }
        cap = {aid: totals[aid] - prior_by_account.get(aid, ZERO) for aid in ordered}
        slices: dict[int, Decimal] = {}
        shortfall: dict[int, Decimal] = {}
        for aid in ordered:
            exact = (prior + amount) * totals[aid] / grand - prior_by_account.get(aid, ZERO)
            wanted = round_money(exact)
            slices[aid] = max(ZERO, min(wanted, cap[aid]))
            shortfall[aid] = exact - slices[aid]
        # largest shortfall first (ties: account number, then id; ``ordered`` is stable)
        by_shortfall = sorted(ordered, key=lambda aid: -shortfall[aid])
        leftover = amount - sum(slices.values(), ZERO)
        for aid in by_shortfall:
            if leftover <= 0:
                break
            add = min(cap[aid] - slices[aid], leftover)
            slices[aid] += add
            leftover -= add
        for aid in reversed(by_shortfall):
            if leftover >= 0:
                break
            take = min(slices[aid], -leftover)
            slices[aid] -= take
            leftover += take
        return [(aid, slices[aid]) for aid in ordered if slices[aid] > 0]

    def _post_entry(
        self, payment: Payment, customer: Customer, deposit: Account, ar: Account, actor: User
    ) -> JournalEntry:
        """Dr the deposit account, Cr A/R, both for the full payment amount."""
        words = ["Payment", payment.method, payment.reference, customer.name]
        memo = " ".join(w for w in words if w)[:MAX_MEMO]
        return LedgerPostingService(self.session).post(
            EntryDraft(
                payment.received_date,
                [
                    LineDraft(deposit.id, debit=payment.amount),  # type: ignore[arg-type]
                    LineDraft(ar.id, credit=payment.amount),  # type: ignore[arg-type]
                ],
                memo=memo,
                source_type=SourceType.PAYMENT,
                source_id=str(payment.id),
            ),
            actor=actor,
        )

    def _commit(self, work: Callable[[], Payment]) -> Payment:
        """Run ``work`` and commit; on any error roll back at once (frees row locks)."""
        try:
            result = work()
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return result

    @staticmethod
    def _snapshot(payment: Payment) -> dict[str, Any]:
        return {
            "customer_id": payment.customer_id,
            "received_date": payment.received_date.isoformat(),
            "method": payment.method,
            "reference": payment.reference,
            "amount": str(payment.amount),
            "deposit_account_id": payment.deposit_account_id,
            "status": payment.status,
        }
