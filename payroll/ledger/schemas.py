from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field

from payroll.common.money import Money
from payroll.common.schemas import AppBaseSchema
from payroll.ledger.models import AccountType, SourceType, SystemRole

ZERO = Decimal("0.00")


class AccountRead(AppBaseSchema):
    id: int
    number: str | None
    name: str
    full_name: str
    parent_id: int | None
    type: AccountType
    detail_type: str | None
    is_active: bool
    system_role: SystemRole | None
    normal_balance: Literal["debit", "credit"]
    balance: Money = Field(description="Balance on the account's normal side")


class AccountNodeRead(AccountRead):
    depth: int


class AccountCreate(AppBaseSchema):
    name: str = Field(min_length=1, max_length=200)
    type: AccountType
    number: str | None = Field(default=None, max_length=20)
    parent_id: int | None = None
    detail_type: str | None = Field(default=None, max_length=100)


class AccountUpdate(AppBaseSchema):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    type: AccountType | None = None
    number: str | None = Field(default=None, max_length=20)
    parent_id: int | None = None  # explicit null makes the account top-level
    detail_type: str | None = Field(default=None, max_length=100)


class SystemRoleBind(AppBaseSchema):
    account_id: int | None = None  # null unbinds


class JournalLineCreate(AppBaseSchema):
    account_id: int
    debit: Money = ZERO
    credit: Money = ZERO
    memo: str | None = Field(default=None, max_length=500)


class JournalEntryCreate(AppBaseSchema):
    entry_date: date
    memo: str | None = Field(default=None, max_length=500)
    lines: list[JournalLineCreate]


class JournalEntryAction(AppBaseSchema):
    """Body of a reverse or void request: the reversing entry's date and memo."""

    entry_date: date
    memo: str | None = Field(default=None, max_length=500)


class JournalLineRead(AppBaseSchema):
    line_no: int
    account_id: int
    account_number: str | None
    account_name: str
    debit: Money
    credit: Money
    memo: str | None


class JournalEntryRead(AppBaseSchema):
    id: int
    entry_no: int
    source_type: SourceType
    source_id: str | None
    entry_date: date
    memo: str | None
    status: Literal["posted", "void"]
    reverses_entry_id: int | None
    reversed_by_entry_id: int | None
    posted_at: datetime
    posted_by: int
    total_debit: Money
    total_credit: Money
    lines: list[JournalLineRead]
