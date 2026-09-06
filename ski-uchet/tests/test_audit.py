r"""Сторож журнала действий: летопись ведётся, но операцию не роняет.

Какое обещание стережём
------------------------

«Всегда можно узнать, кто выдал прибор, кто согласовал заявку и когда».
Это и есть смысл журнала: разбор происшествия — единственная причина,
по которой он существует.

Второе обещание — противоположного свойства: **«прибор выдастся, даже если
журнал сломался»**. Правило Р9.3 свода БПО: сбой аудита никогда не роняет
бизнес-операцию. Кладовщик не должен слышать «выдача не прошла» из-за
неполадки в летописи.

Как обещания ломаются в жизни
------------------------------

Первое — если запись просто забыли поставить в новую операцию. Ловится
проверкой каждой из семи операций учёта.

Второе — если запись аудита сделана в общей транзакции: тогда любая её
поломка утаскивает за собой всю операцию. У донора это решено точкой
сохранения (`begin_nested`), и здесь так же — но проверять надо не
устройство кода, а поведение при настоящей поломке.

Чем доказано
-------------

Запуском операций на временной базе. Отдельно — операция при намеренно
сломанном журнале: результат обязан состояться.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app import audit
from app.models import AUDIT_SYSTEM, AuditLog, Contract, Instrument, InstrumentType, Site
from app.services import (
    add_verification,
    approve_change_request,
    create_change_request,
    issue_instrument,
    reject_change_request,
    return_instrument,
)

TODAY = date(2026, 6, 15)


@pytest.fixture()
def data(session):
    """Один прибор, два участка — минимум, на котором видно все операции."""
    kind = InstrumentType(name="Нивелир", category="geodesy", verification_interval_months=12)
    session.add(kind)
    session.flush()

    contract = Contract(number="Д-1", title="Стройконтроль", customer="ООО Заказчик")
    session.add(contract)
    session.flush()

    first = Site(name="Участок 1", contract_id=contract.id)
    second = Site(name="Участок 2", contract_id=contract.id)
    session.add_all([first, second])
    session.flush()

    instrument = Instrument(
        inventory_no="ИНВ-001",
        name="Нивелир Н-3",
        type_id=kind.id,
        status="warehouse",
    )
    session.add(instrument)
    session.flush()
    session.add(
        __import__("app.models", fromlist=["Verification"]).Verification(
            instrument_id=instrument.id,
            performed_on=TODAY - timedelta(days=30),
            valid_until=TODAY + timedelta(days=300),
            result="ok",
        )
    )
    session.flush()
    return {"instrument": instrument, "first": first, "second": second}


def test_issue_is_recorded(session, data) -> None:
    """Выдача прибора попадает в журнал вместе с тем, кто её оформил."""
    issue_instrument(
        session,
        data["instrument"].id,
        data["first"].id,
        happened_on=TODAY,
        person="Кладовщик Петров",
    )

    entries = audit.for_object(session, "instrument", data["instrument"].id)
    assert entries, "выдача не попала в журнал"
    assert entries[0].actor == "Кладовщик Петров"
    assert "Участок 1" in (entries[0].details or "")


def test_return_records_defect_description(session, data) -> None:
    """Возврат по неисправности сохраняет описание — иначе кладовщик
    не поймёт, что чинить."""
    issue_instrument(session, data["instrument"].id, data["first"].id, happened_on=TODAY)
    return_instrument(
        session,
        data["instrument"].id,
        happened_on=TODAY,
        person="Мастер Сидоров",
        new_status="repair",
        notes="разбит окуляр",
    )

    entries = audit.for_object(session, "instrument", data["instrument"].id)
    assert "разбит окуляр" in (entries[0].details or ""), (
        "описание неисправности не попало в журнал"
    )


def test_failed_verification_is_recorded(session, data) -> None:
    """Брак по поверке виден в журнале: прибор снят с участка."""
    issue_instrument(session, data["instrument"].id, data["first"].id, happened_on=TODAY)
    add_verification(
        session,
        data["instrument"].id,
        performed_on=TODAY,
        result="fail",
        organization="ЦСМ",
    )

    actions = [e.action for e in audit.for_object(session, "instrument", data["instrument"].id)]
    assert "Поверка: брак" in actions


def test_request_lifecycle_is_recorded(session, data) -> None:
    """Заявка, её согласование и отказ — три отдельные записи с авторами."""
    issue_instrument(session, data["instrument"].id, data["first"].id, happened_on=TODAY)
    request = create_change_request(
        session,
        data["instrument"].id,
        data["second"].id,
        "Мастер Сидоров",
        requested_on=TODAY,
    )
    approve_change_request(session, request.id, "Главный инженер", decided_on=TODAY)

    entries = audit.for_object(session, "change_request", request.id)
    actions = {e.action: e.actor for e in entries}
    assert actions.get("Заявка на перемещение подана") == "Мастер Сидоров"
    assert actions.get("Заявка согласована") == "Главный инженер"


def test_rejection_reason_is_recorded(session, data) -> None:
    """Причина отказа сохраняется: без неё решение нельзя объяснить."""
    issue_instrument(session, data["instrument"].id, data["first"].id, happened_on=TODAY)
    request = create_change_request(
        session,
        data["instrument"].id,
        data["second"].id,
        "Мастер Сидоров",
        requested_on=TODAY,
    )
    reject_change_request(
        session, request.id, "Главный инженер", comment="прибор нужен здесь", decided_on=TODAY
    )

    entries = audit.for_object(session, "change_request", request.id)
    assert any("прибор нужен здесь" in (e.details or "") for e in entries)


def test_broken_audit_does_not_break_the_operation(session, data, monkeypatch) -> None:
    """Р9.3: сбой журнала не роняет бизнес-операцию.

    Кладовщик не должен слышать «выдача не прошла» из-за неполадки
    в летописи. Ломаем запись намеренно и убеждаемся, что прибор всё
    равно выдан.
    """

    def explode(*args, **kwargs):
        raise RuntimeError("журнал недоступен")

    monkeypatch.setattr(audit, "AuditLog", explode)

    movement = issue_instrument(
        session,
        data["instrument"].id,
        data["first"].id,
        happened_on=TODAY,
        person="Кладовщик Петров",
    )

    assert movement is not None, "операция упала из-за сбоя журнала"
    session.refresh(data["instrument"])
    assert data["instrument"].status == "in_use", "прибор не выдан из-за сбоя журнала"
    assert data["instrument"].current_site_id == data["first"].id


def test_actor_is_never_empty(session) -> None:
    """Действующее лицо пустым не бывает.

    Запись «неизвестно кто» бесполезна при разборе, а разбор — единственная
    причина, по которой журнал существует (довод донора, core/operator.py).
    """
    entry = audit.write(session, "Проверка", actor="   ")
    session.flush()

    assert entry is not None
    assert entry.actor == audit.UNKNOWN_ACTOR


def test_system_events_are_separated(session) -> None:
    """Системное событие отличимо от людского действия.

    При разборе происшествия системный шум не должен прятать то,
    что сделали люди.
    """
    audit.write_system(session, "Резервная копия создана")
    session.flush()

    entry = audit.recent(session)[0]
    assert entry.audit_type == AUDIT_SYSTEM
