"""
Receivables: customers, items, invoices, payments, deposits and refunds.

Documents are stored here; their money movements are journal entries posted
through ``LedgerPostingService`` (``entry_id``). Balances are never stored.
``payment_application`` and ``application_income`` are append-only, and a deferred
constraint trigger keeps applications within their payment and invoice totals (see
the ``receivables`` migration). Enum-like columns are plain strings.
"""

from datetime import date
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlmodel import Field, SQLModel

from payroll.common.models import AppBaseModel, money_field


class Customer(AppBaseModel, table=True):
    __tablename__ = "customer"
    __table_args__ = (
        Index("uq_customer_name", sa.text("lower(name)"), unique=True),
        CheckConstraint("terms_days BETWEEN 0 AND 365", name="ck_customer_terms_days"),
    )

    name: str = Field(max_length=200)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=50)
    billing_address: str | None = Field(default=None, max_length=1000)
    terms_days: int = Field(default=30)
    is_active: bool = Field(default=True)


class Item(AppBaseModel, table=True):
    __tablename__ = "item"
    __table_args__ = (
        Index("uq_item_name", sa.text("lower(name)"), unique=True),
        CheckConstraint("unit_price >= 0", name="ck_item_unit_price"),
    )

    name: str = Field(max_length=200)
    description: str | None = Field(default=None, max_length=500)
    unit_price: Decimal = money_field()
    income_account_id: int = Field(foreign_key="account.id", ondelete="RESTRICT")
    is_active: bool = Field(default=True)


class Invoice(AppBaseModel, table=True):
    __tablename__ = "invoice"
    __table_args__ = (
        Index(
            "uq_invoice_number",
            "number",
            unique=True,
            postgresql_where=sa.text("number IS NOT NULL"),
        ),
        Index("ix_invoice_customer_id", "customer_id"),
        CheckConstraint("status IN ('draft', 'open', 'void')", name="ck_invoice_status"),
        CheckConstraint(
            "status <> 'open' OR (number IS NOT NULL AND entry_id IS NOT NULL)",
            name="ck_invoice_open_has_number_entry",
        ),
    )

    number: int | None = Field(default=None)
    customer_id: int = Field(foreign_key="customer.id", ondelete="RESTRICT")
    invoice_date: date
    due_date: date
    terms_days: int = Field(default=30)
    status: str = Field(default="draft", max_length=10)
    memo: str | None = Field(default=None, max_length=500)
    total: Decimal = money_field()
    entry_id: int | None = Field(default=None, foreign_key="journal_entry.id", ondelete="RESTRICT")
    void_date: date | None = Field(default=None)


class InvoiceLine(AppBaseModel, table=True):
    __tablename__ = "invoice_line"
    __table_args__ = (
        UniqueConstraint("invoice_id", "line_no", name="uq_invoice_line_invoice_line"),
        CheckConstraint("qty > 0", name="ck_invoice_line_qty"),
        CheckConstraint("unit_price >= 0", name="ck_invoice_line_unit_price"),
        CheckConstraint("amount >= 0", name="ck_invoice_line_amount"),
    )

    invoice_id: int = Field(foreign_key="invoice.id", ondelete="RESTRICT")
    line_no: int
    item_id: int | None = Field(default=None, foreign_key="item.id", ondelete="RESTRICT")
    description: str | None = Field(default=None, max_length=500)
    qty: Decimal = Field(default=Decimal("1.000"), max_digits=12, decimal_places=3)
    unit_price: Decimal = money_field()
    amount: Decimal = money_field()
    # Snapshot taken when the invoice is issued.
    income_account_id: int = Field(foreign_key="account.id", ondelete="RESTRICT")


class Deposit(AppBaseModel, table=True):
    __tablename__ = "deposit"
    __table_args__ = (
        CheckConstraint("total > 0", name="ck_deposit_total"),
        CheckConstraint("status IN ('posted', 'void')", name="ck_deposit_status"),
    )

    deposit_date: date
    bank_account_id: int = Field(foreign_key="account.id", ondelete="RESTRICT")
    memo: str | None = Field(default=None, max_length=500)
    total: Decimal = money_field()
    entry_id: int | None = Field(default=None, foreign_key="journal_entry.id", ondelete="RESTRICT")
    status: str = Field(default="posted", max_length=10)
    void_date: date | None = Field(default=None)


