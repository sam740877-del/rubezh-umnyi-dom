r"""Сторож вложений: документ на месте, база и диск не расходятся.

Какое обещание стережём
------------------------

«Приложенный документ найдётся тогда, когда понадобится». Свидетельство
о поверке спрашивает проверяющий, и спрашивает не через минуту после
загрузки, а через год — когда систему уже переставили на другой сервер,
а папку хранилища перенесли.

Как обещание ломается в жизни
------------------------------

**Абсолютный путь в базе.** Урок «Электро» (`core/doc_filing.py`): «смена
папки документов, переезд программы на другую машину или переименование
сетевого диска с `Z:` на `Y:` разом превращают все записи в ложь.
Программа показывает документы, а файлов по этим путям нет».

**Файл записан, база нет.** Их же урок (`core/doc_attach.py`): «A failure
in between left a file on disk that nothing referred to» — файл на диске,
на который никто не ссылается, и никто о нём не знает.

Чем доказано
-------------

Запуском на временном хранилище: прикладываем настоящий файл, читаем его
обратно, ломаем базу посреди операции и смотрим, что осталось на диске.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import attachments
from app.models import Attachment, Instrument, InstrumentType
from app.seed import seed_demo


@pytest.fixture()
def storage(tmp_path, monkeypatch):
    """Своё хранилище на каждый сценарий.

    Правило донора (`PORTABLE_RULES.md` §6.1): «У каждого сценария своё
    хранилище, создаваемое перед ним и стираемое после. Соседские записи
    трёхдневной давности способны красить чужую проверку».
    """
    monkeypatch.setenv("SKI_STORAGE_ROOT", str(tmp_path / "storage"))
    return tmp_path / "storage"


@pytest.fixture()
def instrument(session):
    """Один прибор, к которому будем прикладывать документы."""
    kind = InstrumentType(name="Нивелир", category="geodesy", verification_interval_months=12)
    session.add(kind)
    session.flush()
    item = Instrument(inventory_no="ИНВ-100", name="Нивелир Н-3", type_id=kind.id)
    session.add(item)
    session.flush()
    return item


def test_file_is_stored_and_read_back(session, storage, instrument) -> None:
    """Приложенный файл читается обратно тем же содержимым."""
    record = attachments.attach(
        session,
        "instrument",
        instrument.id,
        data=b"%PDF-1.4 svidetelstvo",
        original_name="Свидетельство о поверке.pdf",
        kind="certificate",
        uploaded_by="Кладовщик Петров",
    )
    session.commit()

    path = attachments.to_full_path(record.stored_path)
    assert path.exists(), "файла нет на диске"
    assert path.read_bytes() == b"%PDF-1.4 svidetelstvo"
    assert record.original_name == "Свидетельство о поверке.pdf"


def test_path_is_stored_relative_to_root(session, storage, instrument) -> None:
    """В базе лежит путь ОТНОСИТЕЛЬНО корня, а не абсолютный.

    Иначе переезд хранилища превратил бы все записи в ложь, и узналось
    бы это в тот момент, когда бумага понадобилась проверяющему.
    """
    record = attachments.attach(
        session,
        "instrument",
        instrument.id,
        data=b"data",
        original_name="Паспорт.pdf",
        kind="passport",
    )
    session.commit()

    assert not Path(record.stored_path).is_absolute(), (
        f"путь сохранён абсолютным: {record.stored_path}"
    )
    assert "instrument" in record.stored_path
    assert str(instrument.id) in record.stored_path


def test_storage_move_does_not_break_links(session, storage, instrument, tmp_path, monkeypatch) -> None:
    """Хранилище переехало — документы всё равно находятся.

    Ради этого относительный путь и заведён. Переносим папку целиком
    и убеждаемся, что запись по-прежнему указывает на живой файл.
    """
    record = attachments.attach(
        session,
        "instrument",
        instrument.id,
        data=b"vazhnyy dokument",
        original_name="Акт.pdf",
        kind="act",
    )
    session.commit()
    stored = record.stored_path

    moved = tmp_path / "storage-new"
    storage.rename(moved)
    monkeypatch.setenv("SKI_STORAGE_ROOT", str(moved))

    assert attachments.to_full_path(stored).exists(), (
        "после переезда хранилища файл потерялся"
    )


def test_failed_database_leaves_no_orphan_file(session, storage, instrument, monkeypatch) -> None:
    """База отказала — на диске не остаётся файла без записи.

    Порядок работ у донора: черновик рядом с местом, затем база, и только
    потом окончательное имя. Ломаем базу и смотрим, что осталось.
    """
    def explode(*args, **kwargs):
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(session, "flush", explode)

    with pytest.raises(RuntimeError):
        attachments.attach(
            session,
            "instrument",
            instrument.id,
            data=b"nikto ne uznaet",
            original_name="Потеряшка.pdf",
            kind="other",
        )

    left = list(storage.rglob("*")) if storage.exists() else []
    files = [p for p in left if p.is_file()]
    assert not files, f"на диске остались файлы без записи в базе: {files}"


def test_same_name_does_not_overwrite(session, storage, instrument) -> None:
    """Второй документ с тем же именем не затирает первый.

    Два свидетельства с одинаковым именем — это два разных документа.
    """
    first = attachments.attach(
        session, "instrument", instrument.id,
        data=b"pervyy", original_name="Скан.pdf", kind="certificate",
    )
    second = attachments.attach(
        session, "instrument", instrument.id,
        data=b"vtoroy", original_name="Скан.pdf", kind="certificate",
    )
    session.commit()

    assert first.stored_path != second.stored_path
    assert attachments.to_full_path(first.stored_path).read_bytes() == b"pervyy"
    assert attachments.to_full_path(second.stored_path).read_bytes() == b"vtoroy"


def test_executable_files_are_refused(session, storage, instrument) -> None:
    """Хранилище документов — не место для исполняемых файлов."""
    with pytest.raises(attachments.AttachmentError):
        attachments.attach(
            session, "instrument", instrument.id,
            data=b"MZ", original_name="virus.exe", kind="other",
        )


def test_oversized_file_is_refused(session, storage, instrument) -> None:
    """Слишком большой файл не принимаем — обычно это случайность."""
    with pytest.raises(attachments.AttachmentError) as exc:
        attachments.attach(
            session, "instrument", instrument.id,
            data=b"x" * (attachments.MAX_SIZE_BYTES + 1),
            original_name="Огромный.pdf", kind="other",
        )
    assert "МБ" in str(exc.value)


@pytest.mark.parametrize(
    "evil_name",
    [
        "../../чужое.pdf",
        # Достаточно «..», чтобы выбраться из instrument/<id> И из корня:
        # первую пару съедает приставка «<род>_<дата>_», и на коротком
        # имени защита сработала бы случайно, а не по проверке.
        "../../../../../../чужое.pdf",
        r"..\..\..\..\..\..\чужое.pdf",
        "C:/Windows/System32/чужое.pdf",
        "подпапка/глубже/чужое.pdf",
    ],
)
def test_path_traversal_in_name_is_neutralised(
    session, storage, instrument, evil_name
) -> None:
    """Имя с переходом вверх не уводит файл из хранилища.

    Проверяется несколькими именами нарочно: на коротком «../../» защита
    срабатывает случайно — приставка «<род>_<дата>_» съедает один уровень,
    и снятие очистки имени тест бы не заметил.
    """
    record = attachments.attach(
        session, "instrument", instrument.id,
        data=b"data", original_name=evil_name, kind="other",
    )
    session.commit()

    full = attachments.to_full_path(record.stored_path).resolve()
    assert str(full).startswith(str(storage.resolve())), (
        f"файл ушёл из хранилища: {full}"
    )


def test_detaching_keeps_the_file(session, storage, instrument) -> None:
    """Открепление убирает запись, но файл остаётся.

    Документ — доказательство, и восстановить его после ошибочного
    нажатия неоткуда.
    """
    record = attachments.attach(
        session, "instrument", instrument.id,
        data=b"dokazatelstvo", original_name="Свидетельство.pdf", kind="certificate",
    )
    session.commit()
    path = attachments.to_full_path(record.stored_path)

    attachments.delete(session, record.id, deleted_by="Администратор")
    session.commit()

    assert session.get(Attachment, record.id) is None, "запись осталась в базе"
    assert path.exists(), "файл удалён с диска — доказательство потеряно"


def test_attachment_is_written_to_the_journal(session, storage, instrument) -> None:
    """Приложение документа попадает в журнал действий."""
    from app import audit

    attachments.attach(
        session, "instrument", instrument.id,
        data=b"data", original_name="Паспорт.pdf", kind="passport",
        uploaded_by="Кладовщик Петров",
    )
    session.commit()

    entries = audit.for_object(session, "instrument", instrument.id)
    assert any(e.action == "Приложен документ" for e in entries)
    assert any("Паспорт" in (e.details or "") for e in entries)


def test_missing_files_are_found(session, storage, instrument) -> None:
    """Пропавший файл виден проверкой, а не молча числится в базе.

    Запись в базе не доказывает, что файл на месте: проверяем по диску,
    как донор проверяет бэкапы «по внешнему факту, а не по внутренней
    памяти о собственном последнем успехе».
    """
    record = attachments.attach(
        session, "instrument", instrument.id,
        data=b"data", original_name="Пропажа.pdf", kind="other",
    )
    session.commit()

    assert attachments.missing_files(session) == []

    attachments.to_full_path(record.stored_path).unlink()
    missing = attachments.missing_files(session)

    assert len(missing) == 1
    assert missing[0].original_name == "Пропажа.pdf"


def test_counting_is_one_query_for_many_targets(session, storage, instrument) -> None:
    """Счётчики для списка берутся одним запросом, а не по одному на строку."""
    attachments.attach(
        session, "instrument", instrument.id,
        data=b"a", original_name="Один.pdf", kind="other",
    )
    attachments.attach(
        session, "instrument", instrument.id,
        data=b"b", original_name="Два.pdf", kind="other",
    )
    session.commit()

    counts = attachments.count_for_targets(session, "instrument", [instrument.id, 999])
    assert counts == {instrument.id: 2}
