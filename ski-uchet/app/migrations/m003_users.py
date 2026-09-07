r"""Миграция 3: учётные записи.

Одна новая таблица `users`: логин, хеш пароля, роль, признак действующей
записи.

Устройство то же, что у миграций 1 и 2, и по той же причине: `create_all`
заводит недостающие таблицы сам, но версия схемы обязана двинуться —
иначе следующая миграция окажется первой, кто заметит расхождение, и
разбираться придётся уже на базе клиента (урок R-64 донора).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.migrations import table_exists

TITLE = "Учётные записи и роли"

TABLES = ("users",)




def upgrade(session: Session) -> None:
    """Завести таблицу учётных записей, если её ещё нет.

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
