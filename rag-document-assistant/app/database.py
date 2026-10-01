from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.configure import DATABASE_URL


engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)


def get_db_session() -> Generator[Session, None, None]:
    """Provide one SQLAlchemy session for a request or unit of work."""
    with SessionLocal() as session:
        yield session


def check_database_connection() -> None:
    """Raise when PostgreSQL cannot serve a basic query."""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
