r"""Логика бота: что ответить и что записать в базу.

Транспорта здесь нет — он в `app/max_api.py`. Разделение нарочное: логику
надо проверять без сети и без токена, иначе каждый прогон тестов требовал
бы живого бота в мессенджере.

Кто работает в боте
--------------------

**Инженеры строительного контроля, они же мастера участков** — их
единственный канал работы с системой, учётных записей на сайте у них нет
(ответы 49, 52). Опознаются по коду-приглашению от администратора.

**Офисные роли** тоже могут привязаться: им бот доставляет уведомления,
а главному инженеру — ещё и кнопки «согласовать / отклонить» прямо
в сообщении о заявке. Это заметно быстрее, чем идти на сайт.

Решения, принятые при разработке
---------------------------------

Пять вопросов, на которые ответа заказчика не было. Решено по здравому
смыслу; если разойдётся с тем, как оно устроено в жизни, правится здесь.

**1. Что мастер видит первым.** Не голое меню, а сводку: сколько приборов
на участке и что с поверками. Меню — под ней. Человек открывает бота,
чтобы узнать положение дел, а не чтобы полюбоваться кнопками.

**2. Мастеров на участке может быть несколько.** Привязка не одна на
участок, а на человека: двое мастеров — две записи с одним `site_id`.
Уведомление об участке уходит обоим — кто первым увидел, тот и ответил.

**3. Перевод на другой участок** — перевыпуском кода. Старая привязка
отключается, новая заводится. Пока привязка жива, человек видит чужой
участок, поэтому отключение — обязанность администратора, и в интерфейсе
это названо прямо.

**4. Заявку можно отозвать,** пока она не решена. Мастер передумал —
это нормально, и заставлять главного инженера отклонять ненужную заявку
глупо. Отзыв пишется в журнал: заявка не исчезает, а получает исход.

**5. Копившиеся уведомления не вываливаются пачкой.** При возвращении
показываем сводку («вас ждёт 7 сообщений») и последние три. Остальные —
по кнопке. Стена из тридцати сообщений не читается вовсе.
"""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit, notifications, services
from app.max_api import Update, button
from app.models import (
    BotInvite,
    BotLink,
    ChangeRequest,
    Instrument,
    Notification,
    Site,
)

#: Сколько живёт код-приглашение. Приглашение без срока рано или поздно
#: попадает не туда, а отозвать его будет нечем.
INVITE_TTL_DAYS = 7

#: Сколько кнопок с приборами на одной странице. Сорок кнопок — стена,
#: которую не читают; восемь помещаются на экран телефона целиком.
PAGE_SIZE = 8

#: Сколько последних уведомлений показываем вернувшемуся. Остальные —
#: по кнопке: пачка из тридцати сообщений не читается вовсе.
RECENT_NOTIFICATIONS = 3


@dataclass
class Reply:
    """Что бот отвечает: текст и кнопки под ним."""

    text: str
    buttons: list[list[dict[str, str]]] = field(default_factory=list)
    #: Уведомления, которые считаются доставленными, КОГДА этот ответ уйдёт.
    #:
    #: Гасить их сразу при сборке ответа нельзя: упади отправка (связь на
    #: объекте плохая), и человек уже никогда их не увидит — а среди них
    #: напоминание о заканчивающейся поверке. Приёмка 07.09.2026.
    deliver_ids: list[int] = field(default_factory=list)


class BotError(Exception):
    """Ошибка с текстом, который можно показать человеку в чате."""


# --------------------------------------------------------------------------
# Приглашения и привязка
# --------------------------------------------------------------------------


def create_invite(
    session: Session,
    *,
    site_id: int | None = None,
    user_id: int | None = None,
    intended_for: str = "",
    created_by: str | None = None,
) -> BotInvite:
    """Выпустить код-приглашение.

    Код случайный и короткий: его диктуют голосом или пишут в переписке,
    поэтому длинный набор знаков только мешал бы. Одноразовость и срок
    защищают лучше, чем длина.
    """
    if not site_id and not user_id:
        raise BotError("Укажите, куда впускаем: участок или учётную запись.")

    code = secrets.token_hex(3).upper()  # шесть знаков, например «A3F91C»
    invite = BotInvite(
        code=code,
        site_id=site_id,
        user_id=user_id,
        intended_for=intended_for.strip(),
        created_by=created_by,
        expires_at=datetime.now() + timedelta(days=INVITE_TTL_DAYS),
    )
    session.add(invite)
    session.flush()

    audit.write(
        session,
        "Выпущен код-приглашение в бот",
        actor=created_by,
        object_type="site" if site_id else "user",
        object_id=site_id or user_id,
        details=f"код {code} для «{intended_for or 'без имени'}»",
    )
    return invite


