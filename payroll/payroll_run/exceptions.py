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
