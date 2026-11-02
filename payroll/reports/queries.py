"""
Read-only SQL for the ledger reports.

Every report query starts from :func:`line_selection`, the one place that joins
``journal_line`` -> ``journal_entry`` -> ``account``. Anything that narrows which
lines count (a date window, the accounting basis) belongs there so no report
can bypass it. Every caller states its basis: the ledger-as-is reports (trial
balance, registers, journal) pass ``Basis.ACCRUAL``, which applies no basis
predicate; the financial statements pass the user's chosen basis. Balances are always computed from ``journal_line``
rows with ``GROUP BY`` aggregates; nothing is stored.
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
from payroll.reports.basis import Basis, recognized_lines

ZERO = Decimal("0.00")

# The ledger as posted: the trial balance, registers, journal and the trial-balance
# invariant never depend on a cash/accrual predicate (accrual applies none).
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

    Selects ``columns`` from ``journal_line`` joined to its entry and account.
    ``on_or_before`` / ``before`` / ``start`` / ``end`` restrict ``entry_date``
    (``start`` and ``end`` inclusive); ``account_id`` restricts to one account and
    ``account_types`` to accounts of those types; ``exclude_sources`` drops
    entries of those source types. ``basis`` (required, so every caller states
    it) applies :func:`payroll.reports.basis.recognized_lines`, the one
    accounting-basis hook.
    """
    stmt = (
        select(*columns)
        .select_from(JournalLine)
        .join(JournalEntry, col(JournalEntry.id) == col(JournalLine.entry_id))
        .join(Account, col(Account.id) == col(JournalLine.account_id))
    )
    if on_or_before is not None:
        stmt = stmt.where(col(JournalEntry.entry_date) <= on_or_before)
    if before is not None:
        stmt = stmt.where(col(JournalEntry.entry_date) < before)
    if start is not None:
        stmt = stmt.where(col(JournalEntry.entry_date) >= start)
    if end is not None:
        stmt = stmt.where(col(JournalEntry.entry_date) <= end)
    if account_id is not None:
        stmt = stmt.where(col(JournalLine.account_id) == account_id)
    if account_types is not None:
        stmt = stmt.where(col(Account.type).in_([AccountType(t).value for t in account_types]))
    if exclude_sources is not None:
        excluded = [SourceType(t).value for t in exclude_sources]
        stmt = stmt.where(col(JournalEntry.source_type).not_in(excluded))
    recognized = recognized_lines(basis)
    if recognized is not None:
        stmt = stmt.where(recognized)
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


def account_totals(session: Session, **window: Any) -> list[AccountTotals]:
    """Debit and credit totals per account (only accounts with lines), in report order."""
    stmt = (
        line_selection(Account, func.sum(JournalLine.debit), func.sum(JournalLine.credit), **window)
        .group_by(col(Account.id))
        .order_by(*ACCOUNT_ORDER)
    )
    return [
        AccountTotals(account, debit or ZERO, credit or ZERO)
        for account, debit, credit in session.exec(stmt).all()
    ]


def debit_credit_totals(session: Session, **window: Any) -> tuple[Decimal, Decimal]:
    """Total debits and total credits of every line in the window (the trial-balance check)."""
    debit, credit = session.exec(
        line_selection(func.sum(JournalLine.debit), func.sum(JournalLine.credit), **window)
    ).one()
    return debit or ZERO, credit or ZERO


def earnings(session: Session, **window: Any) -> Decimal:
    """Net earnings of the window: Σ(credit − debit) over every income-statement line.

    Equals income + other income − COGS − expenses − other expenses (natural
    signs). A year-end close entry nets a closed year to zero here, because its
    lines on the income-statement accounts offset that year's activity.
    """
    net = session.exec(
        line_selection(
            func.sum(JournalLine.credit - JournalLine.debit),
            account_types=INCOME_STATEMENT_TYPES,
            **window,
        )
    ).one()
    return net or ZERO


def opening_balances(
    session: Session, before: date, account_ids: Iterable[int] | None = None
) -> dict[int, Decimal]:
    """Debit-minus-credit per account for entries dated before ``before``."""
    stmt = line_selection(
        JournalLine.account_id,
        func.sum(JournalLine.debit - JournalLine.credit),
        basis=LEDGER_BASIS,
        before=before,
    ).group_by(col(JournalLine.account_id))
    if account_ids is not None:
        stmt = stmt.where(col(JournalLine.account_id).in_(list(account_ids)))
    return {account_id: net for account_id, net in session.exec(stmt).all()}


def period_lines(session: Session, start: date, end: date, account_id: int | None = None):
    """Lines dated within ``[start, end]`` with their entry and account, in report order."""
    stmt = line_selection(
        JournalLine,
        JournalEntry,
        Account,
        basis=LEDGER_BASIS,
        start=start,
        end=end,
        account_id=account_id,
    ).order_by(*ACCOUNT_ORDER, *LINE_ORDER)
    return session.exec(stmt).all()


def journal_lines(session: Session, start: date, end: date):
    """Lines dated within ``[start, end]``, ordered entry by entry (date, source, number)."""
    stmt = line_selection(
        JournalLine, JournalEntry, Account, basis=LEDGER_BASIS, start=start, end=end
    ).order_by(*LINE_ORDER)
    return session.exec(stmt).all()
