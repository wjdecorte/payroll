from datetime import datetime
from decimal import Decimal

from pydantic import Field, computed_field

from payroll.common.money import Money
from payroll.common.schemas import AppBaseSchema
from payroll.tax.schemas import TaxConstantsSchema

# ---------------------------------------------------------------------------
# Journal Entry schemas
# ---------------------------------------------------------------------------


class JournalLine(AppBaseSchema):
    """A single debit or credit line in a QuickBooks journal entry."""

    account: str
    memo: str = ""
    debit: Money = Decimal("0.00")
    credit: Money = Decimal("0.00")


class JournalEntry(AppBaseSchema):
    """
    A complete double-entry journal entry ready for manual entry in QBO.

    ``dr_total`` and ``cr_total`` must be exactly equal (balanced entry).
    """

    title: str
    note: str = ""
    lines: list[JournalLine]

    @computed_field  # type: ignore[misc]
    @property
    def dr_total(self) -> Decimal:
        return sum((ln.debit for ln in self.lines), Decimal("0.00"))

    @computed_field  # type: ignore[misc]
    @property
    def cr_total(self) -> Decimal:
        return sum((ln.credit for ln in self.lines), Decimal("0.00"))

    @computed_field  # type: ignore[misc]
    @property
    def is_balanced(self) -> bool:
        return self.dr_total == self.cr_total


# ---------------------------------------------------------------------------
# Tax payment summary
# ---------------------------------------------------------------------------


class TaxPaymentSummary(AppBaseSchema):
    """Amounts the S-Corp must remit to tax authorities after each payroll."""

    federal_income_tax: Money
    social_security_total: Money  # EE + ER combined
    medicare_total: Money  # EE (incl. additional) + ER combined
    total_federal_eftps: Money
    georgia_income_tax: Money
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
    gross_salary: Money = Field(..., gt=0, description="Monthly gross cash salary")
    health_insurance: Money = Field(
        default=Decimal("0.00"), ge=0, description="Monthly S-Corp health insurance premium"
    )
    hsa_contribution: Money = Field(
        default=Decimal("0.00"), ge=0, description="Monthly HSA employer contribution"
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
    ytd_fica_wages: Money = Field(
        default=Decimal("0.00"),
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
    fica_wages: Money
    income_tax_wages: Money
    annual_income_tax_wages: Money

    # Social Security
    ss_taxable_wages: Money
    ss_ee: Money
    ss_er: Money
    ss_cap_hit: bool

    # Medicare
    medicare_ee: Money
    additional_medicare: Money
    total_medicare_ee: Money
    medicare_er: Money

    # Income Tax
    # Annual projections are not stored on a saved run, so they are None when a
    # run is loaded from history (only stored amounts are ever shown for it).
    annual_fed_tax: Money | None = None
    federal_withholding: Money
    ga_annual_taxable: Money | None = None
    annual_ga_tax: Money | None = None
    ga_withholding: Money

    # Totals
    total_employee_deductions: Money
    net_pay: Money
    total_employer_taxes: Money
    total_employer_cost: Money
    ytd_fica_before: Money
    ytd_fica_after: Money


class PayrollResult(AppBaseSchema):
    """Full API response for a calculated payroll run."""

    id: int | None = None
    pay_period: str
    gross_salary: Money
    health_insurance: Money
    hsa_contribution: Money
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
    gross_salary: Money
    net_pay: Money
    federal_withholding: Money
    ga_withholding: Money
    total_employer_cost: Money
    tax_year: int
    notes: str | None
    create_date: datetime
