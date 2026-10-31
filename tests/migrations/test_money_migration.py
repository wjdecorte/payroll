"""
Tests for the float -> NUMERIC(14,2) money migration.

Every test runs against its own scratch database (created and dropped here) on the
test Postgres server, so it never touches the shared test schema or any real data.
The Alembic runner reads ``app_settings.database_url`` at call time, so pointing
that setting at the scratch database is enough to aim the migrations there.
"""

from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from payroll import app_settings
from payroll.migrations import alembic_runner
from tests.conftest import TEST_DATABASE_URL

SCRATCH_DB = "payroll_money_migration_test"

INITIAL_REVISION = "c3a8d2f19b40"
PRE_MONEY_REVISION = "4a7f8b2c9d1e"  # head before the money migration

RUN_MONEY_COLUMNS = [
    "gross_salary",
    "health_insurance",
    "hsa_contribution",
    "ytd_fica_before",
    "fica_wages",
    "income_tax_wages",
    "ss_taxable_wages",
    "ss_ee",
    "ss_er",
    "medicare_ee",
    "additional_medicare",
    "total_medicare_ee",
    "medicare_er",
    "federal_withholding",
    "ga_withholding",
    "total_employee_deductions",
    "net_pay",
    "total_employer_taxes",
    "total_employer_cost",
    "ytd_fica_after",
]
CONFIG_MONEY_COLUMNS = [
    "default_gross_salary",
    "default_health_insurance",
    "default_hsa_contribution",
]

# Float values chosen to exercise rounding: 8000.1, 0.0 and 1234.5678 (-> 1234.57).
RUN_FLOATS = {col: 100.25 for col in RUN_MONEY_COLUMNS} | {
    "gross_salary": 8000.1,
    "health_insurance": 0.0,
    "hsa_contribution": 1234.5678,
    "net_pay": 5123.449,
}
CONFIG_FLOATS = {
    "default_gross_salary": 9000.0,
    "default_health_insurance": 725.5,
    "default_hsa_contribution": 350.333,
}


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


def _insert_rows(engine, run_values: dict, config_values: dict) -> None:
    run = {
        "create_date": sa.func.now(),
        "modify_date": sa.func.now(),
        "pay_period": "2026-01",
        "health_in_income_tax": True,
        "hsa_in_income_tax": False,
        "notes": "legacy float row",
        "tax_year": 2026,
        **run_values,
    }
    config = {
        "create_date": sa.func.now(),
        "modify_date": sa.func.now(),
        "default_health_in_income_tax": True,
        "default_hsa_in_income_tax": False,
        "default_use_previous_ytd_fica": True,
        "default_save_run": False,
        **{c: "Acct" for c in _account_columns()},
        "federal_due_date_note": "fed",
        "georgia_due_date_note": "ga",
        **config_values,
    }
    meta = sa.MetaData()
    meta.reflect(engine, only=["payroll_run", "payroll_config"])
    with engine.begin() as conn:
        conn.execute(meta.tables["payroll_run"].insert().values(**run))
        conn.execute(meta.tables["payroll_config"].insert().values(**config))


def _account_columns() -> list[str]:
    return [f"acct_{name}" for name in app_settings.accounts]


def _row(engine, table: str, columns: list[str]) -> dict:
    with engine.connect() as conn:
        result = conn.execute(sa.text(f"SELECT {', '.join(columns)} FROM {table}"))
        return dict(result.mappings().one())


def _column_types(engine, table: str) -> dict[str, sa.types.TypeEngine]:
    return {c["name"]: c["type"] for c in sa.inspect(engine).get_columns(table)}


def _assert_numeric_14_2(engine) -> None:
    for table, columns in (
        ("payroll_run", RUN_MONEY_COLUMNS),
        ("payroll_config", CONFIG_MONEY_COLUMNS),
    ):
        types = _column_types(engine, table)
        for col in columns:
            assert isinstance(types[col], sa.Numeric), (table, col, types[col])
            assert not isinstance(types[col], sa.Float), (table, col, types[col])
            assert (types[col].precision, types[col].scale) == (14, 2), (table, col)


def _assert_converted_values(engine) -> None:
    run = _row(engine, "payroll_run", RUN_MONEY_COLUMNS)
    assert run["gross_salary"] == Decimal("8000.10")
    assert run["health_insurance"] == Decimal("0.00")
    assert run["hsa_contribution"] == Decimal("1234.57")
    assert run["net_pay"] == Decimal("5123.45")
    assert run["ss_ee"] == Decimal("100.25")
    assert all(isinstance(v, Decimal) for v in run.values())
    assert str(run["gross_salary"]) == "8000.10"

    config = _row(engine, "payroll_config", CONFIG_MONEY_COLUMNS)
    assert config == {
        "default_gross_salary": Decimal("9000.00"),
        "default_health_insurance": Decimal("725.50"),
        "default_hsa_contribution": Decimal("350.33"),
    }


