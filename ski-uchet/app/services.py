"""Бизнес-логика: комплектность участков, поверки, выдача и возврат приборов."""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app import audit, notifications
from app.models import (
    BLOCKED_FOR_ISSUE,
    INSTRUMENT_STATUSES,
    ChangeRequest,
    Contract,
    Instrument,
    InstrumentType,
    KitItem,
    KitTemplate,
    Movement,
    Site,
    Verification,
)

#: Горизонт предупреждения по умолчанию. Настоящее значение живёт
#: в настройках (`app/settings.py`) — метролог правит его сам, у разных
#: контор срок разный. Здесь остаётся то, с чего система начинает
#: на пустой базе, и то, что берётся при вызове без сессии.
WARN_DAYS = 30

#: Состояния поверки, при которых выдача запрещена без особой отметки.
#: Держим одной константой, чтобы экран склада и сама операция выдачи
#: не разошлись: список «готово к выдаче» обязан совпадать с тем, что
#: программа реально позволит выдать.
BLOCKS_ISSUE = ("expired", "missing")


class BusinessError(Exception):
    """Нарушение правила учёта — показывается пользователю понятным текстом."""


def add_months(start: date, months: int) -> date:
    """Прибавить месяцы к дате с корректным переносом конца месяца."""
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


# --------------------------------------------------------------------------
# Поверки
# --------------------------------------------------------------------------

VERIFICATION_STATE_LABELS = {
    "not_required": "Не требуется",
    "missing": "Нет данных о поверке",
    "expired": "Поверка просрочена",
    "expiring": "Истекает",
    "ok": "Действует",
}


@dataclass
class VerificationState:
    code: str
    days_left: int | None = None

    @property
    def label(self) -> str:
        return VERIFICATION_STATE_LABELS[self.code]

    @property
    def is_problem(self) -> bool:
        return self.code in ("missing", "expired", "expiring")


def verification_state(
    instrument: Instrument, today: date | None = None, warn_days: int = WARN_DAYS
) -> VerificationState:
    """Состояние поверки прибора на дату."""
    today = today or date.today()
    if not instrument.type.requires_verification:
        return VerificationState("not_required")

    valid_until = instrument.verification_valid_until
    if valid_until is None:
        return VerificationState("missing")

    days_left = (valid_until - today).days
    if days_left < 0:
        return VerificationState("expired", days_left)
    if days_left <= warn_days:
        return VerificationState("expiring", days_left)
    return VerificationState("ok", days_left)


def add_verification(
    session: Session,
    instrument_id: int,
    performed_on: date,
    *,
    kind: str = "verification",
    valid_until: date | None = None,
    certificate_no: str | None = None,
    organization: str | None = None,
    result: str = "ok",
    cost: float | None = None,
    notes: str | None = None,
) -> Verification:
    """Зарегистрировать поверку. Срок действия считается по интервалу типа, если не задан."""
    instrument = session.get(Instrument, instrument_id)
    if instrument is None:
        raise BusinessError("Прибор не найден")

    if valid_until is None and result == "ok":
        valid_until = add_months(performed_on, instrument.type.verification_interval_months)
    if valid_until is not None and valid_until <= performed_on:
        raise BusinessError("Срок действия поверки должен быть позже даты её проведения")

    record = Verification(
        instrument_id=instrument_id,
        kind=kind,
        performed_on=performed_on,
        valid_until=valid_until,
        certificate_no=certificate_no,
        organization=organization,
        result=result,
        cost=cost,
        notes=notes,
    )
    session.add(record)

    # Прибор с браковочным результатом не должен оставаться в работе
    if result == "fail" and instrument.status != "written_off":
        instrument.status = "repair"
        instrument.current_site_id = None
    elif instrument.status == "verification":
        instrument.status = "warehouse"

    session.flush()
    audit.write(
        session,
        "Поверка: брак" if result == "fail" else "Поверка пройдена",
        actor=organization,
        object_type="instrument",
        object_id=instrument.id,
        details=(
            f"действительна до {valid_until:%d.%m.%Y}"
            if valid_until
            else "срок не назначен"
        )
        + (f", свидетельство {certificate_no}" if certificate_no else "")
        + (" — прибор снят с участка и отправлен в ремонт" if result == "fail" else ""),
    )
    return record


