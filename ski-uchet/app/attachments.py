r"""Вложения: файлы к приборам, поверкам, актам и участкам.

Устройство взято у «Электро» (`C:\myproject\core\doc_filing.py` и
`doc_attach.py`) вместе с двумя уроками, каждый из которых там куплен
своей бедой.

**Урок первый: путь хранится относительно корня.** Слова донора о том,
почему абсолютный путь — мина замедленного действия: «смена папки
документов, переезд программы на другую машину или переименование
сетевого диска с `Z:` на `Y:` разом превращают все записи в ложь.
Программа показывает документы, а файлов по этим путям нет. Для документов
это худший исход из возможных: человек узнаёт о беде в тот момент, когда
бумага понадобилась проверяющему».

**Урок второй: сперва черновик, потом база, потом окончательное имя.**
У донора это записано так: «Before step178 the file was copied into the
storage folder first, and the database row was written afterwards. A failure
in between left a file on disk that nothing referred to». Здесь порядок
обратный: файл ложится рядом под временным именем, закрывается граница
базы, и только потом черновик переименовывается. Не записалось в базу —
черновик удаляется, диск остаётся чистым.

Раскладка на диске
-------------------

    <корень>/<вид объекта>/<номер>/<род>_<дата>_<имя файла>

Например: `instrument/17/certificate_2026-09-06_Свидетельство.pdf`.

Раскладка по объектам, а не по видам документов (у донора наоборот):
у СКИ вложения ищут всегда от прибора — «покажи паспорт этого нивелира», —
а не «покажи все паспорта». Папка на объект отвечает на такой вопрос
одним взглядом в файловый менеджер, без базы.
"""
from __future__ import annotations

import re
import shutil
import unicodedata
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.models import ATTACHMENT_KINDS, ATTACHMENT_SOURCES, ATTACHMENT_TARGETS, Attachment

BASE_DIR = Path(__file__).resolve().parent.parent

#: Корень хранилища. Отдельным именем, чтобы разговор «относительно чего»
#: шёл в одном месте (приём донора).
DEFAULT_ROOT = BASE_DIR / "storage"

#: Черновик носит это расширение, пока не закрыта граница базы.
PART_SUFFIX = ".part"

#: Предел размера одного файла. Сканы свидетельств и фото с телефона
#: укладываются в это с запасом; больше — обычно случайность вроде
#: видеозаписи вместо снимка.
MAX_SIZE_BYTES = 25 * 1024 * 1024

#: Что принимаем. Список закрытый: хранилище документов не должно
#: становиться местом, куда кладут исполняемые файлы.
ALLOWED_EXTENSIONS = {
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".heic",
    ".tif",
    ".tiff",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".odt",
    ".ods",
    ".rtf",
    ".txt",
}


class AttachmentError(Exception):
    """Ошибка работы с вложением, с текстом для человека."""


def root() -> Path:
    """Корень хранилища.

    Читается при каждом обращении, а не при загрузке модуля: урок
    «Электро» (`core/backup.py`) — «дефолты в Python вычисляются один раз,
    при чтении файла, то есть путь застывал раньше, чем программа успевала
    что-либо решить о расположении данных».
    """
    import os

    configured = os.environ.get("SKI_STORAGE_ROOT", "").strip()
    return Path(configured) if configured else DEFAULT_ROOT


def to_full_path(stored_path: str) -> Path:
    """Где файл лежит на самом деле: корень плюс то, что в базе."""
    stored = (stored_path or "").strip()
    if not stored:
        raise AttachmentError("У записи не указан путь к файлу.")
    path = Path(stored)
    # Абсолютный путь возвращаем как есть: так читаются записи, сделанные
    # до перехода на относительное хранение, и файлы вне корня.
    return path if path.is_absolute() else root() / path


def safe_name(name: str) -> str:
    """Имя файла, безопасное для файловой системы.

    Убираем всё, чем можно уйти из своей папки (`..`, разделители путей)
    и что ломает файловые системы. Кириллицу оставляем: имя «Свидетельство
    о поверке.pdf» человек узнаёт, а транслитерация превращает его в шараду.
    """
    cleaned = unicodedata.normalize("NFC", (name or "").strip())
    cleaned = cleaned.replace("\\", "_").replace("/", "_")
    cleaned = re.sub(r'[<>:"|?*\x00-\x1f]', "_", cleaned)
    cleaned = cleaned.strip(". ")
    return cleaned[:120] or "файл"


def check_extension(name: str) -> str:
    """Проверить расширение и вернуть его."""
    suffix = Path(safe_name(name)).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(e.lstrip(".") for e in ALLOWED_EXTENSIONS))
        raise AttachmentError(
            f"Такие файлы не принимаем. Можно: {allowed}."
        )
    return suffix


