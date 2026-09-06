r"""Резервные копии базы.

Устройство взято у БПО (`C:\bpo\core\backup.py`), где оно названо каноном
каркаса семьи. Четыре правила, каждое там куплено бедой.

**1. Копия снимается штатным механизмом SQLite (Online Backup API),
а не копированием файла.** Слова донора: «копировать файл базы на живую
означает получить копию в середине чужой транзакции». Для веб-системы это
острее, чем для настольной: в базу пишут постоянно и одновременно.

**2. Копия проверяется сразу после создания.** Их довод дословно: «битая
копия открывается без единой ошибки и разваливается только при попытке
что-то в ней найти — то есть в момент, когда восстановление уже отчаянно
нужно и права на ошибку нет». Проверка — `PRAGMA integrity_check`, читает
всю базу постранично. Битая копия удаляется и за успешную не считается.

**3. Состояние читается фактом по диску, а не служебной отметкой.**
У «Заявок» первая версия сторожа читала отметку `last_backup_date`
«и кричала „не создавался ни разу“ на базе с восемью реальными файлами
в папке». Здесь `list_backups()` смотрит на файлы.

**4. Молчание бэкапа — громкий сигнал.** Тревога не когда срок вышел
(это норма, следующий запуск исправит), а когда молчание длится втрое
дольше интервала: значит, система работала, а копия всё равно не
создалась — папка недоступна, кончилось место, сбой прав. Само это
не исправится.

Чего здесь нет и почему
------------------------

**Восстановления из копии в интерфейсе нет.** Канон семьи
(`КАРКАС_СЕМЬИ.md` §14): «копия без описанного пути возврата — дыра.
В день аварии человек остаётся с папкой копий и без инструкции».
Путь возврата описан в `docs/BACKUP.md`, а кнопки нет намеренно: восстановление
затирает всё, что накопилось после копии, и такое действие должен делать
человек руками, понимая, что теряет.
"""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app import audit

BASE_DIR = Path(__file__).resolve().parent.parent

#: Куда кладём копии. Отдельным именем — чтобы разговор «куда» шёл
#: в одном месте.
DEFAULT_DIR = BASE_DIR / "backups"

BACKUP_PREFIX = "ski_backup_"
BACKUP_SUFFIX = ".db"

#: Имя копии: ski_backup_2026-09-06_154312.db
NAME_PATTERN = re.compile(
    rf"^{BACKUP_PREFIX}(\d{{4}}-\d{{2}}-\d{{2}})_(\d{{6}}){re.escape(BACKUP_SUFFIX)}$"
)

#: Через сколько дней положено снимать копию.
DEFAULT_INTERVAL_DAYS = 1

#: Во сколько раз дольше интервала копия должна молчать, чтобы это
#: считалось тревогой, а не обычной паузой: контору закрыли на выходные,
#: сервер не включали пару дней.
STALE_MULTIPLIER = 3

#: Сколько копий храним. Старые удаляются, иначе диск кончится молча.
KEEP_COUNT = 30


class BackupError(Exception):
    """Ошибка резервного копирования, с текстом для человека."""


@dataclass(frozen=True)
class BackupInfo:
    """Сведения о файле копии — для таблицы на экране."""

    path: Path
    created_at: datetime
    size_bytes: int

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def size_text(self) -> str:
        if self.size_bytes < 1024 * 1024:
            return f"{self.size_bytes / 1024:.0f} КБ"
        return f"{self.size_mb} МБ"

    @property
    def created_text(self) -> str:
        return self.created_at.strftime("%d.%m.%Y %H:%M")


@dataclass(frozen=True)
class StaleWarning:
    """Тревога о молчании копий — готовая к показу."""

    last_backup: date | None
    days_silent: int

    @property
    def text(self) -> str:
        if self.last_backup is None:
            return (
                "Резервных копий нет ни одной. Если база пропадёт, "
                "восстанавливать будет нечего."
            )
        return (
            f"Последняя копия сделана {self.last_backup:%d.%m.%Y} — "
            f"{self.days_silent} дн. назад. Проверьте, доступна ли папка копий "
            "и хватает ли места на диске."
        )


def backup_dir() -> Path:
    """Папка копий.

    Читается при каждом обращении, а не при загрузке модуля: урок
    «Электро» (`core/backup.py`) — «дефолты в Python вычисляются один раз,
    при чтении файла, то есть путь застывал раньше, чем программа успевала
    что-либо решить о расположении данных... бэкапы снимаются со старого
    места, даже когда база уже переехала: копия есть, а данные в ней
    вчерашние».
    """
    configured = os.environ.get("SKI_BACKUP_DIR", "").strip()
    return Path(configured) if configured else DEFAULT_DIR