def expiring_instruments(
    session: Session, days: int | None = None, today: date | None = None
) -> list[tuple[Instrument, VerificationState]]:
    """Приборы с просроченной, истекающей или отсутствующей поверкой.

    Горизонт не назвали — берём из настроек: метролог правит его сам,
    и у разных контор срок разный. Прежде число 30 стояло константой
    в двух файлах и уже начинало разъезжаться.
    """
    from app import settings as настройки

    today = today or date.today()
    if days is None:
        days = настройки.warn_days(session)
    instruments = session.scalars(
        select(Instrument)
        .options(selectinload(Instrument.type), selectinload(Instrument.verifications), selectinload(Instrument.current_site))
        .where(Instrument.status != "written_off")
    ).all()

    rows = [(i, verification_state(i, today, warn_days=days)) for i in instruments]
    rows = [(i, s) for i, s in rows if s.is_problem]
    # Сначала самые «горящие»: просроченные, затем по остатку дней
    return sorted(rows, key=lambda r: (r[1].days_left if r[1].days_left is not None else -10**6))


# --------------------------------------------------------------------------
# Движение приборов: выдача / возврат
# --------------------------------------------------------------------------


def issue_instrument(
    session: Session,
    instrument_id: int,
    site_id: int,
    *,
    happened_on: date | None = None,
    person: str | None = None,
    doc_no: str | None = None,
    notes: str | None = None,
    ignore_verification: bool = False,
) -> Movement:
    """Выдать прибор на участок с записью в журнал движения."""
    happened_on = happened_on or date.today()
    instrument = session.get(Instrument, instrument_id)
    if instrument is None:
        raise BusinessError("Прибор не найден")
    site = session.get(Site, site_id)
    if site is None:
        raise BusinessError("Участок не найден")

    if instrument.status in BLOCKED_FOR_ISSUE:
        raise BusinessError(f"Прибор нельзя выдать: текущий статус — «{instrument.status_label}»")
    if instrument.current_site_id is not None:
        current = instrument.current_site.name if instrument.current_site else "другой участок"
        raise BusinessError(f"Прибор уже выдан на участок «{current}». Сначала оформите возврат")
    if site.status != "active":
        raise BusinessError(f"Участок «{site.name}» не действует ({site.status_label})")

    state = verification_state(instrument, happened_on)
    if state.code in BLOCKS_ISSUE and not ignore_verification:
        raise BusinessError(
            f"{state.label}. Выдача на участок запрещена — проведите поверку "
            f"или подтвердите выдачу принудительно"
        )

    instrument.current_site_id = site_id
    instrument.status = "in_use"
    movement = Movement(
        instrument_id=instrument_id,
        site_id=site_id,
        action="issue",
        happened_on=happened_on,
        person=person,
        doc_no=doc_no,
        notes=notes,
    )
    session.add(movement)
    session.flush()
    audit.write(
        session,
        "Выдан на участок",
        actor=person,
        object_type="instrument",
        object_id=instrument_id,
        details=f"участок: {site.name}"
        + (f", документ: {doc_no}" if doc_no else "")
        + (", поверка не учтена" if ignore_verification else ""),
    )
    return movement


def return_instrument(
    session: Session,
    instrument_id: int,
    *,
    happened_on: date | None = None,
    person: str | None = None,
    doc_no: str | None = None,
    notes: str | None = None,
    new_status: str = "warehouse",
) -> Movement:
    """Вернуть прибор с участка на склад / в ремонт / на поверку."""
    happened_on = happened_on or date.today()
    instrument = session.get(Instrument, instrument_id)
    if instrument is None:
        raise BusinessError("Прибор не найден")
    if instrument.current_site_id is None:
        raise BusinessError("Прибор не числится ни на одном участке")
    if new_status == "in_use":
        raise BusinessError("При возврате нельзя оставить статус «На участке»")
    if new_status == "repair" and not (notes and notes.strip()):
        raise BusinessError(
            "Возврат по неисправности — укажите, что с прибором не так: "
            "без описания кладовщик не поймёт, что чинить"
        )

    movement = Movement(
        instrument_id=instrument_id,
        site_id=instrument.current_site_id,
        action="return",
        happened_on=happened_on,
        person=person,
        doc_no=doc_no,
        notes=notes,
    )
    session.add(movement)
    instrument.current_site_id = None
    instrument.status = new_status
    session.flush()
    audit.write(
        session,
        "Возвращён с участка",
        actor=person,
        object_type="instrument",
        object_id=instrument_id,
        details=f"новое состояние: {INSTRUMENT_STATUSES.get(new_status, new_status)}"
        + (f", документ: {doc_no}" if doc_no else "")
        + (f", неисправность: {notes}" if new_status == "repair" and notes else ""),
    )
    if new_status == "repair":
        # Кладовщику сообщаем только о неисправных: исправный возврат —
        # обычная операция, о которой напоминать незачем (ответ 54).
        notifications.notify_users(
            session,
            ("admin", "keeper"),
            "instrument_returned",
            f"Возвращён неисправным: {instrument.inventory_no} {instrument.name}."
            + (f" Что не так: {notes}" if notes else ""),
            object_type="instrument",
            object_id=instrument_id,
        )
    return movement


