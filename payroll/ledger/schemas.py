from decimal import Decimal
from typing import Literal

from pydantic import Field

from payroll.common.money import Money
from payroll.common.schemas import AppBaseSchema
from payroll.ledger.models import AccountType, SystemRole


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


ZERO = Decimal("0.00")
