from datetime import date
from decimal import Decimal
from typing import Literal

from payroll.common.money import Money
from payroll.common.schemas import AppBaseSchema
from payroll.ledger.models import AccountType, SourceType
from payroll.reports.basis import Basis


class TrialBalanceRow(AppBaseSchema):
    account_id: int
    number: str | None
    name: str
    type: AccountType
    debit: Money
    credit: Money


class TrialBalance(AppBaseSchema):
    as_of: date
    rows: list[TrialBalanceRow]
    total_debit: Money
    total_credit: Money
    balanced: bool


class RegisterLine(AppBaseSchema):
    entry_id: int
    entry_no: int
    line_no: int | None  # None on a cash-basis application row
    source_type: SourceType
    entry_date: date
    memo: str | None
    debit: Money
    credit: Money
    balance: Money
    is_void: bool
    is_reversal: bool
    # Set on a cash-basis application row: the payment (``entry_id`` is its entry) and
    # the invoice the payment was applied to.
    payment_id: int | None = None
    invoice_id: int | None = None


class Register(AppBaseSchema):
    account_id: int
    number: str | None
    name: str
    type: AccountType
    normal_balance: Literal["debit", "credit"]
    basis: Literal["ledger", "cash", "accrual"] = "ledger"
    start: date
    end: date
    opening_balance: Money
    lines: list[RegisterLine]
    total_debit: Money
    total_credit: Money
    closing_balance: Money


class GeneralLedger(AppBaseSchema):
    start: date
    end: date
    accounts: list[Register]


class JournalReportLine(AppBaseSchema):
    line_no: int
    account_id: int
    account_number: str | None
    account_name: str
    debit: Money
    credit: Money
    memo: str | None


class JournalReportEntry(AppBaseSchema):
    id: int
    entry_no: int
    source_type: SourceType
    entry_date: date
    memo: str | None
    status: Literal["posted", "void"]
    is_void: bool
    is_reversal: bool
    reverses_entry_id: int | None
    total_debit: Money
    total_credit: Money
    lines: list[JournalReportLine]


class ColumnHeader(AppBaseSchema):
    """One amount column: the period it covers (a balance sheet's runs fiscal-year start to as-of)."""

    label: str
    start: date
    end: date


class StatementRow(AppBaseSchema):
    """A statement line; ``amounts`` has one value per column (empty for a section heading).

    ``change`` / ``pct_change`` compare the first column with the second and are
    ``None`` without a comparison column; ``pct_change`` is also ``None`` when
    the comparison amount is zero.
    """

    kind: Literal["section", "account", "subtotal", "total"]
    label: str
    account_id: int | None
    depth: int
    amounts: list[Money]
    change: Money | None
    pct_change: Decimal | None


class Invariant(AppBaseSchema):
    name: str
    ok: bool
    detail: str


class Statement(AppBaseSchema):
    title: str
    basis: Basis
    columns: list[ColumnHeader]
    rows: list[StatementRow]
    invariants: list[Invariant]
    basis_note: str


class JournalReport(AppBaseSchema):
    """The journal report with its period (the JSON endpoint returns just ``entries``)."""

    start: date
    end: date
    entries: list[JournalReportEntry]
