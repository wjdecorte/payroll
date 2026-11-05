"""Derived invoice state and the A/R aging report.

Nothing here is stored. An invoice's paid amount is the sum of its applications whose
payment is live as of the date (see :func:`payroll.receivables.credits.payment_live_filter`,
shared with customer credit) and recognized by it; its status follows from the stored
status, the due date and the paid amount. Aging buckets the open balances by days past
due and reconciles the subledger (invoice balances less customer credit) with the
ledger's A/R account.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from sqlmodel import Session, col, func, or_, select

from payroll.ledger.models import Account, JournalLine, SystemRole
from payroll.receivables.credits import customer_credits, payment_live_filter
from payroll.receivables.models import Customer, Invoice, Payment, PaymentApplication
from payroll.reports import queries
from payroll.reports.periods import company_today, ensure_supported_date
from payroll.reports.schemas import Invariant

ZERO = Decimal("0.00")
Status = Literal["draft", "open", "partial", "paid", "overdue", "void"]
STATUSES: tuple[Status, ...] = ("draft", "open", "partial", "paid", "overdue", "void")


@dataclass(frozen=True)
class InvoiceState:
    invoice: Invoice
    paid: Decimal
    balance: Decimal
    status: Status
    days_overdue: int


@dataclass(frozen=True)
class AgingRow:
    customer_id: int
    customer_name: str
    current: Decimal
    d1_30: Decimal
    d31_60: Decimal
    d61_90: Decimal
    d91_plus: Decimal
    total: Decimal
    credit: Decimal


@dataclass(frozen=True)
class ARAging:
    as_of: date
    rows: list[AgingRow]
    totals: AgingRow
    invoices: list[InvoiceState]
    ledger_ar_balance: Decimal
    difference: Decimal
    invariants: list[Invariant]


def _paid_by_invoice(
    session: Session, as_of: date, invoice_id: int | None = None
) -> dict[int, Decimal]:
    stmt = (
        select(PaymentApplication.invoice_id, func.sum(PaymentApplication.amount))
        .join(Payment, col(Payment.id) == col(PaymentApplication.payment_id))
        .where(*payment_live_filter(as_of), col(PaymentApplication.recognized_date) <= as_of)
        .group_by(col(PaymentApplication.invoice_id))
    )
    if invoice_id is not None:
        stmt = stmt.where(col(PaymentApplication.invoice_id) == invoice_id)
    return {invoice_id: Decimal(total) for invoice_id, total in session.exec(stmt).all()}


def _state(invoice: Invoice, paid: Decimal, as_of: date) -> InvoiceState:
    stored = invoice.status
    if stored == "void" and invoice.void_date is not None and invoice.void_date > as_of:
        stored = "open"  # voided later than as_of: it was an ordinary invoice then
    if stored in ("draft", "void"):
        return InvoiceState(invoice, ZERO, ZERO, stored, 0)  # type: ignore[arg-type]
    balance = invoice.total - paid
    days_overdue = max(0, (as_of - invoice.due_date).days)
    if balance == 0:
        status: Status = "paid"
    elif invoice.due_date < as_of:
        status = "overdue"
    elif paid > 0:
        status = "partial"
    else:
        status = "open"
    return InvoiceState(invoice, paid, balance, status, days_overdue)


def invoice_states(
    session: Session,
    as_of: date | None = None,
    customer_id: int | None = None,
    *,
    include_inactive: bool = False,
) -> list[InvoiceState]:
    """Each invoice's paid amount, balance and status as of ``as_of`` (default today).

    An invoice exists when issued with ``invoice_date <= as_of`` and not voided on or
    before ``as_of``. ``include_inactive`` also returns drafts and the invoices voided
    by then (status ``draft`` / ``void``, nothing owed), for the invoice list.
    """
    as_of = as_of or company_today()
    stmt = select(Invoice).order_by(col(Invoice.invoice_date), col(Invoice.id))
    if customer_id is not None:
        stmt = stmt.where(Invoice.customer_id == customer_id)
    if not include_inactive:
        stmt = stmt.where(
            col(Invoice.status).in_(("open", "void")),
            col(Invoice.invoice_date) <= as_of,
            or_(col(Invoice.void_date).is_(None), col(Invoice.void_date) > as_of),
        )
    paid = _paid_by_invoice(session, as_of)
    return [_state(i, paid.get(i.id, ZERO), as_of) for i in session.exec(stmt).all()]  # type: ignore[arg-type]


def invoice_state(session: Session, invoice: Invoice, as_of: date | None = None) -> InvoiceState:
    """The derived state of one invoice (drafts and voids included) as of ``as_of``."""
    as_of = as_of or company_today()
    paid = _paid_by_invoice(session, as_of, invoice.id)
    return _state(invoice, paid.get(invoice.id, ZERO), as_of)  # type: ignore[arg-type]


def _bucket(days_overdue: int) -> str:
    if days_overdue <= 0:
        return "current"
    if days_overdue <= 30:
        return "d1_30"
    if days_overdue <= 60:
        return "d31_60"
    if days_overdue <= 90:
        return "d61_90"
    return "d91_plus"


def _ledger_ar_balance(session: Session, as_of: date) -> Decimal:
    ar = session.exec(
        select(Account).where(Account.system_role == SystemRole.ACCOUNTS_RECEIVABLE)
    ).first()
    if ar is None:
        return ZERO
    net = session.exec(
        queries.line_selection(
            func.sum(JournalLine.debit - JournalLine.credit),
            basis=queries.LEDGER_BASIS,
            on_or_before=as_of,
            account_id=ar.id,
        )
    ).one()
    return net or ZERO


def ar_aging(session: Session, as_of: date) -> ARAging:
    ensure_supported_date(as_of)
    states = invoice_states(session, as_of)
    credits = customer_credits(session, as_of)
    buckets: dict[int, dict[str, Decimal]] = {}
    for state in states:
        if state.balance <= 0:
            continue
        row = buckets.setdefault(state.invoice.customer_id, {})
        key = _bucket(state.days_overdue)
        row[key] = row.get(key, ZERO) + state.balance
    ids = set(buckets) | set(credits)
    names = (
        {
            c.id: c.name
            for c in session.exec(select(Customer).where(col(Customer.id).in_(ids))).all()
        }
        if ids
        else {}
    )

    def make(customer_id: int, name: str, amounts: dict[str, Decimal], credit: Decimal) -> AgingRow:
        cells = {
            k: amounts.get(k, ZERO) for k in ("current", "d1_30", "d31_60", "d61_90", "d91_plus")
        }
        return AgingRow(customer_id, name, **cells, total=sum(cells.values(), ZERO), credit=credit)

    rows = sorted(
        (make(cid, names[cid], buckets.get(cid, {}), credits.get(cid, ZERO)) for cid in ids),
        key=lambda r: (r.customer_name.lower(), r.customer_id),
    )
    totals = make(
        0,
        "Total",
        {
            k: sum((getattr(r, k) for r in rows), ZERO)
            for k in ("current", "d1_30", "d31_60", "d61_90", "d91_plus")
        },
        sum((r.credit for r in rows), ZERO),
    )
    subledger = sum((s.balance for s in states), ZERO) - sum(credits.values(), ZERO)
    ledger = _ledger_ar_balance(session, as_of)
    difference = subledger - ledger
    invariant = Invariant(
        name="ar_subledger_matches_ledger",
        ok=difference == 0,
        detail=(
            "Invoice balances less customer credit equal the ledger A/R balance"
            if difference == 0
            else f"Invoice balances less customer credit differ from the ledger A/R balance by {difference}"
        ),
    )
    open_states = [s for s in states if s.balance > 0]
    return ARAging(as_of, rows, totals, open_states, ledger, difference, [invariant])