# --------------------------------------------------------------------------
# Склад
# --------------------------------------------------------------------------


@dataclass
class WarehouseRow:
    """Прибор на складе вместе с тем, что мешает его выдать."""

    instrument: Instrument
    state: VerificationState

    @property
    def can_issue(self) -> bool:
        """Можно ли выдать прибор прямо сейчас, без особой отметки.

        Причина отказа берётся из той же константы, что и сама операция
        выдачи: иначе экран обещал бы одно, а программа делала другое.
        """
        return self.state.code not in BLOCKS_ISSUE


@dataclass
class WarehouseReport:
    """Что физически лежит на складе и что с этим можно делать."""

    rows: list[WarehouseRow]
    by_status: dict[str, int]

    @property
    def ready(self) -> int:
        """Готовы к выдаче — самое нужное число на этом экране."""
        return sum(1 for row in self.rows if row.can_issue)

    @property
    def blocked(self) -> int:
        """Лежат, но выдать нельзя: просроченная поверка."""
        return sum(1 for row in self.rows if not row.can_issue)


def warehouse_report(
    session: Session, today: date | None = None, type_id: int | None = None
) -> WarehouseReport:
    """Состояние склада: что лежит и что из этого готово к выдаче.

    Складом считаем приборы со статусом «на складе» — то есть те, которыми
    кладовщик может распорядиться. Приборы в ремонте и на поверке физически
    тоже могут лежать в конторе, но распорядиться ими нельзя, поэтому в
    список они не идут; их число видно в сводке по состояниям.
    """
    today = today or date.today()

    query = (
        select(Instrument)
        .options(selectinload(Instrument.type), selectinload(Instrument.verifications))
        .where(Instrument.status == "warehouse")
    )
    if type_id:
        query = query.where(Instrument.type_id == type_id)

    rows = [
        WarehouseRow(instrument=item, state=verification_state(item, today))
        for item in session.scalars(query.order_by(Instrument.inventory_no))
    ]

    counts = dict(
        session.execute(
            select(Instrument.status, func.count(Instrument.id)).group_by(Instrument.status)
        ).all()
    )
    by_status = {key: counts.get(key, 0) for key in INSTRUMENT_STATUSES}

    return WarehouseReport(rows=rows, by_status=by_status)


# --------------------------------------------------------------------------
# Комплектность участка
# --------------------------------------------------------------------------


@dataclass
class CompletenessRow:
    type: InstrumentType
    required_qty: int
    kit_item_id: int | None = None
    instruments: list[Instrument] = field(default_factory=list)
    notes: str | None = None

    @property
    def fact_qty(self) -> int:
        return len(self.instruments)

    @property
    def deficit(self) -> int:
        return max(0, self.required_qty - self.fact_qty)

    @property
    def surplus(self) -> int:
        return max(0, self.fact_qty - self.required_qty)

    @property
    def is_complete(self) -> bool:
        return self.deficit == 0


@dataclass
class CompletenessReport:
    site: Site
    rows: list[CompletenessRow]
    extra: list[Instrument]  # приборы на участке вне комплекта по договору
    problems: list[tuple[Instrument, VerificationState]]

    @property
    def required_total(self) -> int:
        return sum(r.required_qty for r in self.rows)

    @property
    def fact_total(self) -> int:
        return sum(min(r.fact_qty, r.required_qty) for r in self.rows)

    @property
    def deficit_total(self) -> int:
        return sum(r.deficit for r in self.rows)

    @property
    def percent(self) -> int:
        if self.required_total == 0:
            return 100
        return round(self.fact_total / self.required_total * 100)

    @property
    def is_complete(self) -> bool:
        return self.deficit_total == 0


