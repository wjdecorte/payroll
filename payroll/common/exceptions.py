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


@dataclass
class PermissionDeniedError(AppBaseError):
    message: str = "You do not have permission to perform this action"
    http_code: int = 403
