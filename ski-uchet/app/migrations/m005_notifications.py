r"""Миграция 5: ящик уведомлений.

Одна новая таблица `notifications`: событие рождает запись на каждого
адресата, доставщики сменные.

Устройство то же, что у миграций 2-4, и по той же причине: `create_all`
заводит недостающие таблицы сам, но версия схемы обязана двинуться —
иначе следующая миграция окажется первой, кто заметит расхождение
(урок R-64 донора).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.migrations import table_exists

TITLE = "Ящик уведомлений"

TABLES = ("notifications",)




def upgrade(session: Session) -> None:
    """Завести таблицу уведомлений, если её ещё нет.

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
