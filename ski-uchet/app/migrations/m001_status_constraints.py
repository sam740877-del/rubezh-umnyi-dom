r"""Миграция 1: статусы — закрытый список с ограничением на уровне базы.

Долг, замеченный при сверке со сводом БПО (`C:\bpo\RULES.md`, Р7.2:
«статусы — закрытый список с ограничением на уровне базы»). До этой миграции
поля `status` были обычными строками: словари `INSTRUMENT_STATUSES` и прочие
описывали допустимые значения только для человека, а база приняла бы любую
опечатку и тихо завела прибор в несуществующем состоянии.

Почему это важнее, чем кажется: невозможное состояние не ловится тестами
задним числом. Прибор со статусом `in_uze` не попадёт ни в один отчёт, не
покажется ни на складе, ни на участке — он просто исчезнет из системы,
оставшись в базе.

Устройство: ограничения вносятся не этой миграцией напрямую, а через
`Base.metadata` — они объявлены в `app/models.py` рядом с полями, где им и
место. Здесь только перезаливка таблиц, у которых ограничения появились
задним числом.

**Оговорка про SQLite.** SQLite не умеет `ALTER TABLE ADD CONSTRAINT`:
ограничение можно внести только пересозданием таблицы с переносом данных.
Для PostgreSQL (этап 2) достаточно было бы `ALTER TABLE`. Поэтому миграция
разделена по движкам — иначе переезд на PostgreSQL пришлось бы начинать
с починки миграций.
"""
from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

TITLE = "Статусы: закрытый список на уровне базы"

#: Таблица → (колонка, допустимые значения). Держим здесь, а не берём из
#: моделей: миграция описывает состояние схемы НА СВОЙ МОМЕНТ и не должна
#: меняться задним числом, когда в словарь добавят новый статус.
GUARDED = {
    "contracts": ("status", ("active", "suspended", "closed")),
    "sites": ("status", ("active", "mothballed", "closed")),
    "instruments": ("status", ("warehouse", "in_use", "verification", "repair", "written_off")),
    "change_requests": ("status", ("new", "approved", "rejected")),
    "verifications": ("result", ("ok", "fail")),
}


def _has_check(session: Session, table: str, column: str) -> bool:
    """Есть ли уже ограничение на эту колонку.

    Читаем определение таблицы из самой базы: имена ограничений задаёт
    SQLAlchemy, и полагаться на них нельзя — вернее посмотреть, упоминается
    ли колонка в разделе CHECK.

    Спрашивать приходится по-разному. У SQLite определение таблицы лежит
    текстом в `sqlite_master`; у PostgreSQL такой таблицы нет вовсе, и
    ограничения живут в системном каталоге. Развёртывание 08.09.2026
    упало здесь: «relation "sqlite_master" does not exist» — эту функцию
    я пропустил, правя остальные миграции.
    """
    if session.connection().dialect.name == "sqlite":
        row = session.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
            {"name": table},
        ).fetchone()
        if row is None or not row[0]:
            return False
        return "CHECK" in row[0].upper() and column in row[0]

    # У прочих баз спрашиваем через SQLAlchemy: он знает, как устроен
    # системный каталог каждой, и нам не нужно знать этого самим.
    for constraint in inspect(session.connection()).get_check_constraints(table):
        if column in (constraint.get("sqltext") or ""):
            return True
    return False


def _rebuild_sqlite_table(session: Session, table: str) -> None:
    """Пересоздать таблицу с ограничениями, сохранив данные.

    SQLite не умеет добавлять CHECK существующей таблице. Обходной путь —
    штатный, описан в документации SQLite: новая таблица, перенос строк,
    подмена. Внешние ключи на время переноса выключаются, иначе перенос
    упрётся в ссылки соседних таблиц.
    """
    from app.models import Base

    definition = Base.metadata.tables[table]
    columns = ", ".join(f'"{c.name}"' for c in definition.columns)
    tmp = f"{table}__new"

    session.execute(text("PRAGMA foreign_keys=OFF"))
    definition.name = tmp
    try:
        # Создаём ТЕМ ЖЕ соединением, что ведёт миграцию. Через `get_bind()`
        # SQLAlchemy взял бы из пула второе, а SQLite блокирует базу сам от
        # себя: транзакция миграции уже открыта, и CREATE TABLE упирается
        # в «database is locked». Поймано на живой базе с данными.
        definition.create(session.connection())
    finally:
        definition.name = table

    session.execute(text(f'INSERT INTO "{tmp}" ({columns}) SELECT {columns} FROM "{table}"'))
    session.execute(text(f'DROP TABLE "{table}"'))
    session.execute(text(f'ALTER TABLE "{tmp}" RENAME TO "{table}"'))
    session.execute(text("PRAGMA foreign_keys=ON"))


def upgrade(session: Session) -> None:
    """Внести ограничения статусов.

    Границу транзакции держит вызывающий.
    """
    connection = session.connection()
    existing = set(inspect(connection).get_table_names())

    for table, (column, _values) in GUARDED.items():
        if table not in existing:
            # Таблицы ещё нет — create_all заведёт её сразу с ограничением.
            continue
        if _has_check(session, table, column):
            continue
        if connection.dialect.name == "sqlite":
            _rebuild_sqlite_table(session, table)
        else:
            allowed = ", ".join(f"'{v}'" for v in _values)
            session.execute(
                text(
                    f'ALTER TABLE "{table}" ADD CONSTRAINT ck_{table}_{column} '
                    f'CHECK ("{column}" IN ({allowed}))'
                )
            )
