"""Shared fixtures for migration tests."""

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from payroll import app_settings
from tests.conftest import TEST_DATABASE_URL

SCRATCH_DB = "payroll_money_migration_test"


@pytest.fixture
def scratch_engine(monkeypatch):
    """An empty scratch database; the Alembic runner is pointed at it."""
    url = make_url(TEST_DATABASE_URL).set(database=SCRATCH_DB)
    admin = sa.create_engine(make_url(TEST_DATABASE_URL), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
        conn.exec_driver_sql(f'CREATE DATABASE "{SCRATCH_DB}"')

    monkeypatch.setattr(app_settings, "database_url", url.render_as_string(hide_password=False))
    engine = sa.create_engine(url)
    yield engine

    engine.dispose()
    with admin.connect() as conn:
        conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    admin.dispose()
