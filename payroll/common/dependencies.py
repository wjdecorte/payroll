from collections.abc import Generator

from sqlalchemy import create_engine
from sqlmodel import Session

from payroll import app_settings

# Build connect_args based on database type.
# SQLite requires check_same_thread=False for use with FastAPI's async workers.
_connect_args: dict = (
    {"check_same_thread": False} if app_settings.database_url.startswith("sqlite") else {}
)

engine = create_engine(
    app_settings.database_url,
    connect_args=_connect_args,
    echo=app_settings.debug_mode,
)


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency that provides a SQLModel database session."""
    with Session(engine) as session:
        yield session
