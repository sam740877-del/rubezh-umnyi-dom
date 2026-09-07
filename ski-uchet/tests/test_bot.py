r"""Сторож бота: чужого не видно, разговор не теряется, правила те же.

Какое обещание стережём
------------------------

«Мастер делает через бота ровно то же, что делал бы на сайте, и ровно
с теми же ограничениями». Бот — не обход правил учёта, а другой вход
в них. Прибор с просроченной поверкой нельзя выдать ни там, ни там;
возврат по неисправности требует описания одинаково.

Как обещание ломается в жизни
------------------------------

**Чужой участок.** Мастер видит только свой (ответ 35). Кнопки показывают
свои приборы — но `payload` кнопки приходит от человека, и подставить
туда чужой идентификатор ничто не мешает. Экранная застава здесь не
защита, ровно как в дефекте Б-1 «Заявок».

**Потерянный разговор.** Заявка собирается в три шага. Если состояние
живёт в памяти процесса, перезапуск сервера теряет недособранную заявку,
и мастер начинает заново — на объекте, с телефона, в перчатках.

**Посторонний в боте.** Бот отвечает всякому, кто ему напишет. Он не
должен подсказывать постороннему даже то, что такая система существует.

Чем доказано
-------------

Запуском: гоняем настоящий разговор событиями, как их прислал бы MAX,
и смотрим, что записалось в базу. Сети здесь нет — транспорт отделён
в `app/max_api.py` намеренно.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app import bot, notifications, security, services
from app.max_api import Update
from app.models import (
    BotInvite,
    BotLink,
    ChangeRequest,
    Contract,
    Instrument,
    InstrumentType,
    Site,
    Verification,
)

TODAY = date(2026, 6, 15)


@pytest.fixture()
def world(session):
    """Два участка, приборы на первом, мастер ещё не привязан."""
    kind = InstrumentType(name="Нивелир", category="geodesy", verification_interval_months=12)
    session.add(kind)
    session.flush()

    contract = Contract(number="Д-1", title="Стройконтроль", customer="ООО Заказчик")
    session.add(contract)
    session.flush()

    mine = Site(name="Участок 1", contract_id=contract.id)
    other = Site(name="Участок 2", contract_id=contract.id)
    session.add_all([mine, other])
    session.flush()

    def make(no: str, site_id: int | None) -> Instrument:
        item = Instrument(inventory_no=no, name=f"Нивелир {no}", type_id=kind.id)
        session.add(item)
        session.flush()
        session.add(
            Verification(
                instrument_id=item.id,
                performed_on=TODAY - timedelta(days=30),
                valid_until=TODAY + timedelta(days=300),
                result="ok",
            )
        )
        session.flush()
        if site_id:
            services.issue_instrument(session, item.id, site_id, happened_on=TODAY)
        return item

    session.commit()
    return {
        "mine": mine,
        "other": other,
        "own": make("ИНВ-001", mine.id),
        "second": make("ИНВ-002", mine.id),
        "foreign": make("ИНВ-999", other.id),
    }


def event(text: str = "", *, max_user_id: str = "max-1", payload: str = "") -> Update:
    """Событие, как его прислал бы MAX."""
    return Update(
        update_type="message_callback" if payload else "message_created",
        max_user_id=max_user_id,
        chat_id="chat-1",
        text=text,
        display_name="Сидоров С.С.",
        payload=payload,
    )



def reply_to(session, update, today=TODAY) -> bot.Reply:
    """Ответ бота, который обязан быть.

    `handle()` вправе вернуть `None` — «отвечать нечего». В сценариях
    ниже это всегда ошибка: если бот промолчал там, где человек ждёт
    ответа, тест должен сказать об этом прямо, а не упасть при обращении
    к полю несуществующего ответа.
    """
    reply = bot.handle(session, update, today)
    assert reply is not None, "бот промолчал там, где человек ждёт ответа"
    return reply


def link_master(session, world) -> BotLink:
    """Впустить мастера на первый участок."""
    invite = bot.create_invite(
        session, site_id=world["mine"].id, intended_for="Сидоров С.С., мастер"
    )
    session.flush()
    return bot.redeem_invite(session, invite.code, "max-1", "chat-1", "Сидоров С.С.")


# --------------------------------------------------------------------------
# Вход по коду
# --------------------------------------------------------------------------


def test_stranger_learns_nothing(session, world) -> None:
    """Посторонний не узнаёт о системе ничего.

    Бот не подсказывает даже, что за система за ним стоит: единственное,
    что доступно, — назвать код.
    """
    reply = reply_to(session, event("привет"), TODAY)

    assert "код-приглашение" in reply.text
    assert "Участок 1" not in reply.text
    assert "ИНВ-001" not in reply.text


def test_invite_lets_the_master_in(session, world) -> None:
    """По коду мастер входит и сразу видит положение дел на участке."""
    invite = bot.create_invite(session, site_id=world["mine"].id, intended_for="Сидоров")
    session.flush()

    reply = reply_to(session, event(invite.code), TODAY)

    assert "Участок 1" in reply.text
    assert "Приборов на участке: 2" in reply.text
    assert bot.find_link(session, "max-1") is not None


def test_invite_works_once(session, world) -> None:
    """Код одноразовый: перешлют дальше — не сработает."""
    invite = bot.create_invite(session, site_id=world["mine"].id)
    session.flush()
    bot.handle(session, event(invite.code), TODAY)

    reply = reply_to(session, event(invite.code, max_user_id="max-2"), TODAY)

    assert "уже использован" in reply.text
    assert bot.find_link(session, "max-2") is None


def test_expired_invite_is_refused(session, world) -> None:
    """Просроченный код не пускает."""
    invite = bot.create_invite(session, site_id=world["mine"].id)
    invite.expires_at = datetime.now() - timedelta(days=1)
    session.flush()

    reply = reply_to(session, event(invite.code), TODAY)

    assert "Срок кода истёк" in reply.text


def test_transfer_disables_the_old_link(session, world) -> None:
    """Перевод на другой участок отключает старую привязку.

    Пока она жива, человек видит чужой участок.
    """
    link_master(session, world)
    second = bot.create_invite(session, site_id=world["other"].id)
    session.flush()

    bot.handle(session, event(second.code), TODAY)

    active = bot.find_link(session, "max-1")
    assert active is not None
    assert active.site_id == world["other"].id, "мастер остался на старом участке"

    # Старая запись не удалена: на неё ссылается история заявок.
    all_links = session.query(BotLink).filter(BotLink.max_user_id == "max-1").all()
    assert len(all_links) == 2
    assert sum(1 for item in all_links if item.is_active) == 1


# --------------------------------------------------------------------------
# Чужого не видно
# --------------------------------------------------------------------------


def test_kit_shows_only_own_site(session, world) -> None:
    """В комплекте только свои приборы."""
    link_master(session, world)

    reply = reply_to(session, event(payload="kit"), TODAY)

    assert "ИНВ-001" in reply.text
    assert "ИНВ-999" not in reply.text, "показан прибор чужого участка"


def test_foreign_instrument_is_refused_even_by_direct_payload(session, world) -> None:
    """Чужой прибор не отдаётся даже при прямой подстановке в кнопку.

    Кнопки показывают своё, но `payload` приходит от человека. Экранная
    застава здесь не защита — ровно как в дефекте Б-1 «Заявок».
    """
    link_master(session, world)
    bot.handle(session, event(payload="return_ok"), TODAY)

    reply = reply_to(
        session, event(payload=f"return_ok:{world['foreign'].id}"), TODAY
    )

    assert "нет" in reply.text.lower()

    session.refresh(world["foreign"])
    assert world["foreign"].current_site_id == world["other"].id, (
        "чужой прибор сняли с участка через бота"
    )


def test_foreign_instrument_is_refused_by_number(session, world) -> None:
    """И по набранному номеру тоже."""
    link_master(session, world)
    bot.handle(session, event(payload="return_ok"), TODAY)

    reply = reply_to(session, event("ИНВ-999"), TODAY)

    assert reply is not None
    session.refresh(world["foreign"])
    assert world["foreign"].current_site_id == world["other"].id


# --------------------------------------------------------------------------
# Разговор в несколько шагов
# --------------------------------------------------------------------------


def test_request_is_assembled_in_three_steps(session, world) -> None:
    """Заявка собирается по шагам и доходит до базы."""
    link_master(session, world)

    bot.handle(session, event(payload="request"), TODAY)
    bot.handle(session, event(payload=f"request:{world['own'].id}"), TODAY)
    bot.handle(session, event(payload=f"site:{world['other'].id}"), TODAY)
    reply = reply_to(session, event("нужен на соседнем объекте"), TODAY)
    session.commit()

    assert "подана" in reply.text

    request = session.query(ChangeRequest).one()
    assert request.instrument_id == world["own"].id
    assert request.to_site_id == world["other"].id
    assert request.reason == "нужен на соседнем объекте"
    assert request.requested_by == "Сидоров С.С."


def test_dialog_survives_a_restart(session, world) -> None:
    """Недособранная заявка переживает перезапуск сервера.

    Состояние живёт в базе, а не в памяти процесса: иначе мастер на
    объекте начинал бы заново после каждого перезапуска.
    """
    link_master(session, world)
    bot.handle(session, event(payload="request"), TODAY)
    bot.handle(session, event(payload=f"request:{world['own'].id}"), TODAY)
    session.commit()

    # Имитируем перезапуск: забываем всё, что было в памяти.
    session.expire_all()

    link = bot.find_link(session, "max-1")
    assert link is not None, "привязка пропала после перезапуска"
    assert link.state == "request_pick_site", "разговор потерян"

    bot.handle(session, event(payload=f"site:{world['other'].id}"), TODAY)
    reply = reply_to(session, event(payload="no_reason"), TODAY)
    session.commit()

    assert "подана" in reply.text
    assert session.query(ChangeRequest).count() == 1


def test_cancel_returns_to_the_menu(session, world) -> None:
    """Отмена бросает разговор и ничего не записывает."""
    link_master(session, world)
    bot.handle(session, event(payload="request"), TODAY)
    bot.handle(session, event(payload=f"request:{world['own'].id}"), TODAY)

    reply = reply_to(session, event(payload="cancel"), TODAY)
    session.commit()

    assert "Отменено" in reply.text
    link = bot.find_link(session, "max-1")
    assert link is not None and link.state == "idle"
    assert session.query(ChangeRequest).count() == 0


# --------------------------------------------------------------------------
# Правила учёта в боте те же
# --------------------------------------------------------------------------


def test_faulty_return_requires_a_description(session, world) -> None:
    """Возврат по неисправности не пройдёт без объяснения.

    То же правило, что на сайте: иначе кладовщик не поймёт, что чинить.
    """
    link_master(session, world)
    bot.handle(session, event(payload="return_broken"), TODAY)
    bot.handle(session, event(payload=f"return_broken:{world['own'].id}"), TODAY)

    reply = reply_to(session, event("."), TODAY)
    session.commit()

    assert "Опишите" in reply.text
    session.refresh(world["own"])
    assert world["own"].status == "in_use", "прибор вернули без описания неисправности"


def test_faulty_return_goes_to_repair(session, world) -> None:
    """С описанием прибор возвращается и сразу идёт в ремонт (ответ 55)."""
    link_master(session, world)
    bot.handle(session, event(payload="return_broken"), TODAY)
    bot.handle(session, event(payload=f"return_broken:{world['own'].id}"), TODAY)

    reply = reply_to(session, event("сбит уровень после падения"), TODAY)
    session.commit()

    assert "неисправный" in reply.text
    session.refresh(world["own"])
    assert world["own"].status == "repair"
    assert world["own"].current_site_id is None


def test_healthy_return_needs_no_explanation(session, world) -> None:
    """Сдача исправного — обычная операция, без объяснений (ответ 54)."""
    link_master(session, world)
    bot.handle(session, event(payload="return_ok"), TODAY)

    reply = reply_to(session, event(payload=f"return_ok:{world['own'].id}"), TODAY)
    session.commit()

    assert "сдан на склад" in reply.text
    session.refresh(world["own"])
    assert world["own"].status == "warehouse"


# --------------------------------------------------------------------------
# Сообщения
# --------------------------------------------------------------------------


def test_inbox_does_not_dump_everything_at_once(session, world) -> None:
    """Накопившиеся сообщения не вываливаются пачкой.

    Вернувшийся из отпуска получил бы стену из тридцати сообщений
    и не прочитал бы ни одного.
    """
    link = link_master(session, world)
    for i in range(10):
        notifications.notify_site(
            session, world["mine"].id, "verification_due", f"Сообщение {i}"
        )
    session.commit()

    reply = reply_to(session, event(payload="inbox"), TODAY)
    session.commit()

    shown = sum(1 for i in range(10) if f"Сообщение {i}" in reply.text)
    assert shown == bot.RECENT_NOTIFICATIONS, f"показано {shown} сообщений сразу"
    assert "Ещё сообщений" in reply.text


def test_shown_messages_are_marked_delivered_but_not_read(session, world) -> None:
    """Показанное отмечается доставленным, но не прочитанным.

    Доставка не равна прочтению — правило ящика уведомлений.
    """
    link_master(session, world)
    notifications.notify_site(session, world["mine"].id, "verification_due", "Проверка")
    session.commit()

    # Через полный путь, а не через `handle`: гашение происходит, КОГДА
    # ответ ушёл, — иначе упавшая отправка стирала бы непрочитанное.
    client = FakeClient([event(payload="inbox")])
    bot.poll_once(session, client)
    session.commit()

    entry = session.query(bot.Notification).one()
    assert entry.delivered_via == "bot"
    assert entry.read_at is None, "бот погасил отметку о прочтении"


def test_unshown_messages_stay_pending(session, world) -> None:
    """Непоказанное остаётся недоставленным: человек его не видел."""
    link_master(session, world)
    for i in range(10):
        notifications.notify_site(session, world["mine"].id, "verification_due", f"С {i}")
    session.commit()

    client = FakeClient([event(payload="inbox")])
    bot.poll_once(session, client)
    session.commit()

    pending = notifications.pending_for_bot(session)
    assert len(pending) == 10 - bot.RECENT_NOTIFICATIONS


def test_messages_stay_pending_when_the_answer_does_not_arrive(session, world) -> None:
    """Ответ не дошёл — сообщения ждут дальше, а не пропадают.

    Приёмка 07.09.2026: экран гасил сообщения при сборке ответа, до
    отправки. Упади она — связь на объекте плохая, — и человек уже
    никогда бы их не увидел. А среди них напоминание о заканчивающейся
    поверке: пропустив его, мастер выйдет на объект с просроченным
    прибором, и его замеры не примут.
    """
    link_master(session, world)
    notifications.notify_site(session, world["mine"].id, "verification_due", "Поверка!")
    session.commit()

    client = FakeClient([event(payload="inbox")], fail_on_send=True)
    bot.poll_once(session, client)
    session.commit()

    pending = notifications.pending_for_bot(session)
    assert len(pending) == 1, "сообщение погашено, хотя ответ не дошёл"


def test_office_role_gets_messages_only(session, world) -> None:
    """У офисной роли в боте только сообщения: работает она на сайте."""
    user = security.create_user(
        session, "chief", "test-password-1", "chief", require_permission=False
    )
    invite = bot.create_invite(session, user_id=user.id, intended_for="Главный инженер")
    session.flush()

    reply = reply_to(session, event(invite.code, max_user_id="max-9"), TODAY)
    session.commit()

    payloads = [b["payload"] for row in reply.buttons for b in row]
    assert payloads == ["inbox"], f"офисной роли предложены лишние действия: {payloads}"


# --------------------------------------------------------------------------
# Опрос MAX
# --------------------------------------------------------------------------


class FakeClient:
    """Поддельный MAX: отдаёт заготовленные события, копит отправленное.

    Сети в сторожах нет намеренно — транспорт отделён в `app/max_api.py`
    ровно затем, чтобы логику можно было гонять без токена и без бота
    в мессенджере.
    """

    def __init__(self, updates=None, fail_on_send: bool = False) -> None:
        self.updates = list(updates or [])
        self.sent: list[dict] = []
        self.fail_on_send = fail_on_send

    def get_updates(self, marker=None, timeout=30, limit=100):
        batch, self.updates = self.updates, []
        return batch, (marker or 0) + len(batch)

    def send_message(self, text, *, chat_id=None, user_id=None, buttons=None):
        if self.fail_on_send:
            raise RuntimeError("MAX недоступен")
        self.sent.append(
            {"text": text, "chat_id": chat_id, "user_id": user_id, "buttons": buttons}
        )
        return {"ok": True}


def test_poll_answers_the_events(session, world) -> None:
    """Опрос отвечает на события и двигает указатель."""
    link_master(session, world)
    session.commit()

    client = FakeClient([event(payload="kit")])
    handled, marker = bot.poll_once(session, client)
    session.commit()

    assert handled == 1
    assert marker is not None
    assert "ИНВ-001" in client.sent[0]["text"]


def test_one_broken_event_does_not_stop_the_rest(session, world, monkeypatch) -> None:
    """Одно испорченное событие не останавливает опрос.

    Иначе бот замолчал бы для всех из-за одного сообщения.
    """
    link_master(session, world)
    session.commit()

    original = bot.handle
    calls = {"n": 0}

    def flaky(sess, update, today=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("что-то пошло не так")
        return original(sess, update, today)

    monkeypatch.setattr(bot, "handle", flaky)

    client = FakeClient([event(payload="kit"), event(payload="kit")])
    handled, _ = bot.poll_once(session, client)
    session.commit()

    assert handled == 1, "второе событие не обработалось после сбоя первого"


def test_failed_send_is_recorded_not_swallowed(session, world) -> None:
    """Неотправленный ответ попадает в журнал, а не пропадает молча."""
    from app import audit

    link_master(session, world)
    session.commit()

    client = FakeClient([event(payload="kit")], fail_on_send=True)
    bot.poll_once(session, client)
    session.commit()

    actions = [e.action for e in audit.recent(session)]
    assert "Ответ бота не отправлен" in actions


def test_pending_notifications_are_delivered(session, world) -> None:
    """Бот забирает накопившиеся уведомления и отмечает доставку."""
    link_master(session, world)
    notifications.notify_site(session, world["mine"].id, "verification_due", "Поверка!")
    session.commit()

    client = FakeClient()
    delivered = bot.deliver_pending(session, client)
    session.commit()

    assert delivered == 1
    assert client.sent[0]["text"] == "Поверка!"
    assert notifications.pending_for_bot(session) == []


def test_undelivered_stays_pending_when_nobody_is_linked(session, world) -> None:
    """Некому доставить — запись ждёт, а не считается доставленной.

    Придёт мастер на участок, привяжется — получит.
    """
    notifications.notify_site(session, world["mine"].id, "verification_due", "Поверка!")
    session.commit()

    client = FakeClient()
    delivered = bot.deliver_pending(session, client)
    session.commit()

    assert delivered == 0
    assert len(notifications.pending_for_bot(session)) == 1


class TestУказательСобытий:
    r"""Сторож указателя: перезапуск не обрабатывает события заново.

    Как это сломалось в жизни (07.09.2026)
    ---------------------------------------

    Указатель на последнее обработанное событие жил в памяти процесса:
    `marker = None` при каждом запуске. MAX хранит события несколько
    минут и отдаёт всё, что новее указателя, — а указателя после
    перезапуска не было.

    Второй запуск бота получил те же события заново и обработал их
    второй раз: привязка мастера записалась ДВАЖДЫ, и в чате задвоился
    набор кнопок. Нашлось живой проверкой, не тестами.

    На кнопках это косметика. На заявке — два одинаковых перемещения
    одного прибора: главный инженер согласует оба, и прибор «переедет»
    дважды. А перезапуск на сервере конторы — не редкость: обновление,
    перезагрузка, сбой сети.
    """

    def test_marker_survives_restart(self, session) -> None:
        """Указатель, сохранённый одним запуском, читается следующим."""
        from app import settings

        settings.save_bot_marker(session, 37228)
        session.commit()

        assert settings.bot_marker(session) == 37228

    def test_no_marker_at_first_start(self, session) -> None:
        """Первый запуск начинает с пустого: брать неоткуда."""
        from app import settings

        assert settings.bot_marker(session) is None

    def test_broken_marker_does_not_break_start(self, session) -> None:
        """Чепуха в записи — начинаем сначала, а не падаем.

        Правило донора (БПО): испорченное руками значение превращается
        в умолчание. Упасть на старте — значит не отвечать мастерам
        вовсе, и это хуже, чем повторить несколько событий.
        """
        from app import settings
        from app.models import Setting

        for мусор in ("месяц", "", "  ", "-5", "0"):
            row = session.get(Setting, settings.BOT_MARKER)
            if row is None:
                row = Setting(key=settings.BOT_MARKER)
                session.add(row)
            row.value = мусор
            session.flush()

            assert settings.bot_marker(session) is None, f"на {мусор!r}"

    def test_marker_is_not_shown_as_a_setting(self, session) -> None:
        """Служебная отметка не появляется на экране настроек.

        Человеку там нечего делать: он не знает, что такое указатель
        событий, а увидев поле — поправит его.
        """
        from app import settings

        settings.save_bot_marker(session, 37228)
        session.commit()

        ключи = [s["key"] for s in settings.all_settings(session)]
        assert settings.BOT_MARKER not in ключи

    def test_marker_does_not_flood_the_log(self, session) -> None:
        """Сохранение указателя не пишется в журнал.

        Указатель меняется на каждой порции событий. Попади он в журнал —
        настоящие действия утонули бы в служебных строках.
        """
        from sqlalchemy import func, select

        from app import settings
        from app.models import AuditLog

        было = session.scalar(select(func.count()).select_from(AuditLog))
        settings.save_bot_marker(session, 37228)
        settings.save_bot_marker(session, 37229)
        session.commit()

        assert session.scalar(select(func.count()).select_from(AuditLog)) == было

    def test_repeated_event_does_not_double_the_link(self, session, world) -> None:
        """Главное: то же событие дважды не заводит вторую привязку.

        Сторож на сам дефект, а не только на его причину. Пусть указатель
        снова потеряется — задвоиться привязка не должна.
        """
        from sqlalchemy import select

        from app.models import BotInvite, BotLink

        invite = BotInvite(code="ABC123", site_id=world["mine"].id)
        session.add(invite)
        session.flush()

        событие = event("ABC123")
        bot.handle(session, событие)
        session.flush()
        bot.handle(session, событие)
        session.flush()

        живые = session.scalars(
            select(BotLink).where(
                BotLink.max_user_id == событие.max_user_id,
                BotLink.is_active.is_(True),
            )
        ).all()
        assert len(живые) == 1, f"привязок стало {len(живые)}"


class TestТупикиРазговора:
    r"""Сторожа против логических тупиков: из любого шага есть выход.

    Откуда взялись
    ---------------

    Приёмка 07.09.2026. Владелец делал такую же для бота школы массажа
    и знал по опыту: тупики есть всегда. Нашлось четыре, и главный из
    них портил не разговор, а имущество.

    Общая причина у всех одна: бот не различал НАЖАТУЮ кнопку и
    НАБРАННЫЙ текст (`update.payload or update.text`), а на шагах
    свободного ввода принимал за ответ что угодно. В мессенджере старые
    кнопки остаются на экране и нажимаются — и уходили в дело.

    Почему это дороже, чем кажется
    -------------------------------

    Мастер работает на объекте, с телефона, часто в перчатках, связь
    рвётся. Он не листает ленту вверх в поисках кнопки «Отмена» — он
    пишет «отмена», как написал бы человеку. И он прав, а бот был нет.
    """

    def test_cancel_word_works_like_the_button(self, session, world) -> None:
        """«Отмена» словом равна «Отмене» кнопкой — из любого состояния.

        Главный дефект приёмки: на шаге «опишите неисправность» слово
        «отмена» становилось ОПИСАНИЕМ ПОЛОМКИ. Тахеометр уезжал
        в ремонт с неисправностью «отмена», кладовщик получал
        уведомление, а вернуть прибор мастер сам не мог.
        """
        link_master(session, world)
        прибор = world["own"]

        for слово in ("отмена", "Отмена", "НАЗАД", "стоп", "не надо"):
            bot.handle(session, event(payload="return_broken"))
            bot.handle(session, event(payload=f"return_broken:{прибор.id}"))
            ответ = reply_to(session, event(слово))
            session.flush()

            assert "Отменено" in ответ.text, f"{слово!r} не вышло из разговора"
            assert прибор.status == "in_use", f"{слово!r} отправило прибор в ремонт"
            assert прибор.current_site_id == world["mine"].id

    def test_free_text_steps_always_offer_a_way_out(self, session, world) -> None:
        """На шагах свободного ввода всегда есть кнопка выхода.

        Сообщение без единой кнопки — тупик: человеку нечего нажать,
        а что написать, чтобы выйти, ему никто не сказал.
        """
        link_master(session, world)

        bot.handle(session, event(payload="request"))
        bot.handle(session, event(payload=f"request:{world['own'].id}"))
        ответ = reply_to(session, event(payload=f"site:{world['other'].id}"))
        assert ответ.buttons, "шаг причины остался без кнопок"

        bot.handle(session, event(payload="cancel"))
        session.flush()

        bot.handle(session, event(payload="return_broken"))
        ответ = reply_to(session, event(payload=f"return_broken:{world['own'].id}"))
        assert ответ.buttons, "шаг неисправности остался без кнопок"

    def test_stray_text_keeps_the_site_buttons(self, session, world) -> None:
        """Посторонний текст на выборе участка не оставляет без кнопок.

        Было: «Выберите участок кнопкой.» — и ни одной кнопки. Кнопки
        остались выше в ленте, и на телефоне это тупик.
        """
        link_master(session, world)
        bot.handle(session, event(payload="request"))
        bot.handle(session, event(payload=f"request:{world['own'].id}"))

        ответ = reply_to(session, event("привет"))

        assert ответ.buttons, "ответ без кнопок — человеку нечего нажать"
        payloads = [b["payload"] for row in ответ.buttons for b in row]
        assert any(p.startswith("site:") for p in payloads), "участки не показаны"
        assert "cancel" in payloads, "нет выхода"

    def test_button_is_not_taken_for_a_reason(self, session, world) -> None:
        """Нажатие кнопки не становится причиной заявки.

        Двойное нажатие участка — обычное дело на плохой связи. Второе
        уходило причиной: главный инженер получал заявку с обоснованием
        «site:2», а перепо́дать её мастер уже не мог — по прибору есть
        заявка на согласовании. Запертый мастер ждал чужого решения.
        """
        from sqlalchemy import func, select

        from app.models import ChangeRequest

        link_master(session, world)
        bot.handle(session, event(payload="request"))
        bot.handle(session, event(payload=f"request:{world['own'].id}"))
        bot.handle(session, event(payload=f"site:{world['other'].id}"))

        было = session.scalar(select(func.count()).select_from(ChangeRequest))
        ответ = reply_to(session, event(payload=f"site:{world['other'].id}"))
        session.flush()
        стало = session.scalar(select(func.count()).select_from(ChangeRequest))

        assert стало == было, "нажатие кнопки подало заявку"
        assert "написать словами" in ответ.text
        assert ответ.buttons, "и снова без выхода"

    def test_button_is_not_taken_for_a_defect(self, session, world) -> None:
        """Нажатие кнопки не становится описанием неисправности.

        Старая «Мой комплект» давала неисправность «kit» — ровно три
        буквы, порог «слишком коротко» её пропускал.
        """
        link_master(session, world)
        прибор = world["own"]
        bot.handle(session, event(payload="return_broken"))
        bot.handle(session, event(payload=f"return_broken:{прибор.id}"))

        ответ = reply_to(session, event(payload="kit"))
        session.flush()

        assert прибор.status == "in_use", "прибор уехал в ремонт от нажатия кнопки"
        assert "словами" in ответ.text
        assert ответ.buttons

    def test_stale_button_from_another_dialog_is_refused(self, session, world) -> None:
        """Кнопка чужого действия не принимается к делу.

        В шаге «сдать на склад» нажатие старой `site:1` из прежней
        заявки сдавало на склад прибор с номером 1 — совсем не тот,
        что человек имел в виду, и без всякого подтверждения.
        """
        link_master(session, world)
        чужой = world["foreign"]
        bot.handle(session, event(payload="return_ok"))

        reply_to(session, event(payload=f"site:{чужой.id}"))
        session.flush()

        assert чужой.status != "warehouse", "чужая кнопка сдала прибор на склад"

    def test_sites_beyond_the_first_page_are_reachable(self, session, world) -> None:
        """Участков больше восьми — до остальных можно долистать.

        Список обрезался восемью без «дальше» и без предупреждения:
        у конторы с четырнадцатью участками девятый и следующие были
        недостижимы, и мастер не понимал, почему нужного участка нет.
        """
        from app.models import Site

        link_master(session, world)
        for i in range(12):
            session.add(
                Site(
                    name=f"Объект №{i + 10}",
                    contract_id=world["other"].contract_id,
                    status="active",
                )
            )
        session.flush()

        bot.handle(session, event(payload="request"))
        ответ = reply_to(session, event(payload=f"request:{world['own'].id}"))
        подписи = [b["text"] for row in ответ.buttons for b in row]
        assert any("дальше" in p for p in подписи), "до дальних участков не добраться"

        вторая = reply_to(session, event(payload="site_page:1"))
        payloads = [b["payload"] for row in вторая.buttons for b in row]
        assert any(p.startswith("site:") for p in payloads), "вторая страница пуста"
        assert any("назад" in b["text"] for row in вторая.buttons for b in row)

    def test_made_up_site_is_refused_before_the_reason(self, session, world) -> None:
        """Несуществующий участок отбивается сразу, а не после причины.

        Было: `site:999` принимался, и ошибка всплывала только после
        того, как мастер набрал обоснование, — работа впустую.
        """
        link_master(session, world)
        bot.handle(session, event(payload="request"))
        bot.handle(session, event(payload=f"request:{world['own'].id}"))

        ответ = reply_to(session, event(payload="site:999999"))

        assert "нет в списке" in ответ.text
        assert ответ.buttons, "и снова без выхода"

    def test_requester_learns_the_request_was_approved(self, session, world) -> None:
        """Подавший заявку узнаёт о согласии, а не только об отказе.

        Было: уведомлялся только участок-получатель. Тот, кто просил,
        ждал ответа, а прибор просто исчезал из его комплекта.
        """
        from app import services

        link_master(session, world)
        заявка = services.create_change_request(
            session,
            world["own"].id,
            world["other"].id,
            "Сидоров С.С.",
            reason="нужен на соседнем объекте",
            requested_on=TODAY,
        )
        session.flush()
        services.approve_change_request(
            session, заявка.id, "Главный инженер", decided_on=TODAY
        )
        session.flush()

        свои = [
            n for n in notifications.pending_for_bot(session)
            if n.site_id == world["mine"].id and n.kind == "request_decided"
        ]
        assert свои, "подавший не узнал, что заявку согласовали"

    def test_typed_button_label_is_understood(self, session, world) -> None:
        """Набранная надпись кнопки понимается как нажатие.

        Живая проверка 07.09.2026 в MAX: набранное «Вернуть неисправным»
        получило «Не понял. Выберите действие» — а это дословная надпись
        кнопки, которая была у человека перед глазами.

        Мастер на объекте видит кнопку и перепечатывает её. Он сделал
        ровно то, что видел; догадываться, что надпись и команда — разные
        вещи, должен не он.
        """
        link_master(session, world)

        for надпись, ожидается in (
            ("Мой комплект", "Комплект участка"),
            ("мой комплект", "Комплект участка"),
            ("Заявка на перемещение", "Выберите прибор"),
            ("Вернуть неисправным", "Выберите прибор"),
            ("Сдать на склад", "Выберите прибор"),
        ):
            bot.handle(session, event(payload="cancel"))
            session.flush()
            ответ = reply_to(session, event(надпись))

            assert "Не понял" not in ответ.text, f"{надпись!r} не понято"
            assert ожидается in ответ.text, f"{надпись!r} → {ответ.text[:40]!r}"

    def test_nonsense_still_gets_the_menu(self, session, world) -> None:
        """Совсем непонятное по-прежнему возвращает меню, а не молчание.

        Терпимость к надписям не должна превратиться в угадывание:
        на бессмыслицу человек обязан получить кнопки, чтобы понять,
        что вообще можно делать.
        """
        link_master(session, world)

        ответ = reply_to(session, event("асдфгх"))

        assert "Не понял" in ответ.text
        assert ответ.buttons, "не понял — и не показал, что можно"

    def test_typed_inventory_number_opens_the_instrument(self, session, world) -> None:
        """Набранный инвентарный номер открывает карточку прибора.

        Живая проверка 07.09.2026: мастер набрал «СКИ-021» — номер,
        который видит на наклейке прибора в руках, — и получил
        «Не понял. Выберите действие».

        Прочитать номер и написать его — первое, что делает человек
        с прибором в руках. Требовать вместо этого пройти меню и найти
        прибор в списке из сорока — значит требовать, чтобы он думал
        как программа.
        """
        link_master(session, world)

        ответ = reply_to(session, event("ИНВ-001"))

        assert "Не понял" not in ответ.text
        assert "ИНВ-001" in ответ.text
        assert "поверка" in ответ.text, "не сказано главное — что с поверкой"
        payloads = [b["payload"] for row in ответ.buttons for b in row]
        assert any(p.startswith("request:") for p in payloads)
        assert "cancel" in payloads

    def test_action_from_the_card_skips_the_picker(self, session, world) -> None:
        """Действие с карточки не переспрашивает про прибор.

        Прибор человек уже назвал. Спрашивать снова — значит не слушать.
        """
        link_master(session, world)
        прибор = world["own"]
        bot.handle(session, event("ИНВ-001"))

        ответ = reply_to(session, event(payload=f"request:{прибор.id}"))

        assert "Куда перемещаем" in ответ.text, "переспросил про прибор"
        payloads = [b["payload"] for row in ответ.buttons for b in row]
        assert any(p.startswith("site:") for p in payloads)

    def test_foreign_number_is_still_refused(self, session, world) -> None:
        """Чужой прибор по номеру не открывается.

        Терпимость к набранному номеру не должна открыть чужой участок:
        мастер видит только свой (ответ 35).
        """
        link_master(session, world)

        ответ = reply_to(session, event("ИНВ-999"))

        assert "ИНВ-999" not in ответ.text, "показан прибор чужого участка"
        assert "Не понял" in ответ.text


class TestБотРядомССайтом:
    r"""Сторож на бота, поднятого потоком внутри веб-службы.

    Зачем так вообще
    -----------------

    Правильнее — отдельной службой: опрос MAX держит соединение открытым
    десятками секунд. Так и сделано на сервере конторы.

    Но на бесплатном тарифе Render фоновых служб НЕТ: развёртывание
    08.09.2026 отклонило `type: worker` с «service type is not available
    for this plan». А стенд нужен затем, чтобы заказчик увидел связку
    «нажал в боте — изменилось на сайте»: без бота показывать нечего.

    Что стережём: включается ТОЛЬКО по просьбе и ТОЛЬКО с токеном.
    Случайно поднятый бот на сервере конторы означал бы двух ботов
    на одном токене — они отбирали бы события друг у друга, и половина
    сообщений мастера пропадала бы.
    """

    def test_bot_does_not_start_unless_asked(self, monkeypatch) -> None:
        """Без `SKI_BOT_INLINE` бота нет.

        На сервере конторы бот — отдельная служба. Поднимись он ещё и
        внутри сайта, вышло бы два бота на одном токене.
        """
        from app import main

        monkeypatch.delenv("SKI_BOT_INLINE", raising=False)
        monkeypatch.setenv("MAX_BOT_TOKEN", "проверочный-токен")

        assert main._start_bot_if_asked() is None

    def test_bot_does_not_start_without_a_token(self, monkeypatch, capsys) -> None:
        """С просьбой, но без токена — отказ словами, а не падение.

        Забытая переменная на площадке не должна ронять САЙТ: заказчик
        пришёл смотреть систему, и «бот не настроен» лучше, чем пустой
        экран вместо всего.
        """
        from app import main

        monkeypatch.setenv("SKI_BOT_INLINE", "1")
        monkeypatch.delenv("MAX_BOT_TOKEN", raising=False)

        assert main._start_bot_if_asked() is None
        assert "MAX_BOT_TOKEN" in capsys.readouterr().out

    def test_render_yaml_has_no_worker(self) -> None:
        """В описании развёртывания нет фоновой службы.

        Бесплатный тариф Render их не поддерживает: `type: worker`
        отклоняется целиком, и бот на стенде не появится вовсе.
        """
        from pathlib import Path as _Path

        корень = _Path(__file__).resolve().parent.parent
        файлы = [корень / "render.yaml", корень.parent / "render.yaml"]
        for файл in файлы:
            if not файл.exists():
                continue
            текст = файл.read_text(encoding="utf-8")
            код = "\n".join(
                с for с in текст.splitlines() if not с.lstrip().startswith("#")
            )
            assert "type: worker" not in код, (
                f"{файл.name}: фоновая служба недоступна на бесплатном тарифе"
            )
            assert "SKI_BOT_INLINE" in код, (
                f"{файл.name}: бот не включён — стенд останется без бота"
            )
