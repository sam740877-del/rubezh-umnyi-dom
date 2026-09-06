r"""Сторож уведомлений: предупредили — и это доказуемо.

Какое обещание стережём
------------------------

«О заканчивающейся поверке узнают заранее, и остаётся след, что узнали».
Довод владельца, записанный у донора (`C:\zayavki\core\notify_outbox.py`):
уведомления нужны «как факт, что специалист был уведомлён системой. Прямая
отправка такого факта не оставляет — доказать, что человек был предупреждён,
нечем». Спор «мне не сообщали, что срок вышел» разрешается записью в базе,
а не памятью участников.

Как обещание ломается в жизни
------------------------------

**Ежедневный обход заваливает ящик копиями.** Просроченная поверка остаётся
просроченной и завтра, и через месяц: без защиты от повтора человек за
неделю получит семь одинаковых сообщений и перестанет их читать.

**Доставку путают с прочтением.** Урок донора: сообщение ушло в мессенджер —
не значит, что его открыли. Погасив значок раньше времени, система соврёт.

**Сбой уведомления роняет операцию.** То же правило, что у аудита (Р9.3):
выдача прибора важнее напоминания о ней.

Чем доказано
-------------

Запуском: заводим прибор с просроченной поверкой, гоняем обход, смотрим,
кому что пришло и что происходит при повторе.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app import notifications, security
from app.models import (
    Contract,
    Instrument,
    InstrumentType,
    Notification,
    Site,
    User,
    Verification,
)
from app.services import create_change_request, issue_instrument, return_instrument

TODAY = date(2026, 6, 15)


@pytest.fixture()
def world(session):
    """Приборы с разными сроками поверки, участок и получатели."""
    kind = InstrumentType(name="Нивелир", category="geodesy", verification_interval_months=12)
    session.add(kind)
    session.flush()

    contract = Contract(number="Д-1", title="Стройконтроль", customer="ООО Заказчик")
    session.add(contract)
    session.flush()
    site = Site(name="Участок 1", contract_id=contract.id)
    other = Site(name="Участок 2", contract_id=contract.id)
    session.add_all([site, other])
    session.flush()

    def make(inventory_no: str, valid_until: date | None) -> Instrument:
        item = Instrument(inventory_no=inventory_no, name="Нивелир Н-3", type_id=kind.id)
        session.add(item)
        session.flush()
        if valid_until:
            session.add(
                Verification(
                    instrument_id=item.id,
                    performed_on=valid_until - timedelta(days=365),
                    valid_until=valid_until,
                    result="ok",
                )
            )
            session.flush()
        return item

    expired = make("ИНВ-EXP", TODAY - timedelta(days=10))
    soon = make("ИНВ-SOON", TODAY + timedelta(days=14))
    fine = make("ИНВ-OK", TODAY + timedelta(days=300))

    for login, role in (("admin", "admin"), ("chief", "chief"), ("keeper", "keeper")):
        security.create_user(
            session, login, "test-password-1", role, require_permission=False
        )
    session.commit()

    return {
        "expired": expired,
        "soon": soon,
        "fine": fine,
        "site": site,
        "other": other,
    }


def _texts(session) -> list[str]:
    return [n.text for n in session.query(Notification).all()]


# --------------------------------------------------------------------------
# Поверки
# --------------------------------------------------------------------------


def test_expired_and_expiring_are_noticed(session, world) -> None:
    """Просроченная и заканчивающаяся поверки попадают в ящик, здоровая — нет."""
    created = notifications.scan_verifications(session, today=TODAY)
    session.commit()

    assert created > 0
    texts = " ".join(_texts(session))
    assert "ИНВ-EXP" in texts, "просроченная поверка осталась незамеченной"
    assert "ИНВ-SOON" in texts, "заканчивающаяся поверка осталась незамеченной"
    assert "ИНВ-OK" not in texts, "предупредили о приборе, с которым всё в порядке"


def test_watchers_get_the_warning(session, world) -> None:
    """Напоминание получают руководители — ответ на вопрос 57 («оба»)."""
    notifications.scan_verifications(session, today=TODAY)
    session.commit()

    chief = session.query(User).filter(User.login == "chief").one()
    keeper = session.query(User).filter(User.login == "keeper").one()

    assert notifications.for_user(session, chief.id), "главный инженер не предупреждён"
    assert not notifications.for_user(session, keeper.id), (
        "напоминание о поверке ушло кладовщику, хотя адресаты — руководители"
    )


def test_master_of_the_site_is_notified(session, world) -> None:
    """Мастеру участка тоже сообщают — через запись для бота.

    Учётной записи у мастера нет, его опознаёт бот. Поэтому адресат
    записан участком, и запись ждёт своего доставщика.
    """
    issue_instrument(
        session, world["expired"].id, world["site"].id,
        happened_on=TODAY, ignore_verification=True,
    )
    session.commit()

    notifications.scan_verifications(session, today=TODAY)
    session.commit()

    for_bot = notifications.pending_for_bot(session)
    assert any(n.site_id == world["site"].id for n in for_bot), (
        "мастеру участка ничего не ушло"
    )


def test_second_scan_the_same_day_adds_nothing(session, world) -> None:
    """Повторный обход в тот же день не задваивает.

    Просроченная поверка остаётся просроченной и завтра. Без этой защиты
    человек за неделю получил бы семь одинаковых сообщений и перестал
    их читать — а вместе с ними и все остальные.
    """
    first = notifications.scan_verifications(session, today=TODAY)
    session.commit()
    second = notifications.scan_verifications(session, today=TODAY)
    session.commit()

    assert first > 0
    assert second == 0, f"повторный обход создал ещё {second} уведомлений"


def test_written_off_instruments_are_skipped(session, world) -> None:
    """О списанном приборе не напоминают: поверять его никто не будет."""
    world["expired"].status = "written_off"
    session.commit()

    notifications.scan_verifications(session, today=TODAY)
    session.commit()

    assert "ИНВ-EXP" not in " ".join(_texts(session))


# --------------------------------------------------------------------------
# События заявок и возвратов
# --------------------------------------------------------------------------


def test_new_request_reaches_the_approver(session, world) -> None:
    """О новой заявке узнаёт тот, кто её согласует (ответ 53а)."""
    issue_instrument(
        session, world["fine"].id, world["site"].id, happened_on=TODAY
    )
    request = create_change_request(
        session, world["fine"].id, world["other"].id, "Мастер Сидоров", requested_on=TODAY
    )
    session.commit()

    chief = session.query(User).filter(User.login == "chief").one()
    texts = [n.text for n in notifications.for_user(session, chief.id)]

    assert any(f"Заявка №{request.id}" in t for t in texts), (
        "главный инженер не узнал о новой заявке"
    )


def test_faulty_return_reaches_the_keeper(session, world) -> None:
    """О неисправном возврате узнаёт кладовщик — ему чинить (ответ 53г)."""
    issue_instrument(session, world["fine"].id, world["site"].id, happened_on=TODAY)
    return_instrument(
        session, world["fine"].id, happened_on=TODAY,
        new_status="repair", notes="разбит окуляр",
    )
    session.commit()

    keeper = session.query(User).filter(User.login == "keeper").one()
    texts = [n.text for n in notifications.for_user(session, keeper.id)]

    assert any("разбит окуляр" in t for t in texts), (
        "кладовщик не узнал, что прибор вернули неисправным"
    )


def test_healthy_return_does_not_bother_anyone(session, world) -> None:
    """Исправный возврат — обычная операция, о ней не сообщают (ответ 54)."""
    issue_instrument(session, world["fine"].id, world["site"].id, happened_on=TODAY)
    before = session.query(Notification).count()
    return_instrument(session, world["fine"].id, happened_on=TODAY, new_status="warehouse")
    session.commit()

    assert session.query(Notification).count() == before, (
        "об обычном возврате разослали уведомления"
    )


# --------------------------------------------------------------------------
# Состояния: доставка это не прочтение
# --------------------------------------------------------------------------


def test_delivery_is_not_reading(session, world) -> None:
    """Доставили — не значит прочитали.

    Правило донора: сообщение ушло в мессенджер, но человек его ещё не
    открыл, поэтому значок непрочитанного гасить рано.
    """
    notifications.scan_verifications(session, today=TODAY)
    session.commit()

    chief = session.query(User).filter(User.login == "chief").one()
    entries = notifications.for_user(session, chief.id)
    ids = [e.id for e in entries]

    notifications.mark_delivered(session, ids, "bot")
    session.commit()

    for entry in notifications.for_user(session, chief.id):
        assert entry.is_delivered, "доставка не отметилась"
        assert not entry.is_read, "доставка погасила отметку о прочтении"
    assert notifications.unread_count(session, chief.id) == len(ids)


def test_reading_does_not_touch_other_users(session, world) -> None:
    """Чужие записи не трогаются даже при явном списке.

    Идентификаторы приходят из формы, то есть от пользователя, — здесь
    это не формальность.
    """
    notifications.scan_verifications(session, today=TODAY)
    session.commit()

    chief = session.query(User).filter(User.login == "chief").one()
    admin = session.query(User).filter(User.login == "admin").one()
    foreign_ids = [e.id for e in notifications.for_user(session, admin.id)]
    assert foreign_ids, "у второго получателя нет записей — проверять нечего"

    notifications.mark_read(session, chief.id, foreign_ids)
    session.commit()

    assert notifications.unread_count(session, admin.id) == len(foreign_ids), (
        "прочитаны чужие уведомления"
    )


def test_bot_takes_only_what_it_has_not_taken(session, world) -> None:
    """Бот забирает только недоставленное — иначе слал бы одно и то же."""
    issue_instrument(
        session, world["expired"].id, world["site"].id,
        happened_on=TODAY, ignore_verification=True,
    )
    notifications.scan_verifications(session, today=TODAY)
    session.commit()

    first = notifications.pending_for_bot(session)
    assert first

    notifications.mark_delivered(session, [n.id for n in first], "bot")
    session.commit()

    assert notifications.pending_for_bot(session) == [], (
        "бот снова получил то, что уже доставил"
    )


def test_broken_notification_does_not_break_the_operation(session, world, monkeypatch) -> None:
    """Р9.3: сбой уведомления не роняет бизнес-операцию.

    Выдача прибора важнее напоминания о ней.
    """
    def explode(*args, **kwargs):
        raise RuntimeError("ящик недоступен")

    monkeypatch.setattr(notifications, "Notification", explode)

    movement = return_instrument(
        session,
        world["fine"].id,
        happened_on=TODAY,
        new_status="repair",
        notes="сбит уровень",
    ) if world["fine"].current_site_id else None

    if movement is None:
        issue_instrument(session, world["fine"].id, world["site"].id, happened_on=TODAY)
        movement = return_instrument(
            session, world["fine"].id, happened_on=TODAY,
            new_status="repair", notes="сбит уровень",
        )

    assert movement is not None, "операция упала из-за сбоя уведомления"
    session.refresh(world["fine"])
    assert world["fine"].status == "repair"
