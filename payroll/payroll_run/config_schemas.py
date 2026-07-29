from pydantic import Field

from payroll.common.schemas import AppBaseSchema


class PayrollConfigBase(AppBaseSchema):
    """Editable payroll defaults and QuickBooks journal account names."""

    default_gross_salary: float = Field(default=0.0, ge=0)
    default_health_insurance: float = Field(default=0.0, ge=0)
    default_hsa_contribution: float = Field(default=0.0, ge=0)
    default_health_in_income_tax: bool = True
    default_hsa_in_income_tax: bool = False
    default_use_previous_ytd_fica: bool = True
    default_save_run: bool = False
    default_notes: str | None = None

    acct_officer_compensation: str
    acct_payroll_tax_expense: str
    acct_health_insurance_exp: str
    acct_hsa_expense: str
    acct_fed_tax_payable: str
    acct_ga_tax_payable: str
    acct_ss_payable_ee: str
    acct_ss_payable_er: str
    acct_medicare_payable_ee: str
    acct_medicare_payable_er: str
    acct_health_ins_payable: str
    acct_hsa_payable: str
    acct_checking: str

    federal_due_date_note: str
    georgia_due_date_note: str


class PayrollConfigUpdate(PayrollConfigBase):
    """Full replacement update for payroll configuration."""


class PayrollConfigRead(PayrollConfigBase):
    id: int
