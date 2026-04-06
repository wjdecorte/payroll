from datetime import datetime

from pydantic import Field, computed_field

from payroll.common.schemas import AppBaseSchema
from payroll.tax.schemas import TaxConstantsSchema

# ---------------------------------------------------------------------------
# Journal Entry schemas
# ---------------------------------------------------------------------------


class JournalLine(AppBaseSchema):
    """A single debit or credit line in a QuickBooks journal entry."""

    account: str
    memo: str = ""
    debit: float = 0.0
    credit: float = 0.0


class JournalEntry(AppBaseSchema):
    """
    A complete double-entry journal entry ready for manual entry in QBO.

    ``dr_total`` and ``cr_total`` must be equal (balanced entry).
    """

    title: str
    note: str = ""
    lines: list[JournalLine]

    @computed_field  # type: ignore[misc]
    @property
    def dr_total(self) -> float:
        return round(sum(ln.debit for ln in self.lines), 2)

    @computed_field  # type: ignore[misc]
    @property
    def cr_total(self) -> float:
        return round(sum(ln.credit for ln in self.lines), 2)

    @computed_field  # type: ignore[misc]
    @property
    def is_balanced(self) -> bool:
        return abs(self.dr_total - self.cr_total) < 0.02


# ---------------------------------------------------------------------------
# Tax payment summary
# ---------------------------------------------------------------------------


class TaxPaymentSummary(AppBaseSchema):
    """Amounts the S-Corp must remit to tax authorities after each payroll."""

    federal_income_tax: float
    social_security_total: float  # EE + ER combined
    medicare_total: float  # EE (incl. additional) + ER combined
    total_federal_eftps: float
    georgia_income_tax: float
    federal_due_date_note: str = (
        "Deposit via EFTPS by the 15th of the following month (monthly depositor)."
    )
    georgia_due_date_note: str = (
        "Pay via Georgia Tax Center (gtc.dor.ga.gov). "
        "Monthly if liability > $800/year; otherwise quarterly."
    )


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class PayrollInput(AppBaseSchema):
    """Input payload for calculating a monthly payroll run."""

    pay_period: str = Field(
        ...,
        description="Pay period in YYYY-MM format",
        examples=["2026-01"],
    )
    gross_salary: float = Field(..., gt=0, description="Monthly gross cash salary")
    health_insurance: float = Field(
        default=0.0, ge=0, description="Monthly S-Corp health insurance premium"
    )
    hsa_contribution: float = Field(
        default=0.0, ge=0, description="Monthly HSA employer contribution"
    )
    health_in_income_tax: bool = Field(
        default=True,
        description=(
            "Include health insurance in income-tax wages (W-2 Box 1). "
            "Standard for >2% S-Corp shareholders."
        ),
    )
    hsa_in_income_tax: bool = Field(
        default=False,
        description="Include HSA in income-tax wages. Consult your CPA.",
    )
    ytd_fica_wages: float = Field(
        default=0.0,
        ge=0,
        description="Year-to-date FICA wages *before* this payroll run. Ignored when use_previous_ytd_fica is true.",
    )
    use_previous_ytd_fica: bool = Field(
        default=False,
        description=(
            "Automatically use the YTD FICA wages from the most recent saved "
            "payroll run in the same tax year. Overrides ytd_fica_wages."
        ),
    )
    save_run: bool = Field(
        default=True,
        description="Persist this run to the database for historical reference",
    )
    notes: str | None = Field(default=None, description="Optional memo / notes")


class TaxDetail(AppBaseSchema):
    """Full breakdown of all calculated amounts for a payroll run."""

    # Wage bases
    fica_wages: float
    income_tax_wages: float
    annual_income_tax_wages: float

    # Social Security
    ss_taxable_wages: float
    ss_ee: float
    ss_er: float
    ss_cap_hit: bool

    # Medicare
    medicare_ee: float
    additional_medicare: float
    total_medicare_ee: float
    medicare_er: float

    # Income Tax
    annual_fed_tax: float
    federal_withholding: float
    ga_annual_taxable: float
    annual_ga_tax: float
    ga_withholding: float

    # Totals
    total_employee_deductions: float
    net_pay: float
    total_employer_taxes: float
    total_employer_cost: float
    ytd_fica_after: float


class PayrollResult(AppBaseSchema):
    """Full API response for a calculated payroll run."""

    id: int | None = None
    pay_period: str
    gross_salary: float
    health_insurance: float
    hsa_contribution: float
    health_in_income_tax: bool
    hsa_in_income_tax: bool
    notes: str | None
    saved: bool
    tax_year: int
    tax_constants: TaxConstantsSchema
    tax_detail: TaxDetail
    journal_entries: list[JournalEntry]
    tax_payment_summary: TaxPaymentSummary
    create_date: datetime | None = None


class PayrollRunSummary(AppBaseSchema):
    """Abbreviated view of a saved payroll run used in list responses."""

    id: int
    pay_period: str
    gross_salary: float
    net_pay: float
    federal_withholding: float
    ga_withholding: float
    total_employer_cost: float
    tax_year: int
    notes: str | None
    create_date: datetime
