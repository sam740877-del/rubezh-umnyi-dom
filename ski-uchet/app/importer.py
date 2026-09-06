r"""Импорт реестра приборов из Excel.

Устройство взято у «Заявок» (`C:\zayavki\importers\xlsm_importer.py`)
вместе с двумя правилами.

**Сопоставление колонок гибкое.** Слова донора: заголовки ищутся по списку
синонимов «без учёта регистра и лишних пробелов, потому что в реальных
книгах пользователи переименовывали столбцы». Файл заказчика ещё не
пришёл, и угадать его заголовки нельзя — но можно принять любые из
разумного набора, а неузнанные показать человеку и дать сопоставить руками.

**Импорт никогда не «починяет» данные молча** (Р7.7 свода БПО). Разделяем:

- **ошибка** — строка отклонена, в базу не попадёт;
- **замечание** — строка принята, но значение пришлось истолковать.
  Довод донора: «терять заявку из-за одного непонятного слова хуже, чем
  принять её с оговоркой, но молчать о подмене нельзя».

**Сперва проверка, потом запись.** Импорт всегда прогоняется вхолостую
и показывает отчёт; запись — отдельным подтверждённым шагом. Разбирать
испорченный реестр после записи дороже, чем посмотреть отчёт до неё.

Чего здесь нет
---------------

Обновления уже заведённых приборов. Реестр загружают один раз, в пустую
систему; повторная загрузка того же файла пропустит совпадающие
инвентарные номера, а не перезапишет карточки. Дописывать этот случай
вслепую нельзя: непонятно, что считать «изменилось» — данные заказчика
или данные, поправленные кладовщиком после загрузки.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.models import CATEGORIES, Instrument, InstrumentType

#: Синонимы заголовков. Список открытый: увидим реальный файл — допишем.
#: Ключ — наше поле, значения — как это может называться у заказчика.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "inventory_no": (
        "инвентарный номер",
        "инвентарный №",
        "инв. номер",
        "инв. №",
        "инв№",
        "инвномер",
        "номер",
        "учётный номер",
        "учетный номер",
    ),
    "name": (
        "наименование",
        "название",
        "прибор",
        "наименование прибора",
        "наименование си",
        "средство измерения",
    ),
    "type_name": (
        "тип",
        "тип прибора",
        "тип си",
        "вид",
        "вид прибора",
        "группа",
    ),
    "model": ("модель", "марка", "модификация", "исполнение"),
    "manufacturer": ("производитель", "изготовитель", "завод-изготовитель"),
    "serial_no": (
        "заводской номер",
        "заводской №",
        "зав. номер",
        "зав. №",
        "серийный номер",
        "серийный №",
    ),
    "manufactured_year": ("год выпуска", "год изготовления", "год"),
    "verification_valid_until": (
        "поверка до",
        "действует до",
        "срок поверки",
        "дата следующей поверки",
        "следующая поверка",
        "поверка действительна до",
    ),
    "verification_performed_on": (
        "дата поверки",
        "поверка от",
        "дата последней поверки",
        "поверен",
    ),
    "certificate_no": (
        "свидетельство",
        "свидетельство №",
        "номер свидетельства",
        "№ свидетельства",
    ),
    "notes": ("примечание", "комментарий", "примечания"),
}

#: Поля, без которых прибор не прибор.
REQUIRED = ("inventory_no", "name")

#: Сколько строк показываем в предпросмотре: отчёт должен помещаться
#: на экран, а не заставлять листать.
PREVIEW_ROWS = 10


class ImportError_(Exception):
    """Ошибка разбора файла, с текстом для человека."""


@dataclass
class RowResult:
    """Одна строка реестра после разбора."""

    row_no: int
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.error


@dataclass
class ImportReport:
    """Отчёт: что прочитано, что принято, что отклонено и почему."""

    source: str = ""
    dry_run: bool = True
    #: Как заголовки файла легли на наши поля.
    mapping: dict[str, str] = field(default_factory=dict)
    #: Заголовки, которые не удалось узнать.
    unknown_columns: list[str] = field(default_factory=list)
    rows: list[RowResult] = field(default_factory=list)
    created_types: list[str] = field(default_factory=list)
    imported: int = 0

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def valid(self) -> list[RowResult]:
        return [row for row in self.rows if row.is_valid]

    @property
    def rejected(self) -> list[RowResult]:
        return [row for row in self.rows if not row.is_valid]

    @property
    def with_notes(self) -> list[RowResult]:
        return [row for row in self.rows if row.notes]

    @property
    def missing_required(self) -> list[str]:
        """Обязательные поля, которых в файле не нашлось."""
        return [field for field in REQUIRED if field not in self.mapping.values()]

    @property
    def summary(self) -> str:
        """Одна строка для человека."""
        if self.dry_run:
            return (
                f"Прочитано строк: {self.total}. "
                f"Готовы к загрузке: {len(self.valid)}. "
                f"Отклонено: {len(self.rejected)}."
            )
        return f"Загружено приборов: {self.imported} из {self.total}."


def _normalize(text: Any) -> str:
    """Заголовок к виду, по которому его можно узнать."""
    value = str(text or "").strip().lower()
    value = value.replace("ё", "е")
    return re.sub(r"\s+", " ", value)


def guess_mapping(headers: list[Any]) -> tuple[dict[str, str], list[str]]:
    """Разложить заголовки файла по нашим полям.

    Возвращает пару: соответствие «колонка → поле» и список неузнанных
    заголовков. Неузнанное не отбрасываем молча — показываем человеку.
    """
    mapping: dict[str, str] = {}
    unknown: list[str] = []
    taken: set[str] = set()

    for index, header in enumerate(headers):
        cleaned = _normalize(header)
        if not cleaned:
            continue

        matched = None
        for field_name, aliases in COLUMN_ALIASES.items():
            if field_name in taken:
                continue
            if cleaned in aliases or any(cleaned == _normalize(a) for a in aliases):
                matched = field_name
                break

        if matched:
            mapping[str(index)] = matched
            taken.add(matched)
        else:
            unknown.append(str(header).strip())

    return mapping, unknown


def _parse_date(value: Any, row: RowResult, field_title: str) -> date | None:
    """Дата из ячейки. Непонятное — замечание, а не отказ."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    row.notes.append(f"{field_title}: не понял дату «{text}», поле оставлено пустым")
    return None