def check_integrity(path: Path) -> str | None:
    """Проверить копию по содержимому. `None` — копия здорова.

    `PRAGMA integrity_check` читает всю базу постранично и находит
    повреждения, которых простое открытие файла не покажет.
    """
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return f"файл не открылся: {exc}"
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as exc:
        # «file is not a database» — это тоже «копия повреждена», а не
        # «проверка сломалась»: человеку нужен один вывод в обоих случаях.
        return f"файл повреждён: {exc}"
    finally:
        conn.close()

    if len(rows) == 1 and rows[0][0] == "ok":
        return None
    return "; ".join(str(row[0]) for row in rows[:5])


def list_backups(directory: Path | None = None) -> list[BackupInfo]:
    """Копии, лежащие на диске, новые первыми.

    Читаем ФАЙЛЫ, а не служебную отметку. У «Заявок» первая версия сторожа
    читала отметку и «кричала „не создавался ни разу“ на базе с восемью
    реальными файлами в папке».
    """
    folder = directory or backup_dir()
    if not folder.exists():
        return []

    found = []
    for path in folder.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"):
        match = NAME_PATTERN.match(path.name)
        if not match:
            continue
        try:
            stamp = datetime.strptime(f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H%M%S")
            size = path.stat().st_size
        except (ValueError, OSError):
            continue
        found.append(BackupInfo(path=path, created_at=stamp, size_bytes=size))

    return sorted(found, key=lambda item: item.created_at, reverse=True)


def create_backup(session: Session, directory: Path | None = None) -> BackupInfo:
    """Снять копию базы и проверить её.

    Порядок: копия под временным именем, проверка целостности, и только
    потом окончательное имя. Битая копия удаляется — «выглядит успешным,
    а спасти не может» это худший отказ бэкапа.
    """
    folder = directory or backup_dir()
    folder.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now()
    dest = folder / f"{BACKUP_PREFIX}{stamp:%Y-%m-%d_%H%M%S}{BACKUP_SUFFIX}"
    part = dest.with_suffix(dest.suffix + ".part")

    source = session.connection().connection.driver_connection
    if not isinstance(source, sqlite3.Connection):
        raise BackupError(
            "Резервное копирование пока умеет только SQLite. "
            "После переезда на PostgreSQL здесь будет pg_dump."
        )

    try:
        target = sqlite3.connect(part)
        try:
            # Штатный механизм SQLite: копия снимается согласованной,
            # даже когда в базу пишут.
            source.backup(target)
        finally:
            target.close()
    except sqlite3.Error as exc:
        part.unlink(missing_ok=True)
        raise BackupError(f"Копию снять не удалось: {exc}") from exc

    problem = check_integrity(part)
    if problem:
        part.unlink(missing_ok=True)
        raise BackupError(
            f"Копия получилась повреждённой и удалена: {problem}. "
            "Проверьте место на диске."
        )

    part.replace(dest)
    info = BackupInfo(path=dest, created_at=stamp, size_bytes=dest.stat().st_size)

    audit.write_system(
        session,
        "Создана резервная копия",
        details=f"{dest.name}, {info.size_text}",
    )
    _prune(folder)
    return info


def _prune(folder: Path, keep: int = KEEP_COUNT) -> int:
    """Удалить самые старые копии сверх предела.

    Без этого диск кончится молча, и первым проявлением станет
    несостоявшаяся копия — ровно тогда, когда она понадобится.
    """
    items = list_backups(folder)
    removed = 0
    for item in items[keep:]:
        try:
            item.path.unlink()
            removed += 1
        except OSError:
            # Не удалилось — не беда: место кончится позже, а ронять
            # успешную копию из-за уборки нельзя.
            pass
    return removed


def last_backup_date(directory: Path | None = None) -> date | None:
    """Когда снята последняя копия. По диску, а не по отметке."""
    items = list_backups(directory)
    return items[0].created_at.date() if items else None


def staleness(
    directory: Path | None = None,
    today: date | None = None,
    interval_days: int = DEFAULT_INTERVAL_DAYS,
) -> StaleWarning | None:
    """Тревога, если копии молчат опасно долго. Иначе `None`.

    «Опасно долго» — не то же самое, что «пора снимать»: срок наступает
    сразу по истечении интервала, и это норма. Тревога — когда молчание
    длится втрое дольше: система работала, а копия всё равно не создалась,
    и само это не исправится.
    """
    if interval_days <= 0:
        return None

    today = today or date.today()
    threshold = interval_days * STALE_MULTIPLIER
    items = list_backups(directory)

    if not items:
        # Копий нет вовсе — тревога сразу, ждать нечего.
        return StaleWarning(last_backup=None, days_silent=threshold)

    last = items[0].created_at.date()
    silent = (today - last).days
    if silent >= threshold:
        return StaleWarning(last_backup=last, days_silent=silent)
    return None


def is_due(
    directory: Path | None = None,
    today: date | None = None,
    interval_days: int = DEFAULT_INTERVAL_DAYS,
) -> bool:
    """Пора ли снимать копию."""
    if interval_days <= 0:
        return False
    last = last_backup_date(directory)
    if last is None:
        return True
    return ((today or date.today()) - last).days >= interval_days
