"""Report service: ledger reports computed from ``journal_line`` (read-only)."""

import calendar
from collections.abc import Iterable, Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from sqlmodel import Session, select

from payroll.ledger.accounts import AccountService
from payroll.ledger.models import Account, AccountType, JournalEntry, JournalLine
from payroll.reports import queries
from payroll.reports.basis import BASIS_NOTE, DEFAULT_BASIS, Basis
from payroll.reports.periods import InvalidPeriodError, Period, previous_period, previous_year
from payroll.reports.schemas import (
    ColumnHeader,
    GeneralLedger,
    Invariant,
    JournalReportEntry,
    JournalReportLine,
    Register,
    RegisterLine,
    Statement,
    StatementRow,
    TrialBalance,
    TrialBalanceRow,
)

ZERO = queries.ZERO
COMPARISONS: tuple[str, ...] = ("previous_period", "previous_year")
PERCENT = Decimal("0.1")
HUNDRED = Decimal(100)

RowKind = Literal["section", "account", "subtotal", "total"]
Amounts = list[Decimal]


def pct_change(change: Decimal, compare: Decimal) -> Decimal | None:
    """``change / |compare| × 100`` rounded half-up to 0.1; ``None`` when ``compare`` is 0."""
    if compare == 0:
        return None
    return (change / abs(compare) * HUNDRED).quantize(PERCENT, rounding=ROUND_HALF_UP)


def fiscal_year_start(day: date) -> date:
    """First day of the fiscal year containing ``day`` (the fiscal year is the calendar year)."""
    return date(day.year, 1, 1)


def _window_label(start: date, end: date) -> str:
    # Built from the dates, never from Period.label (comparison labels would chain).
    return f"{start.isoformat()} to {end.isoformat()}"


def _check_compare(compare: str | None) -> None:
    if compare is not None and compare not in COMPARISONS:
        raise InvalidPeriodError(message=f"Unknown comparison '{compare}'")


def _month_earlier(day: date) -> date:
    """The same day one month earlier; a month end maps to the previous month end."""
    year, month = (day.year, day.month - 1) if day.month > 1 else (day.year - 1, 12)
    last = calendar.monthrange(year, month)[1]
    if day.day == calendar.monthrange(day.year, day.month)[1]:
        return date(year, month, last)
    return date(year, month, min(day.day, last))


def _row(
    kind: RowKind,
    label: str,
    amounts: Amounts,
    *,
    depth: int = 0,
    account_id: int | None = None,
) -> StatementRow:
    change = pct = None
    if len(amounts) == 2:
        change = amounts[0] - amounts[1]
        pct = pct_change(change, amounts[1])
    return StatementRow(
        kind=kind,
        label=label,
        account_id=account_id,
        depth=depth,
        amounts=amounts,
        change=change,
        pct_change=pct,
    )


def _sum(*columns: Amounts, signs: Sequence[int] | None = None) -> Amounts:
    """Column-wise ``Σ sign × amounts`` (signs default to all +1)."""
    signs = signs or [1] * len(columns)
    return [
        sum((s * c for s, c in zip(signs, values, strict=True)), ZERO) for values in zip(*columns)
    ]


def _equation(as_of: date, left: str, a: Decimal, right: str, b: Decimal) -> str:
    if a == b:
        return f"As of {as_of.isoformat()}: {left} {a} = {right} {b}"
    return f"As of {as_of.isoformat()}: {left} {a} ≠ {right} {b} (difference {a - b})"


def _check_period(start: date, end: date) -> None:
    if start > end:
        raise InvalidPeriodError(message="The start date must not be after the end date")


