"""
Pytest configuration and shared fixtures.

Tests run against a real PostgreSQL database (``TEST_DATABASE_URL``; start one with
``docker compose -f compose.test.yml up -d db``). The schema is built by running the
Alembic migrations, and each test runs inside a rolled-back transaction.
"""

import os

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://payroll:payroll@localhost:5433/payroll_test"
)
# Point the application (and the Alembic runner) at the test database before any
# payroll module reads its settings, so tests can never touch a dev/prod database.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlmodel import Session, SQLModel  # noqa: E402

from payroll.app import app  # noqa: E402
from payroll.common.dependencies import get_session  # noqa: E402
from payroll.migrations import alembic_runner  # noqa: E402


@pytest.fixture(name="engine", scope="session")
def engine_fixture():
    """Postgres engine with the schema built by running the Alembic migrations."""
    test_engine = create_engine(TEST_DATABASE_URL)
    # Start from a clean schema so stale runs cannot leak in.
    SQLModel.metadata.drop_all(test_engine)
    with test_engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
    alembic_runner.upgrade()
    yield test_engine
    test_engine.dispose()


@pytest.fixture(name="session")
def session_fixture(engine):
    """
    Per-test session bound to an outer transaction that is always rolled back.
    ``create_savepoint`` makes ``session.commit()`` release a savepoint only, so
    code under test that commits is still undone at the end of the test.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")

    yield session

    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture(name="client")
def client_fixture(session):
    """FastAPI TestClient with ``get_session`` overridden to the test session."""

    def override_get_session():
        yield session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Shared payroll data fixtures
# ---------------------------------------------------------------------------

PREFIX = "/payroll/api/v1"


@pytest.fixture
def payroll_input() -> dict:
    """Standard payroll input for a monthly payroll run."""
    return {
        "payPeriod": "2026-01",
        "grossSalary": 8000.00,
        "healthInsurance": 500.00,
        "hsaContribution": 300.00,
        "healthInIncomeTax": True,
        "hsaInIncomeTax": False,
        "ytdFicaWages": 0.00,
        "saveRun": True,
        "notes": "January payroll",
    }


@pytest.fixture
def payroll_input_no_benefits() -> dict:
    """Minimal payroll input with no health insurance or HSA."""
    return {
        "payPeriod": "2026-02",
        "grossSalary": 6000.00,
        "healthInsurance": 0.0,
        "hsaContribution": 0.0,
        "healthInIncomeTax": True,
        "hsaInIncomeTax": False,
        "ytdFicaWages": 8000.00,
        "saveRun": True,
    }