def test_upgrade_converts_float_money_to_numeric(scratch_engine):
    alembic_runner.upgrade(PRE_MONEY_REVISION)
    _insert_rows(scratch_engine, RUN_FLOATS, CONFIG_FLOATS)

    alembic_runner.upgrade()

    _assert_numeric_14_2(scratch_engine)
    _assert_converted_values(scratch_engine)


def test_downgrade_restores_float(scratch_engine):
    alembic_runner.upgrade(PRE_MONEY_REVISION)
    _insert_rows(scratch_engine, RUN_FLOATS, CONFIG_FLOATS)
    alembic_runner.upgrade()

    alembic_runner.downgrade(PRE_MONEY_REVISION)

    for table, columns in (
        ("payroll_run", RUN_MONEY_COLUMNS),
        ("payroll_config", CONFIG_MONEY_COLUMNS),
    ):
        types = _column_types(scratch_engine, table)
        for col in columns:
            assert isinstance(types[col], sa.Float), (table, col, types[col])

    run = _row(scratch_engine, "payroll_run", RUN_MONEY_COLUMNS)
    assert run["gross_salary"] == 8000.1
    assert run["hsa_contribution"] == 1234.57
    assert all(isinstance(v, float) for v in run.values())
    config = _row(scratch_engine, "payroll_config", CONFIG_MONEY_COLUMNS)
    assert config["default_health_insurance"] == 725.5

    # And the round trip back up still works.
    alembic_runner.upgrade()
    _assert_numeric_14_2(scratch_engine)


@pytest.mark.parametrize(
    "legacy_revision",
    [INITIAL_REVISION, PRE_MONEY_REVISION],
    ids=["without-next_journal_no", "with-next_journal_no"],
)
def test_legacy_db_without_alembic_version_is_stamped_and_upgraded(scratch_engine, legacy_revision):
    # Recreate a database built by SQLModel.create_all before Alembic: the old
    # (float) schema with no alembic_version table.
    alembic_runner.upgrade(legacy_revision)
    with scratch_engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE alembic_version")
    _insert_rows(scratch_engine, RUN_FLOATS, CONFIG_FLOATS)

    alembic_runner.upgrade()

    _assert_numeric_14_2(scratch_engine)
    _assert_converted_values(scratch_engine)
    assert "next_journal_no" in _column_types(scratch_engine, "payroll_config")
    with scratch_engine.connect() as conn:
        version = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
    assert version == alembic_runner._HEAD_REVISION


def test_head_revision_constant_matches_script_head():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_runner._get_alembic_config())
    assert script.get_current_head() == alembic_runner._HEAD_REVISION


def test_models_match_migrated_schema(scratch_engine):
    """The SQLModel tables and the migrated database agree (types included)."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlmodel import SQLModel

    import payroll.payroll_run.config_models  # noqa: F401
    import payroll.payroll_run.models  # noqa: F401

    alembic_runner.upgrade()
    with scratch_engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diffs = compare_metadata(ctx, SQLModel.metadata)
    assert diffs == []


_BOOT_SCRIPT = """
import sys, time
start_at = float(sys.argv[1])
from payroll.migrations import alembic_runner  # settings read DATABASE_URL from env
time.sleep(max(0.0, start_at - time.time()))
alembic_runner.upgrade()
"""


def test_concurrent_upgrades_are_serialized(scratch_engine):
    """Two workers booting at once must both succeed (gunicorn WORKERS=2).

    Separate processes, like gunicorn workers: Alembic's ``context`` is a
    process-global, so threads would collide inside Alembic instead of
    exercising the database-level race.
    """
    import os
    import subprocess
    import sys
    import time

    alembic_runner.upgrade(PRE_MONEY_REVISION)
    _insert_rows(scratch_engine, RUN_FLOATS, CONFIG_FLOATS)

    env = os.environ | {"DATABASE_URL": app_settings.database_url}
    start_at = str(time.time() + 3)  # both processes start upgrading at the same instant
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _BOOT_SCRIPT, start_at],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    results = [(p.wait(timeout=60), p.stderr.read()) for p in procs]

    for returncode, stderr in results:
        assert returncode == 0, stderr
    _assert_numeric_14_2(scratch_engine)
    _assert_converted_values(scratch_engine)
    with scratch_engine.connect() as conn:
        version = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
    assert version == alembic_runner._HEAD_REVISION
