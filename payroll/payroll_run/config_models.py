from sqlmodel import Field

from payroll.common.models import AppBaseModel


class PayrollConfig(AppBaseModel, table=True):
    """Runtime payroll configuration managed from the admin page."""

    __tablename__ = "payroll_config"

    default_gross_salary: float = Field(default=0.0, ge=0)
    default_health_insurance: float = Field(default=0.0, ge=0)
    default_hsa_contribution: float = Field(default=0.0, ge=0)
    default_health_in_income_tax: bool = Field(default=True)
    default_hsa_in_income_tax: bool = Field(default=False)
    default_use_previous_ytd_fica: bool = Field(default=True)
    default_save_run: bool = Field(default=False)
    default_notes: str | None = Field(default=None)

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

    federal_due_date_note: str = (
        "Deposit via EFTPS by the 15th of the following month (monthly depositor)."
    )
    georgia_due_date_note: str = (
        "Pay via Georgia Tax Center (gtc.dor.ga.gov). "
        "Monthly if liability > $800/year; otherwise quarterly."
    )

    # QBO journal import — incremented by 2 each time a CSV is exported
    next_journal_no: int = Field(default=202, ge=1)
