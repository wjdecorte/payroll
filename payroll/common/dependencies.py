from collections.abc import Generator

from sqlalchemy import create_engine
from sqlmodel import Session

from payroll import app_settings

# A lock wait fails after 10 s instead of holding a request (and its worker) forever.
engine = create_engine(
    app_settings.database_url,
    echo=app_settings.debug_mode,
    connect_args={"options": "-c lock_timeout=10000"},
)


def get_session() -> Generator[Session]:
    """FastAPI dependency that provides a SQLModel database session."""
    with Session(engine) as session:
        yield session
