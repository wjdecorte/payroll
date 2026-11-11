from dataclasses import dataclass

from payroll.exceptions import AppBaseError


@dataclass
class PayrollRunNotFoundError(AppBaseError):
    message: str = "Payroll run not found"
    http_code: int = 404


@dataclass
class PayrollRunAlreadyExistsError(AppBaseError):
    message: str = "A payroll run for this pay period already exists"
    http_code: int = 409


@dataclass
class InvalidPayPeriodError(AppBaseError):
    message: str = "Invalid pay period format — expected YYYY-MM (e.g. '2026-01')"
    http_code: int = 422


@dataclass
class PayrollRunStateError(AppBaseError):
    message: str = "The payroll run is not in a state that allows this action"
    http_code: int = 409


@dataclass
class InvalidPayrollRunError(AppBaseError):
    message: str = "Invalid payroll run"
    http_code: int = 422


@dataclass
class PayrollSettingsMissingError(AppBaseError):
    message: str = (
        "Payroll settings for this tax year are missing — enter them in Settings > Payroll"
    )
    http_code: int = 422


@dataclass
class PayrollRoleNotBoundError(AppBaseError):
    message: str = "A payroll account role is not bound — bind it in Settings > Ledger"
    http_code: int = 422


@dataclass
class TaxPaymentNotFoundError(AppBaseError):
    message: str = "Tax payment not found"
    http_code: int = 404


@dataclass
class InvalidTaxPaymentError(AppBaseError):
    message: str = "Invalid tax payment"
    http_code: int = 422


@dataclass
class TaxPaymentError(AppBaseError):
    message: str = "The tax payment cannot be changed in its current state"
    http_code: int = 409
