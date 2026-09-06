r"""Сторож импорта: чужой файл читается, испорченное не молчит.

Какое обещание стережём
------------------------

«Реестр заказчика попадёт в систему целиком и без искажений». Пустой
системой не пользуются: если импорт окажется сложным, всё останется
как есть — в Excel.

Как обещание ломается в жизни
------------------------------

**Заголовки не те.** Файл заказчика ещё не пришёл, и его столбцы могут
называться как угодно. Урок «Заявок»: заголовки ищутся по синонимам,
«потому что в реальных книгах пользователи переименовывали столбцы».

**Импорт молча «починил» данные.** Правило Р7.7 свода БПО: спорная строка
отклоняется с причиной в отчёте. Тихая подмена хуже отказа: отказ виден,
подмена — нет.

**Записали, не посмотрев.** Разбирать испорченный реестр после записи
дороже, чем прочитать отчёт до неё.

Чем доказано
-------------

Запуском на настоящих файлах Excel, которые собираются здесь же: с
русскими заголовками, с переименованными столбцами, с битыми датами
и с повторами. Выдуманного формата тут нет — есть openpyxl и файлы
на диске.
"""

from __future__ import annotations

from datetime import date

import pytest

from app import importer
from app.models import Instrument, InstrumentType, Verification


def make_book(tmp_path, headers, rows, name="реестр.xlsx"):
    """Собрать настоящий файл Excel."""
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    assert sheet is not None, "новая книга без листа — такого не бывает"
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    path = tmp_path / name
    book.save(path)
    return path


# --------------------------------------------------------------------------
# Чтение заголовков
# --------------------------------------------------------------------------


def test_typical_headers_are_recognised(tmp_path) -> None:
    """Обычные русские заголовки читаются."""
    path = make_book(
        tmp_path,
        ["Инвентарный номер", "Наименование", "Тип прибора", "Заводской номер"],
        [["СКИ-001", "Нивелир Н-3", "Нивелир оптический", "12345"]],
    )

    report = importer.read_file(path)

    assert report.total == 1
    assert report.rows[0].is_valid
    assert report.rows[0].data["inventory_no"] == "СКИ-001"
    assert report.rows[0].data["type_name"] == "Нивелир оптический"


@pytest.mark.parametrize(
    "header",
    ["Инв. №", "инвентарный №", "ИНВЕНТАРНЫЙ НОМЕР", "  Инв.  номер  ", "Учётный номер"],
)
def test_column_is_found_by_synonyms(tmp_path, header) -> None:
    """Столбец узнаётся по синонимам, в любом регистре и с лишними пробелами.

    Файл заказчика ещё не пришёл; угадать точное написание нельзя, но
    можно принять любое из разумных.
    """
    path = make_book(tmp_path, [header, "Наименование"], [["СКИ-001", "Нивелир"]])

    report = importer.read_file(path)

    assert report.rows[0].data["inventory_no"] == "СКИ-001"


def test_unknown_columns_are_shown_not_swallowed(tmp_path) -> None:
    """Неузнанные заголовки показываются человеку, а не исчезают.

    Иначе колонка с важными данными потерялась бы молча.
    """
    path = make_book(
        tmp_path,
        ["Инвентарный номер", "Наименование", "Материально ответственный", "Шифр объекта"],
        [["СКИ-001", "Нивелир", "Петров", "ЖК-1"]],
    )

    report = importer.read_file(path)

    assert "Материально ответственный" in report.unknown_columns
    assert "Шифр объекта" in report.unknown_columns


def test_file_without_required_columns_is_refused(tmp_path) -> None:
    """Без инвентарного номера или наименования импорт не начинается.

    И отказ называет, чего не хватило и что было в заголовках, — иначе
    человек останется гадать.
    """
    path = make_book(tmp_path, ["Дата", "Стоимость"], [["01.01.2026", "1000"]])

    with pytest.raises(importer.ImportError_) as exc:
        importer.read_file(path)

    assert "инвентарный номер" in str(exc.value)
    assert "Дата" in str(exc.value), "не показано, какие заголовки были в файле"


# --------------------------------------------------------------------------
# Разбор строк
# --------------------------------------------------------------------------


def test_row_without_number_is_rejected_with_reason(tmp_path) -> None:
    """Строка без номера отклоняется, и причина названа."""
    path = make_book(
        tmp_path,
        ["Инвентарный номер", "Наименование"],
        [["СКИ-001", "Нивелир"], ["", "Рулетка"]],
    )

    report = importer.read_file(path)

    assert len(report.valid) == 1
    assert len(report.rejected) == 1
    assert "инвентарного номера" in report.rejected[0].error
    assert report.rejected[0].row_no == 3, "не указан номер строки в файле"


