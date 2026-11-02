from datetime import date
from typing import Literal

from payroll.common.money import Money
from payroll.common.schemas import AppBaseSchema
from payroll.ledger.models import AccountType, SourceType


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
    line_no: int
    source_type: SourceType
    entry_date: date
    memo: str | None
    debit: Money
    credit: Money
    balance: Money
    is_void: bool
    is_reversal: bool


class Register(AppBaseSchema):
    account_id: int
    number: str | None
    name: str
    type: AccountType
    normal_balance: Literal["debit", "credit"]
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
