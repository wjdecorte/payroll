from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    app_name: str = "Payroll"
    app_version: str = "1.0.0"
    logger_name: str = "payroll"
    base_url_prefix: str = "/payroll/api/v1"
    debug_mode: bool = False

    # Database — defaults to local SQLite; override with PostgreSQL URL in production
    database_url: str = "sqlite:///./payroll.db"

    # Logging — "standard" for human-readable, "json" for structured/production
    api_log_type: str = "standard"

    # QuickBooks Account Names — update to match your chart of accounts
    acct_officer_compensation: str = "Officer Compensation"
    acct_payroll_tax_expense: str = "Payroll Tax Expense"
    acct_health_insurance_exp: str = "Health Insurance Expense"
    acct_hsa_expense: str = "HSA Contribution Expense"
    acct_fed_tax_payable: str = "Federal Income Tax Withholding Payable"
    acct_ga_tax_payable: str = "Georgia Income Tax Withholding Payable"
    acct_ss_payable_ee: str = "Social Security Tax Payable - Employee"
    acct_ss_payable_er: str = "Social Security Tax Payable - Employer"
    acct_medicare_payable_ee: str = "Medicare Tax Payable - Employee"
    acct_medicare_payable_er: str = "Medicare Tax Payable - Employer"
    acct_health_ins_payable: str = "Health Insurance Payable"
    acct_hsa_payable: str = "HSA Payable"
    acct_checking: str = "Checking Account"

    @property
    def accounts(self) -> dict[str, str]:
        return {
            "officer_compensation": self.acct_officer_compensation,
            "payroll_tax_expense": self.acct_payroll_tax_expense,
            "health_insurance_exp": self.acct_health_insurance_exp,
            "hsa_expense": self.acct_hsa_expense,
            "fed_tax_payable": self.acct_fed_tax_payable,
            "ga_tax_payable": self.acct_ga_tax_payable,
            "ss_payable_ee": self.acct_ss_payable_ee,
            "ss_payable_er": self.acct_ss_payable_er,
            "medicare_payable_ee": self.acct_medicare_payable_ee,
            "medicare_payable_er": self.acct_medicare_payable_er,
            "health_ins_payable": self.acct_health_ins_payable,
            "hsa_payable": self.acct_hsa_payable,
            "checking": self.acct_checking,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()


app_settings = get_settings()