def site_completeness(
    session: Session, site_id: int, today: date | None = None
) -> CompletenessReport:
    """Сверка «требуется по договору» против «фактически на участке»."""
    today = today or date.today()
    site = session.get(Site, site_id)
    if site is None:
        raise BusinessError("Участок не найден")

    kit_items = session.scalars(
        select(KitItem).options(selectinload(KitItem.type)).where(KitItem.site_id == site_id)
    ).all()
    on_site = session.scalars(
        select(Instrument)
        .options(selectinload(Instrument.type), selectinload(Instrument.verifications))
        .where(Instrument.current_site_id == site_id)
    ).all()

    by_type: dict[int, list[Instrument]] = {}
    for instrument in on_site:
        by_type.setdefault(instrument.type_id, []).append(instrument)

    rows = [
        CompletenessRow(
            type=item.type,
            required_qty=item.required_qty,
            kit_item_id=item.id,
            instruments=by_type.get(item.type_id, []),
            notes=item.notes,
        )
        for item in sorted(kit_items, key=lambda k: k.type.name)
    ]

    kit_type_ids = {item.type_id for item in kit_items}
    extra = sorted(
        (i for i in on_site if i.type_id not in kit_type_ids), key=lambda i: i.name
    )
    problems = [
        (i, s) for i, s in ((i, verification_state(i, today)) for i in on_site) if s.is_problem
    ]
    return CompletenessReport(site=site, rows=rows, extra=extra, problems=problems)


# --------------------------------------------------------------------------
# Сводка для главной страницы
# --------------------------------------------------------------------------


def dashboard(session: Session, today: date | None = None) -> dict:
    today = today or date.today()
    total = session.scalar(select(func.count(Instrument.id))) or 0
    by_status = dict(
        session.execute(
            select(Instrument.status, func.count(Instrument.id)).group_by(Instrument.status)
        ).all()
    )
    problems = expiring_instruments(session, today=today)
    sites = session.scalars(
        select(Site).options(selectinload(Site.contract)).where(Site.status == "active")
    ).all()
    site_reports = [site_completeness(session, s.id, today) for s in sites]
    incomplete = [r for r in site_reports if not r.is_complete]

    pending = open_requests(session)
    return {
        "today": today,
        "open_requests": pending,
        "total_instruments": total,
        "by_status": by_status,
        "active_contracts": session.scalar(
            select(func.count(Contract.id)).where(Contract.status == "active")
        )
        or 0,
        "active_sites": len(sites),
        "expired": [r for r in problems if r[1].code == "expired"],
        "missing": [r for r in problems if r[1].code == "missing"],
        "expiring": [r for r in problems if r[1].code == "expiring"],
        "site_reports": sorted(site_reports, key=lambda r: r.percent),
        "incomplete_sites": incomplete,
    }


# --------------------------------------------------------------------------
# Типовые комплекты
# --------------------------------------------------------------------------


def apply_kit_template(session: Session, site_id: int, template_id: int) -> int:
    """Применить типовой комплект к участку. Существующие позиции не затираем,
    а поднимаем количество до требуемого шаблоном."""
    site = session.get(Site, site_id)
    if site is None:
        raise BusinessError("Участок не найден")
    template = session.get(KitTemplate, template_id)
    if template is None:
        raise BusinessError("Типовой комплект не найден")
    if not template.items:
        raise BusinessError(f"В комплекте «{template.name}» нет ни одной позиции")

    existing = {
        item.type_id: item
        for item in session.scalars(select(KitItem).where(KitItem.site_id == site_id))
    }
    changed = 0
    for item in template.items:
        current = existing.get(item.type_id)
        if current is None:
            session.add(
                KitItem(site_id=site_id, type_id=item.type_id, required_qty=item.required_qty)
            )
            changed += 1
        elif current.required_qty < item.required_qty:
            current.required_qty = item.required_qty
            changed += 1
    session.flush()
    if changed:
        audit.write(
            session,
            "Применён типовой комплект",
            object_type="site",
            object_id=site_id,
            details=f"комплект «{template.name}», изменено позиций: {changed}",
        )
    return changed


# --------------------------------------------------------------------------
# Перечень средств измерений — документ для заказчика
# --------------------------------------------------------------------------


