r"""Миграции схемы: пронумерованы, применяются по порядку, ровно один раз.

Версия схемы хранится в самой базе, в таблице `schema_version`. Запуск
приложения сам догоняет базу до текущей версии — руками ничего гонять не надо.

Взято у БПО (`C:\bpo\db\migrations\__init__.py`) вместе с обоснованием.
Там же записан урок R-64, ради которого механизм и существует: **версия схемы
обязана двигаться даже тогда, когда миграция ничего не поменяла**, иначе
следующая миграция окажется первой, кто заметит расхождение, и разбираться
придётся уже на живой базе клиента.

Отличие от донора одно, и оно от природы веба: у БПО программа
однопользовательская, а здесь несколько человек работают одновременно.
Поэтому миграции применяются при старте сервера, до приёма первого запроса,
а не в момент, когда кто-то уже листает экран.

Как добавить миграцию:

1. создать `app/migrations/m00X_короткое_имя.py` с функцией
   `upgrade(session) -> None` и строкой `TITLE = "что делает"`;
2. дописать её в список `MIGRATIONS` ниже — порядок в списке и есть
   порядок применения;
3. сторож `tests/test_migrations.py` сам проверит, что база догоняется
   и что повторный прогон ничего не делает.

Почему не Alembic: система ставится на сервер конторы, где её обслуживает
системный администратор, а не разработчик. Своих тридцати строк хватает,
а лишний инструмент со своей консолью и своими файлами настроек — это ещё
одно место, где развёртывание может встать.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

from sqlalchemy.orm import Session

from app.migrations import m001_status_constraints
from app.models import SchemaVersion


class Migration(NamedTuple):
    """Одна миграция: номер, человеческое название, функция."""

    number: int
    title: str
    upgrade: Callable[[Session], None]


#: Все миграции проекта по порядку.
MIGRATIONS: list[Migration] = [
    Migration(1, m001_status_constraints.TITLE, m001_status_constraints.upgrade),
]


def current_version(session: Session) -> int:
    """Номер последней применённой миграции. Пустая база — 0."""
    row = session.query(SchemaVersion).first()
    return int(row.version) if row else 0


def set_version(session: Session, version: int) -> None:
    """Записать версию схемы (строка в таблице всегда одна)."""
    row = session.query(SchemaVersion).first()
    if row is None:
        session.add(SchemaVersion(version=version))
    else:
        row.version = version


def target_version() -> int:
    """До какой версии положено догнать базу."""
    return MIGRATIONS[-1].number if MIGRATIONS else 0


def apply_migrations(session: Session) -> list[str]:
    """Догнать базу до текущей версии схемы.

    Возвращает список названий применённых миграций: пустой список означает
    «база уже свежая». Границу транзакции держит вызывающий — здесь ни
    `commit()`, ни `rollback()`.
    """
    version = current_version(session)
    applied: list[str] = []
    for migration in MIGRATIONS:
        if migration.number <= version:
            continue
        migration.upgrade(session)
        set_version(session, migration.number)
        version = migration.number
        applied.append(migration.title)
    if not applied and current_version(session) != target_version():
        set_version(session, target_version())
    return applied
