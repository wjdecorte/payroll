"""Tests for the audit_log migration."""

import sqlalchemy as sa

from payroll.migrations import alembic_runner

AUTH_REVISION = "5b2e7c9a4f18"  # head before the audit_log migration


def test_downgrade_drops_audit_log_and_trigger_function(scratch_engine):
    alembic_runner.upgrade()
    with scratch_engine.connect() as conn:
        assert "audit_log" in sa.inspect(conn).get_table_names()

    alembic_runner.downgrade(AUTH_REVISION)
    with scratch_engine.connect() as conn:
        assert "audit_log" not in sa.inspect(conn).get_table_names()
        funcs = conn.exec_driver_sql(
            "SELECT count(*) FROM pg_proc WHERE proname = 'audit_log_reject_change'"
        ).scalar_one()
    assert funcs == 0

    alembic_runner.upgrade()
    with scratch_engine.connect() as conn:
        assert "audit_log" in sa.inspect(conn).get_table_names()