def redeem_invite(
    session: Session, code: str, max_user_id: str, chat_id: str, display_name: str
) -> BotLink:
    """Впустить человека по коду.

    Код одноразовый: использованный больше не сработает, даже если его
    перешлют дальше.
    """
    cleaned = (code or "").strip().upper()
    invite = session.scalar(select(BotInvite).where(BotInvite.code == cleaned))

    if invite is None:
        raise BotError("Код не найден. Проверьте, правильно ли он набран.")
    if invite.is_used:
        raise BotError("Этот код уже использован. Попросите администратора выдать новый.")
    if invite.expires_at and invite.expires_at < datetime.now():
        raise BotError("Срок кода истёк. Попросите администратора выдать новый.")

    existing = session.scalar(
        select(BotLink).where(BotLink.max_user_id == max_user_id, BotLink.is_active.is_(True))
    )
    if existing is not None:
        # Перевод на другой участок: старая привязка отключается, а не
        # удаляется — история заявок ссылается на того, кто их подавал.
        existing.is_active = False

    link = BotLink(
        max_user_id=max_user_id,
        max_chat_id=chat_id,
        display_name=display_name or invite.intended_for or "Мастер",
        site_id=invite.site_id,
        user_id=invite.user_id,
        last_seen_at=datetime.now(),
    )
    session.add(link)

    invite.used_at = datetime.now()
    invite.used_by_max_id = max_user_id
    session.flush()

    audit.write(
        session,
        "Вход в бот по коду-приглашению",
        actor=link.display_name,
        object_type="site" if link.site_id else "user",
        object_id=link.site_id or link.user_id,
        details=f"код {cleaned}",
    )
    return link


def find_link(session: Session, max_user_id: str) -> BotLink | None:
    """Действующая привязка человека, если она есть."""
    return session.scalar(
        select(BotLink).where(
            BotLink.max_user_id == str(max_user_id), BotLink.is_active.is_(True)
        )
    )


def revoke_link(session: Session, link_id: int, *, actor: str | None = None) -> BotLink:
    """Отключить привязку: человек уволился или переведён.

    Не удаляем: заявки и записи в журнале ссылаются на того, кто их подал.
    """
    link = session.get(BotLink, link_id)
    if link is None:
        raise BotError("Привязка не найдена.")
    link.is_active = False
    session.flush()
    audit.write(
        session,
        "Отключён доступ в бот",
        actor=actor,
        object_type="site" if link.site_id else "user",
        object_id=link.site_id or link.user_id,
        details=link.display_name,
    )
    return link


# --------------------------------------------------------------------------
# Черновик разговора
# --------------------------------------------------------------------------


def _draft(link: BotLink) -> dict:
    """Что человек уже успел выбрать."""
    if not link.draft:
        return {}
    try:
        return json.loads(link.draft)
    except ValueError:
        return {}


def _set_draft(link: BotLink, data: dict | None) -> None:
    """Запомнить или стереть черновик."""
    link.draft = json.dumps(data, ensure_ascii=False) if data else None


#: Слова, которыми человек просит выйти. Приравнены к кнопке «Отмена».
#:
#: Заведены по приёмке 07.09.2026. Выйти из разговора можно было только
#: кнопкой или тайными «меню» / «/start», которых мастеру никто не
#: называл. Написанное «отмена» уходило в дело: на шаге неисправности
#: становилось описанием поломки, и прибор уезжал в ремонт.
#:
#: Считаются ТОЛЬКО набранными от руки: у кнопки своя команда в payload,
#: и совпадение текста кнопки со словом выхода ничего не значит.
CANCEL_WORDS = frozenset(
    {
        "отмена", "отменить", "отмени", "назад", "стоп", "хватит",
        "выйти", "выход", "не надо", "нет", "^",
    }
)


def _reset(link: BotLink) -> None:
    """Вернуть человека в главное меню."""
    link.state = "idle"
    link.draft = None


# --------------------------------------------------------------------------
# Экраны
# --------------------------------------------------------------------------


