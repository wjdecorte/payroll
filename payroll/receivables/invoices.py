"""Invoices: drafts, then issuing, replacing and voiding with ledger posting.

A draft never touches the ledger: it has no number and no entry, and discarding it
just marks it ``void``. Issuing assigns the number and posts one journal entry;
replacing reverses that entry and posts a new one; voiding reverses it. Mutations
are owner-only, audited, and commit through a wrapper that rolls back on any error.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlmodel import Session, col, func, select

from payroll import app_settings
from payroll.audit.services import record as record_audit
from payroll.auth.models import User
from payroll.auth.permissions import OWNER, ensure_role
from payroll.common.dates import is_supported_date, supported_range_text
from payroll.common.money import round_money
from payroll.ledger.models import Account, JournalEntry, SourceType, SystemRole
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft
from payroll.receivables._common import utcnow_naive
from payroll.receivables.exceptions import (
    ARNotConfiguredError,
    InvalidInvoiceError,
    InvoiceNotFoundError,
    InvoiceStateError,
)
from payroll.receivables.models import (
    Customer,
    Invoice,
    InvoiceLine,
    Item,
    Payment,
    PaymentApplication,
    ReceivablesSettings,
)

logger = logging.getLogger(f"{app_settings.logger_name}.receivables.invoices")

MAX_LINES = 100
MAX_DESCRIPTION = 500
MAX_MEMO = 500
MAX_TERMS_DAYS = 365
MAX_QTY = Decimal("999999.999")
MAX_AMOUNT = Decimal("999999999999.99")
QTY_STEP = Decimal("0.001")
CENT = Decimal("0.01")
STATUSES = ("draft", "open", "void")


@dataclass(frozen=True)
class InvoiceLineInput:
    item_id: int
    description: str
    qty: Decimal
    unit_price: Decimal


def line_amount(qty: Decimal, unit_price: Decimal) -> Decimal:
    """``qty x unit_price`` rounded half-up to the cent."""
    return round_money(qty * unit_price)


def live_applied(session: Session, invoice_id: int) -> Decimal:
    """Total applied to an invoice by payments that are still ``posted``."""
    stmt = (
        select(func.coalesce(func.sum(PaymentApplication.amount), 0))
        .select_from(PaymentApplication)
        .join(Payment, col(Payment.id) == col(PaymentApplication.payment_id))
        .where(PaymentApplication.invoice_id == invoice_id, Payment.status == "posted")
    )
    return Decimal(session.exec(stmt).one())


def _invalid(message: str) -> InvalidInvoiceError:
    return InvalidInvoiceError(message=message)


def _decimal(value: object, label: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise _invalid(f"{label} must be a number")
    return value


class InvoiceService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ reads

    def get(self, invoice_id: int) -> Invoice:
        invoice = self.session.get(Invoice, invoice_id)
        if invoice is None:
            raise InvoiceNotFoundError()
        return invoice

    def lines(self, invoice_id: int) -> list[InvoiceLine]:
        stmt = (
            select(InvoiceLine)
            .where(InvoiceLine.invoice_id == invoice_id)
            .order_by(col(InvoiceLine.line_no))
        )
        return list(self.session.exec(stmt).all())

    def list(
        self,
        status: str | None = None,
        customer_id: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Invoice]:
        stmt = select(Invoice)
        if status is not None:
            stmt = stmt.where(Invoice.status == status)
        if customer_id is not None:
            stmt = stmt.where(Invoice.customer_id == customer_id)
        stmt = (
            stmt.order_by(col(Invoice.invoice_date).desc(), col(Invoice.id).desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.exec(stmt).all())

    # -------------------------------------------------------------- mutations

    def create_draft(
        self,
        *,
        actor: User,
        customer_id: int,
        invoice_date: date,
        lines: list[InvoiceLineInput],
        terms_days: int | None = None,
        memo: str | None = None,
    ) -> Invoice:
        ensure_role(actor, OWNER)

        def work() -> Invoice:
            invoice = Invoice(
                customer_id=customer_id, invoice_date=invoice_date, due_date=invoice_date
            )
            invoice.total = Decimal("0.00")
            rows = self._fill(invoice, customer_id, invoice_date, lines, terms_days, memo)
            self.session.add(invoice)
            self.session.flush()
            self._replace_lines(invoice, rows)
            record_audit(
                self.session,
                action="invoice.create",
                user_id=actor.id,
                entity="invoice",
                entity_id=str(invoice.id),
                after=self._snapshot(invoice),
            )
            return invoice

        invoice = self._commit(work)
        logger.info("Created draft invoice id=%s", invoice.id)
        return invoice

    def update_draft(
        self,
        invoice_id: int,
        *,
        actor: User,
        customer_id: int,
        invoice_date: date,
        lines: list[InvoiceLineInput],
        terms_days: int | None = None,
        memo: str | None = None,
    ) -> Invoice:
        ensure_role(actor, OWNER)

        def work() -> Invoice:
            invoice = self._locked_draft(invoice_id)
            before = self._snapshot(invoice)
            rows = self._fill(invoice, customer_id, invoice_date, lines, terms_days, memo)
            invoice.modify_date = utcnow_naive()
            self.session.add(invoice)
            self._replace_lines(invoice, rows)
            record_audit(
                self.session,
                action="invoice.update",
                user_id=actor.id,
                entity="invoice",
                entity_id=str(invoice.id),
                before=before,
                after=self._snapshot(invoice),
            )
            return invoice

        return self._commit(work)

    def discard_draft(self, invoice_id: int, *, actor: User) -> Invoice:
        ensure_role(actor, OWNER)

        def work() -> Invoice:
            invoice = self._locked_draft(invoice_id)
            before = self._snapshot(invoice)
            invoice.status = "void"
            invoice.modify_date = utcnow_naive()
            self.session.add(invoice)
            self.session.flush()
            record_audit(
                self.session,
                action="invoice.discard",
                user_id=actor.id,
                entity="invoice",
                entity_id=str(invoice.id),
                before=before,
                after=self._snapshot(invoice),
            )
            return invoice

        return self._commit(work)

    def issue(self, invoice_id: int, *, actor: User) -> Invoice:
        ensure_role(actor, OWNER)

        def work() -> Invoice:
            ar = self._locked_ar()
            if ar is None:
                raise ARNotConfiguredError(
                    message="Bind Accounts receivable in Settings > Ledger first"
                )
            invoice = self._locked_draft(invoice_id, "Only a draft invoice can be issued")
            before = self._snapshot(invoice)
            customer = self.session.get(Customer, invoice.customer_id)
            if customer is None or not customer.is_active:
                raise _invalid("The customer is inactive")
            lines = self.lines(invoice.id)  # type: ignore[arg-type]
            for number, line in enumerate(lines, start=1):
                item = self.session.get(Item, line.item_id) if line.item_id is not None else None
                if item is None:
                    raise _invalid(f"Line {number}: choose an item")
                if not item.is_active:
                    raise _invalid(f"Line {number}: the item is inactive")
                line.income_account_id = item.income_account_id
                self.session.add(line)
            self.session.flush()
            settings = self.session.exec(
                select(ReceivablesSettings).where(ReceivablesSettings.id == 1).with_for_update()
            ).first()
            if settings is None:
                settings = ReceivablesSettings()
                self.session.add(settings)
                self.session.flush()
            number = settings.next_invoice_no
            settings.next_invoice_no = number + 1
            self.session.add(settings)
            entry = self._post_entry(invoice, customer, lines, number, ar, actor)
            invoice.number = number
            invoice.entry_id = entry.id
            invoice.status = "open"
            invoice.modify_date = utcnow_naive()
            self.session.add(invoice)
            self.session.flush()
            record_audit(
                self.session,
                action="invoice.issue",
                user_id=actor.id,
                entity="invoice",
                entity_id=str(invoice.id),
                before=before,
                after=self._snapshot(invoice) | {"number": number, "entry_id": entry.id},
            )
            return invoice

        invoice = self._commit(work)
        logger.info("Issued invoice id=%s number=%s", invoice.id, invoice.number)
        return invoice

    def replace(
        self,
        invoice_id: int,
        *,
        actor: User,
        customer_id: int,
        invoice_date: date,
        lines: list[InvoiceLineInput],
        terms_days: int | None = None,
        memo: str | None = None,
    ) -> Invoice:
        ensure_role(actor, OWNER)

        def work() -> Invoice:
            ar = self._locked_ar()
            if ar is None:
                raise ARNotConfiguredError(
                    message="Bind Accounts receivable in Settings > Ledger first"
                )
            invoice = self._locked_open(invoice_id, "Only an open invoice can be replaced")
            self._require_no_applications(invoice, "replaced")
            before = self._snapshot(invoice)
            rows = self._fill(invoice, customer_id, invoice_date, lines, terms_days, memo)
            invoice.modify_date = utcnow_naive()
            self.session.add(invoice)
            self._replace_lines(invoice, rows)
            customer = self.session.get(Customer, invoice.customer_id)
            old_entry = self.session.get(JournalEntry, invoice.entry_id)
            ledger = LedgerPostingService(self.session)
            ledger.void(old_entry.id, entry_date=old_entry.entry_date, actor=actor)  # type: ignore[union-attr,arg-type]
            new_lines = self.lines(invoice.id)  # type: ignore[arg-type]
            entry = self._post_entry(invoice, customer, new_lines, invoice.number, ar, actor)  # type: ignore[arg-type]
            invoice.entry_id = entry.id
            self.session.add(invoice)
            self.session.flush()
            record_audit(
                self.session,
                action="invoice.replace",
                user_id=actor.id,
                entity="invoice",
                entity_id=str(invoice.id),
                before=before | {"entry_id": old_entry.id},  # type: ignore[union-attr]
                after=self._snapshot(invoice) | {"entry_id": entry.id},
            )
            return invoice

        return self._commit(work)

    def void(
        self, invoice_id: int, *, actor: User, void_date: date, memo: str | None = None
    ) -> Invoice:
        ensure_role(actor, OWNER)

        def work() -> Invoice:
            invoice = self._locked_open(invoice_id, "Only an open invoice can be voided")
            self._require_no_applications(invoice, "voided")
            before = self._snapshot(invoice)
            reversal = LedgerPostingService(self.session).void(
                invoice.entry_id,  # type: ignore[arg-type]
                entry_date=void_date,
                actor=actor,
                memo=memo,
            )
            invoice.status = "void"
            invoice.void_date = void_date
            invoice.modify_date = utcnow_naive()
            self.session.add(invoice)
            self.session.flush()
            record_audit(
                self.session,
                action="invoice.void",
                user_id=actor.id,
                entity="invoice",
                entity_id=str(invoice.id),
                before=before,
                after=self._snapshot(invoice)
                | {"void_date": void_date.isoformat(), "reversal_entry_id": reversal.id},
            )
            return invoice

        return self._commit(work)

    # ---------------------------------------------------------------- helpers

    def _locked_ar(self) -> Account | None:
        """The A/R account, held FOR SHARE so a rebind waits until this transaction ends."""
        return self.session.exec(
            select(Account)
            .where(Account.system_role == SystemRole.ACCOUNTS_RECEIVABLE)
            .with_for_update(read=True)
        ).first()

    def _locked_open(self, invoice_id: int, message: str) -> Invoice:
        invoice = self.session.get(Invoice, invoice_id, with_for_update=True)
        if invoice is None:
            raise InvoiceNotFoundError()
        if invoice.status != "open":
            raise InvoiceStateError(message=message)
        return invoice

    def _require_no_applications(self, invoice: Invoice, verb: str) -> None:
        if live_applied(self.session, invoice.id) > 0:  # type: ignore[arg-type]
            raise InvoiceStateError(
                message=f"An invoice with payments applied cannot be {verb}; void the payment first"
            )

    def _post_entry(
        self,
        invoice: Invoice,
        customer: Customer | None,
        lines: list[InvoiceLine],
        number: int,
        ar: Account,
        actor: User,
    ) -> JournalEntry:
        """Dr A/R for the total; one Cr line per income account (summed, ordered)."""
        per_account: dict[int, tuple[Decimal, list[str]]] = {}
        for line in lines:
            total, texts = per_account.get(line.income_account_id, (Decimal("0.00"), []))
            if line.description:
                texts.append(line.description)
            per_account[line.income_account_id] = (total + line.amount, texts)
        per_account = {
            aid: v for aid, v in per_account.items() if v[0] > 0
        }  # a $0 line has no credit
        accounts = {
            a.id: a
            for a in self.session.exec(select(Account).where(col(Account.id).in_(per_account)))
        }
        ordered = sorted(
            per_account,
            key=lambda aid: (accounts[aid].number is None, accounts[aid].number or "", aid),
        )
        drafts = [LineDraft(ar.id, debit=invoice.total)]  # type: ignore[arg-type]
        for aid in ordered:
            amount, texts = per_account[aid]
            memo = "; ".join(texts)[:MAX_MEMO] or None
            drafts.append(LineDraft(aid, credit=amount, memo=memo))
        name = customer.name if customer is not None else ""
        return LedgerPostingService(self.session).post(
            EntryDraft(
                invoice.invoice_date,
                drafts,
                memo=f"Invoice #{number} {name}"[:MAX_MEMO],
                source_type=SourceType.INVOICE,
                source_id=str(invoice.id),
            ),
            actor=actor,
        )

    def _commit(self, work: Callable[[], Invoice]) -> Invoice:
        """Run ``work`` and commit; on any error roll back at once (frees row locks)."""
        try:
            result = work()
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return result

    def _locked_draft(
        self, invoice_id: int, message: str = "Only a draft invoice can be edited or discarded"
    ) -> Invoice:
        invoice = self.session.get(Invoice, invoice_id, with_for_update=True)
        if invoice is None:
            raise InvoiceNotFoundError()
        if invoice.status != "draft":
            raise InvoiceStateError(message=message)
        return invoice

    def _fill(
        self,
        invoice: Invoice,
        customer_id: int,
        invoice_date: date,
        lines: list[InvoiceLineInput],
        terms_days: int | None,
        memo: str | None,
    ) -> list[dict[str, Any]]:
        """Validate everything and set the header fields; returns the checked line rows."""
        customer = self.session.get(Customer, customer_id) if isinstance(customer_id, int) else None
        if customer is None:
            raise _invalid("Choose a customer")
        if not customer.is_active:
            raise _invalid("The customer is inactive")
        if not isinstance(invoice_date, date) or not is_supported_date(invoice_date):
            raise _invalid(f"The invoice date must be within {supported_range_text()}")
        if terms_days is None:
            terms_days = customer.terms_days
        if (
            not isinstance(terms_days, int)
            or isinstance(terms_days, bool)
            or not 0 <= terms_days <= MAX_TERMS_DAYS
        ):
            raise _invalid(f"Terms must be a whole number of days from 0 to {MAX_TERMS_DAYS}")
        try:
            due_date = invoice_date + timedelta(days=terms_days)
        except OverflowError:
            due_date = date.max
        if not is_supported_date(due_date):
            raise _invalid(f"The due date must be within {supported_range_text()}")
        clean_memo = (memo or "").strip()
        if len(clean_memo) > MAX_MEMO:
            raise _invalid(f"Memo must be at most {MAX_MEMO} characters")

        invoice.customer_id = customer.id  # type: ignore[assignment]
        invoice.invoice_date = invoice_date
        invoice.due_date = due_date
        invoice.terms_days = terms_days
        invoice.memo = clean_memo or None
        rows = self._check_lines(lines)
        invoice.total = sum((row["amount"] for row in rows), Decimal("0.00"))
        if invoice.total <= 0:
            raise _invalid("The invoice total must be greater than zero")
        if invoice.total > MAX_AMOUNT:
            raise _invalid("The invoice total is too large")
        return rows

    def _check_lines(self, lines: list[InvoiceLineInput]) -> list[dict[str, Any]]:
        if not lines:
            raise _invalid("Add at least one line")
        if len(lines) > MAX_LINES:
            raise _invalid(f"An invoice can have at most {MAX_LINES} lines")
        rows: list[dict[str, Any]] = []
        for number, line in enumerate(lines, start=1):
            where = f"Line {number}"
            item = self.session.get(Item, line.item_id) if isinstance(line.item_id, int) else None
            if item is None:
                raise _invalid(f"{where}: choose an item")
            if not item.is_active:
                raise _invalid(f"{where}: the item is inactive")
            qty = _decimal(line.qty, f"{where}: quantity")
            if qty <= 0 or qty > MAX_QTY:
                raise _invalid(f"{where}: quantity must be greater than 0 and at most 999,999.999")
            if qty != qty.quantize(QTY_STEP):
                raise _invalid(f"{where}: quantity can have at most 3 decimal places")
            price = _decimal(line.unit_price, f"{where}: unit price")
            if price < 0 or price > MAX_AMOUNT:
                raise _invalid(f"{where}: unit price must be between 0.00 and 999,999,999,999.99")
            if price != price.quantize(CENT):
                raise _invalid(f"{where}: unit price can have at most 2 decimal places")
            description = (
                (line.description.strip() if isinstance(line.description, str) else "")
                or item.description
                or item.name
            )
            if len(description) > MAX_DESCRIPTION:
                raise _invalid(f"{where}: description must be at most {MAX_DESCRIPTION} characters")
            qty, price = qty.quantize(QTY_STEP), price.quantize(CENT)
            rows.append(
                {
                    "item": item,
                    "description": description,
                    "qty": qty,
                    "unit_price": price,
                    "amount": line_amount(qty, price),
                }
            )
        return rows

    def _replace_lines(self, invoice: Invoice, rows: list[dict[str, Any]]) -> None:
        for old in self.lines(invoice.id):  # type: ignore[arg-type]
            self.session.delete(old)
        self.session.flush()
        for number, row in enumerate(rows, start=1):
            self.session.add(
                InvoiceLine(
                    invoice_id=invoice.id,
                    line_no=number,
                    item_id=row["item"].id,
                    description=row["description"],
                    qty=row["qty"],
                    unit_price=row["unit_price"],
                    amount=row["amount"],
                    income_account_id=row["item"].income_account_id,
                )
            )
        self.session.flush()

    @staticmethod
    def _snapshot(invoice: Invoice) -> dict[str, Any]:
        return {
            "customer_id": invoice.customer_id,
            "invoice_date": invoice.invoice_date.isoformat(),
            "due_date": invoice.due_date.isoformat(),
            "terms_days": invoice.terms_days,
            "status": invoice.status,
            "memo": invoice.memo,
            "total": str(invoice.total),
        }
