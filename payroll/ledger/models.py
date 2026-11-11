"""
Double-entry ledger.

Posted rows are immutable: database triggers (see the ``ledger_core`` migration)
reject any change to ``journal_line`` and allow ``journal_entry`` only the
``posted`` -> ``void`` status change. Balances are always computed from
``journal_line`` rows. Money is ``Decimal`` (``NUMERIC(14, 2)``), never ``float``.
Enum-like columns are stored as plain strings (validated in the service layer).
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlmodel import Field, SQLModel

from payroll.auth.models import utcnow
from payroll.common.models import AppBaseModel, money_field


class AccountType(StrEnum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    INCOME = "income"
    COGS = "cogs"
    EXPENSE = "expense"
    OTHER_INCOME = "other_income"
    OTHER_EXPENSE = "other_expense"


NORMAL_DEBIT: frozenset[AccountType] = frozenset(
    {AccountType.ASSET, AccountType.COGS, AccountType.EXPENSE, AccountType.OTHER_EXPENSE}
)


class SystemRole(StrEnum):
    ACCOUNTS_RECEIVABLE = "accounts_receivable"
    UNDEPOSITED_FUNDS = "undeposited_funds"
    DUE_TO_OWNER = "due_to_owner"
    RETAINED_EARNINGS = "retained_earnings"
    OPENING_BALANCE_EQUITY = "opening_balance_equity"
    PAYROLL_WAGES = "payroll_wages"
    PAYROLL_TAX_EXPENSE = "payroll_tax_expense"
    PAYROLL_FEDERAL_PAYABLE = "payroll_federal_payable"
    PAYROLL_GA_WH_PAYABLE = "payroll_ga_wh_payable"
    PAYROLL_FUTA_PAYABLE = "payroll_futa_payable"
    PAYROLL_SUTA_PAYABLE = "payroll_suta_payable"
    PAYROLL_HEALTH_PAYABLE = "payroll_health_payable"
    PAYROLL_HSA_PAYABLE = "payroll_hsa_payable"


class SourceType(StrEnum):
    MANUAL = "manual"
    INVOICE = "invoice"
    PAYMENT = "payment"
    DEPOSIT = "deposit"
    REFUND = "refund"
    EXPENSE = "expense"
    REIMBURSEMENT = "reimbursement"
    TRANSFER = "transfer"
    PAYROLL_RUN = "payroll_run"
    TAX_PAYMENT = "tax_payment"
    OPENING_BALANCE = "opening_balance"
    YEAR_END_CLOSE = "year_end_close"


class Account(AppBaseModel, table=True):
    __tablename__ = "account"
    __table_args__ = (
        Index(
            "uq_account_number",
            "number",
            unique=True,
            postgresql_where=sa.text("number IS NOT NULL"),
        ),
        Index(
            "uq_account_parent_name",
            sa.text("coalesce(parent_id, 0)"),
            sa.text("lower(name)"),
            unique=True,
        ),
        Index(
            "uq_account_system_role",
            "system_role",
            unique=True,
            postgresql_where=sa.text("system_role IS NOT NULL"),
        ),
    )

    number: str | None = Field(default=None, max_length=20)
    name: str = Field(max_length=200)
    parent_id: int | None = Field(default=None, foreign_key="account.id", ondelete="RESTRICT")
    type: AccountType = Field(sa_type=sa.String(30))
    detail_type: str | None = Field(default=None, max_length=100)
    is_active: bool = Field(default=True)
    system_role: SystemRole | None = Field(default=None, sa_type=sa.String(30))


class JournalEntry(AppBaseModel, table=True):
    __tablename__ = "journal_entry"
    __table_args__ = (
        UniqueConstraint("source_type", "entry_no", name="uq_journal_entry_source_no"),
        Index(
            "uq_journal_entry_reverses",
            "reverses_entry_id",
            unique=True,
            postgresql_where=sa.text("reverses_entry_id IS NOT NULL"),
        ),
        Index("ix_journal_entry_entry_date", "entry_date"),
        CheckConstraint("status IN ('posted', 'void')", name="ck_journal_entry_status"),
    )

    entry_no: int
    source_type: SourceType = Field(sa_type=sa.String(30))
    source_id: str | None = Field(default=None, max_length=100)
    entry_date: date
    memo: str | None = Field(default=None, max_length=500)
    status: str = Field(default="posted", max_length=10)
    reverses_entry_id: int | None = Field(
        default=None, foreign_key="journal_entry.id", ondelete="RESTRICT"
    )
    posted_at: datetime = Field(default_factory=utcnow, nullable=False)
    posted_by: int = Field(foreign_key="app_user.id", ondelete="RESTRICT")
    # Stamped by a database trigger on insert (txid_current()); lets the database
    # reject lines added to an entry after the transaction that created it.
    created_txid: int | None = Field(
        default=None, sa_column=sa.Column(sa.BigInteger, nullable=False)
    )


class JournalLine(AppBaseModel, table=True):
    __tablename__ = "journal_line"
    __table_args__ = (
        UniqueConstraint("entry_id", "line_no", name="uq_journal_line_entry_line"),
        Index("ix_journal_line_account_id", "account_id"),
        CheckConstraint(
            "(debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0)",
            name="ck_journal_line_one_side",
        ),
    )

    entry_id: int = Field(foreign_key="journal_entry.id", ondelete="RESTRICT")
    line_no: int
    account_id: int = Field(foreign_key="account.id", ondelete="RESTRICT")
    debit: Decimal = money_field()
    credit: Decimal = money_field()
    memo: str | None = Field(default=None, max_length=500)


class EntryCounter(SQLModel, table=True):
    __tablename__ = "entry_counter"

    source_type: SourceType = Field(sa_type=sa.String(30), primary_key=True)
    last_no: int = Field(default=0)


class PeriodLock(SQLModel, table=True):
    __tablename__ = "period_lock"

    id: int = Field(default=1, primary_key=True)
    lock_date: date | None = Field(default=None)
    set_by: int | None = Field(default=None, foreign_key="app_user.id", ondelete="RESTRICT")
    set_at: datetime | None = Field(default=None)
