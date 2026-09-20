"""SQLAlchemy engine + session factory for the Postgres catalog."""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from imagelib.db.models import database_url

engine = create_engine(database_url(), echo=False, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


def create_schema() -> None:
    """Create tables via metadata (dev convenience; init_db.sql is authoritative)."""
    from imagelib.db import models  # noqa: F401

    models.Base.metadata.create_all(bind=engine)