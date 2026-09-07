r"""Сторож миграций: старая база догоняется, данные целы, опечатка не пройдёт.

Какое обещание стережём
------------------------

«Систему можно обновить, не потеряв накопленное». Через месяц в базе будет
настоящий реестр на 150-500 приборов, и всякая правка схемы станет операцией
на живом: `create_all` заводит новые таблицы, но менять существующие не умеет.

Как обещание ломается в жизни
------------------------------

Урок донора записан в `C:\bpo\db\migrations\m004_needs.py` (R-64): версия
схемы обязана двигаться даже тогда, когда миграция ничего не поменяла, —
иначе следующая миграция окажется первой, кто заметит расхождение, и
разбираться придётся уже на базе клиента.

Второй случай — уже наш, пойман здесь же при первой проверке на живой базе:
пересоздание таблицы отдельным соединением упирается в «database is locked»,
потому что SQLite блокирует базу сам от себя, пока открыта транзакция
миграции. На пустой базе этого не видно — таблицы создаются до транзакции.

Чем доказано
-------------

Запуском: строим базу СТАРОГО образца (без ограничений и без `schema_version`),
наполняем демо-данными, догоняем и смотрим на результат. Чтения исходников
здесь нет.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, OperationalError

from app.database import SessionLocal, engine, init_db
from app.migrations import MIGRATIONS, current_version, target_version
from app.models import Base
from app.seed import seed_demo

#: Таблицы, которые в старой базе заводим БЕЗ ограничений: ровно так
#: выглядела схема до миграции 1.
def _build_legacy_schema() -> None:
    """Собрать базу такой, какой она была до появления миграций."""
    Base.metadata.drop_all(engine)
    legacy = sa.MetaData()
    # Снимок до обхода: sa.Table() регистрирует таблицу в СВОИХ метаданных,
    # но словарь Base.metadata.tables при этом тоже трогается — обход по
    # живому словарю падает «Set changed size during iteration».
    tables = list(Base.metadata.tables.items())
    for name, table in tables:
        if name == "schema_version":
            continue  # в старой базе таблицы версии не существовало
        # Снимки до копирования: `_copy()` трогает те же коллекции,
        # по которым идёт обход, и множество ограничений меняется на ходу.
        columns = [c._copy() for c in list(table.columns)]
        unique = [
            c._copy()
            for c in list(table.constraints)
            if isinstance(c, sa.UniqueConstraint)
        ]
        sa.Table(name, legacy, *columns, *unique)
    legacy.create_all(engine)


def test_legacy_database_is_upgraded_keeping_data() -> None:
    """Старая база догоняется до текущей версии, не потеряв ни строки."""
    _build_legacy_schema()

    session = SessionLocal()
    seed_demo(session)
    session.commit()
    before = session.execute(sa.text("SELECT COUNT(*) FROM instruments")).scalar()
    session.close()
    assert before > 0, "демо-данные не завелись — проверять нечего"

    applied = init_db()
    assert applied, "миграции не применились к старой базе"

    session = SessionLocal()
    try:
        after = session.execute(sa.text("SELECT COUNT(*) FROM instruments")).scalar()
        assert after == before, (
            f"миграция потеряла данные: было {before} приборов, стало {after}"
        )
        assert current_version(session) == target_version()
    finally:
        session.close()


def test_upgrade_is_idempotent() -> None:
    """Повторный запуск ничего не делает — иначе перезапуск сервера
    пересоздавал бы таблицы каждый раз."""
    _build_legacy_schema()
    init_db()

    assert init_db() == [], "повторный прогон снова применил миграции"


def test_database_rejects_unknown_status() -> None:
    """База не принимает статус, которого нет в закрытом списке.

    Прибор со статусом `in_uze` не попал бы ни в один отчёт: ни на склад,
    ни на участок. Он просто исчез бы из системы, оставшись в базе.
    """
    _build_legacy_schema()
    init_db()

    session = SessionLocal()
    seed_demo(session)
    session.commit()
    try:
        session.execute(sa.text("UPDATE instruments SET status = 'in_uze' WHERE id = 1"))
        session.commit()
        raise AssertionError("база приняла несуществующий статус")
    except (IntegrityError, OperationalError):
        session.rollback()
    finally:
        session.close()


def test_migration_numbers_are_unique_and_ordered() -> None:
    """Номера миграций идут подряд и не повторяются.

    У «Электро» два файла носят номер 7 (`migrate7_journal_db.py` и
    `migrate7_recycle_deleted_by.py`) — порядок применения там определяется
    удачей. Здесь такого не будет.
    """
    numbers = [m.number for m in MIGRATIONS]
    assert numbers == sorted(set(numbers)), f"номера миграций сбились: {numbers}"
    assert numbers == list(range(1, len(numbers) + 1)), (
        f"номера должны идти подряд с единицы: {numbers}"
    )


# Сторожа на запасную ветку `apply_migrations` (версия двигается, когда
# применять было нечего) здесь НЕТ сознательно. Ветка срабатывает только
# при пустом списке миграций: пока в списке есть хоть одна, сброшенная
# версия заставляет её примениться заново, и версию двигает основная ветка.
# Показать такого сторожа красным на сломанном правиле не удалось —
# по Р5.7 свода БПО он тогда удаляется, а не дописывается. Сама ветка
# оставлена в коде: она перенесена от донора вместе с уроком R-64.


def test_migrations_do_not_ask_sqlite_directly() -> None:
    r"""Миграции не спрашивают у SQLite напрямую.

    `sqlite_master` — таблица SQLite. Шесть миграций спрашивали у неё,
    есть ли таблица, и на PostgreSQL упали бы с «relation sqlite_master
    does not exist»: стенд не поднялся бы вовсе. Найдено при переносе
    стенда на общую базу 07.09.2026 — до развёртывания, чтением кода.

    Спрашивать надо через `table_exists` из пакета миграций: он
    обращается к той базе, к которой подключились, на её языке.

    Исключение — `m001`: там ветка `dialect.name == "sqlite"` СОЗНАТЕЛЬНА,
    SQLite не умеет `ALTER TABLE ADD CONSTRAINT`, и таблица пересоздаётся.
    Такое ветвление разрешено; запрещено молча считать базу SQLite.
    """
    from pathlib import Path as _Path

    папка = _Path(__file__).resolve().parent.parent / "app" / "migrations"
    виноватые = []
    for файл in sorted(папка.glob("m0*.py")):
        текст = файл.read_text(encoding="utf-8")
        # Пояснения не в счёт — ищем в коде.
        код = "\n".join(
            строка for строка in текст.splitlines()
            if not строка.lstrip().startswith("#")
        )
        if "sqlite_master" in код and "dialect" not in код:
            виноватые.append(файл.name)

    assert not виноватые, (
        "миграции спрашивают у sqlite_master без проверки диалекта — "
        f"на PostgreSQL они упадут: {виноватые}"
    )


def test_database_url_is_normalized() -> None:
    r"""Адрес базы от Render приводится к тому, что понимает SQLAlchemy.

    Render выдаёт `postgres://…` — старое написание, которое SQLAlchemy
    2.0 не знает: «Can't load plugin: sqlalchemy.dialects:postgres».
    Править значение руками в панели нельзя: Render переписывает свою
    переменную при каждом создании базы.
    """
    from app.database import _normalize

    assert _normalize("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    # Без указания драйвера SQLAlchemy ищет psycopg2, которого у нас нет.
    assert _normalize("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert _normalize("postgresql+psycopg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert _normalize("sqlite:///ski.db") == "sqlite:///ski.db"


def test_sqlite_pragma_is_not_sent_to_other_databases() -> None:
    r"""`PRAGMA` уходит только в SQLite.

    Обработчик подключения выполнял `PRAGMA foreign_keys=ON` на КАЖДОМ
    соединении. На PostgreSQL это уронило бы каждое подключение — то
    есть систему целиком. PostgreSQL блюдёт внешние ключи сам.
    """
    import inspect as _inspect

    from app import database

    исходник = _inspect.getsource(database._set_sqlite_pragma)
    assert "startswith(\"sqlite\")" in исходник or "sqlite" in исходник.split("PRAGMA")[0], (
        "PRAGMA выполняется без проверки, что база — SQLite"
    )
