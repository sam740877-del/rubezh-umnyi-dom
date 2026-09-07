r"""Разговор с MAX Bot API: только транспорт, без единого правила учёта.

Отдельным модулем нарочно. Логику бота (что ответить, что записать в базу)
нужно проверять без сети и без токена — а её проверка через настоящий
мессенджер была бы медленной, ненадёжной и требовала бы живого бота
у каждого, кто запускает тесты.

Здесь только: отправить, забрать обновления, скачать вложение. Все
решения — в `app/bot.py`.

Что проверено по документации (dev.max.ru, 06.09.2026)
--------------------------------------------------------

- Базовый адрес — `https://platform-api2.max.ru`.
- **Токен передаётся заголовком `Authorization`.** Дословно из документации:
  «Передача токена через query-параметры больше не поддерживается».
- Обновления забираются `GET /updates` с параметрами `limit` (1-1000,
  по умолчанию 100), `timeout` (0-90 секунд), `marker` (указатель на
  следующее обновление), `types` (какие события нужны).
- Ответ содержит `updates` и `marker`; передав `marker` обратно,
  подтверждаем, что предыдущие обновления обработаны.
- Сообщения шлются `POST /messages` с `chat_id` или `user_id` и `text`.
- Предел — 30 запросов в секунду.

Оговорка про long polling
--------------------------

Документация прямо предупреждает: «Получение обновлений с помощью Long
Polling ограничено по скорости и сроку хранения событий — этот способ
не подходит для production-окружения», и рекомендует вебхук.

Мы всё же берём long polling: вебхук требует входящих подключений
к серверу конторы, а решение не выставлять систему наружу — то, ради
чего бот и затевался (`ROADMAP.md`, этап 6). Нагрузка конторы — десяток
событий в день против пределов, о которых предупреждает документация.
Развилка описана в плане; если long polling окажется мал, там же три
выхода из положения.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent

BASE_URL = "https://platform-api2.max.ru"

#: Набор доверенных сертификатов.
#:
#: MAX выпускает свой сертификат у РОССИЙСКОГО удостоверяющего центра
#: («Russian Trusted Sub CA», Минцифры). В обычных наборах корневых
#: сертификатов его нет, и connection к платформе падает с
#: «CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate».
#:
#: Это не поломка и не наша ошибка: сертификат настоящий, просто
#: подписан центром, которого браузер не знает. Лечится добавлением
#: корневого сертификата Минцифры — он лежит в `certs/bundle.pem`
#: рядом с приложением и собирается `tools/update_certs.py`.
#:
#: Проверять сертификаты ВООБЩЕ (`verify=False`) — не выход: тогда
#: любой, кто встанет между нами и MAX, сможет читать и подменять
#: сообщения, а бот носит акты приёмки и решения по заявкам.
CERT_BUNDLE = BASE_DIR / "certs" / "bundle.pem"

#: Сколько секунд держать запрос обновлений открытым. Предел MAX — 90.
POLL_TIMEOUT = 30

#: Сколько обновлений забирать за раз. Предел MAX — 1000.
POLL_LIMIT = 100

#: Сколько ждать ответа сверх времени опроса: сеть тоже не мгновенна.
NETWORK_MARGIN = 15


class MaxApiError(Exception):
    """Ошибка обращения к MAX, с текстом для журнала."""


def token() -> str:
    """Токен бота.

    Читается из окружения при каждом обращении, а не при загрузке модуля:
    урок «Электро» — значение, вычисленное при импорте, застывает раньше,
    чем программа успевает что-либо решить.

    В исходниках токена нет и не будет: документация MAX предупреждает
    прямо — попав в чужие руки, он отдаёт бота целиком.
    """
    value = os.environ.get("MAX_BOT_TOKEN", "").strip()
    if not value:
        raise MaxApiError(
            "Не задан токен бота. Положите его в переменную окружения "
            "MAX_BOT_TOKEN (или в файл .env на сервере)."
        )
    return value


def is_configured() -> bool:
    """Настроен ли бот. Без токена все экраны должны говорить об этом прямо."""
    return bool(os.environ.get("MAX_BOT_TOKEN", "").strip())


@dataclass
class Update:
    """Одно событие от MAX, приведённое к тому, что нужно боту.

    Разбор сложен ровно настолько, насколько сложен ответ MAX: `Update`
    там оборачивает разные события с разной вложенностью, и лезть в эту
    вложенность из логики бота — значит привязать её к чужому формату.
    """

    update_type: str
    max_user_id: str
    chat_id: str
    text: str = ""
    display_name: str = ""
    payload: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_message(self) -> bool:
        return self.update_type in ("message_created", "bot_started")

    @property
    def is_button(self) -> bool:
        """Нажата кнопка под сообщением."""
        return self.update_type == "message_callback"


def parse_update(raw: dict[str, Any]) -> Update | None:
    """Достать из ответа MAX то, что нужно боту. `None` — событие не наше.

    Формат читается терпимо: у разных событий поля лежат на разной
    глубине, и отсутствие любого из них не должно ронять опрос. Событие,
    из которого не вышло достать отправителя, пропускаем — отвечать
    всё равно некому.
    """
    update_type = str(raw.get("update_type") or "")
    if not update_type:
        return None

    message = raw.get("message") or {}
    body = message.get("body") or {}
    sender = (message.get("sender") or raw.get("user") or {})
    recipient = message.get("recipient") or {}
    callback = raw.get("callback") or {}

    if callback:
        sender = callback.get("user") or sender

    max_user_id = str(sender.get("user_id") or raw.get("user_id") or "")
    chat_id = str(
        recipient.get("chat_id")
        or raw.get("chat_id")
        or message.get("chat_id")
        or ""
    )
    if not max_user_id:
        return None

    return Update(
        update_type=update_type,
        max_user_id=max_user_id,
        chat_id=chat_id,
        text=str(body.get("text") or raw.get("text") or ""),
        display_name=str(sender.get("name") or sender.get("first_name") or ""),
        payload=str(callback.get("payload") or ""),
        attachments=list(body.get("attachments") or []),
        raw=raw,
    )


class MaxClient:
    """Обёртка над HTTP: обновления и отправка.

    Отдельным классом, чтобы в тестах его подменяли целиком: логика бота
    не должна знать, есть ли сеть.
    """

    def __init__(self, api_token: str | None = None, base_url: str = BASE_URL) -> None:
        self._token = api_token
        self._base_url = base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        # Заголовок, а не query-параметр: документация MAX прямо говорит,
        # что передача токена в адресе больше не поддерживается.
        return {"Authorization": self._token or token()}

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        timeout = kwargs.pop("timeout", 30)
        # Свой набор сертификатов, если он собран: иначе MAX не проходит
        # проверку — его центр Минцифры браузеру незнаком.
        проверка = str(CERT_BUNDLE) if CERT_BUNDLE.exists() else True
        try:
            with httpx.Client(timeout=timeout, verify=проверка) as client:
                response = client.request(
                    method, f"{self._base_url}{path}", headers=self._headers(), **kwargs
                )
        except httpx.HTTPError as exc:
            raise MaxApiError(f"MAX недоступен: {exc}") from exc

        if response.status_code >= 400:
            raise MaxApiError(
                f"MAX ответил {response.status_code}: {response.text[:200]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise MaxApiError(f"MAX прислал не JSON: {response.text[:200]}") from exc

    def get_updates(
        self, marker: int | None = None, timeout: int = POLL_TIMEOUT, limit: int = POLL_LIMIT
    ) -> tuple[list[Update], int | None]:
        """Забрать новые события. Возвращает события и новый указатель.

        Указатель (`marker`) возвращается MAX и передаётся в следующий
        запрос: этим мы подтверждаем, что предыдущие события обработаны.
        Потеряв указатель, получим их заново — лучше повтор, чем пропажа.
        """
        params: dict[str, Any] = {"timeout": timeout, "limit": limit}
        if marker is not None:
            params["marker"] = marker

        data = self._request(
            "GET", "/updates", params=params, timeout=timeout + NETWORK_MARGIN
        )
        updates = []
        for raw in data.get("updates") or []:
            parsed = parse_update(raw)
            if parsed is not None:
                updates.append(parsed)
        return updates, data.get("marker")

    def send_message(
        self,
        text: str,
        *,
        chat_id: str | None = None,
        user_id: str | None = None,
        buttons: list[list[dict[str, str]]] | None = None,
    ) -> dict[str, Any]:
        """Отправить сообщение. Нужен либо `chat_id`, либо `user_id`."""
        if not chat_id and not user_id:
            raise MaxApiError("Некому отправлять: не указан ни чат, ни получатель.")

        params: dict[str, Any] = {}
        if chat_id:
            params["chat_id"] = chat_id
        elif user_id:
            params["user_id"] = user_id

        body: dict[str, Any] = {"text": text}
        if buttons:
            body["attachments"] = [
                {"type": "inline_keyboard", "payload": {"buttons": buttons}}
            ]
        return self._request("POST", "/messages", params=params, json=body)


def button(text: str, payload: str) -> dict[str, str]:
    """Кнопка под сообщением.

    `payload` возвращается боту при нажатии — это и есть команда,
    которую человек «набрал», не набирая.
    """
    return {"type": "callback", "text": text, "payload": payload}
