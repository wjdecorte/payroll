"""Sanity checks for the Postgres-backed test infrastructure."""

from sqlmodel import select

from payroll import app_settings
from payroll.payroll_run.config_models import PayrollConfig


def test_engine_is_postgres(engine):
    assert engine.dialect.name == "postgresql"


def test_session_is_isolated_between_tests_part1(session):
    session.add(PayrollConfig(**{f"acct_{k}": v for k, v in app_settings.accounts.items()}))
    session.commit()  # commits must still be rolled back after the test
    assert session.exec(select(PayrollConfig)).all()


def test_session_is_isolated_between_tests_part2(session):
    assert session.exec(select(PayrollConfig)).all() == []
