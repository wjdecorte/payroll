"""Writing audit rows. ``record`` joins the caller's transaction and never commits."""

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlmodel import Session

from .models import AuditLog


def json_safe(value: Any) -> Any:
    """
    Convert ``value`` to something JSON-serializable without failing.

    Decimal -> string (exact), datetime/date -> ISO string, Enum -> its value,
    mappings/sequences/sets recurse (dict keys become strings), anything else
    unknown -> ``str(value)``.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Enum):
        return json_safe(value.value)
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [json_safe(v) for v in value]
    return str(value)


def record(
    session: Session,
    *,
    action: str,
    user_id: int | None = None,
    entity: str | None = None,
    entity_id: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ip: str | None = None,
) -> AuditLog:
    """
    Add an audit row to the caller's session and flush (so ``id``/``ts`` exist).
    Does NOT commit: the row lives or dies with the caller's transaction.
    """
    row = AuditLog(
        action=action,
        user_id=user_id,
        entity=entity,
        entity_id=entity_id,
        before=json_safe(before),
        after=json_safe(after),
        ip=ip,
    )
    session.add(row)
    session.flush()
    return row
