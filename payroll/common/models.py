from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlmodel import Field, SQLModel


class AppBaseModel(SQLModel):
    """
    Base model for all database-backed entities.

    Provides auto-incrementing primary key and audit timestamps.
    Subclass with ``table=True`` to create a real database table::

        class MyModel(AppBaseModel, table=True):
            name: str
    """

    id: int | None = Field(default=None, primary_key=True)
    create_date: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    modify_date: datetime = Field(default_factory=datetime.utcnow, nullable=False)


def money_field(default: Any = Decimal("0.00"), **kwargs: Any) -> Any:
    """A money column: ``NUMERIC(14, 2)`` holding a ``Decimal`` (never ``float``)."""
    return Field(default=default, max_digits=14, decimal_places=2, **kwargs)
