r"""Миграция 4: вложения.

Одна новая таблица `attachments`: файлы к приборам, поверкам, актам
и участкам.

Устройство то же, что у миграций 2 и 3, и по той же причине: `create_all`
заводит недостающие таблицы сам, но версия схемы обязана двинуться —
иначе следующая миграция окажется первой, кто заметит расхождение
(урок R-64 донора).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.migrations import table_exists

TITLE = "Вложения к приборам и документам"

TABLES = ("attachments",)




def upgrade(session: Session) -> None:
    """Завести таблицу вложений, если её ещё нет.

    Границу транзакции держит вызывающий.
    """
    missing = [name for name in TABLES if not table_exists(session, name)]
    if not missing:
        return

    from app.models import Base

    Base.metadata.create_all(
        session.connection(),
        tables=[Base.metadata.tables[name] for name in missing],
    )
