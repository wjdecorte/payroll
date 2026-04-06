from dataclasses import dataclass

from payroll.exceptions import AppBaseError


@dataclass
class ResourceNotFoundError(AppBaseError):
    message: str = "Resource not found"
    http_code: int = 404


@dataclass
class ValidationError(AppBaseError):
    message: str = "Validation error"
    http_code: int = 422
