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

def _normalize(url: str) -> str:
    """Привести адрес базы к тому виду, который понимает SQLAlchemy 2.0.

    Render (и Heroku до него) выдают адрес вида `postgres://…`. Это
    старое написание: SQLAlchemy 2.0 его не знает и падает с
    «Can't load plugin: sqlalchemy.dialects:postgres».

    Чинить это правкой значения в панели нельзя — Render переписывает
    свою переменную при каждом создании базы. Значит, приводить должны мы.
    """
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        # Без указания драйвера SQLAlchemy ищет psycopg2, которого у нас
        # нет: стоит psycopg 3. Говорим прямо, чем подключаться.
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


DATABASE_URL = _normalize(
    os.environ.get("SKI_DATABASE_URL", "").strip() or f"sqlite:///{DEFAULT_DB_PATH}"
)

def _engine_options() -> dict:
    """Настройки движка. Для базы в памяти — особые.

    SQLite держит базу «:memory:» ровно столько, сколько открыто
    соединение: обычный пул выдал бы каждой сессии СВОЮ пустую базу.
    `StaticPool` держит одно соединение на всё время, и все сессии
    видят одни данные.

    Нужно это только тестам, но живёт здесь: подключение — дело
    `database.py`, а не тех, кто им пользуется.
    """
    if ":memory:" in DATABASE_URL:
        from sqlalchemy.pool import StaticPool

        return {
            "poolclass": StaticPool,
            # Сессии ходят из разных потоков (веб-клиент в тестах),
            # а SQLite по умолчанию это запрещает.
            "connect_args": {"check_same_thread": False},
        }
    return {}


engine = create_engine(DATABASE_URL, future=True, echo=False, **_engine_options())
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ANN001
    """Включаем контроль внешних ключей — SQLite по умолчанию его игнорирует.

    Только для SQLite: `PRAGMA` — его команда, и на PostgreSQL она
    уронила бы КАЖДОЕ подключение, то есть всю систему целиком.
    PostgreSQL блюдёт внешние ключи сам, и просить его об этом не нужно.
    """
    if not DATABASE_URL.startswith("sqlite"):
        return
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
