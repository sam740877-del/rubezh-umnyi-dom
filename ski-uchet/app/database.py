"""Подключение к БД и сессии SQLAlchemy."""
from __future__ import annotations

import os
from pathlib import Path
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / "ski.db"

DATABASE_URL = os.environ.get("SKI_DATABASE_URL", f"sqlite:///{DEFAULT_DB_PATH}")

engine = create_engine(DATABASE_URL, future=True, echo=False)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ANN001
    """Включаем контроль внешних ключей — SQLite по умолчанию его игнорирует."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def init_db() -> list[str]:
    r"""Подготовить базу к работе: создать таблицы и догнать версию схемы.

    Одна дверь для всех — запуск сервера, тесты, `manage.py`, будущий
    импортёр реестра. Урок БПО (`C:\bpo\db\schema.py`): пока каждый
    готовил базу по-своему, «старая база» находилась только на живых данных.

    Возвращает названия применённых миграций; пустой список означает, что
    база уже свежая.
    """
    from app import models  # noqa: F401  (регистрация моделей в метаданных)
    from app.migrations import apply_migrations

    models.Base.metadata.create_all(engine)

    session = SessionLocal()
    try:
        applied = apply_migrations(session)
        session.commit()
        return applied
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Сессия с автоматическим commit/rollback."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """Зависимость FastAPI."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
