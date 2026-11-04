"""
CSV and PDF export of the financial reports.

CSV (UTF-8, ``\\r\\n``): a title / period header block, a blank row, then one table.
Cells are typed. A ``str`` is *text* (account names, memos, labels) and goes through
:func:`csv_safe`, which neutralizes spreadsheet formula injection. A ``Decimal`` /
``int`` / ``date`` is written as-is, so a negative amount stays ``-12.00``. Zero
debits and credits are left blank, as on the pages.

PDF: :func:`to_pdf` renders an HTML string with WeasyPrint, which is given a URL
fetcher that always refuses, so no ``file://`` or network resource can ever be read.
WeasyPrint is imported lazily: it needs the native Pango libraries, and the CSV side
must work without them.
"""

import csv
import io
from datetime import date
from decimal import Decimal

from payroll.receivables.balances import AgingRow, ARAging
from payroll.reports.schemas import (
    GeneralLedger,
    JournalReport,
    Register,
    Statement,
    TrialBalance,
)

DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

Cell = str | Decimal | int | date | None


def csv_safe(value: str) -> str:
    """Prefix an apostrophe to text a spreadsheet would run as a formula."""
    return "'" + value if value.startswith(DANGEROUS_PREFIXES) else value


def _cell(value: Cell) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return csv_safe(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _amount(value: Decimal) -> Decimal | None:
    """A debit / credit cell: blank for zero."""
    return value if value else None


def _name(number: str | None, name: str) -> str:
    return f"{number} {name}" if number else name


def _trial_balance(trial: TrialBalance) -> list[list[Cell]]:
    rows: list[list[Cell]] = [
        ["Trial Balance"],
        ["As of", trial.as_of],
        [],
        ["Account", "Debit", "Credit"],
    ]
    rows += [[_name(r.number, r.name), _amount(r.debit), _amount(r.credit)] for r in trial.rows]
    rows.append(["Total", trial.total_debit, trial.total_credit])
    return rows


def _statement(statement: Statement) -> list[list[Cell]]:
    compared = len(statement.columns) > 1
    rows: list[list[Cell]] = [
        [statement.title],
        ["Basis", statement.basis.value.capitalize()],
        ["Note", statement.basis_note],
        [],
    ]
    header: list[Cell] = [statement.title, *(c.label for c in statement.columns)]
    if compared:
        header += ["Change", "% change"]
    rows.append(header)
    for row in statement.rows:
        line: list[Cell] = [row.label]
        if row.kind != "section":
            line += row.amounts
            if compared:
                line += [row.change, row.pct_change if row.pct_change is not None else "n/a"]
        rows.append(line)
    rows.append([])
    rows.append(["Checks"])
    rows += [[i.name, "OK" if i.ok else "FAILED", i.detail] for i in statement.invariants]
    return rows


def _register(register: Register) -> list[list[Cell]]:
    rows: list[list[Cell]] = [
        [],
        [_name(register.number, register.name)],
        ["Date", "Entry", "Memo", "Debit", "Credit", "Balance", "Status"],
        [None, None, "Opening balance", None, None, register.opening_balance],
    ]
    for line in register.lines:
        status = "Void" if line.is_void else "Reversal" if line.is_reversal else None
        rows.append(
            [
                line.entry_date,
                line.entry_no,
                line.memo,
                _amount(line.debit),
                _amount(line.credit),
                line.balance,
                status,
            ]
        )
    rows.append(
        [
            None,
            None,
            "Totals / closing balance",
            register.total_debit,
            register.total_credit,
            register.closing_balance,
        ]
    )
    return rows


def _general_ledger(ledger: GeneralLedger) -> list[list[Cell]]:
    rows: list[list[Cell]] = [["General Ledger"], ["Period", ledger.start, "to", ledger.end]]
    for register in ledger.accounts:
        rows += _register(register)
    return rows


def _journal(journal: JournalReport) -> list[list[Cell]]:
    rows: list[list[Cell]] = [
        ["Journal"],
        ["Period", journal.start, "to", journal.end],
        [],
        [
            "Date",
            "Entry",
            "Source",
            "Status",
            "Entry memo",
            "Account",
            "Line memo",
            "Debit",
            "Credit",
        ],
    ]
    for entry in journal.entries:
        status = "Void" if entry.is_void else "Reversal" if entry.is_reversal else "Posted"
        for line in entry.lines:
            rows.append(
                [
                    entry.entry_date,
                    entry.entry_no,
                    entry.source_type.value,
                    status,
                    entry.memo,
                    _name(line.account_number, line.account_name),
                    line.memo,
                    _amount(line.debit),
                    _amount(line.credit),
                ]
            )
    return rows


def _aging_row(row: AgingRow, name: str) -> list[Cell]:
    return [
        name,
        row.current,
        row.d1_30,
        row.d31_60,
        row.d61_90,
        row.d91_plus,
        row.total,
        row.credit,
    ]


def _ar_aging(aging: ARAging) -> list[list[Cell]]:
    rows: list[list[Cell]] = [
        ["A/R Aging"],
        ["As of", aging.as_of],
        [],
        ["Customer", "Current", "1-30", "31-60", "61-90", "91+", "Total", "Unapplied credit"],
    ]
    rows += [_aging_row(r, r.customer_name) for r in aging.rows]
    rows.append(_aging_row(aging.totals, "Total"))
    rows += [
        [],
        ["Open invoices"],
        [
            "Invoice",
            "Customer",
            "Invoice date",
            "Due date",
            "Days overdue",
            "Total",
            "Paid",
            "Balance",
        ],
    ]
    names = {r.customer_id: r.customer_name for r in aging.rows}
    for s in aging.invoices:
        inv = s.invoice
        rows.append(
            [
                inv.number,
                names.get(inv.customer_id, ""),
                inv.invoice_date,
                inv.due_date,
                s.days_overdue,
                inv.total,
                s.paid,
                s.balance,
            ]
        )
    rows += [
        [],
        ["Ledger A/R balance", aging.ledger_ar_balance],
        ["Difference", aging.difference],
        [],
        ["Checks"],
    ]
    rows += [[i.name, "OK" if i.ok else "FAILED", i.detail] for i in aging.invariants]
    return rows


def _table(report: object) -> list[list[Cell]]:
    if isinstance(report, Statement):
        return _statement(report)
    if isinstance(report, TrialBalance):
        return _trial_balance(report)
    if isinstance(report, GeneralLedger):
        return _general_ledger(report)
    if isinstance(report, JournalReport):
        return _journal(report)
    if isinstance(report, ARAging):
        return _ar_aging(report)
    raise TypeError(f"Cannot export {type(report).__name__} as CSV")


def to_csv(
    report: Statement | TrialBalance | GeneralLedger | JournalReport | ARAging,
) -> str:
    """The report as CSV text (UTF-8 by the caller, ``\\r\\n`` line ends)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    for row in _table(report):
        writer.writerow([_cell(c) for c in row])
    return buffer.getvalue()


class FetchBlockedError(Exception):
    """Raised by :class:`NoFetch` for every resource a document asks for."""


class NoFetch:
    """A WeasyPrint URL fetcher that refuses everything (``file:``, ``http:``, ``data:``).

    WeasyPrint 68+ expects a callable taking the URL and carrying ``_fail_on_errors``;
    ``False`` makes a refused resource a logged warning (it is skipped) rather than
    aborting the render.
    """

    _fail_on_errors = False

    def __call__(self, url: str, *args: object, **kwargs: object) -> object:
        raise FetchBlockedError("blocked: PDF rendering never fetches resources")


def to_pdf(html: str) -> bytes:
    """Render ``html`` (a complete document) to PDF bytes without fetching anything."""
    import weasyprint  # lazy: needs the native Pango libraries

    return weasyprint.HTML(string=html, url_fetcher=NoFetch()).write_pdf()