#: Надписи кнопок → команда. Человек, который ПЕРЕПЕЧАТАЛ то, что
#: написано на кнопке, должен быть понят: он сделал ровно то, что видел.
#:
#: Живая проверка 07.09.2026: набранное «Вернуть неисправным» получало
#: «Не понял. Выберите действие» — а это дословная надпись кнопки,
#: которая была у человека перед глазами. Понимать её должен бот, а не
#: человек догадываться, что надпись и команда — разные вещи.
MENU_WORDS = {
    "мой комплект": "kit",
    "комплект": "kit",
    "приборы": "kit",
    "заявка на перемещение": "request",
    "заявка": "request",
    "перемещение": "request",
    "сдать на склад": "return_ok",
    "сдать": "return_ok",
    "склад": "return_ok",
    "вернуть неисправным": "return_broken",
    "неисправность": "return_broken",
    "сломался": "return_broken",
    "сообщения": "inbox",
    "сообщение": "inbox",
}


def main_menu(link: BotLink) -> list[list[dict[str, str]]]:
    """Кнопки главного меню — разные по роли.

    Мастеру нужны действия, офисной роли — уведомления: она работает
    на сайте, а бот ей нужен как канал сообщений.
    """
    if link.site_id:
        return [
            [button("Мой комплект", "kit")],
            [button("Заявка на перемещение", "request")],
            [button("Сдать на склад", "return_ok")],
            [button("Вернуть неисправным", "return_broken")],
            [button("Сообщения", "inbox")],
        ]
    return [[button("Сообщения", "inbox")]]


def greeting(session: Session, link: BotLink, today: date | None = None) -> Reply:
    """Что человек видит, открыв бота.

    Не голое меню, а положение дел: сколько приборов на участке и что
    с поверками. Человек открывает бота, чтобы узнать, как дела, — меню
    он и так найдёт.
    """
    today = today or date.today()
    name = link.display_name or "Здравствуйте"

    if not link.site_id:
        pending = _unread_count(session, link)
        text = f"{name}, вы подключены к системе учёта СКИ."
        if pending:
            text += f"\nВас ждёт сообщений: {pending}."
        return Reply(text, main_menu(link))

    instruments = _site_instruments(session, link.site_id)
    problems = [
        item for item in instruments
        if services.verification_state(item, today).code in ("expired", "expiring", "missing")
    ]

    lines = [f"{name}, участок «{link.site.name}»."]
    lines.append(f"Приборов на участке: {len(instruments)}.")
    if problems:
        lines.append(f"Требуют внимания по поверке: {len(problems)}.")
    else:
        lines.append("По поверкам всё в порядке.")

    pending = _unread_count(session, link)
    if pending:
        lines.append(f"Непрочитанных сообщений: {pending}.")

    return Reply("\n".join(lines), main_menu(link))


def _site_instruments(session: Session, site_id: int) -> list[Instrument]:
    """Приборы, числящиеся за участком."""
    return list(
        session.scalars(
            select(Instrument)
            .where(Instrument.current_site_id == site_id)
            .order_by(Instrument.inventory_no)
        )
    )


def kit_screen(session: Session, link: BotLink, today: date | None = None) -> Reply:
    """Комплект участка со сроками поверок."""
    if not link.site_id:
        return Reply("Комплект показывается мастеру участка.", main_menu(link))

    today = today or date.today()
    instruments = _site_instruments(session, link.site_id)
    if not instruments:
        return Reply(
            f"На участке «{link.site.name}» приборов не числится.", main_menu(link)
        )

    lines = [f"Комплект участка «{link.site.name}»:", ""]
    for item in instruments:
        lines.append(
            f"{item.inventory_no} — {item.name}\n   "
            f"{_verification_line(item, today)}"
        )

    return Reply("\n".join(lines), main_menu(link))


def _verification_line(instrument: Instrument, today: date) -> str:
    """Что с поверкой, одной строкой для человека.

    Одним местом: то же нужно и в комплекте, и на карточке прибора,
    найденного по набранному номеру. Разъехавшиеся формулировки —
    начало того, что в одном экране прибор «в порядке», а в другом
    «истекает».
    """
    state = services.verification_state(instrument, today)
    return {
        "expired": "поверка просрочена",
        "expiring": f"поверка истекает через {state.days_left} дн.",
        "missing": "нет данных о поверке",
    }.get(state.code, "поверка в порядке")


