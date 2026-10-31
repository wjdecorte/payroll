"""
Pytest configuration and shared fixtures.

Tests run against a real PostgreSQL database (``TEST_DATABASE_URL``; start one with
``docker compose -f compose.test.yml up -d db``). The schema is built by running the
Alembic migrations, and each test runs inside a rolled-back transaction.
"""

import os
from contextlib import contextmanager

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://payroll:payroll@localhost:5433/payroll_test"
)
# Point the application (and the Alembic runner) at the test database before any
# payroll module reads its settings, so tests can never touch a dev/prod database.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
# Deterministic, test-only AES-256 key (base64 of bytes 0..31); never used outside tests.
TEST_ENCRYPTION_KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
os.environ["ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlmodel import Session, SQLModel  # noqa: E402

from payroll import app_settings  # noqa: E402
from payroll.app import app, create_app  # noqa: E402
from payroll.auth import sessions as auth_sessions  # noqa: E402
from payroll.auth.dependencies import (  # noqa: E402
    CSRF_HEADER,
    csrf_cookie_name,
    new_presession_csrf_token,
    session_cookie_name,
)
from payroll.auth.models import User  # noqa: E402
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


def add_presession_csrf(client: TestClient) -> str:
    """
    Give ``client`` a valid pre-session CSRF double-submit pair: the signed cookie
    plus a default ``X-CSRF-Token`` header carrying the same value. Every unsafe
    request the client sends then passes ``verify_csrf`` while no session exists.
    """
    token = new_presession_csrf_token()
    client.cookies.set(csrf_cookie_name(), token)
    client.headers[CSRF_HEADER] = token
    return token


@pytest.fixture(name="db_override")
def db_override_fixture(session):
    """Route every request's ``get_session`` to the rolled-back test session."""

    def override_get_session():
        yield session

    app.dependency_overrides[get_session] = override_get_session
    yield
    app.dependency_overrides.clear()


def _test_client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(name="client")
def client_fixture(db_override):
    """
    Unauthenticated TestClient with ``get_session`` overridden to the test session.

    CSRF protection is on app-wide, so the client carries a pre-session CSRF
    cookie and the matching ``X-CSRF-Token`` default header; ``login_as`` swaps the
    header for the session's token. Tests that exercise CSRF failures should build
    their own bare ``TestClient``. Every route except the login pages,
    ``/healthcheck`` and ``/static`` needs a signed-in user: use ``owner_client``
    (or another role client) for those.
    """
    with _test_client() as c:
        add_presession_csrf(c)
        yield c


@pytest.fixture
def debug_app(session, monkeypatch):
    """
    A separate app instance built with ``debug_mode`` on (so it serves the API
    docs), with ``get_session`` overridden to the test session. Use it with a
    plain ``TestClient(debug_app)`` (no ``with``: the lifespan is not needed).
    """
    monkeypatch.setattr(app_settings, "debug_mode", True)
    application = create_app()
    monkeypatch.setattr(app_settings, "debug_mode", False)

    def override_get_session():
        yield session

    application.dependency_overrides[get_session] = override_get_session
    return application


@pytest.fixture(name="anon_client")
def anon_client_fixture(db_override):
    """Its own unauthenticated client (pre-session CSRF pair, no session)."""
    with _test_client() as c:
        add_presession_csrf(c)
        yield c


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def make_user(
    session: Session,
    *,
    role: str = "owner",
    email: str = "owner@example.com",
    password_hash: str = "$argon2id$placeholder",
    **overrides,
) -> User:
    """Insert (flush, not commit) a user. The default hash matches no password."""
    user = User(
        email=email,
        display_name=overrides.pop("display_name", email.split("@")[0].title()),
        password_hash=password_hash,
        role=role,
        **overrides,
    )
    session.add(user)
    session.flush()
    return user


def login_as(client: TestClient, user: User, *, mfa_verified: bool = True) -> str:
    """
    Create a session for ``user`` (fully authenticated unless ``mfa_verified`` is
    False), put its cookie on ``client`` and make ``client`` send its CSRF token by
    default. Returns the CSRF token. ``user`` must belong to the test session.
    """
    db = Session.object_session(user)
    raw, auth = auth_sessions.create_session(db, user, ip="testclient", user_agent="pytest")
    auth.mfa_verified = mfa_verified
    db.flush()
    client.cookies.set(session_cookie_name(), raw)
    client.headers[CSRF_HEADER] = auth.csrf_token
    return auth.csrf_token


def _role_user(session: Session, role: str) -> User:
    return make_user(
        session,
        role=role,
        email=f"{role}.fixture@example.com",
        display_name=f"Test {role.title()}",
    )


@pytest.fixture
def owner_user(session) -> User:
    return _role_user(session, "owner")


@pytest.fixture
def accountant_user(session) -> User:
    return _role_user(session, "accountant")


@pytest.fixture
def viewer_user(session) -> User:
    return _role_user(session, "viewer")


@contextmanager
def _signed_in_client(user: User):
    """A separate TestClient (own cookie jar) signed in as ``user`` with its CSRF token."""
    with _test_client() as c:
        login_as(c, user)
        yield c


@pytest.fixture
def owner_client(db_override, owner_user):
    with _signed_in_client(owner_user) as c:
        yield c


@pytest.fixture
def accountant_client(db_override, accountant_user):
    with _signed_in_client(accountant_user) as c:
        yield c


@pytest.fixture
def viewer_client(db_override, viewer_user):
    with _signed_in_client(viewer_user) as c:
        yield c


# ---------------------------------------------------------------------------
# Shared payroll data fixtures
# ---------------------------------------------------------------------------

PREFIX = "/payroll/api/v1"


@pytest.fixture
def payroll_input() -> dict:
    """Standard payroll input for a monthly payroll run (money as JSON strings)."""
    return {
        "payPeriod": "2026-01",
        "grossSalary": "8000.00",
        "healthInsurance": "500.00",
        "hsaContribution": "300.00",
        "healthInIncomeTax": True,
        "hsaInIncomeTax": False,
        "ytdFicaWages": "0.00",
        "saveRun": True,
        "notes": "January payroll",
    }


@pytest.fixture
def payroll_input_no_benefits() -> dict:
    """Minimal payroll input with no health insurance or HSA."""
    return {
        "payPeriod": "2026-02",
        "grossSalary": "6000.00",
        "healthInsurance": "0.00",
        "hsaContribution": "0.00",
        "healthInIncomeTax": True,
        "hsaInIncomeTax": False,
        "ytdFicaWages": "8000.00",
        "saveRun": True,
    }
