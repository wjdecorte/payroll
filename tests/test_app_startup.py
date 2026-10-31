"""Startup must fail loudly when the database migration fails."""

import pytest
from fastapi.testclient import TestClient

from payroll.app import app
from payroll.migrations import alembic_runner


def test_migration_failure_stops_startup(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("migration exploded")

    monkeypatch.setattr(alembic_runner, "upgrade", boom)
    with pytest.raises(RuntimeError, match="migration exploded"):
        with TestClient(app):
            pass


def test_startup_migration_keeps_app_and_server_loggers_enabled(db_override):
    """
    The lifespan runs the Alembic migration (env.py) inside the worker; it must
    not disable or re-route the loggers configured by the app and the server.
    """
    import logging

    names = ("payroll.app", "payroll.auth", "payroll.migrations", "uvicorn.error", "uvicorn.access")
    with TestClient(app):
        pass
    for name in names:
        assert logging.getLogger(name).disabled is False, name
    payroll_logger = logging.getLogger("payroll")
    assert payroll_logger.handlers, "app handler was removed"
    assert payroll_logger.propagate is False
