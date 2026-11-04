"""
Read-only SQL for the ledger reports.

Every report query starts from :func:`line_selection`, the one place that joins a basis's
row source (:func:`payroll.reports.basis.row_source`) to ``account``. Anything that
narrows which lines count (a date window, the accounting basis) belongs there so no
report can bypass it. Every caller states its basis: the ledger-as-is reports (trial
balance, registers, journal) pass ``LEDGER_BASIS`` (accrual: the plain
``journal_line`` ⋈ ``journal_entry`` join); the financial statements pass the user's
chosen basis. Aggregates select their columns from ``row_source(basis)``; detail rows of
any basis come from :func:`basis_lines`. Balances are always computed with ``GROUP BY``
aggregates; nothing is stored.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func
from sqlalchemy.sql import Select
from sqlmodel import Session, col, select

from payroll.ledger.models import (
    NORMAL_DEBIT,
    Account,
    AccountType,
    JournalEntry,
    JournalLine,
    SourceType,
)
from payroll.reports.basis import Basis, row_source

ZERO = Decimal("0.00")

# The ledger as posted: the trial balance, registers, journal and the trial-balance
# invariant never depend on the cash/accrual choice (accrual reads the plain join).
LEDGER_BASIS = Basis.ACCRUAL

# Entries a period's income statement leaves out: a year-end close zeroes the
# income-statement accounts into equity, which is not that year's activity.
PNL_EXCLUDED_SOURCES: frozenset[SourceType] = frozenset({SourceType.YEAR_END_CLOSE})

INCOME_STATEMENT_TYPES: frozenset[AccountType] = frozenset(
    {
        AccountType.INCOME,
        AccountType.COGS,
        AccountType.EXPENSE,
        AccountType.OTHER_INCOME,
        AccountType.OTHER_EXPENSE,
    }
)
BALANCE_SHEET_TYPES: frozenset[AccountType] = frozenset(
    {AccountType.ASSET, AccountType.LIABILITY, AccountType.EQUITY}
)

# Deterministic report ordering: accounts by number (unnumbered last) then name;
# lines by date, then source type and entry number (numbering is per source type).
ACCOUNT_ORDER = (col(Account.number).asc().nulls_last(), col(Account.name), col(Account.id))
LINE_ORDER = (
    col(JournalEntry.entry_date),
    col(JournalEntry.source_type),
    col(JournalEntry.entry_no),
    col(JournalLine.line_no),
)


def line_selection(
    *columns: Any,
    basis: Basis,
    on_or_before: date | None = None,
    before: date | None = None,
    start: date | None = None,
    end: date | None = None,
    account_id: int | None = None,
    account_types: Iterable[AccountType] | None = None,
    exclude_sources: Iterable[SourceType] | None = None,
) -> Select:
    """The base selection of ledger lines every report is built on.

    Selects ``columns`` from the row source of ``basis`` (required, so every caller
    states it; see :func:`payroll.reports.basis.row_source`) joined to ``account``.
    Columns of the lines are taken from ``row_source(basis)`` (``rows.debit``,
    ``rows.entry_date``, ...); for accrual those are the ``journal_line`` /
    ``journal_entry`` columns themselves, so the ledger-as-is reports can also select
    the ``JournalLine`` / ``JournalEntry`` entities. Other bases refuse those entities
    and their columns (``ValueError``): they would cross join the row source. ``on_or_before`` / ``before`` /
    ``start`` / ``end`` restrict the line date (``start`` and ``end`` inclusive);
    ``account_id`` restricts to one account and ``account_types`` to accounts of those
    types; ``exclude_sources`` drops lines of those source types.
    """
    rows = row_source(basis)
    if rows.basis != Basis.ACCRUAL:
        # Only accrual's row source *is* journal_line ⋈ journal_entry; their columns next
        # to another source's FROM would silently cross join.
        ledger_tables = {JournalLine.__table__, JournalEntry.__table__}  # type: ignore[attr-defined]
        if any(f in ledger_tables for f in select(*columns).get_final_froms()):
            raise ValueError(
                f"{rows.basis.value} basis: select columns of row_source(basis), "
                "not JournalLine / JournalEntry"
            )
    stmt = (
        select(*columns).select_from(rows.from_).join(Account, col(Account.id) == rows.account_id)
    )
    if on_or_before is not None:
        stmt = stmt.where(rows.entry_date <= on_or_before)
    if before is not None:
        stmt = stmt.where(rows.entry_date < before)
    if start is not None:
        stmt = stmt.where(rows.entry_date >= start)
    if end is not None:
        stmt = stmt.where(rows.entry_date <= end)
    if account_id is not None:
        stmt = stmt.where(rows.account_id == account_id)
    if account_types is not None:
        stmt = stmt.where(col(Account.type).in_([AccountType(t).value for t in account_types]))
    if exclude_sources is not None:
        excluded = [SourceType(t).value for t in exclude_sources]
        stmt = stmt.where(rows.source_type.not_in(excluded))
    return stmt


def is_debit_normal(account_type: str) -> bool:
    return AccountType(account_type) in NORMAL_DEBIT


def natural(account_type: str, debit: Decimal, credit: Decimal) -> Decimal:
    """Net amount on the account's normal side (negative for a contra balance)."""
    return debit - credit if is_debit_normal(account_type) else credit - debit


