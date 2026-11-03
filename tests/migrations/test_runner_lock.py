"""The advisory-lock release must never mask the migration's own outcome."""

import pytest
from sqlalchemy import create_engine, text

from payroll import app_settings
from payroll.migrations import alembic_runner


def _kill_lock_holder() -> None:
    """Terminate the backend holding the migration advisory lock (its connection dies)."""
    engine = create_engine(app_settings.database_url)
    try:
        with engine.begin() as conn:
            killed = conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_locks"
                    " WHERE locktype = 'advisory' AND granted"
                    " AND ((classid::bigint << 32) | objid::bigint) = :key"
                ),
                {"key": alembic_runner._MIGRATION_LOCK_KEY},
            ).all()
        assert killed, "no backend held the migration lock"
    finally:
        engine.dispose()


def _spy_warnings(monkeypatch) -> list[str]:
    # A spy, not caplog: once the app configured logging, payroll.* does not propagate.
    messages: list[str] = []
    monkeypatch.setattr(
        alembic_runner.logger, "warning", lambda msg, *a, **k: messages.append(msg % a)
    )
    return messages


def test_unlock_failure_does_not_replace_migration_error(engine, monkeypatch):
    def failing_upgrade(cfg, revision):
        _kill_lock_holder()
        raise RuntimeError("migration exploded")

    warnings = _spy_warnings(monkeypatch)
    monkeypatch.setattr(alembic_runner.command, "upgrade", failing_upgrade)
    with pytest.raises(RuntimeError, match="migration exploded"):
        alembic_runner.upgrade()
    assert any("advisory lock" in m for m in warnings)


def test_unlock_failure_after_successful_migration_is_only_logged(engine, monkeypatch):
    warnings = _spy_warnings(monkeypatch)
    monkeypatch.setattr(
        alembic_runner.command, "upgrade", lambda cfg, revision: _kill_lock_holder()
    )
    alembic_runner.upgrade()  # does not raise
    assert any("advisory lock" in m for m in warnings)
