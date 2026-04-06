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


def _get_alembic_config() -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    # Override sqlalchemy.url from application settings so we never have
    # to hard-code the database URL in alembic.ini
    cfg.set_main_option("sqlalchemy.url", app_settings.database_url)
    return cfg


def upgrade(revision: str = "head") -> None:
    """Upgrade the database to the given revision (default: latest)."""
    logger.info("Running database migration: upgrade to '%s'", revision)
    command.upgrade(_get_alembic_config(), revision)


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
    # Allow quick CLI use: python -m payroll.migrations.alembic_runner upgrade
    import logging

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