def instrument_buttons(
    instruments: list[Instrument], action: str, page: int = 0
) -> list[list[dict[str, str]]]:
    """Кнопки выбора прибора, разбитые по страницам.

    Сорок кнопок — стена, которую не читают. Восемь помещаются на экран
    телефона целиком, остальные — по кнопке «дальше». Для тех, кому
    удобнее набрать, работает поиск по инвентарному номеру текстом.
    """
    start = page * PAGE_SIZE
    chunk = instruments[start : start + PAGE_SIZE]

    rows = [
        [button(f"{item.inventory_no} · {item.name[:22]}", f"{action}:{item.id}")]
        for item in chunk
    ]

    nav = []
    if page > 0:
        nav.append(button("← назад", f"{action}_page:{page - 1}"))
    if start + PAGE_SIZE < len(instruments):
        nav.append(button("дальше →", f"{action}_page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([button("Отмена", "cancel")])
    return rows


def _target_sites(session: Session, link: BotLink) -> list[Site]:
    """Куда мастер может попросить перевести прибор.

    Свой участок не предлагаем: перемещать на себя же незачем.
    """
    return list(
        session.scalars(
            select(Site)
            .where(Site.status == "active", Site.id != link.site_id)
            .order_by(Site.name)
        ).all()
    )


def site_buttons(sites: list[Site], page: int = 0) -> list[list[dict[str, str]]]:
    """Кнопки выбора участка — с листанием, как у приборов.

    Раньше список обрезался восемью без «дальше» и без предупреждения:
    у конторы с четырнадцатью участками девятый и следующие были
    недостижимы, и мастер не понимал, почему нужного участка нет
    (приёмка 07.09.2026).
    """
    start = page * PAGE_SIZE
    chunk = sites[start : start + PAGE_SIZE]

    rows = [[button(site.name[:30], f"site:{site.id}")] for site in chunk]

    nav = []
    if page > 0:
        nav.append(button("← назад", f"site_page:{page - 1}"))
    if start + PAGE_SIZE < len(sites):
        nav.append(button("дальше →", f"site_page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([button("Отмена", "cancel")])
    return rows


def _unread_count(session: Session, link: BotLink) -> int:
    """Сколько сообщений ждёт человека."""
    query = select(func.count(Notification.id)).where(Notification.delivered_via.is_(None))
    if link.site_id:
        query = query.where(Notification.site_id == link.site_id)
    elif link.user_id:
        query = query.where(Notification.recipient_id == link.user_id)
    else:
        return 0
    return int(session.scalar(query) or 0)


def inbox_screen(session: Session, link: BotLink, show_all: bool = False) -> Reply:
    """Сообщения, которые ждали человека.

    Показываем последние три, а не всю пачку: вернувшийся после отпуска
    получил бы стену из тридцати сообщений и не прочитал бы ни одного.
    """
    query = select(Notification).where(Notification.delivered_via.is_(None))
    if link.site_id:
        query = query.where(Notification.site_id == link.site_id)
    elif link.user_id:
        query = query.where(Notification.recipient_id == link.user_id)
    else:
        return Reply("Сообщений для вас нет.", main_menu(link))

    pending = list(
        session.scalars(query.order_by(Notification.created_at.desc()).limit(50))
    )
    if not pending:
        return Reply("Новых сообщений нет.", main_menu(link))

    shown = pending if show_all else pending[:RECENT_NOTIFICATIONS]
    lines = []
    for item in shown:
        lines.append(f"{item.created_at:%d.%m %H:%M} — {item.text}")

    buttons = []
    if not show_all and len(pending) > len(shown):
        lines.append("")
        lines.append(f"Ещё сообщений: {len(pending) - len(shown)}.")
        buttons.append([button("Показать все", "inbox_all")])
    buttons.extend(main_menu(link))

    # Гасим не здесь, а КОГДА ответ уйдёт: список едет вместе с ответом.
    # Отмечаем только показанное — непоказанное человек ещё не видел.
    return Reply("\n".join(lines), buttons, [item.id for item in shown])


# --------------------------------------------------------------------------
# Обработка события
# --------------------------------------------------------------------------


def handle(session: Session, update: Update, today: date | None = None) -> Reply | None:
    """Ответ бота на одно событие. `None` — отвечать нечего.

    Единственная дверь: и текст, и нажатая кнопка приходят сюда. Кнопка
    в MAX возвращает `payload` — это та же команда, только человек её
    не набирал.

    Границу транзакции держит вызывающий.
    """
    today = today or date.today()
    command = (update.payload or update.text or "").strip()
    link = find_link(session, update.max_user_id)

    if link is None:
        return _handle_stranger(session, update, command)

    link.last_seen_at = datetime.now()
    if update.chat_id:
        link.max_chat_id = update.chat_id

    # Код от уже привязанного человека — это перевод на другой участок.
    # Проверяем до остальных команд: иначе код уходил бы в «не понял»,
    # и перевести мастера было бы нечем.
    if link.state == "idle" and _looks_like_code(command):
        try:
            moved = redeem_invite(
                session, command, update.max_user_id, update.chat_id, update.display_name
            )
        except BotError as exc:
            return Reply(str(exc))
        return greeting(session, moved, today)

    if command.lower() in ("/start", "начать", "меню", "/menu"):
        _reset(link)
        return greeting(session, link, today)
    if command == "cancel" or (not update.is_button and command.lower() in CANCEL_WORDS):
        # Слово «отмена» РАВНО кнопке «Отмена». Приёмка 07.09.2026: мастер
        # на шаге неисправности написал «отмена», слово ушло в описание,
        # и тахеометр уехал в ремонт с неисправностью «отмена». Человек
        # на объекте пишет то, что просит выйти, а не ищет кнопку выше
        # в ленте — и он прав, а не бот.
        _reset(link)
        return Reply("Отменено.", main_menu(link))

    # Человек в середине разговора — продолжаем его, а не начинаем заново.
    if link.state != "idle":
        return _continue_dialog(session, link, command, update, today)

    return _handle_command(session, link, command, today)



def _looks_like_code(text: str) -> bool:
    """Похоже ли это на код-приглашение.

    Код выдаётся `secrets.token_hex(3)` — шесть знаков из 0-9 и A-F.
    Узкая проверка нарочно: широкая («шесть букв или цифр») принимала
    за код обычные слова.
    """
    cleaned = (text or "").strip().upper()
    return len(cleaned) == 6 and all(c in "0123456789ABCDEF" for c in cleaned)


def _handle_stranger(session: Session, update: Update, command: str) -> Reply:
    """Человек, которого система не знает.

    Единственное, что ему доступно, — назвать код-приглашение. Никаких
    сведений об учёте посторонний не получает: бот не подсказывает даже,
    существует ли такая система.
    """
    cleaned = command.strip().upper()
    # Код — шесть знаков ИЗ ШЕСТНАДЦАТЕРИЧНЫХ (0-9, A-F): его выдаёт
    # `secrets.token_hex(3)`. Проверять надо именно это, а не «шесть
    # букв или цифр»: обычное слово «привет» тоже шестибуквенное, и
    # человек получал бы «код не найден» вместо приветствия.
    if _looks_like_code(cleaned):
        try:
            link = redeem_invite(
                session,
                cleaned,
                update.max_user_id,
                update.chat_id,
                update.display_name,
            )
        except BotError as exc:
            return Reply(str(exc))
        return greeting(session, link)

    return Reply(
        "Здравствуйте. Чтобы начать работу, отправьте код-приглашение — "
        "его выдаёт администратор системы учёта."
    )


def _handle_command(
    session: Session, link: BotLink, command: str, today: date
) -> Reply | None:
    """Команда из главного меню."""
    # Набранное словами приводим к команде: человек перепечатал надпись
    # кнопки, и это не повод отвечать «не понял».
    command = MENU_WORDS.get(command.strip().lower(), command)

    if command == "kit":
        return kit_screen(session, link, today)
    if command == "inbox":
        return inbox_screen(session, link)
    if command == "inbox_all":
        return inbox_screen(session, link, show_all=True)

    if command in ("request", "return_ok", "return_broken"):
        return _start_instrument_pick(session, link, command)

    # Действие с УЖЕ выбранным прибором: карточка, которую человек
    # получил, набрав инвентарный номер. Шаг выбора пропускаем — прибор
    # он уже назвал, спрашивать снова значит не слушать.
    if command.startswith(("request:", "return_ok:", "return_broken:")):
        action, _, _ = command.partition(":")
        instrument = _find_instrument(session, link, command, action)
        if instrument is None:
            return Reply("Такого прибора на вашем участке нет.", main_menu(link))
        link.state = {
            "request": "request_pick_instrument",
            "return_ok": "return_pick_instrument",
            "return_broken": "return_pick_instrument",
        }[action]
        _set_draft(link, {"action": action})
        return _picked_instrument(session, link, command, {"action": action}, today)

    if command.startswith(("request_page:", "return_ok_page:", "return_broken_page:")):
        action, _, page = command.partition("_page:")
        instruments = _site_instruments(session, link.site_id) if link.site_id else []
        return Reply(
            "Выберите прибор:", instrument_buttons(instruments, action, int(page or 0))
        )

    # Набран инвентарный номер прибора — показываем, что с ним можно.
    # Мастер видит номер на наклейке и пишет его: это первое, что делает
    # человек с прибором в руках. Отвечать ему «не понял» — значит
    # требовать, чтобы он думал как программа (живая проверка 07.09.2026).
    if link.site_id:
        instrument = _find_instrument(session, link, command)
        if instrument is not None:
            return Reply(
                f"{instrument.inventory_no} — {instrument.name}.\n"
                f"{_verification_line(instrument, today)}\n\n"
                "Что делаем?",
                [
                    [button("Заявка на перемещение", f"request:{instrument.id}")],
                    [button("Сдать на склад", f"return_ok:{instrument.id}")],
                    [button("Вернуть неисправным", f"return_broken:{instrument.id}")],
                    [button("Отмена", "cancel")],
                ],
            )

    return Reply("Не понял. Выберите действие:", main_menu(link))


def _start_instrument_pick(session: Session, link: BotLink, action: str) -> Reply:
    """Начать действие, для которого нужен прибор."""
    if not link.site_id:
        return Reply("Это действие доступно мастеру участка.", main_menu(link))

    instruments = _site_instruments(session, link.site_id)
    if not instruments:
        return Reply("За вашим участком приборов не числится.", main_menu(link))

    link.state = {
        "request": "request_pick_instrument",
        "return_ok": "return_pick_instrument",
        "return_broken": "return_pick_instrument",
    }[action]
    _set_draft(link, {"action": action})

    return Reply(
        "Выберите прибор (или наберите инвентарный номер):",
        instrument_buttons(instruments, action),
    )


def _continue_dialog(
    session: Session, link: BotLink, command: str, update: Update, today: date
) -> Reply | None:
    """Человек в середине разговора: продолжаем с того места, где встали."""
    draft = _draft(link)

    if link.state in ("request_pick_instrument", "return_pick_instrument"):
        return _picked_instrument(session, link, command, draft, today)
    if link.state == "request_pick_site":
        return _picked_site(session, link, command, draft, today)
    # Двум последним шагам нужно знать, НАЖАЛИ кнопку или НАБРАЛИ текст:
    # они принимают свободный ввод, и нажатие старой кнопки из ленты
    # уходило в дело — причиной заявки или описанием неисправности.
    if link.state == "request_reason":
        return _entered_reason(session, link, command, update, draft, today)
    if link.state == "return_defect":
        return _entered_defect(session, link, command, update, draft, today)

    _reset(link)
    return Reply("Начнём сначала.", main_menu(link))


def _find_instrument(
    session: Session, link: BotLink, command: str, action: str | None = None
) -> Instrument | None:
    """Прибор по нажатой кнопке или по набранному инвентарному номеру.

    Номером искать разрешено нарочно: кнопки удобны, пока приборов
    немного, а мастеру с сорока позициями быстрее набрать номер, который
    он и так видит на наклейке.

    `action` — то действие, которого бот сейчас ждёт. Кнопка ЧУЖОГО
    действия к делу не принимается: в мессенджере старые кнопки остаются
    на экране и нажимаются. Приёмка 07.09.2026: в шаге «сдать на склад»
    нажатие старой `site:1` из прежней заявки сдавало на склад прибор
    с номером 1 — совсем не тот, что человек имел в виду.
    """
    if ":" in command:
        prefix, _, raw_id = command.partition(":")
        if action is not None and prefix != action:
            return None
        if raw_id.isdigit():
            found = session.get(Instrument, int(raw_id))
            # Чужой прибор не отдаём даже по прямому обращению: мастер
            # видит только свой участок (ответ 35), и подстановка чужого
            # номера в кнопку не должна это обходить.
            if found is not None and found.current_site_id == link.site_id:
                return found
        return None

    typed = command.strip().upper()
    if not typed:
        return None
    return session.scalar(
        select(Instrument).where(
            func.upper(Instrument.inventory_no) == typed,
            Instrument.current_site_id == link.site_id,
        )
    )


def _picked_instrument(
    session: Session, link: BotLink, command: str, draft: dict, today: date
) -> Reply:
    """Прибор выбран — спрашиваем следующее."""
    action = draft.get("action", "")

    if command.startswith(("request_page:", "return_ok_page:", "return_broken_page:")):
        prefix, _, page = command.partition("_page:")
        instruments = _site_instruments(session, link.site_id)
        return Reply(
            "Выберите прибор:", instrument_buttons(instruments, prefix, int(page or 0))
        )

    instrument = _find_instrument(session, link, command, action)
    if instrument is None:
        instruments = _site_instruments(session, link.site_id)
        return Reply(
            "Такого прибора на вашем участке нет. Выберите из списка "
            "или наберите инвентарный номер:",
            instrument_buttons(instruments, action or "request"),
        )

    draft["instrument_id"] = instrument.id
    draft["instrument_no"] = instrument.inventory_no

    if action == "request":
        link.state = "request_pick_site"
        _set_draft(link, draft)
        sites = _target_sites(session, link)
        if not sites:
            _reset(link)
            return Reply("Других действующих участков нет.", main_menu(link))
        return Reply(
            f"{instrument.inventory_no} {instrument.name}.\nКуда перемещаем?",
            site_buttons(sites),
        )

    if action == "return_broken":
        link.state = "return_defect"
        _set_draft(link, draft)
        return Reply(
            f"{instrument.inventory_no} {instrument.name}.\n"
            "Опишите, что с прибором не так — без этого кладовщик не поймёт, "
            "что чинить.",
            # Кнопка обязательна: шаг просит свободный текст, и без неё
            # выйти было нечем. Приёмка 07.09.2026: мастер писал «отмена»,
            # слово становилось описанием поломки, прибор уезжал в ремонт.
            [[button("Отмена", "cancel")]],
        )

    # Сдача исправного: объяснений не требуем, это обычная операция (ответ 54).
    return _do_return(session, link, instrument, today, status="warehouse")


def _picked_site(
    session: Session, link: BotLink, command: str, draft: dict, today: date
) -> Reply:
    """Участок назначения выбран — спрашиваем причину."""
    sites = _target_sites(session, link)

    # Листание списка участков.
    if command.startswith("site_page:"):
        _, _, raw_page = command.partition(":")
        страница = int(raw_page) if raw_page.isdigit() else 0
        return Reply("Куда перемещаем?", site_buttons(sites, страница))

    if not command.startswith("site:") or not command.partition(":")[2].isdigit():
        # Кнопки ПОВТОРЯЕМ, а не отсылаем человека искать их выше в ленте.
        # Приёмка 07.09.2026: ответ «Выберите участок кнопкой» приходил
        # без единой кнопки — на телефоне это тупик.
        return Reply(
            "Выберите участок кнопкой из списка ниже.", site_buttons(sites)
        )

    _, _, raw_id = command.partition(":")
    # Участок должен быть из ТОГО ЖЕ списка, что показан. Иначе поддельная
    # команда `site:999` принималась, и ошибка всплывала только после
    # того, как мастер набрал причину, — работа впустую.
    if int(raw_id) not in {site.id for site in sites}:
        return Reply(
            "Такого участка нет в списке. Выберите кнопкой:", site_buttons(sites)
        )

    draft["to_site_id"] = int(raw_id)
    link.state = "request_reason"
    _set_draft(link, draft)
    return Reply(
        "Зачем перемещаем? Напишите коротко — это увидит главный инженер.\n"
        "Или нажмите «Без причины».",
        [[button("Без причины", "no_reason")], [button("Отмена", "cancel")]],
    )


def _entered_reason(
    session: Session, link: BotLink, command: str, update: Update, draft: dict, today: date
) -> Reply:
    """Причина введена — подаём заявку."""
    if update.is_button and command != "no_reason":
        # Нажата ЧУЖАЯ кнопка: старая из ленты или та же участковая
        # второй раз (двойное нажатие — обычное дело на плохой связи).
        # Приёмка 07.09.2026: такое нажатие уходило причиной заявки —
        # главный инженер получал заявку с обоснованием «site:2»,
        # а перепо́дать её мастер уже не мог: по прибору есть заявка
        # на согласовании. Причину ПИШУТ, а не нажимают.
        return Reply(
            "Причину нужно написать словами — коротко, своими.\n"
            "Или нажмите «Без причины».",
            [[button("Без причины", "no_reason")], [button("Отмена", "cancel")]],
        )

    reason = "" if command == "no_reason" else command.strip()

    try:
        request = services.create_change_request(
            session,
            draft["instrument_id"],
            draft["to_site_id"],
            link.display_name,
            reason=reason or None,
            requested_on=today,
        )
    except services.BusinessError as exc:
        _reset(link)
        return Reply(f"Заявку подать не удалось: {exc}", main_menu(link))

    _reset(link)
    return Reply(
        f"Заявка №{request.id} подана. Главный инженер её увидит.\n"
        f"Прибор: {draft.get('instrument_no')} → участок «{request.to_site.name}».",
        main_menu(link),
    )


def _entered_defect(
    session: Session, link: BotLink, command: str, update: Update, draft: dict, today: date
) -> Reply:
    """Описание неисправности введено — оформляем возврат."""
    if update.is_button:
        # Нажатая кнопка — не описание поломки. Приёмка 07.09.2026:
        # повторное нажатие кнопки прибора отправляло его в ремонт
        # с неисправностью «return_broken:1», а старая «Мой комплект» —
        # с неисправностью «kit» (три буквы, порог ниже её пропускал).
        return Reply(
            "Опишите неисправность словами — что именно с прибором не так.\n"
            "Например: «сбит уровень после падения».",
            [[button("Отмена", "cancel")]],
        )

    description = command.strip()
    if len(description) < 3:
        return Reply(
            "Опишите неисправность словами — этого мало. "
            "Например: «сбит уровень после падения».",
            [[button("Отмена", "cancel")]],
        )

    instrument = session.get(Instrument, draft.get("instrument_id", 0))
    if instrument is None:
        _reset(link)
        return Reply("Прибор не найден.", main_menu(link))

    return _do_return(session, link, instrument, today, status="repair", notes=description)


def _do_return(
    session: Session,
    link: BotLink,
    instrument: Instrument,
    today: date,
    *,
    status: str,
    notes: str | None = None,
) -> Reply:
    """Оформить возврат прибора на склад."""
    try:
        services.return_instrument(
            session,
            instrument.id,
            happened_on=today,
            person=link.display_name,
            new_status=status,
            notes=notes,
        )
    except services.BusinessError as exc:
        _reset(link)
        return Reply(f"Не получилось: {exc}", main_menu(link))

    _reset(link)
    if status == "repair":
        return Reply(
            f"{instrument.inventory_no} {instrument.name} — возвращён как неисправный.\n"
            "Кладовщик уже уведомлён.",
            main_menu(link),
        )
    return Reply(
        f"{instrument.inventory_no} {instrument.name} — сдан на склад.",
        main_menu(link),
    )


# --------------------------------------------------------------------------
# Опрос MAX
# --------------------------------------------------------------------------


def poll_once(session: Session, client, marker: int | None = None) -> tuple[int, int | None]:
    """Забрать порцию событий и ответить на них.

    Возвращает число обработанных событий и новый указатель.

    Клиент передаётся снаружи, а не создаётся здесь: так его подменяют
    в тестах, и логика бота не зависит от того, есть ли сеть.

    Границу транзакции держит вызывающий.
    """
    updates, next_marker = client.get_updates(marker=marker)
    handled = 0

    for update in updates:
        try:
            reply = handle(session, update)
        except Exception as exc:
            # Одно испорченное событие не должно останавливать опрос:
            # иначе бот замолчит для всех из-за одного сообщения.
            audit.write_system(
                session,
                "Ошибка обработки события бота",
                details=f"{update.max_user_id}: {exc}",
            )
            continue

        if reply is None:
            continue

        try:
            client.send_message(
                reply.text,
                chat_id=update.chat_id or None,
                user_id=None if update.chat_id else update.max_user_id,
                buttons=reply.buttons or None,
            )
            handled += 1
            # Ответ ДОШЁЛ — только теперь сообщения считаются доставленными.
            if reply.deliver_ids:
                notifications.mark_delivered(session, reply.deliver_ids, "bot")
        except Exception as exc:
            # Ответ не ушёл — записываем и идём дальше. Событие уже
            # обработано: заявка подана, прибор возвращён. Повторять
            # обработку нельзя, а вот молчать о неудаче — нельзя тем более.
            audit.write_system(
                session,
                "Ответ бота не отправлен",
                details=f"{update.max_user_id}: {exc}",
            )

    return handled, next_marker


def deliver_pending(session: Session, client, limit: int = 50) -> int:
    """Разослать накопившиеся уведомления тем, кто привязан к боту.

    Ящик уведомлений не знает о транспорте (см. `app/notifications.py`) —
    бот сам приходит за своими записями и отмечает, чем доставил.
    """
    pending = notifications.pending_for_bot(session, limit=limit)
    delivered = []

    for item in pending:
        links = list(
            session.scalars(
                select(BotLink).where(
                    BotLink.site_id == item.site_id, BotLink.is_active.is_(True)
                )
            )
        )
        if not links:
            # Некому доставлять: на участке нет привязанного мастера.
            # Запись остаётся недоставленной — придёт мастер, получит.
            continue

        sent_any = False
        for link in links:
            if not link.max_chat_id:
                continue
            try:
                client.send_message(item.text, chat_id=link.max_chat_id)
                sent_any = True
            except Exception as exc:
                audit.write_system(
                    session,
                    "Уведомление не доставлено ботом",
                    object_type=item.object_type,
                    object_id=item.object_id,
                    details=f"{link.display_name}: {exc}",
                )
        if sent_any:
            delivered.append(item.id)

    if delivered:
        notifications.mark_delivered(session, delivered, "bot")
    return len(delivered)
