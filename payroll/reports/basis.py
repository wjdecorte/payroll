"""
Accounting basis for the financial statements, and the row source each basis reads.

Cash is the default view (owner's decision). :func:`row_source` is the single place that
decides which rows a basis recognizes; :func:`payroll.reports.queries.line_selection`
builds every report query on it, so no report can bypass it.

* **Accrual** (and every ledger-as-is report: trial balance, registers, journal) reads
  the plain ``journal_line`` ⋈ ``journal_entry`` join: the ledger as posted.
* **Cash** reads a subquery with the same columns: every ledger line except those of
  invoice entries (reversals and replacements copy ``source_type='invoice'``, so they go
  too), plus **application rows**. Each ``payment_application`` adds, on its
  ``recognized_date``, a debit of its amount to its A/R account and a credit of each
  ``application_income`` slice to that income account. If the payment is void, the mirror
  pair (credit A/R, debit each slice) is added on the payment's ``void_date``, or on the
  recognition date if the void is dated earlier, so an application voided before it
  was ever recognized never reaches income (the receivables subledger agrees: a payment
  stops counting on its void date). Application rows carry the payment's entry
  (``entry_id``, number, ``source_type='payment'``, status), a null line id and line
  number, the memo ``Invoice #<number> paid`` and their ``application_id``; the mirror
  rows also carry ``reverses_entry_id`` = the payment's entry.

Each pair balances on its own date, so cash-basis debits equal credits on every date and
the Balance Sheet balances. Money received but not applied stays as a credit in A/R.
"""

from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from typing import Any

import sqlalchemy as sa
from sqlalchemy.sql import FromClause
from sqlmodel import col, select

from payroll.ledger.models import JournalEntry, JournalLine, SourceType
from payroll.receivables.models import ApplicationIncome, Invoice, Payment, PaymentApplication


class Basis(StrEnum):
    CASH = "cash"
    ACCRUAL = "accrual"


DEFAULT_BASIS = Basis.CASH

BASIS_NOTE = (
    "Cash basis recognizes income when an invoice is paid. Money received but not yet "
    "applied to an invoice stays in Accounts Receivable as a credit; accrual basis "
    "recognizes income when the invoice is issued."
)

# The row-source columns, in order (the shape of journal_line ⋈ journal_entry).
COLUMNS: tuple[str, ...] = (
    "line_id",
    "entry_id",
    "line_no",
    "account_id",
    "debit",
    "credit",
    "line_memo",
    "entry_date",
    "source_type",
    "entry_no",
    "source_id",
    "entry_memo",
    "status",
    "reverses_entry_id",
    "application_id",
)


@dataclass(frozen=True)
class RowSource:
    """A FROM clause plus its columns by name (see :data:`COLUMNS`).

    Report queries select ``rows.debit``, filter on ``rows.entry_date`` and so on, and
    start from ``rows.from_``. Accrual's columns are the ``journal_line`` /
    ``journal_entry`` columns themselves, so its SQL is the plain join.
    """

    basis: Basis
    from_: FromClause
    line_id: Any
    entry_id: Any
    line_no: Any
    account_id: Any
    debit: Any
    credit: Any
    line_memo: Any
    entry_date: Any
    source_type: Any
    entry_no: Any
    source_id: Any
    entry_memo: Any
    status: Any
    reverses_entry_id: Any
    application_id: Any

    def columns(self) -> list[Any]:
        """Every column, labelled with its name (for detail selects)."""
        return [getattr(self, name).label(name) for name in COLUMNS]


def _ledger_columns() -> dict[str, Any]:
    return {
        "line_id": col(JournalLine.id),
        "entry_id": col(JournalLine.entry_id),
        "line_no": col(JournalLine.line_no),
        "account_id": col(JournalLine.account_id),
        "debit": col(JournalLine.debit),
        "credit": col(JournalLine.credit),
        "line_memo": col(JournalLine.memo),
        "entry_date": col(JournalEntry.entry_date),
        "source_type": col(JournalEntry.source_type),
        "entry_no": col(JournalEntry.entry_no),
        "source_id": col(JournalEntry.source_id),
        "entry_memo": col(JournalEntry.memo),
        "status": col(JournalEntry.status),
        "reverses_entry_id": col(JournalEntry.reverses_entry_id),
        "application_id": sa.cast(sa.null(), sa.Integer),
    }


