"""Report service: ledger reports computed from ``journal_line`` (read-only)."""

from datetime import date
from decimal import Decimal

from sqlmodel import Session, select

from payroll.ledger.accounts import AccountService
from payroll.ledger.models import Account, JournalEntry, JournalLine
from payroll.reports import queries
from payroll.reports.periods import InvalidPeriodError
from payroll.reports.schemas import (
    GeneralLedger,
    JournalReportEntry,
    JournalReportLine,
    Register,
    RegisterLine,
    TrialBalance,
    TrialBalanceRow,
)

ZERO = queries.ZERO


def _check_period(start: date, end: date) -> None:
    if start > end:
        raise InvalidPeriodError(message="The start date must not be after the end date")


class ReportService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Trial balance
    # ------------------------------------------------------------------

    def trial_balance(self, as_of: date, *, include_zero: bool = False) -> TrialBalance:
        """Each account's net balance as of ``as_of`` in exactly one of debit / credit."""
        nets: dict[int, tuple[Account, Decimal]] = {
            t.account.id: (t.account, t.debit - t.credit)
            for t in queries.account_totals(self.session, on_or_before=as_of)
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