def test_broken_date_is_a_note_not_a_rejection(tmp_path) -> None:
    """Непонятная дата — замечание, а не отказ.

    Правило донора: «терять заявку из-за одного непонятного слова хуже,
    чем принять её с оговоркой, но молчать о подмене нельзя».
    """
    path = make_book(
        tmp_path,
        ["Инвентарный номер", "Наименование", "Поверка до"],
        [["СКИ-001", "Нивелир", "когда-нибудь"]],
    )

    report = importer.read_file(path)
    row = report.rows[0]

    assert row.is_valid, "строка отклонена из-за одной непонятной даты"
    assert row.notes, "подмена прошла молча"
    assert "не понял дату" in row.notes[0]


def test_dates_are_read_in_several_formats(tmp_path) -> None:
    """Даты читаются и как текст, и как настоящие даты Excel."""
    path = make_book(
        tmp_path,
        ["Инвентарный номер", "Наименование", "Поверка до"],
        [
            ["СКИ-001", "Нивелир", "15.06.2027"],
            ["СКИ-002", "Рулетка", date(2027, 3, 20)],
            ["СКИ-003", "Теодолит", "2027-01-10"],
        ],
    )

    report = importer.read_file(path)
    dates = [row.data["verification_valid_until"] for row in report.valid]

    assert dates == [date(2027, 6, 15), date(2027, 3, 20), date(2027, 1, 10)]


def test_absurd_year_is_noted(tmp_path) -> None:
    """Год выпуска за пределами разумного не принимается молча."""
    path = make_book(
        tmp_path,
        ["Инвентарный номер", "Наименование", "Год выпуска"],
        [["СКИ-001", "Нивелир", "1812"]],
    )

    report = importer.read_file(path)
    row = report.rows[0]

    assert row.data["manufactured_year"] is None
    assert any("1812" in note for note in row.notes)


def test_empty_trailing_rows_are_skipped(tmp_path) -> None:
    """Пустые строки в конце листа — не ошибка, а конец данных."""
    path = make_book(
        tmp_path,
        ["Инвентарный номер", "Наименование"],
        [["СКИ-001", "Нивелир"], [None, None], ["", ""]],
    )

    report = importer.read_file(path)

    assert report.total == 1, "пустые строки посчитаны за данные"


# --------------------------------------------------------------------------
# Повторы
# --------------------------------------------------------------------------


def test_duplicate_inside_the_file_is_caught(session, tmp_path) -> None:
    """Повтор внутри файла — ошибка данных, а не повод перезаписать.

    Инвентарные номера присвоены и уникальны (ответ на вопрос 25).
    """
    path = make_book(
        tmp_path,
        ["Инвентарный номер", "Наименование"],
        [["СКИ-001", "Нивелир"], ["СКИ-001", "Другой нивелир"]],
    )

    report = importer.read_file(path)
    importer.check_duplicates(session, report)

    assert len(report.valid) == 1
    assert "уже встречался в строке 2" in report.rejected[0].error


def test_duplicate_with_the_database_is_caught(session, tmp_path) -> None:
    """Прибор, который уже есть в системе, повторно не заводится."""
    kind = InstrumentType(name="Нивелир", category="geodesy", verification_interval_months=12)
    session.add(kind)
    session.flush()
    session.add(Instrument(inventory_no="СКИ-001", name="Уже есть", type_id=kind.id))
    session.flush()

    path = make_book(
        tmp_path, ["Инвентарный номер", "Наименование"], [["ски-001", "Нивелир"]]
    )
    report = importer.read_file(path)
    importer.check_duplicates(session, report)

    assert not report.valid, "повтор не пойман — регистр номера не учтён"
    assert "уже есть в системе" in report.rejected[0].error


# --------------------------------------------------------------------------
# Запись в базу
# --------------------------------------------------------------------------


def test_nothing_is_written_until_confirmed(session, tmp_path) -> None:
    """Чтение файла в базу ничего не пишет.

    Сперва человек смотрит отчёт, потом решает. Разбирать испорченный
    реестр после записи дороже.
    """
    path = make_book(tmp_path, ["Инвентарный номер", "Наименование"], [["СКИ-001", "Нивелир"]])

    importer.read_file(path)

    assert session.query(Instrument).count() == 0


def test_confirmed_import_creates_instruments(session, tmp_path) -> None:
    """Подтверждённый импорт заводит приборы."""
    path = make_book(
        tmp_path,
        ["Инвентарный номер", "Наименование", "Тип прибора", "Заводской номер", "Поверка до"],
        [
            ["СКИ-001", "Нивелир Н-3", "Нивелир оптический", "12345", "15.06.2027"],
            ["СКИ-002", "Рулетка Р30", "Рулетка измерительная", "77", "01.03.2027"],
        ],
    )

    report = importer.read_file(path)
    importer.check_duplicates(session, report)
    importer.apply_import(session, report, actor="Кладовщик")
    session.commit()

    assert report.imported == 2
    assert session.query(Instrument).count() == 2

    first = session.query(Instrument).filter(Instrument.inventory_no == "СКИ-001").one()
    assert first.name == "Нивелир Н-3"
    assert first.serial_no == "12345"
    assert first.status == "warehouse", "прибор должен появиться на складе"


