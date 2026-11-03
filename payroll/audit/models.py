"""
Append-only audit log.

``user_id`` deliberately has no foreign key: audit rows must survive deleting the
user and be writable before authentication exists. A database trigger (see the
``audit_log`` migration) rejects UPDATE, DELETE and TRUNCATE. ``ts`` is naive UTC,
like every other timestamp.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import Column, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from payroll.auth.models import utcnow


class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_ts", "ts"),
        Index("ix_audit_log_entity_entity_id", "entity", "entity_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utcnow, nullable=False)
    user_id: int | None = Field(default=None)
    action: str = Field(max_length=100)
    entity: str | None = Field(default=None, max_length=100)
    entity_id: str | None = Field(default=None, max_length=100)
    before: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    after: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    ip: str | None = Field(default=None, max_length=45)
