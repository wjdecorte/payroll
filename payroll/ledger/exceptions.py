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


@dataclass
class InvalidEntryError(AppBaseError):
    message: str = "Invalid journal entry"
    http_code: int = 422


@dataclass
class UnbalancedEntryError(AppBaseError):
    message: str = "A journal entry needs at least 2 lines and equal debits and credits"
    http_code: int = 422


@dataclass
class InactiveAccountError(AppBaseError):
    message: str = "Inactive accounts cannot be posted to"
    http_code: int = 422


@dataclass
class PeriodLockedError(AppBaseError):
    message: str = "The entry date is in a locked period"
    http_code: int = 409


@dataclass
class EntryNotFoundError(AppBaseError):
    message: str = "Journal entry not found"
    http_code: int = 404


@dataclass
class AlreadyReversedError(AppBaseError):
    message: str = "This journal entry has already been reversed"
    http_code: int = 409
