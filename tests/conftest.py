"""
Pytest configuration and shared fixtures.

Uses an in-memory SQLite database so tests run fast with no external dependencies.
The FastAPI dependency ``get_session`` is overridden to use the test database.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel

# Import all models so SQLModel registers their metadata before creating tables
import payroll.payroll_run.models  # noqa: F401
from payroll.app import app
from payroll.common.dependencies import get_session

# ---------------------------------------------------------------------------
# In-memory test database
# ---------------------------------------------------------------------------

TEST_DATABASE_URL = "sqlite://"  # Pure in-memory; destroyed after each session


@pytest.fixture(name="engine", scope="session")
def engine_fixture():
    """Create an in-memory SQLite engine for the entire test session."""
    test_engine = create_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(test_engine)
    yield test_engine
    SQLModel.metadata.drop_all(test_engine)


@pytest.fixture(name="session")
def session_fixture(engine):
    """
    Provide a database session per test, rolling back after each test
    to ensure test isolation.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)

    yield session

    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture(name="client")
def client_fixture(session):
    """
    FastAPI TestClient with the database session overridden to use the
    test in-memory database.
    """

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