def unique_path(dest: Path) -> Path:
    """Свободное имя рядом с занятым: «Имя (1).pdf», «Имя (2).pdf».

    Замена молча не делается: два свидетельства с одинаковым именем — это
    два разных документа, и затирать первое вторым нельзя.
    """
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    counter = 1
    while True:
        candidate = dest.parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def planned_path(target_type: str, target_id: int, kind: str, name: str) -> Path:
    """Куда ляжет файл: <корень>/<вид>/<номер>/<род>_<дата>_<имя>."""
    if target_type not in ATTACHMENT_TARGETS:
        raise AttachmentError(f"Неизвестное назначение вложения: {target_type}")
    if kind not in ATTACHMENT_KINDS:
        raise AttachmentError(f"Неизвестный род документа: {kind}")

    folder = root() / target_type / str(target_id)
    stamp = date.today().isoformat()
    return folder / f"{kind}_{stamp}_{safe_name(name)}"


def attach(
    session: Session,
    target_type: str,
    target_id: int,
    *,
    data: bytes,
    original_name: str,
    kind: str = "other",
    source: str = "web",
    uploaded_by: str | None = None,
    notes: str | None = None,
) -> Attachment:
    """Приложить файл: диск и база вместе.

    Порядок работ — из донора (`core/doc_attach.py`): черновик рядом с
    будущим местом, затем запись в базу, и только потом окончательное имя.
    Сбой на любом шаге не оставляет ни файла без записи, ни записи без файла.

    Границу транзакции держит вызывающий: здесь `flush`, но не `commit`.
    """
    if not data:
        raise AttachmentError("Файл пустой — прикреплять нечего.")
    if len(data) > MAX_SIZE_BYTES:
        raise AttachmentError(
            f"Файл больше {MAX_SIZE_BYTES // 1024 // 1024} МБ. "
            "Уменьшите снимок или отсканируйте с меньшим разрешением."
        )
    if source not in ATTACHMENT_SOURCES:
        raise AttachmentError(f"Неизвестный источник: {source}")
    check_extension(original_name)

    dest = unique_path(planned_path(target_type, target_id, kind, original_name))
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + PART_SUFFIX)

    part.write_bytes(data)
    try:
        record = Attachment(
            target_type=target_type,
            target_id=target_id,
            kind=kind,
            source=source,
            stored_path=str(dest.relative_to(root())),
            original_name=safe_name(original_name),
            size_bytes=len(data),
            uploaded_by=uploaded_by,
            notes=notes,
        )
        session.add(record)
        session.flush()
    except Exception:
        # База отказала — черновик убираем, диск остаётся чистым.
        part.unlink(missing_ok=True)
        raise

    # База приняла запись — файл получает окончательное имя.
    part.replace(dest)

    audit.write(
        session,
        "Приложен документ",
        actor=uploaded_by,
        object_type=target_type,
        object_id=target_id,
        details=f"{ATTACHMENT_KINDS[kind]}: {record.original_name}"
        + (f" (из бота)" if source == "bot" else ""),
    )
    return record


def for_target(session: Session, target_type: str, target_id: int) -> list[Attachment]:
    """Все вложения объекта, новые первыми."""
    return list(
        session.scalars(
            select(Attachment)
            .where(Attachment.target_type == target_type, Attachment.target_id == target_id)
            .order_by(Attachment.uploaded_at.desc(), Attachment.id.desc())
        )
    )


def count_for_targets(
    session: Session, target_type: str, target_ids: list[int]
) -> dict[int, int]:
    """Сколько вложений у каждого объекта — одним запросом.

    Нужно для списков: без этого страница на 500 приборов сделала бы
    500 запросов, по одному на строку.
    """
    if not target_ids:
        return {}

    from sqlalchemy import func

    rows = session.execute(
        select(Attachment.target_id, func.count(Attachment.id))
        .where(
            Attachment.target_type == target_type,
            Attachment.target_id.in_(target_ids),
        )
        .group_by(Attachment.target_id)
    ).all()
    return {target_id: count for target_id, count in rows}


def delete(
    session: Session, attachment_id: int, *, deleted_by: str | None = None
) -> None:
    """Убрать вложение.

    Файл с диска НЕ удаляется намеренно: документ — это доказательство,
    и восстановить его после ошибочного нажатия неоткуда. Запись из базы
    уходит, файл остаётся в хранилище — при нужде его найдут по пути
    из журнала действий.
    """
    record = session.get(Attachment, attachment_id)
    if record is None:
        raise AttachmentError("Вложение не найдено.")

    stored = record.stored_path
    name = record.original_name
    target_type, target_id = record.target_type, record.target_id
    session.delete(record)
    session.flush()

    audit.write(
        session,
        "Документ откреплён",
        actor=deleted_by,
        object_type=target_type,
        object_id=target_id,
        details=f"{name}; файл остался в хранилище: {stored}",
    )


def missing_files(session: Session) -> list[Attachment]:
    """Записи, для которых файла на диске больше нет.

    Проверка по содержимому, а не по вере в базу: урок «Электро» о бэкапах
    («молчание системы проверяется по внешнему факту, а не по внутренней
    памяти о собственном последнем успехе») касается и документов —
    запись в базе не доказывает, что файл на месте.
    """
    found = []
    for record in session.scalars(select(Attachment)):
        try:
            if not to_full_path(record.stored_path).exists():
                found.append(record)
        except AttachmentError:
            found.append(record)
    return found
