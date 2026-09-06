"""Общая подготовка тестов.

База живёт В ПАМЯТИ, а не на диске. Замер: пересоздание восемнадцати
таблиц с семью миграциями стоит 213 мс на диске и 22 мс в памяти —
на 183 тестах это 39 секунд против четырёх.

Прогон, который идёт две минуты, перестают гонять на каждую правку,
и сторожа теряют смысл: они ловят беду тем позже, чем реже их зовут.

Метки для выборочного прогона
------------------------------

Тесты помечены по назначению, чтобы гонять не всё подряд:

    pytest -m core      правила учёта, без веба
    pytest -m web       страницы и формы
    pytest -m guard     сторожа устройства: слои, миграции, секреты
    pytest -m "not slow"  всё, кроме долгого

Метки объявлены в `pytest.ini`; проставляются автоматически по имени
файла — руками к каждому тесту дописывать нечего.
"""
import os
import tempfile
from pathlib import Path

import pytest

# База в памяти. Общее подключение на весь прогон — иначе каждая
# сессия получала бы СВОЮ пустую базу: у SQLite «:memory:» живёт
# ровно столько, сколько открыто соединение.
os.environ["SKI_DATABASE_URL"] = "sqlite:///:memory:"

# Хранилище вложений и копий — во временную папку, а не рядом с рабочей
# базой: тесты не должны писать в то, чем пользуется человек.
_TMP = Path(tempfile.mkdtemp(prefix="ski-tests-"))
os.environ.setdefault("SKI_STORAGE_ROOT", str(_TMP / "storage"))
os.environ.setdefault("SKI_BACKUP_DIR", str(_TMP / "backups"))

# Хеширование пароля стоит 179 мс — намеренно, чтобы подбор был дорогим.
# В тестах подбирать некому, а три пароля в каждой подготовке съедали
# треть прогона. Послабление живёт только здесь: в бою переменной нет.
os.environ["SKI_FAST_HASH"] = "1"

from app.database import SessionLocal, engine, init_db  # noqa: E402
from app.models import Base  # noqa: E402

#: Какому файлу какая метка. Проставляется само — см. `pytest_collection_modifyitems`.
МЕТКИ_ПО_ФАЙЛАМ = {
    "test_services": "core",
    "test_audit": "core",
    "test_notifications": "core",
    "test_attachments": "core",
    "test_importer": "core",
    "test_bot": "core",
    "test_backup": "core",
    "test_web": "web",
    "test_security": "web",
    "test_layers": "guard",
    "test_migrations": "guard",
}


def pytest_collection_modifyitems(items):
    """Расставить метки по именам файлов.

    Руками к каждому тесту дописывать `@pytest.mark.web` — работа
    без смысла: файл и так говорит, что внутри. Забытая метка
    вернула бы тест в «прогоняем всё», то есть в исходную беду.
    """
    for item in items:
        имя = Path(str(item.fspath)).stem
        метка = МЕТКИ_ПО_ФАЙЛАМ.get(имя)
        if метка:
            item.add_marker(getattr(pytest.mark, метка))


@pytest.fixture()
def session():
    """Чистая база на каждый тест.

    Пересоздаём таблицы, а не откатываем транзакцию: часть кода делает
    свой `commit` (заявки, импорт, бот), и откатом их не убрать.
    """
    Base.metadata.drop_all(engine)
    init_db()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()
