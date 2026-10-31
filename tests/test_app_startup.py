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
