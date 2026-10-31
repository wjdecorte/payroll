"""
Alembic environment configuration.

This file is loaded by Alembic when running migrations. It imports all
SQLModel models so their metadata is available for autogenerate support.
"""

from logging.config import fileConfig

from alembic import context
from sqlmodel import SQLModel

# Import all models to register them with SQLModel metadata
import payroll.auth.models  # noqa: F401
import payroll.payroll_run.config_models  # noqa: F401
import payroll.payroll_run.models  # noqa: F401

# Alembic Config object — gives access to values in alembic.ini
config = context.config

# Set up Python logging from the alembic.ini [loggers] section
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

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

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
