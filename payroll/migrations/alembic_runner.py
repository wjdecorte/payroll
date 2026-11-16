"""
Alembic runner helpers.

Called by app.py on startup to ensure the database schema is current.
Can also be invoked manually from the project root:

    python -m payroll.migrations.alembic_runner upgrade
    python -m payroll.migrations.alembic_runner downgrade base
"""

import logging
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

from payroll import app_settings

logger = logging.getLogger(f"{app_settings.logger_name}.migrations")

# Resolve the alembic.ini file relative to this file's location
_ALEMBIC_INI = Path(__file__).parent / "alembic.ini"

# Revision IDs — update _HEAD_REVISION whenever a new migration is added.
_INITIAL_REVISION = "c3a8d2f19b40"  # baseline tables, no next_journal_no
_JOURNAL_NO_REVISION = "4a7f8b2c9d1e"  # adds next_journal_no to payroll_config
_MONEY_REVISION = "9d4e6b1f2a37"  # money columns float -> NUMERIC(14,2)
_HEAD_REVISION = "e5f1b3c9d7a2"  # payroll ledger (run lifecycle, settings, tax payments)

# Postgres advisory-lock key serializing migrations across processes (e.g. two
# gunicorn workers booting at once). Arbitrary constant, unique to this app.
_MIGRATION_LOCK_KEY = 7_322_918_604_150_421


def _get_alembic_config() -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    # Keep the caller's logging setup: env.py skips alembic.ini's fileConfig.
    cfg.attributes["configure_logger"] = False
    # Override sqlalchemy.url from application settings so we never have
    # to hard-code the database URL in alembic.ini
    cfg.set_main_option("sqlalchemy.url", app_settings.database_url)
    return cfg


def _get_legacy_stamp_revision() -> str | None:
    """
    If the database has application tables but no alembic_version table (created
    by SQLModel's create_all before Alembic was introduced), return the revision
    to stamp at so only pending migrations are applied.

    Returns None when the database is either fresh (no app tables) or already
    managed by Alembic (has alembic_version).

    Stamping logic:
    - DB lacks next_journal_no → stamp at _INITIAL_REVISION so every migration runs
    - DB has next_journal_no but float money → stamp at _JOURNAL_NO_REVISION so
      the money-to-numeric migration runs
    - DB has next_journal_no and numeric money → stamp at _MONEY_REVISION (the last
      schema create_all could have produced); later migrations, e.g. the auth
      tables, still run
    """
    from sqlalchemy import Float, create_engine, inspect

    engine = create_engine(app_settings.database_url)
    try:
        with engine.connect() as conn:
            inspector = inspect(conn)
            table_names = inspector.get_table_names()
            has_app_tables = "payroll_run" in table_names or "payroll_config" in table_names
            has_alembic = "alembic_version" in table_names

            if not has_app_tables or has_alembic:
                return None  # fresh DB or already Alembic-managed

            # Legacy DB — decide which revision to stamp at based on schema state
            if "payroll_config" in table_names:
                cols = {c["name"] for c in inspector.get_columns("payroll_config")}
                has_next_journal_no = "next_journal_no" in cols
            else:
                has_next_journal_no = False

            if not has_next_journal_no:
                # Column missing — stamp at initial so the add-column migration runs
                return _INITIAL_REVISION

            money_types = [
                c["type"]
                for table, col in (
                    ("payroll_run", "gross_salary"),
                    ("payroll_config", "default_gross_salary"),
                )
                if table in table_names
                for c in inspector.get_columns(table)
                if c["name"] == col
            ]
            if any(isinstance(t, Float) for t in money_types):
                # Money still stored as float — let the money-to-numeric migration run
                return _JOURNAL_NO_REVISION
            return _MONEY_REVISION
    finally:
        engine.dispose()


def upgrade(revision: str = "head") -> None:
    """
    Upgrade the database to the given revision (default: latest).

    Automatically handles legacy databases that were created by SQLModel's
    create_all before Alembic was introduced: inspects the actual schema to
    determine the correct revision to stamp at, then runs any pending migrations.

    Holds a Postgres session-level advisory lock for the whole check/stamp/upgrade,
    so concurrent callers (several workers starting together) run one at a time:
    the later ones wait, then find the database already at the target revision.
    """
    from sqlalchemy import create_engine, text

    cfg = _get_alembic_config()
    lock_engine = create_engine(app_settings.database_url)
    try:
        with lock_engine.connect() as lock_conn:
            lock_conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _MIGRATION_LOCK_KEY})
            lock_conn.commit()  # session-level lock: keep no transaction open while waiting
            try:
                stamp_at = _get_legacy_stamp_revision()
                if stamp_at is not None:
                    logger.info(
                        "Legacy database detected (no alembic_version). Stamping at '%s'.",
                        stamp_at,
                    )
                    command.stamp(cfg, stamp_at)

                logger.info("Running database migration: upgrade to '%s'", revision)
                command.upgrade(cfg, revision)
            finally:
                _release_migration_lock(lock_conn)
    finally:
        lock_engine.dispose()


def _release_migration_lock(lock_conn) -> None:
    """
    Release the migration advisory lock. Never raises: a failure here (e.g. the
    lock connection died, which releases a session-level lock anyway) is logged
    and must not replace the migration's own exception or outcome.
    """
    from sqlalchemy import text

    try:
        lock_conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _MIGRATION_LOCK_KEY})
        lock_conn.commit()
    except Exception as exc:
        logger.warning(
            "Could not release the migration advisory lock (%s); it is released when "
            "its connection closes.",
            type(exc).__name__,
        )


def downgrade(revision: str) -> None:
    """Downgrade the database to the given revision."""
    logger.info("Running database migration: downgrade to '%s'", revision)
    command.downgrade(_get_alembic_config(), revision)


def generate_revision(message: str, autogenerate: bool = True) -> None:
    """Generate a new migration revision file."""
    command.revision(
        _get_alembic_config(),
        message=message,
        autogenerate=autogenerate,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("Usage: python -m payroll.migrations.alembic_runner <upgrade|downgrade> [revision]")
        sys.exit(1)

    cmd = sys.argv[1]
    rev = sys.argv[2] if len(sys.argv) > 2 else "head"

    if cmd == "upgrade":
        upgrade(rev)
    elif cmd == "downgrade":
        downgrade(rev)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