@dataclass(frozen=True)
class AccountTotals:
    account: Account
    debit: Decimal
    credit: Decimal


def account_totals(session: Session, *, basis: Basis, **window: Any) -> list[AccountTotals]:
    """Debit and credit totals per account (only accounts with lines), in report order."""
    rows = row_source(basis)
    stmt = (
        line_selection(Account, func.sum(rows.debit), func.sum(rows.credit), basis=basis, **window)
        .group_by(col(Account.id))
        .order_by(*ACCOUNT_ORDER)
    )
    return [
        AccountTotals(account, debit or ZERO, credit or ZERO)
        for account, debit, credit in session.exec(stmt).all()
    ]


def debit_credit_totals(
    session: Session, *, basis: Basis, **window: Any
) -> tuple[Decimal, Decimal]:
    """Total debits and total credits of every line in the window (the trial-balance check)."""
    rows = row_source(basis)
    debit, credit = session.exec(
        line_selection(func.sum(rows.debit), func.sum(rows.credit), basis=basis, **window)
    ).one()
    return debit or ZERO, credit or ZERO


def earnings(session: Session, *, basis: Basis, **window: Any) -> Decimal:
    """Net earnings of the window: Σ(credit − debit) over every income-statement line.

    Equals income + other income − COGS − expenses − other expenses (natural
    signs). A year-end close entry nets a closed year to zero here, because its
    lines on the income-statement accounts offset that year's activity.
    """
    rows = row_source(basis)
    net = session.exec(
        line_selection(
            func.sum(rows.credit - rows.debit),
            basis=basis,
            account_types=INCOME_STATEMENT_TYPES,
            **window,
        )
    ).one()
    return net or ZERO


def opening_balances(
    session: Session,
    before: date,
    account_ids: Iterable[int] | None = None,
    *,
    basis: Basis = LEDGER_BASIS,
) -> dict[int, Decimal]:
    """Debit-minus-credit per account for lines dated before ``before`` (default: as posted)."""
    rows = row_source(basis)
    stmt = line_selection(
        rows.account_id,
        func.sum(rows.debit - rows.credit),
        basis=basis,
        before=before,
    ).group_by(rows.account_id)
    if account_ids is not None:
        stmt = stmt.where(rows.account_id.in_(list(account_ids)))
    return {account_id: net for account_id, net in session.exec(stmt).all()}


def journal_lines(session: Session, start: date, end: date):
    """Lines dated within ``[start, end]``, ordered entry by entry (date, source, number)."""
    stmt = line_selection(
        JournalLine, JournalEntry, Account, basis=LEDGER_BASIS, start=start, end=end
    ).order_by(*LINE_ORDER)
    return session.exec(stmt).all()


@dataclass(frozen=True)
class BasisLine:
    """One row of a basis's row source with its account (detail reports).

    ``line_id`` and ``line_no`` are ``None`` for a cash-basis application row, which
    carries its payment's entry (``entry_id``, ``entry_no``, ``source_type``, status) and
    ``application_id``; its reversing mirror (payment void) also has ``reverses_entry_id``.
    """

    line_id: int | None
    entry_id: int
    line_no: int | None
    account: Account
    debit: Decimal
    credit: Decimal
    line_memo: str | None
    entry_date: date
    source_type: str
    entry_no: int
    source_id: str | None
    entry_memo: str | None
    status: str
    reverses_entry_id: int | None
    application_id: int | None

    @property
    def memo(self) -> str | None:
        return self.line_memo or self.entry_memo

    @property
    def is_void(self) -> bool:
        return self.status == "void"

    @property
    def is_reversal(self) -> bool:
        return self.reverses_entry_id is not None


def basis_lines(
    session: Session,
    start: date,
    end: date,
    *,
    basis: Basis,
    account_id: int | None = None,
) -> list[BasisLine]:
    """The rows ``basis`` recognizes dated within ``[start, end]``, by account then line order.

    Within an account: date, source type, entry number, line number (application rows,
    which have none, after the entry's own lines), then application id and the reversing
    mirror after the original.
    """
    rows = row_source(basis)
    stmt = line_selection(
        *rows.columns(), Account, basis=basis, start=start, end=end, account_id=account_id
    ).order_by(
        *ACCOUNT_ORDER,
        rows.entry_date,
        rows.source_type,
        rows.entry_no,
        rows.line_no.asc().nulls_last(),
        rows.application_id.asc().nulls_first(),
        rows.reverses_entry_id.asc().nulls_first(),
    )
    out = []
    for row in session.exec(stmt).all():
        values = row._mapping
        out.append(
            BasisLine(
                account=values[Account],
                **{
                    name: values[name]
                    for name in BasisLine.__dataclass_fields__
                    if name != "account"
                },
            )
        )
    return out
