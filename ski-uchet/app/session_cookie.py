r"""Сеанс пользователя в подписанном печенье (cookie).

Почему не серверные сеансы в базе: система живёт внутри локальной сети
конторы, пользователей 5-15 (ответ 36), и таблица сеансов означала бы
запись в базу на каждый запрос ради того, что помещается в 200 байт.
Подписанное печенье даёт то же самое без единого лишнего запроса.

Почему не JWT: библиотека ради `hmac` из стандартной поставки. Формат
здесь проще и читается глазами при разборе.

**Что лежит внутри и чего там нет.** В печенье только идентификатор
пользователя и время выдачи. Роль и права **не хранятся**: иначе
администратор, понизивший роль, ждал бы, пока у человека истечёт сеанс.
Роль читается из базы на каждом запросе — это один запрос по первичному
ключу, дешевле любой сложности с отзывом.

Подпись — HMAC-SHA256 на ключе приложения. Ключ берётся из окружения
`SKI_SECRET_KEY`; если его нет, при запуске создаётся файл `.secret_key`
рядом с базой. Ключ в исходниках не лежит: с ним любой желающий подпишет
себе печенье администратора.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

#: Имя печенья и срок его жизни. Двенадцать часов — рабочий день с запасом:
#: кладовщик не должен входить заново после обеда, но и оставленный на ночь
#: браузер утром потребует пароль.
COOKIE_NAME = "ski_session"
MAX_AGE_SECONDS = 12 * 60 * 60


def _key_file() -> Path:
    """Где лежит ключ подписи, если он не задан в окружении."""
    return BASE_DIR / ".secret_key"


def secret_key() -> bytes:
    """Ключ подписи сеансов.

    Порядок: переменная окружения, затем файл рядом с базой, затем создаём
    новый. Смена ключа разлогинивает всех — это нормальная цена за то,
    что ключ не лежит в исходниках.
    """
    from_env = os.environ.get("SKI_SECRET_KEY", "").strip()
    if from_env:
        return from_env.encode("utf-8")

    path = _key_file()
    if path.exists():
        stored = path.read_text(encoding="utf-8").strip()
        if stored:
            return stored.encode("utf-8")

    generated = secrets.token_urlsafe(48)
    try:
        path.write_text(generated, encoding="utf-8")
    except OSError:
        # Записать не вышло — работаем с ключом в памяти. Сеансы не переживут
        # перезапуск, но система останется работоспособной.
        pass
    return generated.encode("utf-8")


def _sign(payload: bytes) -> str:
    """Подпись полезной части."""
    digest = hmac.new(secret_key(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def issue(user_id: int) -> str:
    """Выдать значение печенья для пользователя."""
    payload = json.dumps({"uid": int(user_id), "at": int(time.time())}).encode("utf-8")
    body = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{body}.{_sign(payload)}"


def read(value: str | None) -> int | None:
    """Достать идентификатор пользователя из печенья.

    Возвращает `None` при любой неправде: подделанная подпись, испорченный
    формат, истёкший срок. Молча — подделка печенья не повод показывать
    подделывающему, что именно у него не сошлось.
    """
    if not value or "." not in value:
        return None

    body, _, signature = value.partition(".")
    try:
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (ValueError, TypeError):
        return None

    # compare_digest, а не ==: по времени обычного сравнения строк
    # подпись подбирается по одному знаку.
    if not hmac.compare_digest(_sign(payload), signature):
        return None

    try:
        data = json.loads(payload.decode("utf-8"))
        issued_at = int(data["at"])
        user_id = int(data["uid"])
    except (ValueError, KeyError, TypeError):
        return None

    if time.time() - issued_at > MAX_AGE_SECONDS:
        return None
    return user_id