def _parse_year(value: Any, row: RowResult) -> int | None:
    """Год выпуска. Бессмысленное значение — замечание."""
    if value in (None, ""):
        return None
    try:
        year = int(float(str(value).strip()))
    except (ValueError, TypeError):
        row.notes.append(f"Год выпуска: не понял «{value}», поле оставлено пустым")
        return None

    if not (1900 <= year <= date.today().year + 1):
        row.notes.append(f"Год выпуска {year} за пределами разумного, оставлен пустым")
        return None
    return year


def read_file(path: str | Path, sheet: str | None = None) -> ImportReport:
    """Прочитать файл и разобрать строки. В базу ничего не пишет.

    Это первый из двух шагов: человек смотрит отчёт и решает, грузить ли.
    """
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover — зависимость в requirements
        raise ImportError_("Не установлен openpyxl — читать Excel нечем.") from exc

    path = Path(path)
    if not path.exists():
        raise ImportError_(f"Файл не найден: {path}")

    try:
        book = load_workbook(path, data_only=True, read_only=True)
    except Exception as exc:
        raise ImportError_(f"Файл не открылся как книга Excel: {exc}") from exc

    try:
        worksheet = book[sheet] if sheet else book.worksheets[0]
    except KeyError as exc:
        raise ImportError_(f"В книге нет листа «{sheet}»") from exc

    report = ImportReport(source=path.name, dry_run=True)
    rows = worksheet.iter_rows(values_only=True)

    try:
        headers = list(next(rows))
    except StopIteration:
        raise ImportError_("Файл пустой — читать нечего.") from None

    report.mapping, report.unknown_columns = guess_mapping(headers)
    if report.missing_required:
        titles = {"inventory_no": "инвентарный номер", "name": "наименование"}
        missing = ", ".join(titles.get(f, f) for f in report.missing_required)
        raise ImportError_(
            f"В файле не нашлось обязательных колонок: {missing}. "
            f"Заголовки первой строки: {', '.join(str(h) for h in headers if h)}"
        )

    for offset, raw in enumerate(rows, start=2):
        row = RowResult(row_no=offset)
        values: dict[str, Any] = {}

        for index, field_name in report.mapping.items():
            position = int(index)
            values[field_name] = raw[position] if position < len(raw) else None

        # Пустая строка в конце листа — не ошибка, просто конец данных.
        if not any(str(v or "").strip() for v in values.values()):
            continue

        inventory_no = str(values.get("inventory_no") or "").strip()
        name = str(values.get("name") or "").strip()

        if not inventory_no:
            row.error = "нет инвентарного номера"
        elif not name:
            row.error = "нет наименования"
        else:
            row.data = {
                "inventory_no": inventory_no,
                "name": name,
                "type_name": str(values.get("type_name") or "").strip(),
                "model": str(values.get("model") or "").strip() or None,
                "manufacturer": str(values.get("manufacturer") or "").strip() or None,
                "serial_no": str(values.get("serial_no") or "").strip() or None,
                "manufactured_year": _parse_year(values.get("manufactured_year"), row),
                "verification_valid_until": _parse_date(
                    values.get("verification_valid_until"), row, "Поверка действует до"
                ),
                "verification_performed_on": _parse_date(
                    values.get("verification_performed_on"), row, "Дата поверки"
                ),
                "certificate_no": str(values.get("certificate_no") or "").strip() or None,
                "notes": str(values.get("notes") or "").strip() or None,
            }

        report.rows.append(row)

    book.close()
    return report