def test_verification_is_created_from_the_file(session, tmp_path) -> None:
    """Срок поверки из файла становится записью о поверке.

    Историю не переносим, берём текущее состояние (ответ на вопрос 24).
    """
    path = make_book(
        tmp_path,
        ["Инвентарный номер", "Наименование", "Поверка до", "Свидетельство"],
        [["СКИ-001", "Нивелир", "15.06.2027", "С-123/2026"]],
    )

    report = importer.read_file(path)
    importer.apply_import(session, report)
    session.commit()

    record = session.query(Verification).one()
    assert record.valid_until == date(2027, 6, 15)
    assert record.certificate_no == "С-123/2026"
    assert record.result == "ok"


def test_unknown_type_is_created_and_reported(session, tmp_path) -> None:
    """Незнакомый тип заводится, но об этом сказано.

    Номенклатура заказчика богаче нашего справочника, и терять прибор
    из-за отсутствующего типа неправильно. Но интервал поверки у нового
    типа взят наугад, и кладовщик обязан его проверить.
    """
    path = make_book(
        tmp_path,
        ["Инвентарный номер", "Наименование", "Тип прибора"],
        [["СКИ-001", "Хитрый прибор", "Виброметр переносной"]],
    )

    report = importer.read_file(path)
    importer.apply_import(session, report)
    session.commit()

    assert "Виброметр переносной" in report.created_types
    assert any("проверьте межповерочный интервал" in n for n in report.rows[0].notes)
    assert session.query(InstrumentType).filter(
        InstrumentType.name == "Виброметр переносной"
    ).count() == 1


def test_rejected_rows_are_not_written(session, tmp_path) -> None:
    """Отклонённое в базу не попадает."""
    path = make_book(
        tmp_path,
        ["Инвентарный номер", "Наименование"],
        [["СКИ-001", "Нивелир"], ["", "Безымянный"]],
    )

    report = importer.read_file(path)
    importer.apply_import(session, report)
    session.commit()

    assert session.query(Instrument).count() == 1
    assert report.imported == 1


def test_import_is_written_to_the_journal(session, tmp_path) -> None:
    """Импорт попадает в журнал действий с числами."""
    from app import audit

    path = make_book(tmp_path, ["Инвентарный номер", "Наименование"], [["СКИ-001", "Нивелир"]])
    report = importer.read_file(path)
    importer.apply_import(session, report)
    session.commit()

    entries = [e for e in audit.recent(session) if e.action == "Импорт реестра приборов"]
    assert entries, "импорт не попал в журнал"
    assert "загружено 1" in (entries[0].details or "")


def test_broken_file_says_so_plainly(tmp_path) -> None:
    """Не-Excel отвергается понятным текстом, а не следом ошибки."""
    path = tmp_path / "не-книга.xlsx"
    path.write_bytes(b"not an excel file at all")

    with pytest.raises(importer.ImportError_) as exc:
        importer.read_file(path)

    assert "не открылся" in str(exc.value)


def test_same_type_in_different_spelling_is_not_duplicated(session, tmp_path) -> None:
    """Один тип в разных написаниях не задваивается.

    Пойман на живом импорте: сравнение шло через SQL-функцию `lower()`,
    а она в SQLite работает только с латиницей (урок Р9.7 свода БПО).
    «Виброметр» и «виброметр» считались разными, тип не находился НИКОГДА,
    и каждый прибор заводил новый — пока база не упиралась в ограничение
    уникальности и весь импорт не падал.

    Тесты этого не видели, потому что в них был один прибор нового типа.
    """
    path = make_book(
        tmp_path,
        ["Инвентарный номер", "Наименование", "Тип прибора"],
        [
            ["СКИ-001", "Первый", "Виброметр переносной"],
            ["СКИ-002", "Второй", "виброметр переносной"],
            ["СКИ-003", "Третий", "Виброметр  переносной"],
        ],
    )

    report = importer.read_file(path)
    importer.apply_import(session, report)
    session.commit()

    assert report.imported == 3, "импорт не прошёл целиком"
    types = session.query(InstrumentType).filter(
        InstrumentType.name.ilike("%иброметр%")
    ).all()
    assert len(types) == 1, f"тип задвоился: {[t.name for t in types]}"


def test_existing_type_is_reused_regardless_of_case(session, tmp_path) -> None:
    """Тип из справочника узнаётся в любом написании, а не заводится заново."""
    session.add(
        InstrumentType(
            name="Нивелир оптический", category="geodesy", verification_interval_months=12
        )
    )
    session.flush()

    path = make_book(
        tmp_path,
        ["Инвентарный номер", "Наименование", "Тип прибора"],
        [["СКИ-001", "Нивелир", "НИВЕЛИР ОПТИЧЕСКИЙ"]],
    )
    report = importer.read_file(path)
    importer.apply_import(session, report)
    session.commit()

    assert report.created_types == [], "заведён лишний тип вместо существующего"
    assert session.query(InstrumentType).count() == 1
