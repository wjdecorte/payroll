from dataclasses import dataclass

from payroll.exceptions import AppBaseError


@dataclass
class AccountNotFoundError(AppBaseError):
    message: str = "Account not found"
    http_code: int = 404


@dataclass
class AccountConflictError(AppBaseError):
    message: str = "An account with this number, name or system role already exists"
    http_code: int = 409


@dataclass
class InvalidAccountError(AppBaseError):
    message: str = "Invalid account"
    http_code: int = 422


@dataclass
class AccountInUseError(AppBaseError):
    message: str = "Account is in use"
    http_code: int = 409
