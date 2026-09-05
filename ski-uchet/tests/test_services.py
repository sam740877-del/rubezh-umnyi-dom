from datetime import date, timedelta

import pytest

from app.models import Contract, Instrument, InstrumentType, KitItem, Site
from app.seed import seed_demo
from app.services import (
    BusinessError,
    add_months,
    add_verification,
    dashboard,
    expiring_instruments,
    issue_instrument,
    return_instrument,
    site_completeness,
    verification_state,
)

TODAY = date(2026, 6, 15)


@pytest.fixture()
def fixture_data(session):
    level = InstrumentType(name="Нивелир", category="geodesy", verification_interval_months=12)
    tape = InstrumentType(name="Рулетка", category="vik", verification_interval_months=12)
    tripod = InstrumentType(name="Штатив", category="geodesy", requires_verification=False)
    session.add_all([level, tape, tripod])
    session.flush()

    contract = Contract(number="Д-1", title="Стройконтроль", customer="ООО Заказчик")
    session.add(contract)
    session.flush()
    site = Site(name="Участок 1", contract_id=contract.id)
    other = Site(name="Участок 2", contract_id=contract.id)
    session.add_all([site, other])
    session.flush()

    session.add_all(
        [
            KitItem(site_id=site.id, type_id=level.id, required_qty=1),
            KitItem(site_id=site.id, type_id=tape.id, required_qty=2),
        ]
    )
    instruments = {
        "level": Instrument(inventory_no="И-1", name="Нивелир B40", type_id=level.id),
        "tape1": Instrument(inventory_no="И-2", name="Рулетка 30", type_id=tape.id),
        "tape2": Instrument(inventory_no="И-3", name="Рулетка 10", type_id=tape.id),
        "tripod": Instrument(inventory_no="И-4", name="Штатив", type_id=tripod.id),
    }
    session.add_all(instruments.values())
    session.flush()

    for key in ("level", "tape1", "tape2"):
        add_verification(
            session,
            instruments[key].id,
            performed_on=TODAY - timedelta(days=100),
            valid_until=TODAY + timedelta(days=200),
        )
    session.flush()
    return {"site": site, "other": other, "instruments": instruments, "types": {"level": level}}


def test_add_months_handles_month_end():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 12, 15), 12) == date(2027, 12, 15)


def test_verification_state_transitions(session, fixture_data):
    instrument = fixture_data["instruments"]["level"]
    assert verification_state(instrument, TODAY).code == "ok"
    assert verification_state(instrument, TODAY + timedelta(days=190)).code == "expiring"
    assert verification_state(instrument, TODAY + timedelta(days=250)).code == "expired"

    tripod = fixture_data["instruments"]["tripod"]
    assert verification_state(tripod, TODAY).code == "not_required"


def test_verification_missing_when_no_records(session, fixture_data):
    session.add(
        Instrument(inventory_no="И-9", name="Новый нивелир", type_id=fixture_data["types"]["level"].id)
    )
    session.flush()
    fresh = session.query(Instrument).filter_by(inventory_no="И-9").one()
    assert verification_state(fresh, TODAY).code == "missing"


def test_valid_until_computed_from_type_interval(session, fixture_data):
    instrument = fixture_data["instruments"]["tripod"]
    record = add_verification(session, instrument.id, performed_on=date(2026, 3, 10))
    assert record.valid_until == date(2027, 3, 10)


def test_failed_verification_sends_instrument_to_repair(session, fixture_data):
    instrument = fixture_data["instruments"]["tape1"]
    issue_instrument(session, instrument.id, fixture_data["site"].id, happened_on=TODAY)
    add_verification(session, instrument.id, performed_on=TODAY, result="fail")
    assert instrument.status == "repair"
    assert instrument.current_site_id is None


def test_issue_and_return_cycle(session, fixture_data):
    instrument = fixture_data["instruments"]["level"]
    site = fixture_data["site"]

    issue_instrument(session, instrument.id, site.id, happened_on=TODAY, person="Иванов")
    assert instrument.status == "in_use"
    assert instrument.current_site_id == site.id

    return_instrument(session, instrument.id, happened_on=TODAY + timedelta(days=5))
    assert instrument.status == "warehouse"
    assert instrument.current_site_id is None
    assert [m.action for m in instrument.movements] == ["return", "issue"]


def test_cannot_issue_twice_without_return(session, fixture_data):
    instrument = fixture_data["instruments"]["level"]
    issue_instrument(session, instrument.id, fixture_data["site"].id, happened_on=TODAY)
    with pytest.raises(BusinessError, match="уже выдан"):
        issue_instrument(session, instrument.id, fixture_data["other"].id, happened_on=TODAY)