class Payment(AppBaseModel, table=True):
    __tablename__ = "payment"
    __table_args__ = (
        Index("ix_payment_customer_id", "customer_id"),
        CheckConstraint("amount > 0", name="ck_payment_amount"),
        CheckConstraint("status IN ('posted', 'void')", name="ck_payment_status"),
    )

    customer_id: int = Field(foreign_key="customer.id", ondelete="RESTRICT")
    received_date: date
    method: str | None = Field(default=None, max_length=30)
    reference: str | None = Field(default=None, max_length=100)
    memo: str | None = Field(default=None, max_length=500)
    amount: Decimal = money_field()
    deposit_account_id: int = Field(foreign_key="account.id", ondelete="RESTRICT")
    entry_id: int | None = Field(default=None, foreign_key="journal_entry.id", ondelete="RESTRICT")
    status: str = Field(default="posted", max_length=10)
    void_date: date | None = Field(default=None)
    deposit_id: int | None = Field(default=None, foreign_key="deposit.id", ondelete="RESTRICT")


class PaymentApplication(AppBaseModel, table=True):
    __tablename__ = "payment_application"
    __table_args__ = (
        UniqueConstraint("payment_id", "invoice_id", name="uq_payment_application_pay_inv"),
        Index("ix_payment_application_invoice_id", "invoice_id"),
        CheckConstraint("amount > 0", name="ck_payment_application_amount"),
    )

    payment_id: int = Field(foreign_key="payment.id", ondelete="RESTRICT")
    invoice_id: int = Field(foreign_key="invoice.id", ondelete="RESTRICT")
    amount: Decimal = money_field()
    recognized_date: date
    ar_account_id: int = Field(foreign_key="account.id", ondelete="RESTRICT")


class ApplicationIncome(AppBaseModel, table=True):
    """The stored income split of an application (cash-basis recognition)."""

    __tablename__ = "application_income"
    __table_args__ = (
        UniqueConstraint("application_id", "account_id", name="uq_application_income_app_acct"),
        CheckConstraint("amount > 0", name="ck_application_income_amount"),
    )

    application_id: int = Field(foreign_key="payment_application.id", ondelete="RESTRICT")
    account_id: int = Field(foreign_key="account.id", ondelete="RESTRICT")
    amount: Decimal = money_field()


class CustomerRefund(AppBaseModel, table=True):
    __tablename__ = "customer_refund"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_customer_refund_amount"),
        CheckConstraint("status IN ('posted', 'void')", name="ck_customer_refund_status"),
    )

    customer_id: int = Field(foreign_key="customer.id", ondelete="RESTRICT")
    refund_date: date
    method: str | None = Field(default=None, max_length=30)
    reference: str | None = Field(default=None, max_length=100)
    memo: str | None = Field(default=None, max_length=500)
    amount: Decimal = money_field()
    bank_account_id: int = Field(foreign_key="account.id", ondelete="RESTRICT")
    entry_id: int | None = Field(default=None, foreign_key="journal_entry.id", ondelete="RESTRICT")
    status: str = Field(default="posted", max_length=10)
    void_date: date | None = Field(default=None)


class ReceivablesSettings(SQLModel, table=True):
    __tablename__ = "receivables_settings"
    __table_args__ = (CheckConstraint("id = 1", name="ck_receivables_settings_singleton"),)

    id: int = Field(default=1, primary_key=True)
    next_invoice_no: int = Field(default=1001)
    company_name: str | None = Field(default=None, max_length=200)
    company_address: str | None = Field(default=None, max_length=1000)
    remit_to: str | None = Field(default=None, max_length=1000)
    invoice_footer: str | None = Field(default=None, max_length=1000)
