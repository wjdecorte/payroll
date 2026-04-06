from sqlmodel import Field

from payroll.common.models import AppBaseModel


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
    gross_salary: float
    health_insurance: float = Field(default=0.0)
    hsa_contribution: float = Field(default=0.0)
    health_in_income_tax: bool = Field(default=True)
    hsa_in_income_tax: bool = Field(default=False)
    ytd_fica_before: float = Field(default=0.0)
    notes: str | None = Field(default=None)

    # ------------------------------------------------------------------
    # Calculated — FICA
    # ------------------------------------------------------------------
    fica_wages: float = Field(default=0.0)
    income_tax_wages: float = Field(default=0.0)
    ss_taxable_wages: float = Field(default=0.0)
    ss_ee: float = Field(default=0.0)
    ss_er: float = Field(default=0.0)
    medicare_ee: float = Field(default=0.0)
    additional_medicare: float = Field(default=0.0)
    total_medicare_ee: float = Field(default=0.0)
    medicare_er: float = Field(default=0.0)

    # ------------------------------------------------------------------
    # Calculated — Income Tax
    # ------------------------------------------------------------------
    federal_withholding: float = Field(default=0.0)
    ga_withholding: float = Field(default=0.0)

    # ------------------------------------------------------------------
    # Calculated — Totals
    # ------------------------------------------------------------------
    total_employee_deductions: float = Field(default=0.0)
    net_pay: float = Field(default=0.0)
    total_employer_taxes: float = Field(default=0.0)
    total_employer_cost: float = Field(default=0.0)
    ytd_fica_after: float = Field(default=0.0)

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------
    tax_year: int = Field(default=2026)
