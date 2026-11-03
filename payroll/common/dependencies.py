from collections.abc import Generator

from sqlalchemy import create_engine
from sqlmodel import Session

from payroll import app_settings

engine = create_engine(app_settings.database_url, echo=app_settings.debug_mode)


def get_session() -> Generator[Session]:
    """FastAPI dependency that provides a SQLModel database session."""
    with Session(engine) as session:
        yield session
