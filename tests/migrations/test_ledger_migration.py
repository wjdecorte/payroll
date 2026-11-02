"""Tests for the ledger_core migration."""

import sqlalchemy as sa

from payroll.migrations import alembic_runner
from tests.migrations.test_money_migration import RUN_MONEY_COLUMNS

AUDIT_REVISION = "7c1f3a5e8b92"  # head before the ledger_core migration
MONEY_VALUES = dict.fromkeys(RUN_MONEY_COLUMNS, 0) | {"gross_salary": 8000}
LEDGER_TABLES = {"account", "journal_entry", "journal_line", "entry_counter", "period_lock"}
LEDGER_TRIGGERS = {
    "journal_line_balanced",
    "journal_line_immutable",
    "journal_line_no_truncate",
    "journal_entry_guard",
    "journal_entry_no_truncate",
}
LEDGER_FUNCTIONS = {
    "journal_line_check_balanced",
    "journal_line_reject_change",
    "journal_entry_guard_change",
}


def _triggers(conn) -> set[str]:
    rows = conn.exec_driver_sql("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
    return {r[0] for r in rows} & LEDGER_TRIGGERS


def _functions(conn) -> set[str]:
    rows = conn.exec_driver_sql("SELECT proname FROM pg_proc")
    return {r[0] for r in rows} & LEDGER_FUNCTIONS


def _insert_payroll_run(engine) -> None:
    meta = sa.MetaData()
    meta.reflect(engine, only=["payroll_run"])
    with engine.begin() as conn:
        conn.execute(
            meta.tables["payroll_run"]
            .insert()
            .values(
                create_date=sa.func.now(),
                modify_date=sa.func.now(),
                pay_period="2026-01",
                health_in_income_tax=True,
                hsa_in_income_tax=False,
                notes="pre-ledger run",
                tax_year=2026,
                **MONEY_VALUES,
            )
        )


def test_upgrade_creates_tables_triggers_and_period_lock_row(scratch_engine):
    alembic_runner.upgrade(AUDIT_REVISION)
    _insert_payroll_run(scratch_engine)
    alembic_runner.upgrade()

    with scratch_engine.connect() as conn:
        assert LEDGER_TABLES <= set(sa.inspect(conn).get_table_names())
        assert _triggers(conn) == LEDGER_TRIGGERS
        assert _functions(conn) == LEDGER_FUNCTIONS
        assert conn.exec_driver_sql("SELECT id, lock_date FROM period_lock").all() == [(1, None)]
        runs = conn.exec_driver_sql("SELECT pay_period, gross_salary FROM payroll_run").all()
    assert [(p, float(g)) for p, g in runs] == [("2026-01", 8000.0)]


def test_downgrade_removes_tables_triggers_and_functions(scratch_engine):
    alembic_runner.upgrade(AUDIT_REVISION)
    _insert_payroll_run(scratch_engine)
    alembic_runner.upgrade()
    alembic_runner.downgrade(AUDIT_REVISION)

    with scratch_engine.connect() as conn:
        assert not LEDGER_TABLES & set(sa.inspect(conn).get_table_names())
        assert _triggers(conn) == set()
        assert _functions(conn) == set()
        assert conn.exec_driver_sql("SELECT count(*) FROM payroll_run").scalar_one() == 1

    alembic_runner.upgrade()
    with scratch_engine.connect() as conn:
        assert LEDGER_TABLES <= set(sa.inspect(conn).get_table_names())
