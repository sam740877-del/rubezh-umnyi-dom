"""Бизнес-логика: комплектность участков, поверки, выдача и возврат приборов."""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    BLOCKED_FOR_ISSUE,
    Contract,
    Instrument,
    InstrumentType,
    KitItem,
    Movement,
    Site,
    Verification,
)

WARN_DAYS = 30  # за сколько дней до конца поверки прибор считается «истекающим»


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
    return record


def expiring_instruments(
    session: Session, days: int = WARN_DAYS, today: date | None = None
) -> list[tuple[Instrument, VerificationState]]:
    """Приборы с просроченной, истекающей или отсутствующей поверкой."""
    today = today or date.today()
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
    if state.code in ("expired", "missing") and not ignore_verification:
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
    return movement


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

    return {
        "today": today,
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
