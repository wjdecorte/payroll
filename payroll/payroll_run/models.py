from decimal import Decimal

from sqlmodel import Field

from payroll.common.models import AppBaseModel, money_field


class PayrollRun(AppBaseModel, table=True):
    """
    Persisted record of a completed payroll calculation.

    Stores all inputs and computed outputs so the user can view historical
    payroll runs and reproduce QuickBooks journal entries at any time.
    """

    __tablename__ = "payroll_run"

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
    # Metadata
    # ------------------------------------------------------------------
    tax_year: int = Field(default=2026)
