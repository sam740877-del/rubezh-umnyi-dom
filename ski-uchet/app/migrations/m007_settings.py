r"""Миграция 7: настройки.

Одна новая таблица `settings`: то, что правит человек, а не программист.

Устройство то же, что у миграций 2-6, и по той же причине: `create_all`
заводит недостающие таблицы сам, но версия схемы обязана двинуться
(урок R-64 донора).
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

TITLE = "Настройки"

TABLES = ("settings",)


def _exists(session: Session, table: str) -> bool:
    """Есть ли такая таблица в базе."""
    row = session.execute(
        text("SELECT name FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {"name": table},
    ).fetchone()
    return row is not None


def upgrade(session: Session) -> None:
    """Завести таблицу настроек, если её ещё нет.

    Границу транзакции держит вызывающий.
    """
    missing = [name for name in TABLES if not _exists(session, name)]
    if not missing:
        return

    from app.models import Base

    Base.metadata.create_all(
        session.connection(),
        tables=[Base.metadata.tables[name] for name in missing],
    )