@dataclass
class InstrumentListRow:
    """Строка перечня СИ по объекту."""

    instrument: Instrument
    state: VerificationState

    @property
    def valid_until(self) -> date | None:
        return self.instrument.verification_valid_until

    @property
    def certificate_no(self) -> str | None:
        """Номер свидетельства о последней поверке.

        Заказчик проверяет именно его: без номера строка «поверен до»
        ничем не подтверждена.
        """
        свежая = None
        for запись in self.instrument.verifications:
            if запись.result != "ok" or not запись.valid_until:
                continue
            if свежая is None or запись.valid_until > свежая.valid_until:
                свежая = запись
        return свежая.certificate_no if свежая else None


@dataclass
class InstrumentListReport:
    """Перечень СИ, применяемых на объекте.

    Документ, который заказчик требует чаще всего (ответ на вопрос 19).
    Отличается от сверки комплектности: там «сколько нужно и сколько
    есть», здесь «какие приборы работают на объекте и до какого срока
    они поверены».
    """

    site: Site
    rows: list[InstrumentListRow]
    prepared_on: date

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def problems(self) -> int:
        """Сколько приборов не готовы к работе.

        Число выносится наверх нарочно: перечень с просроченной поверкой
        заказчику лучше не отдавать, а узнать об этом надо до печати,
        а не от заказчика.
        """
        return sum(1 for row in self.rows if row.state.code in ("expired", "missing"))


def instrument_list(
    session: Session, site_id: int, today: date | None = None
) -> InstrumentListReport:
    """Перечень средств измерений, применяемых на объекте."""
    today = today or date.today()
    site = session.get(Site, site_id)
    if site is None:
        raise BusinessError("Участок не найден")

    приборы = session.scalars(
        select(Instrument)
        .options(selectinload(Instrument.type), selectinload(Instrument.verifications))
        .where(Instrument.current_site_id == site_id)
        .order_by(Instrument.inventory_no)
    ).all()

    return InstrumentListReport(
        site=site,
        rows=[
            InstrumentListRow(instrument=прибор, state=verification_state(прибор, today))
            for прибор in приборы
        ],
        prepared_on=today,
    )


# --------------------------------------------------------------------------
# Заявки на перемещение
# --------------------------------------------------------------------------

APPROVER_TITLE = "Главный инженер"


def create_change_request(
    session: Session,
    instrument_id: int,
    to_site_id: int,
    requested_by: str,
    *,
    reason: str | None = None,
    requested_on: date | None = None,
) -> ChangeRequest:
    """Мастер подаёт заявку на перемещение прибора, который за ним числится."""
    requested_on = requested_on or date.today()
    instrument = session.get(Instrument, instrument_id)
    if instrument is None:
        raise BusinessError("Прибор не найден")
    if instrument.current_site_id is None:
        raise BusinessError(
            "Заявку можно подать только на прибор, закреплённый за участком. "
            "Выдачу со склада оформляет кладовщик"
        )
    if instrument.current_site_id == to_site_id:
        raise BusinessError("Прибор уже находится на этом участке")

    target = session.get(Site, to_site_id)
    if target is None:
        raise BusinessError("Участок назначения не найден")
    if target.status != "active":
        raise BusinessError(f"Участок «{target.name}» не действует ({target.status_label})")

    open_request = session.scalar(
        select(ChangeRequest).where(
            ChangeRequest.instrument_id == instrument_id, ChangeRequest.status == "new"
        )
    )
    if open_request is not None:
        raise BusinessError("По этому прибору уже есть заявка на согласовании")
    if not requested_by.strip():
        raise BusinessError("Укажите, кто подаёт заявку")

    request = ChangeRequest(
        instrument_id=instrument_id,
        from_site_id=instrument.current_site_id,
        to_site_id=to_site_id,
        requested_by=requested_by.strip(),
        requested_on=requested_on,
        reason=reason,
        status="new",
    )
    session.add(request)
    session.flush()
    audit.write(
        session,
        "Заявка на перемещение подана",
        actor=requested_by,
        object_type="change_request",
        object_id=request.id,
        details=f"прибор {instrument.inventory_no} → участок «{target.name}»"
        + (f", причина: {reason}" if reason else ""),
    )
    notifications.notify_users(
        session,
        ("admin", "chief"),
        "request_new",
        f"Заявка №{request.id} на согласование: {instrument.inventory_no} "
        f"{instrument.name} → участок «{target.name}». Подал: {requested_by.strip()}."
        + (f" Причина: {reason}" if reason else ""),
        object_type="change_request",
        object_id=request.id,
    )
    return request