def test_cannot_issue_with_expired_verification(session, fixture_data):
    instrument = fixture_data["instruments"]["level"]
    late = TODAY + timedelta(days=300)
    with pytest.raises(BusinessError, match="просрочена"):
        issue_instrument(session, instrument.id, fixture_data["site"].id, happened_on=late)

    issue_instrument(
        session, instrument.id, fixture_data["site"].id, happened_on=late, ignore_verification=True
    )
    assert instrument.status == "in_use"


def test_cannot_issue_instrument_in_repair(session, fixture_data):
    instrument = fixture_data["instruments"]["level"]
    instrument.status = "repair"
    session.flush()
    with pytest.raises(BusinessError, match="нельзя выдать"):
        issue_instrument(session, instrument.id, fixture_data["site"].id, happened_on=TODAY)


def test_return_requires_being_on_site(session, fixture_data):
    with pytest.raises(BusinessError, match="не числится"):
        return_instrument(session, fixture_data["instruments"]["level"].id)


def test_site_completeness_counts_deficit_and_extra(session, fixture_data):
    site = fixture_data["site"]
    instruments = fixture_data["instruments"]
    issue_instrument(session, instruments["level"].id, site.id, happened_on=TODAY)
    issue_instrument(session, instruments["tape1"].id, site.id, happened_on=TODAY)
    issue_instrument(session, instruments["tripod"].id, site.id, happened_on=TODAY)

    report = site_completeness(session, site.id, TODAY)
    rows = {row.type.name: row for row in report.rows}
    assert rows["Нивелир"].fact_qty == 1 and rows["Нивелир"].deficit == 0
    assert rows["Рулетка"].fact_qty == 1 and rows["Рулетка"].deficit == 1
    assert report.deficit_total == 1
    assert report.percent == 67
    assert not report.is_complete
    assert [i.inventory_no for i in report.extra] == ["И-4"]


def test_site_completeness_full_kit(session, fixture_data):
    site = fixture_data["site"]
    instruments = fixture_data["instruments"]
    for key in ("level", "tape1", "tape2"):
        issue_instrument(session, instruments[key].id, site.id, happened_on=TODAY)

    report = site_completeness(session, site.id, TODAY)
    assert report.is_complete
    assert report.percent == 100


def test_expiring_instruments_sorted_by_urgency(session, fixture_data):
    """Сначала — просроченные и без данных, затем истекающие по остатку дней."""
    session.add(
        Instrument(inventory_no="И-8", name="Нивелир без поверки", type_id=fixture_data["types"]["level"].id)
    )
    session.flush()
    expired = fixture_data["instruments"]["tape2"]
    expired.verifications.clear()
    add_verification(
        session,
        expired.id,
        performed_on=TODAY - timedelta(days=400),
        valid_until=TODAY - timedelta(days=35),
    )
    session.flush()
    session.expire_all()

    rows = expiring_instruments(session, days=30, today=TODAY + timedelta(days=180))
    by_inv = {i.inventory_no: state.code for i, state in rows}
    assert by_inv["И-3"] == "expired"
    assert by_inv["И-8"] == "missing"
    assert by_inv["И-1"] == "expiring"
    assert "И-4" not in by_inv  # штативу поверка не требуется
    # самые «горящие» — в начале списка
    assert rows[0][1].code in ("missing", "expired")
    assert rows[-1][1].code == "expiring"


def test_dashboard_and_demo_seed(session):
    seed_demo(session, today=TODAY)
    session.commit()
    data = dashboard(session, today=TODAY)
    assert data["total_instruments"] == 24
    assert data["active_contracts"] == 2
    assert data["active_sites"] == 3
    assert data["expired"], "в демо-данных есть просроченные поверки"
    assert data["site_reports"]


# --------------------------------------------------------------------------
# Заявки на перемещение и типовые комплекты
# --------------------------------------------------------------------------

from app.models import ChangeRequest, KitTemplate, KitTemplateItem  # noqa: E402
from app.services import (  # noqa: E402
    apply_kit_template,
    approve_change_request,
    create_change_request,
    open_requests,
    reject_change_request,
)


def test_request_requires_instrument_on_site(session, fixture_data):
    instrument = fixture_data["instruments"]["level"]
    with pytest.raises(BusinessError, match="закреплённый за участком"):
        create_change_request(session, instrument.id, fixture_data["other"].id, "Мастер")


