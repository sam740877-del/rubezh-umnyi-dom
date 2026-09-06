r"""Миграция 6: привязки и приглашения бота.

Две таблицы: `bot_links` — кто из мессенджера к чему привязан,
`bot_invites` — коды-приглашения, которыми администратор впускает людей.

Устройство то же, что у миграций 2-5, и по той же причине: `create_all`
заводит недостающие таблицы сам, но версия схемы обязана двинуться
(урок R-64 донора).
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

TITLE = "Бот: привязки и приглашения"

TABLES = ("bot_links", "bot_invites")


def _exists(session: Session, table: str) -> bool:
    """Есть ли такая таблица в базе."""
    row = session.execute(
        text("SELECT name FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {"name": table},
    ).fetchone()
    return row is not None


def upgrade(session: Session) -> None:
    """Завести таблицы бота, если их ещё нет.

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
