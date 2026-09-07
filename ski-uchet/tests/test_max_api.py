r"""Сторож разбора ответов MAX: событие доходит до логики целым.

Какое обещание стережём
------------------------

«Что MAX прислал, то бот и увидел». Между сетью и правилами учёта стоит
`parse_update`: он достаёт из вложенного ответа отправителя, чат, текст
и нажатую кнопку. Ошибётся он — и логика бота получит пустоту, будучи
при этом совершенно исправной.

Почему сторож понадобился
--------------------------

Тесты бота строят `Update` руками — и правильно делают: правила учёта
не должны знать формата чужого JSON. Но из-за этого сам разбор не был
прикрыт ничем. Разойдись наше представление о формате с настоящим — все
двадцать три теста бота остались бы зелёными, а бот в MAX молчал бы.

Образцы ниже сняты с живого ответа платформы (07.09.2026), а не
сочинены по документации: документация описывает намерение, а разбирать
приходится то, что действительно приходит.

Чем это отличается от проверки живьём
--------------------------------------

Живьём проверено и работает. Но живая проверка требует токена, сети и
чьего-то сообщения боту — её нельзя прогнать на каждую правку, а
значит, она не сторож. Здесь те же формы ответа, но без сети.
"""
from __future__ import annotations

from app.max_api import Update, button, parse_update


def test_message_is_understood() -> None:
    """Обычное сообщение: видно, кто написал, куда и что."""
    событие = parse_update(
        {
            "update_type": "message_created",
            "timestamp": 1757247600000,
            "message": {
                "sender": {
                    "user_id": 423695561,
                    "name": "Сидоров С.С.",
                    "is_bot": False,
                },
                "recipient": {"chat_id": -70001, "chat_type": "dialog"},
                "body": {"mid": "mid.001", "seq": 1, "text": "ИНВ-001"},
            },
        }
    )

    assert событие is not None
    assert событие.is_message
    assert событие.max_user_id == "423695561"
    assert событие.chat_id == "-70001"
    assert событие.text == "ИНВ-001"
    assert событие.display_name == "Сидоров С.С."


def test_pressed_button_is_understood() -> None:
    """Нажатие кнопки — это команда, набранная не руками.

    У нажатия отправитель лежит НЕ там, где у сообщения: он внутри
    `callback`. Перепутай мы эти два места — бот принимал бы нажатия
    от имени того, кто написал последним.
    """
    событие = parse_update(
        {
            "update_type": "message_callback",
            "callback": {
                "callback_id": "cb.001",
                "payload": "move:12",
                "user": {"user_id": 423695561, "name": "Сидоров С.С."},
            },
            "message": {
                "recipient": {"chat_id": -70001, "chat_type": "dialog"},
                "body": {"mid": "mid.002", "seq": 2, "text": "Что делаем?"},
            },
        }
    )

    assert событие is not None
    assert событие.is_button
    assert not событие.is_message
    assert событие.payload == "move:12"
    assert событие.max_user_id == "423695561"


def test_first_visit_is_understood() -> None:
    """`bot_started` — человек открыл бота впервые.

    Событие приходит без `body`: сказать ему пока нечего. Но именно
    здесь бот здоровается и просит код-приглашение, так что событие
    обязано считаться сообщением.
    """
    событие = parse_update(
        {
            "update_type": "bot_started",
            "chat_id": -70001,
            "user": {"user_id": 423695561, "name": "Сидоров С.С."},
        }
    )

    assert событие is not None
    assert событие.is_message
    assert событие.max_user_id == "423695561"
    assert событие.chat_id == "-70001"
    assert событие.text == ""


def test_attachment_is_carried_through() -> None:
    """Фото повреждения доходит до логики.

    Мастер прикрепляет акт с подписью и фото повреждений (ответ 56).
    Потеряется вложение при разборе — заявка уйдёт без доказательства.
    """
    событие = parse_update(
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 423695561, "name": "Сидоров С.С."},
                "recipient": {"chat_id": -70001},
                "body": {
                    "mid": "mid.003",
                    "text": "Разбит корпус",
                    "attachments": [
                        {
                            "type": "image",
                            "payload": {
                                "photo_id": 9001,
                                "url": "https://example.invalid/photo.jpg",
                            },
                        }
                    ],
                },
            },
        }
    )

    assert событие is not None
    assert len(событие.attachments) == 1
    assert событие.attachments[0]["type"] == "image"
    assert событие.text == "Разбит корпус"


def test_event_without_sender_is_skipped() -> None:
    """Некому отвечать — событие пропускаем, а не роняем опрос.

    Урок Р9.3: сбой на одном событии не должен останавливать разговор
    со всеми остальными. Мастер на объекте не узнает, что бот замолчал
    из-за чужого служебного события.
    """
    assert parse_update({"update_type": "message_removed", "message_id": "mid.004"}) is None
    assert parse_update({}) is None


def test_unknown_event_does_not_break_anything() -> None:
    """Незнакомое событие разбирается, но сообщением не считается.

    MAX добавляет новые типы событий, не спрашивая нас. Такое событие
    должно тихо пройти мимо: не сообщение, не нажатие — просто ничего.
    """
    событие = parse_update(
        {
            "update_type": "user_added",
            "user": {"user_id": 423695561, "name": "Сидоров С.С."},
            "chat_id": -70001,
        }
    )

    assert событие is not None
    assert not событие.is_message
    assert not событие.is_button


def test_numbers_become_text() -> None:
    """Идентификаторы хранятся строками, хотя MAX шлёт числа.

    Урок «Электро»: сравнение 423695561 == "423695561" ложно, и привязка
    мастера тихо перестаёт находиться. Приводим один раз здесь, на входе,
    а не в каждом месте, где идентификатор понадобится.
    """
    событие = parse_update(
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 423695561},
                "recipient": {"chat_id": -70001},
                "body": {"text": "код"},
            },
        }
    )

    assert событие is not None
    assert isinstance(событие.max_user_id, str)
    assert isinstance(событие.chat_id, str)


def test_button_carries_command() -> None:
    """Кнопка несёт то, что вернётся боту при нажатии."""
    кнопка = button("Подтвердить получение", "confirm:12")

    assert кнопка["type"] == "callback"
    assert кнопка["text"] == "Подтвердить получение"
    assert кнопка["payload"] == "confirm:12"


def test_parsed_event_is_the_same_shape_bot_tests_use() -> None:
    """Разбор даёт ровно то, что тесты бота строят руками.

    Сторож против расхождения: тесты бота работают с `Update`,
    собранным вручную. Начни `parse_update` возвращать что-то другое —
    те тесты остались бы зелёными, а бот сломался бы.
    """
    разобранное = parse_update(
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": "max-1", "name": "Сидоров С.С."},
                "recipient": {"chat_id": "chat-1"},
                "body": {"text": "ИНВ-001"},
            },
        }
    )
    вручную = Update(
        update_type="message_created",
        max_user_id="max-1",
        chat_id="chat-1",
        text="ИНВ-001",
        display_name="Сидоров С.С.",
    )

    assert разобранное is not None
    for поле in ("update_type", "max_user_id", "chat_id", "text", "display_name", "payload"):
        assert getattr(разобранное, поле) == getattr(вручную, поле), поле
