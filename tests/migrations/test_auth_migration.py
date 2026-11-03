"""Tests for the auth tables migration (app_user, recovery_code, auth_session)."""

import sqlalchemy as sa

from payroll.migrations import alembic_runner

AUTH_TABLES = {"app_user", "recovery_code", "auth_session"}
MONEY_REVISION = "9d4e6b1f2a37"  # head before the auth migration


def _tables(engine) -> set[str]:
    with engine.connect() as conn:
        return set(sa.inspect(conn).get_table_names())


def test_auth_tables_exist(engine):
    """The shared test database (built by migrations) has every auth table."""
    assert AUTH_TABLES <= _tables(engine)


def test_auth_table_columns(engine):
    with engine.connect() as conn:
        inspector = sa.inspect(conn)
        user_cols = {c["name"] for c in inspector.get_columns("app_user")}
        session_cols = {c["name"]: c for c in inspector.get_columns("auth_session")}
        code_cols = {c["name"] for c in inspector.get_columns("recovery_code")}
    assert {
        "email",
        "display_name",
        "password_hash",
        "role",
        "totp_secret_enc",
        "totp_enabled",
        "totp_last_step",
        "failed_logins",
        "locked_until",
        "lock_count",
        "is_active",
    } <= user_cols
    assert {"user_id", "code_hash", "used_at"} <= code_cols
    assert {
        "user_id",
        "created_at",
        "last_seen_at",
        "expires_at",
        "ip",
        "user_agent",
        "mfa_verified",
        "csrf_token",
    } <= set(session_cols)
    # Session id is the sha256 hex of the token: a 64-char string, not an integer.
    assert isinstance(session_cols["id"]["type"], sa.String)
    assert session_cols["id"]["type"].length == 64


def test_downgrade_drops_auth_tables_and_upgrade_restores(scratch_engine):
    alembic_runner.upgrade()
    assert AUTH_TABLES <= _tables(scratch_engine)

    alembic_runner.downgrade(MONEY_REVISION)
    assert not (AUTH_TABLES & _tables(scratch_engine))

    alembic_runner.upgrade()
    assert AUTH_TABLES <= _tables(scratch_engine)


def test_legacy_numeric_db_without_alembic_version_gets_auth_tables(scratch_engine):
    """A pre-Alembic DB already on NUMERIC money must still receive the auth tables."""
    alembic_runner.upgrade(MONEY_REVISION)
    with scratch_engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE alembic_version")

    alembic_runner.upgrade()

    assert AUTH_TABLES <= _tables(scratch_engine)
    with scratch_engine.connect() as conn:
        version = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
    assert version == alembic_runner._HEAD_REVISION
