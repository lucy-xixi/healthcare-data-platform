"""SQLite engine and schema creation."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from healthcare_data_platform.persistence.models import Base


def default_db_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "platform.sqlite"


def get_engine(db_path: Path | None = None) -> Engine:
    path = db_path or default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}",
        future=True,
        poolclass=NullPool,
        connect_args={"timeout": 120},
    )
    event.listen(engine, "connect", _enable_sqlite)
    return engine


def _enable_sqlite(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA temp_store=MEMORY")
    cursor.execute("PRAGMA cache_size=-131072")
    cursor.close()


def init_db(engine: Engine | None = None) -> Engine:
    bound = engine or get_engine()
    Base.metadata.create_all(bound)
    return bound