def check_duplicates(session: Session, report: ImportReport) -> None:
    """Отметить строки, чьи инвентарные номера уже есть в базе или в файле.

    Инвентарные номера присвоены и уникальны (ответ на вопрос 25), поэтому
    повтор — это ошибка данных, а не повод перезаписать карточку.
    """
    seen: dict[str, int] = {}

    for row in report.rows:
        if not row.is_valid:
            continue
        number = row.data["inventory_no"]

        first = seen.get(number.upper())
        if first is not None:
            row.error = f"инвентарный номер {number} уже встречался в строке {first}"
            continue
        seen[number.upper()] = row.row_no

        exists = session.scalar(
            select(func.count(Instrument.id)).where(
                func.upper(Instrument.inventory_no) == number.upper()
            )
        )
        if exists:
            row.error = f"прибор с номером {number} уже есть в системе"


def _resolve_type(
    session: Session, type_name: str, row: RowResult, report: ImportReport
) -> InstrumentType:
    """Найти тип прибора по названию или завести новый.

    Тип ищется без учёта регистра и лишних пробелов: в реестре «Нивелир
    оптический» и «нивелир  оптический» — одно и то же.

    Ненайденный тип заводится, а не отклоняет строку: номенклатура
    заказчика богаче нашего справочника, и терять прибор из-за
    отсутствующего типа неправильно. Но молчать нельзя — новые типы
    перечислены в отчёте, чтобы кладовщик проверил интервалы поверки.
    """
    cleaned = re.sub(r"\s+", " ", (type_name or "").strip())
    if not cleaned:
        cleaned = "Не указан"
        row.notes.append("Тип прибора не указан — отнесён к «Не указан»")

    # Сравниваем В PYTHON, а не в SQL. Урок Р9.7 свода БПО: встроенный
    # в SQLite `lower()` работает только с латиницей, и «Виброметр» с
    # «виброметр» он считает разными. Пойман здесь же на живом импорте:
    # тип не находился НИКОГДА, и каждый прибор заводил новый тип, пока
    # база не упиралась в ограничение уникальности.
    #
    # Перебор допустим: типов в справочнике десятки, а не тысячи.
    # Настоящее лекарство — нормализованная колонка (приём «Электро»),
    # но заводить её ради импорта, который делается раз, дороже.
    target = cleaned.lower()
    for existing in session.scalars(select(InstrumentType)):
        if (existing.name or "").strip().lower() == target:
            return existing

    created = InstrumentType(
        name=cleaned,
        category="other",
        # Интервал по умолчанию: год. Настоящий берётся из паспорта
        # прибора (ответ на вопрос 15), поэтому кладовщик обязан
        # проверить новые типы — они названы в отчёте.
        verification_interval_months=12,
        requires_verification=True,
    )
    session.add(created)
    session.flush()

    if cleaned not in report.created_types:
        report.created_types.append(cleaned)
    row.notes.append(f"Заведён новый тип «{cleaned}» — проверьте межповерочный интервал")
    return created


def apply_import(
    session: Session, report: ImportReport, *, actor: str | None = None
) -> ImportReport:
    """Записать в базу то, что прошло проверку.

    Второй из двух шагов. Отклонённые строки не пишутся — их список
    остаётся в отчёте, чтобы заказчик поправил файл и загрузил остаток.

    Границу транзакции держит вызывающий.
    """
    from app.models import Verification

    report.dry_run = False
    report.imported = 0

    for row in report.valid:
        data = row.data
        kind = _resolve_type(session, data.get("type_name", ""), row, report)

        instrument = Instrument(
            inventory_no=data["inventory_no"],
            name=data["name"],
            type_id=kind.id,
            model=data.get("model"),
            manufacturer=data.get("manufacturer"),
            serial_no=data.get("serial_no"),
            manufactured_year=data.get("manufactured_year"),
            status="warehouse",
            notes=data.get("notes"),
        )
        session.add(instrument)
        session.flush()

        # Историю поверок не переносим — берём текущее состояние
        # (ответ на вопрос 24). Одна запись, если срок в файле указан.
        valid_until = data.get("verification_valid_until")
        if valid_until:
            performed_on = data.get("verification_performed_on")
            if performed_on is None:
                # Даты проведения в файле не было: считаем от срока назад
                # по интервалу типа. Это допущение, и оно названо вслух.
                months = kind.verification_interval_months or 12
                performed_on = date(
                    valid_until.year - (months // 12),
                    valid_until.month,
                    min(valid_until.day, 28),
                )
                row.notes.append(
                    "Дата поверки не указана — рассчитана от срока действия назад"
                )
            session.add(
                Verification(
                    instrument_id=instrument.id,
                    kind="verification",
                    performed_on=performed_on,
                    valid_until=valid_until,
                    certificate_no=data.get("certificate_no"),
                    result="ok",
                )
            )
            session.flush()

        report.imported += 1

    audit.write_system(
        session,
        "Импорт реестра приборов",
        details=(
            f"файл {report.source}: загружено {report.imported}, "
            f"отклонено {len(report.rejected)}"
            + (f", новых типов: {len(report.created_types)}" if report.created_types else "")
        ),
    )
    return report
