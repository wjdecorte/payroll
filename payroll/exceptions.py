from dataclasses import dataclass
from typing import ClassVar

# Registry of all registered exception subclasses
ALL_EXCEPTIONS: dict[int, type["AppBaseError"]] = {}
_error_counter: int = 0


@dataclass
class AppBaseError(Exception):
    """
    Base exception for all application errors.

    Subclasses are auto-registered with a sequential error_number.
    All subclasses are caught by the global exception handler in app.py
    and returned as a structured JSON response.
    """

    message: str
    http_code: int = 500
    error_number: ClassVar[int]

    @classmethod
    def __init_subclass__(cls, **kwargs: object) -> None:
        global _error_counter
        super().__init_subclass__(**kwargs)
        _error_counter += 1
        cls.error_number = _error_counter
        ALL_EXCEPTIONS[_error_counter] = cls

    @property
    def code(self) -> str:
        return f"payroll.error.{self.error_number}"

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "type": self.__class__.__name__,
            "message": self.message,
        }