class ReportService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Profit and Loss / Balance Sheet
    # ------------------------------------------------------------------

    def profit_and_loss(
        self, period: Period, *, basis: Basis = DEFAULT_BASIS, compare: str | None = None
    ) -> Statement:
        """Income-statement sections for ``period`` plus an optional comparison column."""
        _check_period(period.start, period.end)
        _check_compare(compare)
        basis = Basis(basis)
        windows = [period]
        if compare == "previous_period":
            windows.append(previous_period(period))
        elif compare == "previous_year":
            windows.append(previous_year(period))
        per_column = [
            # A year-end close inside the window would net every account to zero.
            self._natural_totals(
                basis,
                queries.INCOME_STATEMENT_TYPES,
                start=window.start,
                end=window.end,
                exclude_sources=queries.PNL_EXCLUDED_SOURCES,
            )
            for window in windows
        ]
        accounts = AccountService(self.session).list(include_inactive=True)

        def section(label: str, type_: AccountType) -> tuple[list[StatementRow], Amounts]:
            return self._section(label, f"Total {label}", "subtotal", [type_], accounts, per_column)

        income_rows, income = section("Income", AccountType.INCOME)
        cogs_rows, cogs = section("Cost of Goods Sold", AccountType.COGS)
        expense_rows, expenses = section("Expenses", AccountType.EXPENSE)
        other_income_rows, other_income = section("Other Income", AccountType.OTHER_INCOME)
        other_expense_rows, other_expenses = section("Other Expenses", AccountType.OTHER_EXPENSE)
        gross_profit = _sum(income, cogs, signs=[1, -1])
        operating = _sum(gross_profit, expenses, signs=[1, -1])
        net_income = _sum(operating, other_income, other_expenses, signs=[1, 1, -1])
        rows = [
            *income_rows,
            *cogs_rows,
            _row("total", "Gross Profit", gross_profit),
            *expense_rows,
            _row("total", "Net Operating Income", operating),
            *other_income_rows,
            *other_expense_rows,
            _row("total", "Net Income", net_income),
        ]
        ends = [window.end for window in windows]
        _, assets, liabilities_equity = self._balance_sheet_rows(ends, basis, accounts)
        return Statement(
            title="Profit and Loss",
            basis=basis,
            columns=[
                ColumnHeader(label=_window_label(w.start, w.end), start=w.start, end=w.end)
                for w in windows
            ],
            rows=rows,
            invariants=self._invariants(ends, assets, liabilities_equity),
            basis_note=BASIS_NOTE,
        )

    def balance_sheet(
        self, as_of: date, *, basis: Basis = DEFAULT_BASIS, compare: str | None = None
    ) -> Statement:
        """Assets, liabilities and equity (with synthesized earnings) as of ``as_of``."""
        _check_compare(compare)
        basis = Basis(basis)
        ends = [as_of]
        if compare == "previous_period":
            ends.append(_month_earlier(as_of))
        elif compare == "previous_year":
            ends.append(previous_year(Period(as_of, as_of, "")).end)
        accounts = AccountService(self.session).list(include_inactive=True)
        rows, assets, liabilities_equity = self._balance_sheet_rows(ends, basis, accounts)
        return Statement(
            title="Balance Sheet",
            basis=basis,
            columns=[
                ColumnHeader(
                    label=f"As of {end.isoformat()}", start=fiscal_year_start(end), end=end
                )
                for end in ends
            ],
            rows=rows,
            invariants=self._invariants(ends, assets, liabilities_equity),
            basis_note=BASIS_NOTE,
        )

    def _balance_sheet_rows(
        self, ends: list[date], basis: Basis, accounts: list[Account]
    ) -> tuple[list[StatementRow], Amounts, Amounts]:
        """Balance-sheet rows for each as-of date, with Total Assets and Liabilities + Equity."""
        per_column = [
            self._natural_totals(basis, queries.BALANCE_SHEET_TYPES, on_or_before=end)
            for end in ends
        ]
        asset_rows, assets = self._section(
            "Assets", "Total Assets", "total", [AccountType.ASSET], accounts, per_column, True
        )
        liability_rows, liabilities = self._section(
            "Liabilities",
            "Total Liabilities",
            "subtotal",
            [AccountType.LIABILITY],
            accounts,
            per_column,
            True,
        )
        equity_rows, equity_accounts = self._section(
            "Equity", "Total Equity", "subtotal", [AccountType.EQUITY], accounts, per_column, True
        )
        # Earnings not yet closed into equity accounts: everything before the fiscal
        # year of each column (correct with or without a year-end close entry, since a
        # close nets the closed year to zero) and the fiscal year to date.
        prior_earnings = [
            queries.earnings(self.session, before=fiscal_year_start(end), basis=basis)
            for end in ends
        ]
        net_income = [
            queries.earnings(self.session, start=fiscal_year_start(end), end=end, basis=basis)
            for end in ends
        ]
        synthesized = [
            _row("account", label, values, depth=1)
            for label, values in (
                ("Prior Years Earnings", prior_earnings),
                ("Net Income", net_income),
            )
            if any(values)
        ]
        equity = _sum(equity_accounts, prior_earnings, net_income)
        liabilities_equity = _sum(liabilities, equity)
        rows = [
            *asset_rows,
            *liability_rows,
            *equity_rows[:-1],
            *synthesized,
            _row("subtotal", "Total Equity", equity),
            _row("total", "Liabilities + Equity", liabilities_equity),
        ]
        return rows, assets, liabilities_equity

    def _natural_totals(
        self, basis: Basis, types: Iterable[AccountType], **window: Any
    ) -> dict[int, Decimal]:
        """Natural-sign net per account with lines in the window (presence = activity)."""
        return {
            t.account.id: queries.natural(t.account.type, t.debit, t.credit)
            for t in queries.account_totals(
                self.session, basis=basis, account_types=types, **window
            )
        }

    @staticmethod
    def _section(
        label: str,
        total_label: str,
        total_kind: RowKind,
        types: list[AccountType],
        accounts: list[Account],
        per_column: list[dict[int, Decimal]],
        always: bool = False,
    ) -> tuple[list[StatementRow], Amounts]:
        """Heading, account rows (parents rolled up over their children) and the total row.

        Accounts with no lines in any column are hidden (a zero balance with activity
        stays). A section without visible accounts is omitted unless ``always``.
        """
        members = [a for a in accounts if a.type in types]
        member_ids = {a.id for a in members}
        children: dict[int | None, list[Account]] = {}
        for account in members:
            # A sub-account shares its parent's type; anything else is listed at the top.
            parent = account.parent_id if account.parent_id in member_ids else None
            children.setdefault(parent, []).append(account)
        rows: list[StatementRow] = []

        def walk(account: Account, depth: int) -> tuple[Amounts, bool]:
            position = len(rows)
            own = [column.get(account.id, ZERO) for column in per_column]
            active = any(account.id in column for column in per_column)
            rolled = own
            for child in children.get(account.id, []):
                child_amounts, child_visible = walk(child, depth + 1)
                rolled = _sum(rolled, child_amounts)
                active = active or child_visible
            if active:
                row = _row("account", account.name, rolled, depth=depth, account_id=account.id)
                rows.insert(position, row)
            return rolled, active

        total = [ZERO] * len(per_column)
        for root in children.get(None, []):
            amounts, _ = walk(root, 1)
            total = _sum(total, amounts)
        if not rows and not always:
            return [], total
        return [_row("section", label, []), *rows, _row(total_kind, total_label, total)], total

    def _invariants(
        self, ends: list[date], assets: Amounts, liabilities_equity: Amounts
    ) -> list[Invariant]:
        """Report-level checks, computed every time and reported (never raised).

        The trial-balance check reads the ledger as posted (no basis predicate).
        """
        balance = [
            (end, a, le) for end, a, le in zip(ends, assets, liabilities_equity, strict=True)
        ]
        trial = [
            (
                end,
                *queries.debit_credit_totals(
                    self.session, on_or_before=end, basis=queries.LEDGER_BASIS
                ),
            )
            for end in ends
        ]
        return [
            Invariant(
                name="assets_equal_liabilities_plus_equity",
                ok=all(a == le for _, a, le in balance),
                detail="; ".join(
                    _equation(end, "assets", a, "liabilities + equity", le)
                    for end, a, le in balance
                ),
            ),
            Invariant(
                name="trial_balance_balanced",
                ok=all(debit == credit for _, debit, credit in trial),
                detail="; ".join(
                    _equation(end, "debits", debit, "credits", credit)
                    for end, debit, credit in trial
                ),
            ),
        ]

    # ------------------------------------------------------------------
    # Trial balance
    # ------------------------------------------------------------------

    def trial_balance(self, as_of: date, *, include_zero: bool = False) -> TrialBalance:
        """Each account's net balance as of ``as_of`` in exactly one of debit / credit."""
        nets: dict[int, tuple[Account, Decimal]] = {
            t.account.id: (t.account, t.debit - t.credit)
            for t in queries.account_totals(
                self.session, on_or_before=as_of, basis=queries.LEDGER_BASIS
            )
        }
        if include_zero:
            for account in AccountService(self.session).list(include_inactive=True):
                nets.setdefault(account.id, (account, ZERO))
        rows = [
            TrialBalanceRow(
                account_id=account.id,
                number=account.number,
                name=account.name,
                type=account.type,
                debit=net if net > 0 else ZERO,
                credit=-net if net < 0 else ZERO,
            )
            for account, net in nets.values()
            if include_zero or net != 0
        ]
        rows.sort(key=lambda r: (r.number is None, r.number or "", r.name, r.account_id))
        total_debit = sum((r.debit for r in rows), ZERO)
        total_credit = sum((r.credit for r in rows), ZERO)
        return TrialBalance(
            as_of=as_of,
            rows=rows,
            total_debit=total_debit,
            total_credit=total_credit,
            balanced=total_debit == total_credit,
        )

    # ------------------------------------------------------------------
    # Registers and general ledger
    # ------------------------------------------------------------------

    def account_register(self, account_id: int, start: date, end: date) -> Register:
        _check_period(start, end)
        account = AccountService(self.session).get(account_id)
        opening = queries.opening_balances(self.session, start, [account_id])
        rows = queries.period_lines(self.session, start, end, account_id)
        return self._registers([account], rows, opening, start, end)[0]

    def general_ledger(
        self, start: date, end: date, account_id: int | None = None
    ) -> GeneralLedger:
        """One register per account with activity in the period or an opening balance."""
        _check_period(start, end)
        if account_id is not None:
            return GeneralLedger(
                start=start, end=end, accounts=[self.account_register(account_id, start, end)]
            )
        opening = {a: n for a, n in queries.opening_balances(self.session, start).items() if n != 0}
        rows = queries.period_lines(self.session, start, end)
        accounts = {line_account.id: line_account for _, _, line_account in rows}
        missing = [a for a in opening if a not in accounts]
        if missing:
            for account in self.session.exec(select(Account).where(Account.id.in_(missing))):  # type: ignore[attr-defined]
                accounts[account.id] = account
        ordered = sorted(
            accounts.values(), key=lambda a: (a.number is None, a.number or "", a.name, a.id)
        )
        return GeneralLedger(
            start=start, end=end, accounts=self._registers(ordered, rows, opening, start, end)
        )

    @staticmethod
    def _registers(
        accounts: list[Account],
        rows: list[tuple[JournalLine, JournalEntry, Account]],
        opening: dict[int, Decimal],
        start: date,
        end: date,
    ) -> list[Register]:
        by_account: dict[int, list[tuple[JournalLine, JournalEntry]]] = {a.id: [] for a in accounts}
        for line, entry, account in rows:
            by_account[account.id].append((line, entry))
        registers = []
        for account in accounts:
            debit_normal = queries.is_debit_normal(account.type)
            opening_balance = queries.natural(account.type, opening.get(account.id, ZERO), ZERO)
            running = opening_balance
            lines = []
            for line, entry in by_account[account.id]:
                running += queries.natural(account.type, line.debit, line.credit)
                lines.append(
                    RegisterLine(
                        entry_id=entry.id,
                        entry_no=entry.entry_no,
                        line_no=line.line_no,
                        source_type=entry.source_type,
                        entry_date=entry.entry_date,
                        memo=line.memo or entry.memo,
                        debit=line.debit,
                        credit=line.credit,
                        balance=running,
                        is_void=entry.status == "void",
                        is_reversal=entry.reverses_entry_id is not None,
                    )
                )
            registers.append(
                Register(
                    account_id=account.id,
                    number=account.number,
                    name=account.name,
                    type=account.type,
                    normal_balance="debit" if debit_normal else "credit",
                    start=start,
                    end=end,
                    opening_balance=opening_balance,
                    lines=lines,
                    total_debit=sum((ln.debit for ln in lines), ZERO),
                    total_credit=sum((ln.credit for ln in lines), ZERO),
                    closing_balance=running,
                )
            )
        return registers

    # ------------------------------------------------------------------
    # Journal
    # ------------------------------------------------------------------

    def journal(self, start: date, end: date) -> list[JournalReportEntry]:
        """Every entry dated in the period with all its lines, by date, source, number."""
        _check_period(start, end)
        entries: dict[int, JournalReportEntry] = {}
        for line, entry, account in queries.journal_lines(self.session, start, end):
            report = entries.get(entry.id)
            if report is None:
                report = entries[entry.id] = JournalReportEntry(
                    id=entry.id,
                    entry_no=entry.entry_no,
                    source_type=entry.source_type,
                    entry_date=entry.entry_date,
                    memo=entry.memo,
                    status=entry.status,
                    is_void=entry.status == "void",
                    is_reversal=entry.reverses_entry_id is not None,
                    reverses_entry_id=entry.reverses_entry_id,
                    total_debit=ZERO,
                    total_credit=ZERO,
                    lines=[],
                )
            report.lines.append(
                JournalReportLine(
                    line_no=line.line_no,
                    account_id=account.id,
                    account_number=account.number,
                    account_name=account.name,
                    debit=line.debit,
                    credit=line.credit,
                    memo=line.memo,
                )
            )
            report.total_debit += line.debit
            report.total_credit += line.credit
        return list(entries.values())