def _ledger_join() -> FromClause:
    return sa.join(
        JournalLine.__table__,  # type: ignore[attr-defined]
        JournalEntry.__table__,  # type: ignore[attr-defined]
        col(JournalEntry.id) == col(JournalLine.entry_id),
    )


def _cash_subquery() -> FromClause:
    """The cash-basis rows: non-invoice ledger lines plus application rows (see module doc)."""
    money = sa.Numeric(14, 2)
    zero = sa.cast(sa.literal(0), money)
    ledger = select(*(c.label(n) for n, c in _ledger_columns().items())).select_from(_ledger_join())
    ledger = ledger.where(col(JournalEntry.source_type) != SourceType.INVOICE.value)

    # One event per application, plus one reversing event when its payment is void.
    recognized = select(
        col(PaymentApplication.id).label("application_id"),
        col(PaymentApplication.recognized_date).label("event_date"),
        sa.literal(False).label("reversing"),
    )
    voided = (
        select(
            col(PaymentApplication.id).label("application_id"),
            sa.func.greatest(col(Payment.void_date), col(PaymentApplication.recognized_date)).label(
                "event_date"
            ),
            sa.literal(True).label("reversing"),
        )
        .join(Payment, col(Payment.id) == col(PaymentApplication.payment_id))
        .where(col(Payment.status) == "void")
    )
    events = sa.union_all(recognized, voided).subquery("application_event")

    def application_rows(account_id: Any, amount: Any, normal_debit: bool, *joins: Any):
        """Rows on ``account_id``: ``amount`` on the normal side, the other side when reversing."""
        on_normal = sa.case((events.c.reversing, zero), else_=amount)
        on_other = sa.case((events.c.reversing, amount), else_=zero)
        debit, credit = (on_normal, on_other) if normal_debit else (on_other, on_normal)
        stmt = (
            select(
                sa.cast(sa.null(), sa.Integer).label("line_id"),
                col(JournalEntry.id).label("entry_id"),
                sa.cast(sa.null(), sa.Integer).label("line_no"),
                account_id.label("account_id"),
                debit.label("debit"),
                credit.label("credit"),
                sa.func.concat("Invoice #", col(Invoice.number), " paid").label("line_memo"),
                events.c.event_date.label("entry_date"),
                col(JournalEntry.source_type).label("source_type"),
                col(JournalEntry.entry_no).label("entry_no"),
                col(JournalEntry.source_id).label("source_id"),
                col(JournalEntry.memo).label("entry_memo"),
                col(JournalEntry.status).label("status"),
                sa.case((events.c.reversing, col(JournalEntry.id))).label("reverses_entry_id"),
                col(PaymentApplication.id).label("application_id"),
            )
            .select_from(events)
            .join(PaymentApplication, col(PaymentApplication.id) == events.c.application_id)
        )
        for target, onclause in joins:
            stmt = stmt.join(target, onclause)
        return (
            stmt.join(Payment, col(Payment.id) == col(PaymentApplication.payment_id))
            .join(JournalEntry, col(JournalEntry.id) == col(Payment.entry_id))
            .join(Invoice, col(Invoice.id) == col(PaymentApplication.invoice_id))
        )

    receivable = application_rows(
        col(PaymentApplication.ar_account_id), col(PaymentApplication.amount), True
    )
    income = application_rows(
        col(ApplicationIncome.account_id),
        col(ApplicationIncome.amount),
        False,
        (ApplicationIncome, col(ApplicationIncome.application_id) == col(PaymentApplication.id)),
    )
    return sa.union_all(ledger, receivable, income).subquery("cash_line")


@cache
def row_source(basis: Basis) -> RowSource:
    """The rows ``basis`` recognizes. Cached: one object per basis, so every column a
    caller takes from it refers to the same FROM clause the query starts from."""
    basis = Basis(basis)
    if basis == Basis.ACCRUAL:
        return RowSource(basis=basis, from_=_ledger_join(), **_ledger_columns())
    cash = _cash_subquery()
    return RowSource(basis=basis, from_=cash, **{name: cash.c[name] for name in COLUMNS})
