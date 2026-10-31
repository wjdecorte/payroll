import base64
import binascii
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    # Application
    app_name: str = "DCI Payroll"
    app_version: str = "1.2.0"
    # Git commit the running image was built from; set via BUILD_SHA at image build time
    build_sha: str = "dev"
    # Monotonic CI run number; 0 for local runs
    build_number: int = 0
    logger_name: str = "payroll"
    base_url_prefix: str = "/payroll/api/v1"
    debug_mode: bool = False

    # Database — PostgreSQL only; the default targets the local dev database (compose.yml)
    database_url: str = "postgresql://payroll:payroll@localhost:5432/payroll"

    # Secrets at rest — base64-encoded 32 random bytes (AES-256-GCM); required, no default.
    # Generate: python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
    encryption_key: str = Field(repr=False)

    # Sessions / login hardening
    session_cookie_name: str = "dci_session"
    session_cookie_secure: bool = True
    session_idle_minutes: int = 30
    session_absolute_hours: int = 12
    max_failed_logins: int = 5
    lockout_minutes: int = 15
    totp_issuer: str = "DCI Books"
    # Largest request body accepted (bytes); bigger requests get 413 before parsing.
    max_request_body_bytes: int = 1_048_576

    # Logging — "standard" for human-readable, "json" for structured/production
    api_log_type: str = "standard"

    # QuickBooks Account Names — update to match your chart of accounts
    acct_officer_compensation: str = "Payroll Expenses:Wages"
    acct_payroll_tax_expense: str = "Payroll Expenses:Taxes"
    acct_health_insurance_exp: str = "Health Insurance Expense"
    acct_hsa_expense: str = "HSA Contribution Expense"
    acct_fed_tax_payable: str = "Payroll Liabilities:Federal Taxes (941/943/944)"
    acct_ga_tax_payable: str = "Payroll Liabilities:GA Income Tax"
    acct_ss_payable_ee: str = "Payroll Liabilities:Federal Taxes (941/943/944)"
    acct_ss_payable_er: str = "Payroll Liabilities:Federal Taxes (941/943/944)"
    acct_medicare_payable_ee: str = "Payroll Liabilities:Federal Taxes (941/943/944)"
    acct_medicare_payable_er: str = "Payroll Liabilities:Federal Taxes (941/943/944)"
    acct_health_ins_payable: str = "Payroll Liabilities:S-Corp Health Insurance"
    acct_hsa_payable: str = "Payroll Liabilities:Co. HSA"
    acct_checking: str = "Business Checking"

    @field_validator("encryption_key")
    @classmethod
    def _validate_encryption_key(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("ENCRYPTION_KEY must be valid base64 encoding of 32 bytes") from exc
        if len(decoded) != 32:
            raise ValueError(f"ENCRYPTION_KEY must decode to exactly 32 bytes (got {len(decoded)})")
        return value

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
