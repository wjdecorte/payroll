from dataclasses import dataclass

from payroll.exceptions import AppBaseError


@dataclass
class VendorNotFoundError(AppBaseError):
    message: str = "Vendor not found"
    http_code: int = 404


@dataclass
class ExpenseNotFoundError(AppBaseError):
    message: str = "Expense not found"
    http_code: int = 404


@dataclass
class TransferNotFoundError(AppBaseError):
    message: str = "Transfer not found"
    http_code: int = 404


@dataclass
class ReimbursementNotFoundError(AppBaseError):
    message: str = "Reimbursement not found"
    http_code: int = 404


@dataclass
class AttachmentNotFoundError(AppBaseError):
    message: str = "Attachment not found"
    http_code: int = 404


@dataclass
class InvalidVendorError(AppBaseError):
    message: str = "Invalid vendor"
    http_code: int = 422


@dataclass
class InvalidExpenseError(AppBaseError):
    message: str = "Invalid expense"
    http_code: int = 422


@dataclass
class InvalidTransferError(AppBaseError):
    message: str = "Invalid transfer"
    http_code: int = 422


@dataclass
class InvalidReimbursementError(AppBaseError):
    message: str = "Invalid reimbursement"
    http_code: int = 422


@dataclass
class InvalidAttachmentError(AppBaseError):
    message: str = "Invalid attachment"
    http_code: int = 422


@dataclass
class VendorConflictError(AppBaseError):
    message: str = "A vendor with this name already exists"
    http_code: int = 409


@dataclass
class ExpenseError(AppBaseError):
    message: str = "The expense is not in a state that allows this"
    http_code: int = 409


@dataclass
class ReimbursementError(AppBaseError):
    message: str = "The reimbursement cannot be made or changed"
    http_code: int = 409


@dataclass
class TransferError(AppBaseError):
    message: str = "The transfer is not in a state that allows this"
    http_code: int = 409
