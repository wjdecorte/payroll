"""Tests for the append-only audit log."""

import enum
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, select

from payroll.audit.models import AuditLog
from payroll.audit.services import json_safe, record


class _Color(enum.Enum):
    RED = "red"


def test_record_persists_row(session: Session):
    row = record(
        session,
        action="thing.save",
        user_id=7,
        entity="thing",
        entity_id="42",
        before={"a": 1},
        after={"a": Decimal("2.50")},
        ip="127.0.0.1",
    )
    assert row.id is not None
    assert row.ts is not None
    session.expire_all()
    got = session.get(AuditLog, row.id)
    assert got.action == "thing.save"
    assert got.user_id == 7
    assert got.entity == "thing"
    assert got.entity_id == "42"
    assert got.before == {"a": 1}
    assert got.after == {"a": "2.50"}
    assert got.ip == "127.0.0.1"


def test_record_minimal_row(session: Session):
    row = record(session, action="x")
    assert row.user_id is None and row.before is None and row.after is None


def test_update_is_rejected(session: Session):
    row = record(session, action="x")
    with pytest.raises(DBAPIError, match="append-only"):
        with session.begin_nested():
            session.execute(text("UPDATE audit_log SET action = 'y' WHERE id = :i"), {"i": row.id})
    assert session.get(AuditLog, row.id) is not None


def test_delete_is_rejected(session: Session):
    row = record(session, action="x")
    with pytest.raises(DBAPIError, match="append-only"):
        with session.begin_nested():
            session.execute(text("DELETE FROM audit_log WHERE id = :i"), {"i": row.id})
    assert session.exec(select(AuditLog).where(AuditLog.id == row.id)).first() is not None


def test_truncate_is_rejected(session: Session):
    record(session, action="x")
    with pytest.raises(DBAPIError, match="append-only"):
        with session.begin_nested():
            session.execute(text("TRUNCATE audit_log"))


def test_record_does_not_commit(engine):
    with engine.connect() as conn:
        s = Session(bind=conn)
        row = record(s, action="rollback.check")
        row_id = row.id
        s.rollback()
        s.close()
    with Session(engine) as check:
        assert check.get(AuditLog, row_id) is None


def test_json_safe_converts_values():
    when = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    out = json_safe(
        {
            "d": Decimal("1.10"),
            "dt": when,
            "day": date(2026, 1, 2),
            "e": _Color.RED,
            "nested": [{"x": Decimal("3")}],
            "t": (1, 2),
            "ok": None,
            3: "intkey",
            "s": {1},
        }
    )
    assert out["d"] == "1.10"
    assert out["dt"] == when.isoformat()
    assert out["day"] == "2026-01-02"
    assert out["e"] == "red"
    assert out["nested"] == [{"x": "3"}]
    assert out["t"] == [1, 2]
    assert out["ok"] is None
    assert out["3"] == "intkey"
    assert out["s"] == [1]


def test_json_safe_falls_back_to_str_and_none():
    assert json_safe(None) is None
    assert json_safe(object()).startswith("<object")
