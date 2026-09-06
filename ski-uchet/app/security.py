r"""Учётные записи, роли и права.

Устройство взято у «Заявок» (`C:\zayavki\core\security.py`) — единственной
реализации ролей во всей семье, обкатанной на живой конторе. Оттуда же
взяты три правила, купленные их ошибками:

1. **Права считаются от действующего лица, переданного явно.** Глобальной
   переменной «текущий пользователь» здесь нет. Урок R-12 «Заявок»: в одном
   процессе работают много людей, и глобал даёт «молчаливую порчу от имени
   не того человека» — худший род ошибки. Для веба это не тонкость, а
   обязательное условие: запросы идут одновременно.
2. **Пользователей отключают, а не удаляют.** Идентификатор человека — это
   вся история его действий: кто выдал прибор, кто согласовал заявку.
   Удаление стёрло бы летопись.
3. **Последнего администратора нельзя ни удалить, ни понизить** — иначе
   в систему не войдёт никто.

Роли под ответы заказчика (вопросы 34, 46): на сайте работают администратор,
кладовщик и главный инженер. Инженеры строительного контроля они же мастера
участков работают через бота в MAX, учётные записи на сайте им не нужны —
поэтому роли «мастер» здесь нет.

Пароли: PBKDF2-HMAC-SHA256 средствами стандартной библиотеки, без внешних
зависимостей. Формат хранения начинается с имени схемы (`pbkdf2_sha256$...`) —
приём донора: когда схему сменят, старые пароли останутся читаемыми и
перехешируются при первом входе, а не отвалятся все разом.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.models import AUDIT_SYSTEM, User

#: Параметры хеширования. Менять только вместе с перехешированием паролей.
HASH_ALGORITHM = "sha256"

#: Двести тысяч повторов — не «медленно», а НАМЕРЕННО дорого: столько
#: же будет стоить каждая попытка подбора. Число хранится в самой строке
#: пароля, поэтому старые пароли переживают его изменение.
HASH_ITERATIONS = 200_000

#: В тестах повторов меньше — иначе прогон уходит в хеширование.
#: Замер: 179 мс на пароль, три пароля в каждой подготовке, шесть десятков
#: веб-тестов. Треть времени прогона тратилась на защиту от подбора,
#: которого в тестах не бывает.
#:
#: Послабление включается ТОЛЬКО переменной окружения, которую ставит
#: `tests/conftest.py`. В бою её нет, и ослабить хеширование случайно
#: нельзя: забытая в коде «отладочная» константа — обычный путь к тому,
#: что боевые пароли годами лежат под слабой защитой.
if os.environ.get("SKI_FAST_HASH") == "1":  # pragma: no cover — только тесты
    HASH_ITERATIONS = 1_000
HASH_PREFIX = "pbkdf2_sha256"
SALT_BYTES = 16

#: Минимальная длина пароля.
MIN_PASSWORD_LENGTH = 8

#: Защита от перебора: сколько неудач терпим и на сколько секунд закрываем.
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCK_SECONDS = 60


class SecurityError(Exception):
    """Ошибка доступа с готовым текстом для человека."""


class AccessDenied(SecurityError):
    """Роли не хватает прав на это действие."""


class Messages:
    """Тексты модуля. Собраны в одном месте — их читают люди, не программисты."""

    LOGIN_REQUIRED = "Введите логин."
    PASSWORD_REQUIRED = "Введите пароль."
    BAD_CREDENTIALS = "Неверный логин или пароль."
    USER_DISABLED = "Учётная запись отключена. Обратитесь к администратору."
    TOO_MANY_ATTEMPTS = "Слишком много неудачных попыток. Повторите через {seconds} сек."
    PASSWORD_TOO_SHORT = f"Пароль короче {MIN_PASSWORD_LENGTH} знаков."
    PASSWORD_MISMATCH = "Пароли не совпадают."
    LOGIN_TAKEN = "Логин «{login}» уже занят."
    LAST_ADMIN = "Это последний администратор — без него в систему не войдёт никто."
    ACCESS_DENIED = "Недостаточно прав для этого действия."
    USER_NOT_FOUND = "Учётная запись не найдена."


# --------------------------------------------------------------------------
# Роли и права
# --------------------------------------------------------------------------


class Role(str, Enum):
    """Роли на сайте. Мастера участков работают в боте и сюда не входят."""

    ADMIN = "admin"
    KEEPER = "keeper"
    CHIEF = "chief"


class Permission(str, Enum):
    """Атомарные действия.

    Право даётся на действие, а не на экран: экран можно обойти, действие —
    нет. Урок Б-1 «Заявок»: спрятанная кнопка не есть право, потому что
    импорт, миграция и правка базы руками идут мимо экрана.
    """

    # Склад и движение приборов
    INSTRUMENT_ISSUE = "instrument.issue"
    INSTRUMENT_RETURN = "instrument.return"
    INSTRUMENT_EDIT = "instrument.edit"
    # Метрология
    VERIFICATION_ADD = "verification.add"
    # Заявки на перемещение
    REQUEST_DECIDE = "request.decide"
    # Договоры, участки, комплекты
    CONTRACT_EDIT = "contract.edit"
    SITE_EDIT = "site.edit"
    KIT_EDIT = "kit.edit"
    # Справочники и учётные записи
    CATALOG_EDIT = "catalog.edit"
    USER_MANAGE = "user.manage"
    # Журнал действий
    AUDIT_VIEW = "audit.view"


ROLE_TITLES: dict[Role, str] = {
    Role.ADMIN: "Администратор",
    Role.KEEPER: "Кладовщик",
    Role.CHIEF: "Главный инженер",
}

#: Единственное место, где роль превращается в набор действий.
#:
#: Кладовщик — материально ответственное лицо: выдаёт, принимает, ведёт
#: карточки приборов и поверки. Заявки не решает (ответ 46: согласует
#: главный инженер).
#:
#: Главный инженер решает заявки и смотрит журнал действий, но склад не
#: ведёт: выдачу оформляет МОЛ (ответ 43).
#:
#: Администратор может всё: он и настраивает систему, и подменяет любого.
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ADMIN: frozenset(Permission),
    Role.KEEPER: frozenset(
        {
            Permission.INSTRUMENT_ISSUE,
            Permission.INSTRUMENT_RETURN,
            Permission.INSTRUMENT_EDIT,
            Permission.VERIFICATION_ADD,
            Permission.SITE_EDIT,
            Permission.KIT_EDIT,
        }
    ),
    Role.CHIEF: frozenset(
        {
            Permission.REQUEST_DECIDE,
            Permission.AUDIT_VIEW,
            Permission.CONTRACT_EDIT,
            Permission.SITE_EDIT,
            Permission.KIT_EDIT,
        }
    ),
}


def parse_role(value: object) -> Role:
    """Привести значение к роли. Неизвестное — самая безопасная роль.

    Приём донора: неизвестная роль не падает и не даёт прав администратора.
    """
    if isinstance(value, Role):
        return value
    try:
        return Role(str(value))
    except ValueError:
        return Role.KEEPER


def role_title(role: Role | str | None) -> str:
    """Название роли для человека."""
    parsed = parse_role(role)
    return ROLE_TITLES.get(parsed, parsed.value)


def can(role: Role | str | None, permission: Permission) -> bool:
    """Есть ли у роли это право."""
    return permission in ROLE_PERMISSIONS.get(parse_role(role), frozenset())


def require(actor: CurrentUser | None, permission: Permission) -> None:
    """Потребовать право. Бросает `AccessDenied`, если его нет.

    Проверка стоит в операции, а не в шаблоне: спрятанная кнопка не есть
    право. Без действующего лица прав нет никаких — fail-closed.
    """
    if actor is None or not can(actor.role, permission):
        raise AccessDenied(Messages.ACCESS_DENIED)


# --------------------------------------------------------------------------
# Снимок вошедшего
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CurrentUser:
    """Снимок пользователя, не привязанный к сессии SQLAlchemy.

    Приём донора: держать в сеансе ORM-объект нельзя — его сессия закроется
    раньше, чем шаблон дорисует страницу. Снимок неизменяем и переживает
    закрытие сессии.
    """

    id: int
    login: str
    display_name: str
    role: Role
    must_change_password: bool = False

    @property
    def name(self) -> str:
        """Как звать человека: имя, а если не задано — логин."""
        return self.display_name or self.login

    @property
    def title(self) -> str:
        """Подпись в шапке: имя и роль."""
        return f"{self.name} · {role_title(self.role)}"

    def can(self, permission: Permission | str) -> bool:
        """Удобная проверка для шаблона: `{% if user.can('request.decide') %}`."""
        if isinstance(permission, str):
            try:
                permission = Permission(permission)
            except ValueError:
                return False
        return can(self.role, permission)


def snapshot(user: User) -> CurrentUser:
    """Снять неизменяемый снимок с записи пользователя."""
    return CurrentUser(
        id=user.id,
        login=user.login,
        display_name=user.display_name or "",
        role=parse_role(user.role),
        must_change_password=bool(user.must_change_password),
    )


# --------------------------------------------------------------------------
# Пароли
# --------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Построить строку хранения пароля."""
    salt = os.urandom(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        HASH_ALGORITHM, password.encode("utf-8"), salt, HASH_ITERATIONS
    )
    return f"{HASH_PREFIX}${HASH_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Проверить пароль.

    Сравнение через `compare_digest`, а не `==`: обычное сравнение строк
    выходит из цикла на первом несовпавшем байте, и по времени ответа
    можно подбирать хеш по одному знаку.
    """
    stored = (stored or "").strip()
    if not stored:
        return False
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != HASH_PREFIX:
        return False
    try:
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        expected = bytes.fromhex(parts[3])
    except (ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac(HASH_ALGORITHM, password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


def validate_password(password: str, confirmation: str | None = None) -> None:
    """Проверить требования к новому паролю."""
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise SecurityError(Messages.PASSWORD_TOO_SHORT)
    if confirmation is not None and password != confirmation:
        raise SecurityError(Messages.PASSWORD_MISMATCH)


# --------------------------------------------------------------------------
# Защита входа от перебора
# --------------------------------------------------------------------------

#: Логин → (число неудач, время последней). Живёт только в памяти процесса:
#: перезапуск сервера снимает блокировку, и это осознанно — иначе опечатка
#: администратора требовала бы правки базы.
_login_failures: dict[str, tuple[int, float]] = {}


def _failure_key(login: str) -> str:
    return (login or "").strip().lower()


def login_lock_seconds_left(login: str) -> int:
    """Сколько секунд осталось до конца паузы. Ноль — вход открыт."""
    attempts, last = _login_failures.get(_failure_key(login), (0, 0.0))
    if attempts < LOGIN_MAX_ATTEMPTS:
        return 0
    left = LOGIN_LOCK_SECONDS - int(time.time() - last)
    return max(left, 0)


def register_login_failure(login: str) -> None:
    """Засчитать неудачную попытку входа."""
    key = _failure_key(login)
    attempts, _ = _login_failures.get(key, (0, 0.0))
    _login_failures[key] = (attempts + 1, time.time())


def reset_login_failures(login: str) -> None:
    """Успешный вход обнуляет счётчик."""
    _login_failures.pop(_failure_key(login), None)


# --------------------------------------------------------------------------
# Вход
# --------------------------------------------------------------------------


def find_user(session: Session, login: str) -> User | None:
    """Найти пользователя по логину без учёта регистра.

    Логины латиницей, поэтому встроенный `lower()` подходит. Для русских
    названий он не работает — урок Р9.7 свода БПО.
    """
    cleaned = (login or "").strip()
    if not cleaned:
        return None
    return session.scalar(select(User).where(func.lower(User.login) == cleaned.lower()))


def authenticate(session: Session, login: str, password: str) -> CurrentUser:
    """Проверить логин и пароль.

    Бросает `SecurityError` с текстом, который можно показать человеку
    без перевода с программистского.
    """
    if not (login or "").strip():
        raise SecurityError(Messages.LOGIN_REQUIRED)

    left = login_lock_seconds_left(login)
    if left > 0:
        raise SecurityError(Messages.TOO_MANY_ATTEMPTS.format(seconds=left))

    user = find_user(session, login)
    # Один и тот же текст для «нет такого логина» и «неверный пароль»:
    # разные ответы подсказывали бы подбирающему, какие логины существуют.
    if user is None or not verify_password(password or "", user.password_hash):
        register_login_failure(login)
        audit.write(
            session,
            "Неудачная попытка входа",
            actor=(login or "").strip() or audit.UNKNOWN_ACTOR,
            object_type="user",
            object_id=user.id if user else None,
            details="неверный логин или пароль",
            audit_type=AUDIT_SYSTEM,
        )
        raise SecurityError(Messages.BAD_CREDENTIALS)

    if not user.is_active:
        register_login_failure(login)
        raise SecurityError(Messages.USER_DISABLED)

    reset_login_failures(login)
    user.last_login_at = datetime.now()
    audit.write(
        session,
        "Вход в систему",
        actor=user.display_name or user.login,
        object_type="user",
        object_id=user.id,
    )
    return snapshot(user)


# --------------------------------------------------------------------------
# Управление учётными записями
# --------------------------------------------------------------------------


def list_users(session: Session, only_active: bool = False) -> list[User]:
    """Все учётные записи, отключённые в конце списка."""
    query = select(User)
    if only_active:
        query = query.where(User.is_active.is_(True))
    return list(session.scalars(query.order_by(User.is_active.desc(), User.login)))


def count_active_admins(session: Session, exclude_id: int | None = None) -> int:
    """Сколько действующих администраторов, не считая указанного."""
    query = select(func.count(User.id)).where(
        User.role == Role.ADMIN.value, User.is_active.is_(True)
    )
    if exclude_id is not None:
        query = query.where(User.id != exclude_id)
    return int(session.scalar(query) or 0)


def is_first_run(session: Session) -> bool:
    """Первый запуск — когда учётных записей ещё нет.

    Донор считает первым запуском состояние «ни у кого не задан пароль»,
    а не «пользователей нет» (их урок Л-14: окно входа подсказывало логин
    `admin` без пароля всегда, а это инструкция для постороннего). Здесь
    пароль обязателен у всех, поэтому достаточно проверить, что записей нет.
    """
    return int(session.scalar(select(func.count(User.id))) or 0) == 0


def create_user(
    session: Session,
    login: str,
    password: str,
    role: Role | str,
    *,
    display_name: str = "",
    email: str = "",
    actor: CurrentUser | None = None,
    require_permission: bool = True,
) -> User:
    """Завести учётную запись.

    `require_permission=False` — только для первого запуска, когда
    администратора ещё не существует и спрашивать право не у кого.
    """
    if require_permission:
        require(actor, Permission.USER_MANAGE)

    cleaned = (login or "").strip()
    if not cleaned:
        raise SecurityError(Messages.LOGIN_REQUIRED)
    if find_user(session, cleaned) is not None:
        raise SecurityError(Messages.LOGIN_TAKEN.format(login=cleaned))
    validate_password(password)

    user = User(
        login=cleaned,
        password_hash=hash_password(password),
        role=parse_role(role).value,
        display_name=(display_name or "").strip(),
        email=(email or "").strip(),
        is_active=True,
    )
    session.add(user)
    session.flush()

    audit.write(
        session,
        "Заведена учётная запись",
        actor=actor.name if actor else "первый запуск",
        object_type="user",
        object_id=user.id,
        details=f"{user.login}, роль: {role_title(user.role)}",
    )
    return user


def set_active(
    session: Session, user_id: int, active: bool, *, actor: CurrentUser | None = None
) -> User:
    """Включить или отключить учётную запись.

    Удаления нет намеренно: идентификатор человека это вся история его
    действий, и стереть её значит потерять учёт по людям (правило донора).
    """
    require(actor, Permission.USER_MANAGE)

    user = session.get(User, user_id)
    if user is None:
        raise SecurityError(Messages.USER_NOT_FOUND)
    if not active and parse_role(user.role) is Role.ADMIN:
        if count_active_admins(session, exclude_id=user.id) == 0:
            raise SecurityError(Messages.LAST_ADMIN)

    user.is_active = active
    session.flush()
    audit.write(
        session,
        "Учётная запись включена" if active else "Учётная запись отключена",
        actor=actor.name if actor else None,
        object_type="user",
        object_id=user.id,
        details=user.login,
    )
    return user


def change_role(
    session: Session, user_id: int, role: Role | str, *, actor: CurrentUser | None = None
) -> User:
    """Сменить роль. Последнего администратора понизить нельзя."""
    require(actor, Permission.USER_MANAGE)

    user = session.get(User, user_id)
    if user is None:
        raise SecurityError(Messages.USER_NOT_FOUND)

    new_role = parse_role(role)
    if parse_role(user.role) is Role.ADMIN and new_role is not Role.ADMIN:
        if count_active_admins(session, exclude_id=user.id) == 0:
            raise SecurityError(Messages.LAST_ADMIN)

    was = role_title(user.role)
    user.role = new_role.value
    session.flush()
    audit.write(
        session,
        "Изменена роль",
        actor=actor.name if actor else None,
        object_type="user",
        object_id=user.id,
        details=f"{user.login}: {was} → {role_title(new_role)}",
    )
    return user


def set_password(
    session: Session,
    user_id: int,
    password: str,
    *,
    confirmation: str | None = None,
    actor: CurrentUser | None = None,
    force_change: bool = False,
) -> User:
    """Задать пароль. Свой пароль меняет каждый, чужой — администратор."""
    user = session.get(User, user_id)
    if user is None:
        raise SecurityError(Messages.USER_NOT_FOUND)
    if actor is None or (actor.id != user.id and not can(actor.role, Permission.USER_MANAGE)):
        raise AccessDenied(Messages.ACCESS_DENIED)

    validate_password(password, confirmation)
    user.password_hash = hash_password(password)
    user.must_change_password = force_change
    session.flush()
    audit.write(
        session,
        "Изменён пароль",
        actor=actor.name if actor else None,
        object_type="user",
        object_id=user.id,
        details="свой пароль" if actor.id == user.id else user.login,
    )
    return user
