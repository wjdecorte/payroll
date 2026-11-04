"""
Alembic environment configuration.

This file is loaded by Alembic when running migrations. It imports all
SQLModel models so their metadata is available for autogenerate support.
"""

from logging.config import fileConfig

from alembic import context
from sqlmodel import SQLModel

# Import all models to register them with SQLModel metadata
import payroll.audit.models  # noqa: F401
import payroll.auth.models  # noqa: F401
import payroll.ledger.models  # noqa: F401
import payroll.payroll_run.config_models  # noqa: F401
import payroll.payroll_run.models  # noqa: F401
import payroll.receivables.models  # noqa: F401

# Alembic Config object — gives access to values in alembic.ini
config = context.config

# Set up Python logging from the alembic.ini [loggers] section, but only for a
# standalone ``alembic`` run. When the app (lifespan, in every worker) or the
# runner calls Alembic, it sets ``configure_logger=False``: fileConfig would
# otherwise replace the root handlers and, with the default
# disable_existing_loggers=True, silence every payroll.* and uvicorn.* logger.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# SQLModel's metadata contains all registered table models
target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.

    Configures the context with just a URL (no engine), useful for generating
    SQL scripts without a live database connection.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode with a live database connection.
    """
    from sqlalchemy import create_engine

    connectable = create_engine(config.get_main_option("sqlalchemy.url"))

    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()  # do not leave pooled connections behind for the GC


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
