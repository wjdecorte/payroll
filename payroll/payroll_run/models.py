import datetime as dt
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, Index
from sqlmodel import Field

from payroll.common.models import AppBaseModel, money_field


class PayrollRun(AppBaseModel, table=True):
    """
    Persisted record of a completed payroll calculation.

    Stores all inputs and computed outputs so the user can view historical
    payroll runs and reproduce QuickBooks journal entries at any time.
    """

    __tablename__ = "payroll_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'approved', 'void', 'imported')", name="ck_payroll_run_status"
        ),
        CheckConstraint(
            "status <> 'approved' OR (pay_date IS NOT NULL AND entry_id IS NOT NULL "
            "AND net_pay_source IS NOT NULL)",
            name="ck_payroll_run_approved_fields",
        ),
        CheckConstraint(
            "(net_pay_source IS NULL AND net_pay_account_id IS NULL) "
            "OR (net_pay_source IS NOT NULL AND net_pay_source = 'owner' AND net_pay_account_id IS NULL) "
            "OR (net_pay_source IS NOT NULL AND net_pay_source = 'account' AND net_pay_account_id IS NOT NULL)",
            name="ck_payroll_run_net_pay_source",
        ),
        CheckConstraint(
            "status <> 'void' OR (pay_date IS NOT NULL AND entry_id IS NOT NULL "
            "AND void_entry_id IS NOT NULL AND void_date IS NOT NULL AND void_date >= pay_date)",
            name="ck_payroll_run_void_fields",
        ),
        CheckConstraint(
            "status NOT IN ('draft', 'imported') OR (entry_id IS NULL "
            "AND void_entry_id IS NULL AND void_date IS NULL)",
            name="ck_payroll_run_no_entry_fields",
        ),
        CheckConstraint(
            "status <> 'approved' OR (void_entry_id IS NULL AND void_date IS NULL)",
            name="ck_payroll_run_approved_no_void",
        ),
        CheckConstraint(
            "futa_wages >= 0 AND futa >= 0 AND suta_wages >= 0 AND suta >= 0",
            name="ck_payroll_run_unemployment_nonneg",
        ),
        # One non-void run per pay period (a void run frees its period).
        Index(
            "uq_payroll_run_period_active",
            "pay_period",
            unique=True,
            postgresql_where=sa.text("status <> 'void'"),
        ),
    )

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------
    pay_period: str = Field(index=True, description="Format: YYYY-MM (e.g. '2026-01')")
    gross_salary: Decimal = money_field(default=...)
    health_insurance: Decimal = money_field()
    hsa_contribution: Decimal = money_field()
    health_in_income_tax: bool = Field(default=True)
    hsa_in_income_tax: bool = Field(default=False)
    ytd_fica_before: Decimal = money_field()
    notes: str | None = Field(default=None)

    # ------------------------------------------------------------------
    # Calculated — FICA
    # ------------------------------------------------------------------
    fica_wages: Decimal = money_field()
    income_tax_wages: Decimal = money_field()
    ss_taxable_wages: Decimal = money_field()
    ss_ee: Decimal = money_field()
    ss_er: Decimal = money_field()
    medicare_ee: Decimal = money_field()
    additional_medicare: Decimal = money_field()
    total_medicare_ee: Decimal = money_field()
    medicare_er: Decimal = money_field()

    # ------------------------------------------------------------------
    # Calculated — Income Tax
    # ------------------------------------------------------------------
    federal_withholding: Decimal = money_field()
    ga_withholding: Decimal = money_field()

    # ------------------------------------------------------------------
    # Calculated — Totals
    # ------------------------------------------------------------------
    total_employee_deductions: Decimal = money_field()
    net_pay: Decimal = money_field()
    total_employer_taxes: Decimal = money_field()
    total_employer_cost: Decimal = money_field()
    ytd_fica_after: Decimal = money_field()

    # ------------------------------------------------------------------
    # Calculated — unemployment taxes (FUTA / SUTA); rate used is stored
    # ------------------------------------------------------------------
    futa_wages: Decimal = money_field()
    futa: Decimal = money_field()
    suta_wages: Decimal = money_field()
    suta: Decimal = money_field()
    suta_rate: Decimal = Field(default=Decimal("0"), max_digits=8, decimal_places=6)

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------
    tax_year: int = Field(default=2026)

    # ------------------------------------------------------------------
    # Lifecycle and ledger posting (see the payroll_ledger migration).
    # A new run is a draft; the database freezes every non-draft run.
    # ------------------------------------------------------------------
    status: str = Field(default="draft", max_length=10)
    pay_date: dt.date | None = Field(default=None)
    net_pay_source: str | None = Field(default=None, max_length=10)
    net_pay_account_id: int | None = Field(
        default=None, foreign_key="account.id", ondelete="RESTRICT"
    )
    entry_id: int | None = Field(default=None, foreign_key="journal_entry.id", ondelete="RESTRICT")
    void_entry_id: int | None = Field(
        default=None, foreign_key="journal_entry.id", ondelete="RESTRICT"
    )
    void_date: dt.date | None = Field(default=None)