def test_approved_request_moves_instrument(session, fixture_data):
    instrument = fixture_data["instruments"]["level"]
    site, other = fixture_data["site"], fixture_data["other"]
    issue_instrument(session, instrument.id, site.id, happened_on=TODAY)

    request = create_change_request(
        session, instrument.id, other.id, "Мастер", requested_on=TODAY
    )
    assert request.from_site_id == site.id

    approve_change_request(session, request.id, "Главный инженер", decided_on=TODAY)
    assert request.status == "approved"
    assert instrument.current_site_id == other.id
    # в журнале три записи: первичная выдача, возврат и выдача на новый участок
    assert len(instrument.movements) == 3
    assert {m.doc_no for m in instrument.movements} >= {f"ЗАЯВКА-{request.id}"}


def test_reject_requires_comment(session, fixture_data):
    instrument = fixture_data["instruments"]["level"]
    issue_instrument(session, instrument.id, fixture_data["site"].id, happened_on=TODAY)
    request = create_change_request(
        session, instrument.id, fixture_data["other"].id, "Мастер", requested_on=TODAY
    )

    with pytest.raises(BusinessError, match="комментарий обязателен"):
        reject_change_request(session, request.id, "Главный инженер", "")

    reject_change_request(session, request.id, "Главный инженер", "Прибор нужен на своём участке")
    assert request.status == "rejected"
    assert request.decision_comment == "Прибор нужен на своём участке"
    assert instrument.current_site_id == fixture_data["site"].id


def test_one_open_request_per_instrument(session, fixture_data):
    instrument = fixture_data["instruments"]["level"]
    issue_instrument(session, instrument.id, fixture_data["site"].id, happened_on=TODAY)
    create_change_request(
        session, instrument.id, fixture_data["other"].id, "Мастер", requested_on=TODAY
    )
    with pytest.raises(BusinessError, match="уже есть заявка"):
        create_change_request(
            session, instrument.id, fixture_data["other"].id, "Мастер", requested_on=TODAY
        )
    assert len(open_requests(session)) == 1


def test_decided_request_cannot_be_decided_again(session, fixture_data):
    instrument = fixture_data["instruments"]["level"]
    issue_instrument(session, instrument.id, fixture_data["site"].id, happened_on=TODAY)
    request = create_change_request(
        session, instrument.id, fixture_data["other"].id, "Мастер", requested_on=TODAY
    )
    approve_change_request(session, request.id, "Главный инженер", decided_on=TODAY)
    with pytest.raises(BusinessError, match="уже обработана"):
        reject_change_request(session, request.id, "Главный инженер", "поздно")


def test_kit_template_applies_and_raises_quantities(session, fixture_data):
    types = {t.name: t for t in session.query(InstrumentType).all()}
    template = KitTemplate(name="Монолит")
    session.add(template)
    session.flush()
    session.add_all([
        KitTemplateItem(template_id=template.id, type_id=types["Нивелир"].id, required_qty=2),
        KitTemplateItem(template_id=template.id, type_id=types["Штатив"].id, required_qty=1),
    ])
    session.flush()

    # на участке уже есть «Нивелир 1 шт.» и «Рулетка 2 шт.»
    changed = apply_kit_template(session, fixture_data["site"].id, template.id)
    assert changed == 2

    report = site_completeness(session, fixture_data["site"].id, TODAY)
    rows = {row.type.name: row.required_qty for row in report.rows}
    assert rows["Нивелир"] == 2   # поднято до требуемого шаблоном
    assert rows["Рулетка"] == 2   # своё требование не затёрто
    assert rows["Штатив"] == 1    # добавлено из шаблона


def test_return_to_repair_requires_description(session, fixture_data):
    """Возврат по неисправности без описания не проходит — так решил заказчик."""
    instrument = fixture_data["instruments"]["level"]
    issue_instrument(session, instrument.id, fixture_data["site"].id, happened_on=TODAY)

    with pytest.raises(BusinessError, match="что с прибором не так"):
        return_instrument(session, instrument.id, happened_on=TODAY, new_status="repair")

    return_instrument(
        session,
        instrument.id,
        happened_on=TODAY,
        new_status="repair",
        notes="Сбита юстировка после падения",
    )
    assert instrument.status == "repair"
    returns = [m for m in instrument.movements if m.action == "return"]
    assert returns[0].notes == "Сбита юстировка после падения"


def test_return_to_warehouse_needs_no_description(session, fixture_data):
    instrument = fixture_data["instruments"]["level"]
    issue_instrument(session, instrument.id, fixture_data["site"].id, happened_on=TODAY)
    return_instrument(session, instrument.id, happened_on=TODAY)
    assert instrument.status == "warehouse"
