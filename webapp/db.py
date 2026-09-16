"""Silnik SQLAlchemy + sesje.

SQLite z WAL jako default (zero infra dla PoC). `DATABASE_URL` pozwala
przesiąść się na Postgresa bez zmian w kodzie — kolejka zadań używa
`SELECT ... FOR UPDATE SKIP LOCKED` tam, gdzie backend to wspiera
(patrz `webapp/jobs.py`).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from webapp.config import settings
from webapp.models import Base

_is_sqlite = settings.database_url.startswith("sqlite")

engine = create_engine(
    settings.database_url,
    future=True,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _rec):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        # WAL: worker pisze, web czyta — bez tego dostajemy "database is locked".
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()


SessionLocal = sessionmaker(
    bind=engine, class_=Session, expire_on_commit=False, future=True
)


log = logging.getLogger("webapp.db")


def init_db() -> None:
    Base.metadata.create_all(engine)
    add_missing_columns()


def add_missing_columns() -> int:
    """Dołóż kolumny, których brakuje istniejącym tabelom.

    `create_all` tworzy tylko brakujące TABELE — nowa kolumna w modelu na
    starej bazie kończy się `no such column` przy pierwszym zapytaniu. Pełny
    Alembic to za dużo na PoC, a każda zmiana schematu do tej pory była
    addytywna (nowa, nullowalna kolumna). Tyle właśnie robimy tutaj — i nic
    więcej: NOT NULL bez defaultu, zmiana typu czy usunięcie kolumny
    wymagają migracji pisanej ręcznie.
    """
    insp = inspect(engine)
    added = 0
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            if not col.nullable and col.default is None and col.server_default is None:
                log.warning(
                    "column %s.%s is NOT NULL without a default — add it by hand",
                    table.name,
                    col.name,
                )
                continue
            ddl = (
                f"ALTER TABLE {table.name} ADD COLUMN {col.name} "
                f"{col.type.compile(dialect=engine.dialect)}"
            )
            with engine.begin() as conn:
                conn.execute(text(ddl))
            log.info("schema: added column %s.%s", table.name, col.name)
            added += 1
    return added


@contextmanager
def session_scope() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
