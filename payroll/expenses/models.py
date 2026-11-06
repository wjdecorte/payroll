"""
Expenses: vendors, expenses, transfers, owner reimbursements and attachments.

Documents are stored here; their money movements are journal entries posted through
``LedgerPostingService`` (``entry_id``). Integrity beyond column CHECKs lives in
database triggers (see the ``expenses`` migration): expense lines must sum to the
expense total and a reimbursement's expenses to its total (deferred, at COMMIT),
``expense.reimbursement_id`` is set-once, and expense lines and attachments are
append-only. Enum-like columns are plain strings.
"""

import datetime as dt
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlmodel import Field, SQLModel

from payroll.common.models import AppBaseModel, money_field

# Largest attachment the database accepts (10 MB); the service enforces it earlier.
MAX_ATTACHMENT_BYTES = 10_485_760


def _status_check(table: str) -> tuple[CheckConstraint, CheckConstraint]:
    """Posted documents carry no void fields; void ones carry all of them."""
    return (
        CheckConstraint("status IN ('posted', 'void')", name=f"ck_{table}_status"),
        CheckConstraint(
            "(status = 'posted' AND void_entry_id IS NULL AND void_date IS NULL) "
            "OR (status = 'void' AND entry_id IS NOT NULL "
            "AND void_entry_id IS NOT NULL AND void_date IS NOT NULL)",
            name=f"ck_{table}_void_fields",
        ),
    )


class Vendor(AppBaseModel, table=True):
    __tablename__ = "vendor"
    __table_args__ = (Index("uq_vendor_name", sa.text("lower(name)"), unique=True),)

    name: str = Field(max_length=200)
    note: str | None = Field(default=None, max_length=500)
    active: bool = Field(default=True)


class Expense(AppBaseModel, table=True):
    __tablename__ = "expense"
    __table_args__ = (
        Index("ix_expense_vendor_id", "vendor_id"),
        Index("ix_expense_reimbursement_id", "reimbursement_id"),
        CheckConstraint("(vendor_id IS NULL) <> (payee_text IS NULL)", name="ck_expense_payee"),
        CheckConstraint("source IN ('account', 'owner')", name="ck_expense_source"),
        CheckConstraint(
            "(source = 'owner') = (paid_from_account_id IS NULL)", name="ck_expense_source_account"
        ),
        CheckConstraint("total > 0", name="ck_expense_total"),
        CheckConstraint("void_date IS NULL OR void_date >= date", name="ck_expense_void_date"),
        *_status_check("expense"),
    )

    date: dt.date
    vendor_id: int | None = Field(default=None, foreign_key="vendor.id", ondelete="RESTRICT")
    payee_text: str | None = Field(default=None, max_length=200)
    memo: str | None = Field(default=None, max_length=500)
    source: str = Field(default="account", max_length=10)
    paid_from_account_id: int | None = Field(
        default=None, foreign_key="account.id", ondelete="RESTRICT"
    )
    total: Decimal = money_field()
    status: str = Field(default="posted", max_length=10)
    entry_id: int | None = Field(default=None, foreign_key="journal_entry.id", ondelete="RESTRICT")
    void_entry_id: int | None = Field(
        default=None, foreign_key="journal_entry.id", ondelete="RESTRICT"
    )
    void_date: dt.date | None = Field(default=None)
    reimbursement_id: int | None = Field(
        default=None, foreign_key="reimbursement.id", ondelete="RESTRICT"
    )


class ExpenseLine(AppBaseModel, table=True):
    __tablename__ = "expense_line"
    __table_args__ = (
        UniqueConstraint("expense_id", "line_no", name="uq_expense_line_expense_line"),
        CheckConstraint("amount > 0", name="ck_expense_line_amount"),
    )

    expense_id: int = Field(foreign_key="expense.id", ondelete="RESTRICT")
    line_no: int
    account_id: int = Field(foreign_key="account.id", ondelete="RESTRICT")
    memo: str | None = Field(default=None, max_length=500)
    amount: Decimal = money_field()


class Transfer(AppBaseModel, table=True):
    __tablename__ = "transfer"
    __table_args__ = (
        CheckConstraint("from_account_id <> to_account_id", name="ck_transfer_accounts"),
        CheckConstraint("amount > 0", name="ck_transfer_amount"),
        CheckConstraint("void_date IS NULL OR void_date >= date", name="ck_transfer_void_date"),
        *_status_check("transfer"),
    )

    date: dt.date
    from_account_id: int = Field(foreign_key="account.id", ondelete="RESTRICT")
    to_account_id: int = Field(foreign_key="account.id", ondelete="RESTRICT")
    amount: Decimal = money_field()
    memo: str | None = Field(default=None, max_length=500)
    status: str = Field(default="posted", max_length=10)
    entry_id: int | None = Field(default=None, foreign_key="journal_entry.id", ondelete="RESTRICT")
    void_entry_id: int | None = Field(
        default=None, foreign_key="journal_entry.id", ondelete="RESTRICT"
    )
    void_date: dt.date | None = Field(default=None)


class Reimbursement(AppBaseModel, table=True):
    __tablename__ = "reimbursement"
    __table_args__ = (
        CheckConstraint("total > 0", name="ck_reimbursement_total"),
        CheckConstraint(
            "void_date IS NULL OR void_date >= date", name="ck_reimbursement_void_date"
        ),
        *_status_check("reimbursement"),
    )

    date: dt.date
    paid_from_account_id: int = Field(foreign_key="account.id", ondelete="RESTRICT")
    total: Decimal = money_field()
    memo: str | None = Field(default=None, max_length=500)
    status: str = Field(default="posted", max_length=10)
    entry_id: int | None = Field(default=None, foreign_key="journal_entry.id", ondelete="RESTRICT")
    void_entry_id: int | None = Field(
        default=None, foreign_key="journal_entry.id", ondelete="RESTRICT"
    )
    void_date: dt.date | None = Field(default=None)


class Attachment(SQLModel, table=True):
    """A file attached to a document. Append-only: UPDATE and DELETE are refused."""

    __tablename__ = "attachment"
    __table_args__ = (
        Index("ix_attachment_owner", "owner_type", "owner_id"),
        CheckConstraint("owner_type IN ('expense')", name="ck_attachment_owner_type"),
        CheckConstraint(f"size BETWEEN 1 AND {MAX_ATTACHMENT_BYTES}", name="ck_attachment_size"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_attachment_sha256"),
    )

    id: int | None = Field(default=None, primary_key=True)
    owner_type: str = Field(max_length=20)
    owner_id: int
    filename: str = Field(max_length=150)
    content_type: str = Field(max_length=100)
    sha256: str = Field(max_length=64)
    stored_name: str = Field(max_length=200)
    size: int
    uploaded_at: dt.datetime = Field(default_factory=dt.datetime.utcnow, nullable=False)
    uploaded_by: int = Field(foreign_key="app_user.id", ondelete="RESTRICT")
