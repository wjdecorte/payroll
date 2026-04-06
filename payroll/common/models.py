from datetime import datetime

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
