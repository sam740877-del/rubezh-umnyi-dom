r"""Сторож резервных копий: копия жива, а её молчание слышно.

Какое обещание стережём
------------------------

«Если база пропадёт, будет из чего восстановить». Обещание кажется
исполненным, пока копию не попробуют открыть, — и тогда узнают правду
в самый неподходящий момент.

Как обещание ломается в жизни
------------------------------

**Повреждённая копия неотличима от целой.** Урок «Электро» (шаг 235):
копирование считало успехом любой непустой файл, а «обрыв места на диске
оставляет файл, который есть, но не читается как SQLite-база — незаметно
до момента восстановления». Их вывод: «тихая порча копии — худший отказ
бэкапа: выглядит успешным, а спасти не может».

**Молчание неотличимо от нормы.** Их же правило (шаг 236): «молчание
системы, которая должна была сработать сама, проверяется по внешнему
факту (файл на диске), а не по внутренней памяти о собственном последнем
успехе». У «Заявок» первая версия сторожа читала служебную отметку
и «кричала „не создавался ни разу“ на базе с восемью реальными файлами
в папке».

Чем доказано
-------------

Запуском на временной папке: снимаем копию, читаем из неё данные,
портим файл и смотрим, заметит ли система.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from app import backup
from app.models import Contract, Instrument, InstrumentType
from app.seed import seed_demo


@pytest.fixture()
def folder(tmp_path, monkeypatch):
    """Своя папка копий на каждый сценарий."""
    target = tmp_path / "backups"
    monkeypatch.setenv("SKI_BACKUP_DIR", str(target))
    return target


@pytest.fixture()
def filled(session):
    """База с данными — иначе копировать нечего."""
    seed_demo(session)
    session.commit()
    return session


def test_backup_is_created_and_holds_the_data(filled, folder) -> None:
    """Копия снимается и содержит те же данные, что и база."""
    expected = filled.query(Instrument).count()
    assert expected > 0

    info = backup.create_backup(filled)
    filled.commit()

    assert info.path.exists()
    assert info.size_bytes > 0

    # Читаем копию как отдельную базу: это единственная честная проверка
    # того, что в ней есть данные, а не только заголовок файла.
    conn = sqlite3.connect(info.path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]
    finally:
        conn.close()

    assert count == expected, f"в копии {count} приборов вместо {expected}"


def test_corrupted_backup_is_refused_and_removed(filled, folder, monkeypatch) -> None:
    """Повреждённая копия не засчитывается за успешную и удаляется.

    «Выглядит успешным, а спасти не может» — худший отказ бэкапа.
    """
    def broken(path):
        return "database disk image is malformed"

    monkeypatch.setattr(backup, "check_integrity", broken)

    with pytest.raises(backup.BackupError) as exc:
        backup.create_backup(filled)

    assert "повреждённой" in str(exc.value)
    left = list(folder.glob("*")) if folder.exists() else []
    assert not [p for p in left if p.is_file()], (
        f"повреждённая копия осталась на диске: {left}"
    )


def test_integrity_check_catches_a_broken_file(tmp_path) -> None:
    """Проверка целостности отличает базу от мусора.

    Битый файл открывается без единой ошибки и разваливается только при
    попытке что-то в нём найти — то есть когда восстановление уже нужно.
    """
    good = tmp_path / "good.db"
    conn = sqlite3.connect(good)
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()

    assert backup.check_integrity(good) is None, "здоровая база названа битой"

    # Случай первый: файл вообще не база. Ловится исключением.
    not_a_database = tmp_path / "bad.db"
    not_a_database.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)

    assert backup.check_integrity(not_a_database) is not None, (
        "битый файл принят за целую базу"
    )

    # Случай второй: файл повреждён внутри, а не подменён целиком.
    #
    # Проверено запуском: SQLite сообщает о такой порче ИСКЛЮЧЕНИЕМ
    # («database disk image is malformed»), а не строками результата.
    # Значит, ветка разбора строк (`rows[0][0] == "ok"`) отсюда
    # недостижима, и подлогом её не поймать — при попытке проверить зубы
    # подмена этой ветки оставляла сторожа зелёным.
    #
    # Ветка в коде оставлена намеренно: она перенесена от донора и
    # закрывает случай, когда SQLite всё же возвращает строки (иные
    # версии, иные виды порчи). Но сторожем она не подтверждена, и
    # притворяться, что подтверждена, нельзя.
    corrupted = tmp_path / "corrupted.db"
    conn = sqlite3.connect(corrupted)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, payload TEXT)")
    conn.executemany(
        "INSERT INTO t (payload) VALUES (?)", [("x" * 500,) for _ in range(200)]
    )
    conn.commit()
    conn.close()

    # Портим страницу в середине файла, не трогая заголовок: файл
    # по-прежнему открывается, но данные в нём расходятся с оглавлением.
    raw = bytearray(corrupted.read_bytes())
    page_size = 4096
    for offset in range(page_size * 2, min(page_size * 3, len(raw))):
        raw[offset] = 0
    corrupted.write_bytes(bytes(raw))

    assert backup.check_integrity(corrupted) is not None, (
        "повреждённая внутри база названа здоровой — "
        "именно так «успешная» копия и оказывается пустышкой"
    )


def test_state_is_read_from_disk_not_from_a_marker(filled, folder) -> None:
    """Состояние читается по файлам на диске.

    У «Заявок» первая версия сторожа читала служебную отметку и кричала
    «не создавался ни разу» на базе с восемью реальными файлами в папке.
    Здесь копия, снятая мимо системы, всё равно видна.
    """
    assert backup.list_backups() == []

    backup.create_backup(filled)
    filled.commit()

    items = backup.list_backups()
    assert len(items) == 1
    assert backup.last_backup_date() == date.today()


def test_silence_raises_the_alarm(folder) -> None:
    """Молчание копий — тревога, а не тишина."""
    # Копий нет вовсе — тревога сразу, ждать нечего.
    warning = backup.staleness()
    assert warning is not None
    assert warning.last_backup is None
    assert "нет ни одной" in warning.text


def test_fresh_backup_is_not_an_alarm(filled, folder) -> None:
    """Свежая копия тревоги не вызывает — иначе её перестанут читать."""
    backup.create_backup(filled)
    filled.commit()

    assert backup.staleness() is None


def test_alarm_waits_longer_than_the_interval(filled, folder) -> None:
    """Тревога не сразу по истечении срока, а втрое позже.

    Срок вышел — это норма: следующий запуск всё исправит. Тревога —
    когда система работала, а копия всё равно не создалась.
    """
    backup.create_backup(filled)
    filled.commit()

    interval = backup.DEFAULT_INTERVAL_DAYS
    today = date.today()

    # Срок вышел, но это ещё не беда.
    assert backup.staleness(today=today + timedelta(days=interval)) is None
    assert backup.is_due(today=today + timedelta(days=interval)), (
        "срок снятия копии не наступил, хотя интервал прошёл"
    )

    # Молчание втрое дольше — беда.
    late = today + timedelta(days=interval * backup.STALE_MULTIPLIER)
    assert backup.staleness(today=late) is not None


def test_old_backups_are_pruned(filled, folder) -> None:
    """Старые копии удаляются: иначе диск кончится молча.

    А первым проявлением станет несостоявшаяся копия — ровно тогда,
    когда она понадобится.
    """
    from datetime import datetime

    folder.mkdir(parents=True, exist_ok=True)
    # Подкладываем больше копий, чем положено хранить.
    for day in range(backup.KEEP_COUNT + 5):
        stamp = datetime(2026, 1, 1) + timedelta(days=day)
        name = f"{backup.BACKUP_PREFIX}{stamp:%Y-%m-%d_%H%M%S}{backup.BACKUP_SUFFIX}"
        (folder / name).write_bytes(b"old backup stub")

    assert len(backup.list_backups()) == backup.KEEP_COUNT + 5

    backup.create_backup(filled)
    filled.commit()

    assert len(backup.list_backups()) == backup.KEEP_COUNT, (
        "лишние копии остались, диск будет заполняться"
    )


def test_backup_is_written_to_the_journal(filled, folder) -> None:
    """Снятие копии попадает в журнал действий как системное событие."""
    from app import audit
    from app.models import AUDIT_SYSTEM

    backup.create_backup(filled)
    filled.commit()

    entries = [e for e in audit.recent(filled) if e.action == "Создана резервная копия"]
    assert entries, "снятие копии не попало в журнал"
    assert entries[0].audit_type == AUDIT_SYSTEM


def test_restore_path_is_documented() -> None:
    """Путь возврата описан.

    Канон семьи: «копия без описанного пути возврата — дыра. В день аварии
    человек остаётся с папкой копий и без инструкции». Кнопки в интерфейсе
    нет намеренно, поэтому проверяем, что есть описание.
    """
    from pathlib import Path

    doc = Path(__file__).resolve().parent.parent / "docs" / "BACKUP.md"
    assert doc.exists(), "нет docs/BACKUP.md — копия без пути возврата это дыра"

    text = doc.read_text(encoding="utf-8")
    assert "Как вернуть базу из копии" in text
    assert "storage" in text, "не сказано, что вложения в копию не входят"
