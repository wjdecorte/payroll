from dataclasses import dataclass

from payroll.exceptions import AppBaseError


@dataclass
class CustomerNotFoundError(AppBaseError):
    message: str = "Customer not found"
    http_code: int = 404


@dataclass
class ItemNotFoundError(AppBaseError):
    message: str = "Item not found"
    http_code: int = 404


@dataclass
class InvoiceNotFoundError(AppBaseError):
    message: str = "Invoice not found"
    http_code: int = 404


@dataclass
class PaymentNotFoundError(AppBaseError):
    message: str = "Payment not found"
    http_code: int = 404


@dataclass
class DepositNotFoundError(AppBaseError):
    message: str = "Deposit not found"
    http_code: int = 404


@dataclass
class RefundNotFoundError(AppBaseError):
    message: str = "Refund not found"
    http_code: int = 404


@dataclass
class InvalidCustomerError(AppBaseError):
    message: str = "Invalid customer"
    http_code: int = 422


@dataclass
class CustomerConflictError(AppBaseError):
    message: str = "A customer with this name already exists"
    http_code: int = 409


@dataclass
class InvalidItemError(AppBaseError):
    message: str = "Invalid item"
    http_code: int = 422


@dataclass
class ItemConflictError(AppBaseError):
    message: str = "An item with this name already exists"
    http_code: int = 409


@dataclass
class InvalidInvoiceError(AppBaseError):
    message: str = "Invalid invoice"
    http_code: int = 422


@dataclass
class InvoiceStateError(AppBaseError):
    message: str = "The invoice is not in a state that allows this"
    http_code: int = 409


@dataclass
class InvoiceNumberError(AppBaseError):
    message: str = "Invalid or duplicate invoice number"
    http_code: int = 409


@dataclass
class InvalidPaymentError(AppBaseError):
    message: str = "Invalid payment"
    http_code: int = 422


@dataclass
class OverApplicationError(AppBaseError):
    message: str = "The applied amount exceeds what is available"
    http_code: int = 409


@dataclass
class DepositError(AppBaseError):
    message: str = "The deposit cannot be made or changed"
    http_code: int = 409


@dataclass
class InvalidRefundError(AppBaseError):
    message: str = "Invalid refund"
    http_code: int = 422


@dataclass
class ARNotConfiguredError(AppBaseError):
    message: str = "The Accounts Receivable account role is not set up"
    http_code: int = 422
