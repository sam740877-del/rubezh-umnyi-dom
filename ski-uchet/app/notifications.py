r"""Ящик уведомлений: событие рождает запись на каждого адресата.

Устройство взято у «Заявок» (`C:\zayavki\core\notify_outbox.py`) вместе
с главной мыслью, записанной там дословно: **ядро не знает о транспорте**.
Сайт показывает непрочитанные при входе, бот в MAX (этап 6) заберёт те же
записи и отметит, чем доставил.

Почему ящик, а не прямая отправка
----------------------------------

Довод владельца, записанный у донора: уведомления нужны «как факт, что
специалист был уведомлён системой. Прямая отправка такого факта не
оставляет — доказать, что человек был предупреждён, нечем». Для поверок
это существенно: спор «мне не сообщали, что срок вышел» разрешается
записью в базе, а не памятью участников.

Три состояния, и они разные
----------------------------

**Создано** — запись есть, адресат назначен.
**Доставлено** — `delivered_via` заполнено: показали на сайте или отправили в MAX.
**Прочитано** — `read_at` заполнено: человек открыл.

Доставка не равна прочтению. Сообщение ушло в мессенджер — не значит, что
его открыли, и гасить отметку раньше времени нельзя.

Сбой уведомления не роняет операцию
------------------------------------

То же правило, что у аудита (Р9.3 свода БПО): заявка важнее письма о ней,
а выдача прибора важнее напоминания о поверке.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from app import audit
from app.models import (
    NOTIFICATION_KINDS,
    Instrument,
    Notification,
    Site,
    User,
)

#: За сколько дней до конца поверки предупреждаем. Пока то же число, что
#: и на экранах (WARN_DAYS в services). Вопрос 16 заказчику ещё открыт:
#: «за сколько дней прибор должен попадать в предупреждение» — когда
#: ответят, порог станет настройкой, а не константой.
WARN_DAYS = 30

#: Роли, которым идут напоминания о поверках. Ответ на вопрос 57: «оба» —
#: главный инженер и руководитель строительного контроля. Роли руководителя
#: СК на сайте пока нет, он появится вместе с ней; список тут именно
#: списком, а не одним полем, ровно поэтому.
VERIFICATION_WATCHERS = ("admin", "chief")


def _create(
    session: Session,
    kind: str,
    text: str,
    *,
    recipient_id: int | None = None,
    site_id: int | None = None,
    object_type: str | None = None,
    object_id: int | None = None,
) -> Notification | None:
    """Создать одну запись. Сбой не роняет вызывающую операцию."""
    if kind not in NOTIFICATION_KINDS:
        raise ValueError(f"Неизвестный вид уведомления: {kind}")
    try:
        entry = Notification(
            kind=kind,
            text=text,
            recipient_id=recipient_id,
            site_id=site_id,
            object_type=object_type,
            object_id=object_id,
        )
        # Точка сохранения, как в аудите: испорченное уведомление
        # откатывает только себя, а не операцию, которая его породила.
        with session.begin_nested():
            session.add(entry)
        return entry
    except Exception:
        return None


def notify_users(
    session: Session,
    roles: tuple[str, ...],
    kind: str,
    text: str,
    *,
    object_type: str | None = None,
    object_id: int | None = None,
) -> int:
    """Уведомить всех действующих пользователей указанных ролей.

    Возвращает число созданных записей.
    """
    recipients = session.scalars(
        select(User).where(User.role.in_(roles), User.is_active.is_(True))
    ).all()

    created = 0
    for user in recipients:
        if _create(
            session,
            kind,
            text,
            recipient_id=user.id,
            object_type=object_type,
            object_id=object_id,
        ):
            created += 1
    return created


def notify_site(
    session: Session,
    site_id: int,
    kind: str,
    text: str,
    *,
    object_type: str | None = None,
    object_id: int | None = None,
) -> Notification | None:
    """Уведомить мастера участка.

    У мастера учётной записи на сайте нет — его опознаёт бот. Поэтому
    адресат записан участком: бот заберёт запись, когда дойдёт этап 6.
    """
    return _create(
        session,
        kind,
        text,
        site_id=site_id,
        object_type=object_type,
        object_id=object_id,
    )


# --------------------------------------------------------------------------
# Поверки: кого и о чём предупреждаем
# --------------------------------------------------------------------------


def _verification_text(instrument: Instrument, valid_until: date, today: date) -> tuple[str, str]:
    """Вид уведомления и готовый текст.

    Текст готовится здесь, при создании записи. Доставщик его не сочиняет
    и не дополняет — правило донора: одно событие выглядит одинаково
    и на сайте, и в мессенджере.
    """
    days = (valid_until - today).days
    where = f", участок «{instrument.current_site.name}»" if instrument.current_site else ""

    if days < 0:
        return (
            "verification_expired",
            f"Поверка просрочена на {-days} дн.: {instrument.inventory_no} "
            f"{instrument.name}{where}. Выдавать прибор нельзя.",
        )
    return (
        "verification_due",
        f"Поверка заканчивается через {days} дн. ({valid_until:%d.%m.%Y}): "
        f"{instrument.inventory_no} {instrument.name}{where}.",
    )


def scan_verifications(
    session: Session, today: date | None = None, warn_days: int = WARN_DAYS
) -> int:
    """Пройти по парку и создать уведомления о поверках.

    Запускается расписанием (или вручную кнопкой). Повторный запуск в тот
    же день ничего не задваивает: перед созданием смотрим, не было ли уже
    такого уведомления об этом приборе сегодня.

    Возвращает число созданных записей.
    """
    from app.services import verification_state

    today = today or date.today()
    created = 0

    instruments = session.scalars(
        select(Instrument).where(Instrument.status != "written_off")
    ).all()

    for instrument in instruments:
        state = verification_state(instrument, today, warn_days)
        if state.code not in ("expired", "expiring"):
            continue

        valid_until = instrument.verification_valid_until
        if valid_until is None:
            continue

        kind, text = _verification_text(instrument, valid_until, today)

        if _already_sent_today(session, kind, instrument.id, today):
            continue

        # Руководителям — по их учётным записям.
        created += notify_users(
            session,
            VERIFICATION_WATCHERS,
            kind,
            text,
            object_type="instrument",
            object_id=instrument.id,
        )
        # Мастеру участка, если прибор на участке. Ответ на вопрос 57:
        # напоминания получают и мастер, и руководители.
        if instrument.current_site_id:
            if notify_site(
                session,
                instrument.current_site_id,
                kind,
                text,
                object_type="instrument",
                object_id=instrument.id,
            ):
                created += 1

    if created:
        audit.write_system(
            session,
            "Разосланы напоминания о поверках",
            details=f"создано уведомлений: {created}",
        )
    return created


def _already_sent_today(session: Session, kind: str, instrument_id: int, today: date) -> bool:
    """Было ли уже такое уведомление об этом приборе за сутки.

    Иначе ежедневный обход завалил бы ящик копиями одного и того же:
    просроченная поверка остаётся просроченной и завтра, и через месяц.
    Человек за неделю получил бы семь одинаковых сообщений и перестал
    читать их вместе со всеми остальными.

    Смотрим на СУТКИ ДО МОМЕНТА ОБХОДА, а не на календарный день записи.
    Так проверка не зависит от того, совпадает ли расчётная дата обхода
    с датой на часах сервера: при переносе обхода на вчерашнее число
    (пересчёт, отладка) сравнение по календарному дню не находило
    вчерашних записей и слало всё заново.
    """
    since = datetime.now() - timedelta(days=1)
    found = session.scalar(
        select(func.count(Notification.id)).where(
            Notification.kind == kind,
            Notification.object_type == "instrument",
            Notification.object_id == instrument_id,
            Notification.created_at >= since,
        )
    )
    return bool(found)


# --------------------------------------------------------------------------
# Чтение ящика
# --------------------------------------------------------------------------


def for_user(
    session: Session, user_id: int, *, only_unread: bool = False, limit: int = 100
) -> list[Notification]:
    """Уведомления пользователя, новые первыми."""
    query = select(Notification).where(Notification.recipient_id == user_id)
    if only_unread:
        query = query.where(Notification.read_at.is_(None))
    return list(
        session.scalars(
            query.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit)
        )
    )


def unread_count(session: Session, user_id: int) -> int:
    """Сколько непрочитанных — для значка в шапке."""
    return int(
        session.scalar(
            select(func.count(Notification.id)).where(
                Notification.recipient_id == user_id,
                Notification.read_at.is_(None),
            )
        )
        or 0
    )


def pending_for_bot(session: Session, limit: int = 100) -> list[Notification]:
    """Что бот ещё не забирал.

    Готовим заранее: на этапе 6 бот берёт отсюда записи, адресованные
    участкам, и отмечает доставку. Ядро при этом о боте ничего не знает —
    он сам приходит за своими записями.
    """
    return list(
        session.scalars(
            select(Notification)
            .where(
                Notification.site_id.is_not(None),
                Notification.delivered_via.is_(None),
            )
            .order_by(Notification.created_at)
            .limit(limit)
        )
    )


# --------------------------------------------------------------------------
# Отметки
# --------------------------------------------------------------------------


def mark_delivered(session: Session, ids: list[int], via: str) -> int:
    """Отметить, каким транспортом уведомления ушли.

    Доставка — не прочтение: человек ещё не открыл сообщение, поэтому
    `read_at` не трогаем, иначе значок непрочитанного погас бы раньше
    времени (правило донора, записанное после их разбора).
    """
    if not ids:
        return 0
    result = session.execute(
        update(Notification)
        .where(Notification.id.in_(ids), Notification.delivered_via.is_(None))
        .values(delivered_via=via, delivered_at=datetime.now())
    )
    return int(result.rowcount or 0)


def mark_read(session: Session, user_id: int, ids: list[int] | None = None) -> int:
    """Пометить прочитанными. Без списка — все свои разом.

    Чужие записи не трогаются даже при явном списке идентификаторов:
    правило донора, и оно здесь не формальность — идентификаторы приходят
    из формы, то есть от пользователя.
    """
    query = update(Notification).where(
        Notification.recipient_id == user_id,
        Notification.read_at.is_(None),
    )
    if ids is not None:
        if not ids:
            return 0
        query = query.where(Notification.id.in_(ids))

    result = session.execute(query.values(read_at=datetime.now()))
    return int(result.rowcount or 0)