def approve_change_request(
    session: Session,
    request_id: int,
    decided_by: str,
    *,
    decided_on: date | None = None,
    comment: str | None = None,
    ignore_verification: bool = False,
) -> ChangeRequest:
    """Согласование главным инженером: заявка исполняется сразу — возврат и выдача."""
    decided_on = decided_on or date.today()
    request = session.get(ChangeRequest, request_id)
    if request is None:
        raise BusinessError("Заявка не найдена")
    if not request.is_open:
        raise BusinessError(f"Заявка уже обработана: {request.status_label}")
    if not decided_by.strip():
        raise BusinessError("Укажите, кто согласовал заявку")

    doc_no = f"ЗАЯВКА-{request.id}"

    # Обе операции подписываются ТЕМ, КТО СОГЛАСОВАЛ, а не тем, кто подавал
    # заявку. Раньше здесь стояло `request.requested_by`, и журнал
    # приписывал выдачу с возвратом мастеру, которого в системе в этот
    # момент не было вовсе. Для разбора «кто перемещал прибор» такой
    # журнал врёт, а журнал — единственная опора при разборе.
    #
    # Кто просил, видно в самой заявке и в примечании ниже: одно другого
    # не заменяет. Ответственный за операцию — тот, чьим решением она
    # состоялась.
    moved_by = decided_by.strip()
    reason = f"Перемещение по заявке №{request.id} (подал: {request.requested_by})"

    return_instrument(
        session,
        request.instrument_id,
        happened_on=decided_on,
        person=moved_by,
        doc_no=doc_no,
        notes=reason,
    )
    issue_instrument(
        session,
        request.instrument_id,
        request.to_site_id,
        happened_on=decided_on,
        person=moved_by,
        doc_no=doc_no,
        notes=reason,
        ignore_verification=ignore_verification,
    )

    request.status = "approved"
    request.decided_by = decided_by.strip()
    request.decided_on = decided_on
    request.decision_comment = comment
    session.flush()
    audit.write(
        session,
        "Заявка согласована",
        actor=decided_by,
        object_type="change_request",
        object_id=request.id,
        details=f"перемещение исполнено: прибор №{request.instrument_id}"
        + (f", комментарий: {comment}" if comment else ""),
    )
    notifications.notify_site(
        session,
        request.to_site_id,
        "request_decided",
        f"Заявка №{request.id} согласована: прибор "
        f"{request.instrument.inventory_no} {request.instrument.name} "
        f"перемещён на ваш участок."
        + (f" Комментарий: {comment}" if comment else ""),
        object_type="change_request",
        object_id=request.id,
    )
    return request


def reject_change_request(
    session: Session,
    request_id: int,
    decided_by: str,
    comment: str,
    *,
    decided_on: date | None = None,
) -> ChangeRequest:
    """Отказ по заявке. Комментарий обязателен — иначе мастер не поймёт причину."""
    decided_on = decided_on or date.today()
    request = session.get(ChangeRequest, request_id)
    if request is None:
        raise BusinessError("Заявка не найдена")
    if not request.is_open:
        raise BusinessError(f"Заявка уже обработана: {request.status_label}")
    if not comment or not comment.strip():
        raise BusinessError("При отказе комментарий обязателен — укажите причину")
    if not decided_by.strip():
        raise BusinessError("Укажите, кто принял решение")

    request.status = "rejected"
    request.decided_by = decided_by.strip()
    request.decided_on = decided_on
    request.decision_comment = comment.strip()
    session.flush()
    audit.write(
        session,
        "Заявка отклонена",
        actor=decided_by,
        object_type="change_request",
        object_id=request.id,
        details=f"причина отказа: {comment.strip()}",
    )
    # Отказ уходит на участок, ОТКУДА подавали: мастер ждёт ответа там,
    # где прибор у него и стоит.
    if request.from_site_id:
        notifications.notify_site(
            session,
            request.from_site_id,
            "request_decided",
            f"Заявка №{request.id} отклонена. Причина: {comment.strip()}",
            object_type="change_request",
            object_id=request.id,
        )
    return request


def open_requests(session: Session) -> list[ChangeRequest]:
    return list(
        session.scalars(
            select(ChangeRequest)
            .options(
                selectinload(ChangeRequest.instrument),
                selectinload(ChangeRequest.from_site),
                selectinload(ChangeRequest.to_site),
            )
            .where(ChangeRequest.status == "new")
            .order_by(ChangeRequest.requested_on)
        )
    )
