"""
Payroll ledger support tables: per-year unemployment settings, deposit due-date rules
and tax payments (money paid to an agency from a bank account).

Tax payments are documents; their money movement is a journal entry posted through
``LedgerPostingService`` (``entry_id``). Enum-like columns are plain strings with CHECKs
(see the ``payroll_ledger`` migration).
"""

import datetime as dt
from decimal import Decimal

from sqlalchemy import CheckConstraint, Index
from sqlmodel import Field, SQLModel

from payroll.common.models import AppBaseModel, money_field


class PayrollSettings(AppBaseModel, table=True):
    """FUTA and SUTA rates and wage bases for one tax year."""

    __tablename__ = "payroll_settings"
    __table_args__ = (
        CheckConstraint(
            "futa_rate BETWEEN 0 AND 1 AND suta_rate BETWEEN 0 AND 1",
            name="ck_payroll_settings_rates",
        ),
        CheckConstraint(
            "futa_wage_base >= 0 AND suta_wage_base >= 0", name="ck_payroll_settings_bases"
        ),
    )

    tax_year: int = Field(unique=True)
    futa_rate: Decimal = Field(max_digits=8, decimal_places=6)
    futa_wage_base: Decimal = money_field(default=...)
    suta_rate: Decimal = Field(max_digits=8, decimal_places=6)
    suta_wage_base: Decimal = money_field(default=...)


class PayrollDepositRules(SQLModel, table=True):
    """
    Singleton (``id = 1``) deposit due-date rules per agency: a cadence (``monthly`` or
    ``quarterly``) and a due day, 1..31 or 0 for the last day of the month.
    """

    __tablename__ = "payroll_deposit_rules"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_payroll_deposit_rules_singleton"),
        *(
            check
            for agency in ("irs_941", "ga_dor", "futa", "ga_dol")
            for check in (
                CheckConstraint(
                    f"{agency}_cadence IN ('monthly', 'quarterly')",
                    name=f"ck_payroll_deposit_rules_{agency}_cadence",
                ),
                CheckConstraint(
                    f"{agency}_due_day BETWEEN 0 AND 31",
                    name=f"ck_payroll_deposit_rules_{agency}_due_day",
                ),
            )
        ),
    )

    id: int = Field(default=1, primary_key=True)
    irs_941_cadence: str = Field(default="monthly", max_length=10)
    irs_941_due_day: int = Field(default=15)
    ga_dor_cadence: str = Field(default="monthly", max_length=10)
    ga_dor_due_day: int = Field(default=15)
    futa_cadence: str = Field(default="quarterly", max_length=10)
    futa_due_day: int = Field(default=0)
    ga_dol_cadence: str = Field(default="quarterly", max_length=10)
    ga_dol_due_day: int = Field(default=0)


class TaxPayment(AppBaseModel, table=True):
    __tablename__ = "tax_payment"
    __table_args__ = (
        Index("ix_tax_payment_agency_date", "agency", "payment_date"),
        CheckConstraint("amount > 0", name="ck_tax_payment_amount"),
        CheckConstraint(
            "agency IN ('irs_941', 'irs_futa', 'ga_dor', 'ga_dol')", name="ck_tax_payment_agency"
        ),
        CheckConstraint("status IN ('posted', 'void')", name="ck_tax_payment_status"),
        CheckConstraint(
            "(status = 'posted' AND void_entry_id IS NULL AND void_date IS NULL) "
            "OR (status = 'void' AND entry_id IS NOT NULL "
            "AND void_entry_id IS NOT NULL AND void_date IS NOT NULL)",
            name="ck_tax_payment_void_fields",
        ),
        CheckConstraint(
            "void_date IS NULL OR void_date >= payment_date", name="ck_tax_payment_void_date"
        ),
    )

    payment_date: dt.date
    agency: str = Field(max_length=10)
    bank_account_id: int = Field(foreign_key="account.id", ondelete="RESTRICT")
    amount: Decimal = money_field()
    memo: str | None = Field(default=None, max_length=500)
    status: str = Field(default="posted", max_length=10)
    entry_id: int | None = Field(default=None, foreign_key="journal_entry.id", ondelete="RESTRICT")
    void_entry_id: int | None = Field(
        default=None, foreign_key="journal_entry.id", ondelete="RESTRICT"
    )
    void_date: dt.date | None = Field(default=None)
