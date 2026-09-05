"""Справочник типов средств контроля и демонстрационные данные."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Contract, Instrument, InstrumentType, KitItem, Site
from app.services import add_verification, issue_instrument

# name, category, requires_verification, interval_months
SEED_TYPES: list[tuple[str, str, bool, int]] = [
    # Геодезия
    ("Нивелир оптический", "geodesy", True, 12),
    ("Нивелир цифровой", "geodesy", True, 12),
    ("Тахеометр электронный", "geodesy", True, 12),
    ("Теодолит", "geodesy", True, 12),
    ("GNSS-приёмник геодезический", "geodesy", True, 12),
    ("Лазерный дальномер", "geodesy", True, 12),
    ("Построитель плоскостей (лазерный уровень)", "geodesy", True, 12),
    ("Рейка нивелирная", "geodesy", True, 24),
    ("Штатив геодезический", "geodesy", False, 12),
    # Адгезия и прочность
    ("Адгезиметр отрывной", "adhesion", True, 12),
    ("Адгезиметр решётчатого надреза", "adhesion", False, 12),
    ("Склерометр (молоток Шмидта)", "adhesion", True, 12),
    ("Измеритель прочности бетона ударно-импульсный", "adhesion", True, 12),
    ("Прибор отрыва со скалыванием", "adhesion", True, 12),
    ("Измеритель защитного слоя бетона", "adhesion", True, 12),
    # ВИК
    ("Штангенциркуль", "vik", True, 12),
    ("Рулетка измерительная металлическая", "vik", True, 12),
    ("Линейка измерительная металлическая", "vik", True, 24),
    ("Угольник поверочный", "vik", True, 24),
    ("Набор щупов", "vik", True, 12),
    ("Шаблон сварщика УШС-3", "vik", True, 12),
    ("Универсальный шаблон сварщика УШС-2", "vik", True, 12),
    ("Лупа измерительная 10х", "vik", False, 12),
    ("Толщиномер покрытий", "vik", True, 12),
    ("Уровень строительный", "vik", True, 24),
    ("Правило контрольное 2 м", "vik", False, 12),
    ("Клин измерительный КМ", "vik", True, 12),
    # Климат
    ("Термогигрометр", "climate", True, 12),
    ("Пирометр (бесконтактный термометр)", "climate", True, 12),
    ("Влагомер строительных материалов", "climate", True, 12),
    ("Анемометр", "climate", True, 12),
    # Электро
    ("Мегаомметр", "electro", True, 12),
    ("Мультиметр", "electro", True, 12),
]


def seed_types(session: Session) -> int:
    """Заполнить справочник типов. Существующие записи не трогаем."""
    existing = {name for name in session.scalars(select(InstrumentType.name))}
    added = 0
    for name, category, requires, interval in SEED_TYPES:
        if name in existing:
            continue
        session.add(
            InstrumentType(
                name=name,
                category=category,
                requires_verification=requires,
                verification_interval_months=interval,
            )
        )
        added += 1
    session.flush()
    return added


def _type_id(session: Session, name: str) -> int:
    type_id = session.scalar(select(InstrumentType.id).where(InstrumentType.name == name))
    if type_id is None:
        raise ValueError(f"Тип «{name}» не найден в справочнике")
    return type_id


def seed_demo(session: Session, today: date | None = None) -> None:
    """Демонстрационный набор: 2 договора, 3 участка, комплекты и приборы."""
    today = today or date.today()
    if session.scalar(select(func.count(Contract.id))):
        raise ValueError("В базе уже есть договоры — демо-данные не добавлены")

    seed_types(session)

    contract1 = Contract(
        number="СМР-2025/14",
        title="Строительный контроль на объекте «ЖК Северный, к.3»",
        customer="ООО «СтройИнвест»",
        signed_on=date(today.year, 1, 15),
        valid_until=date(today.year + 1, 6, 30),
        status="active",
    )
    contract2 = Contract(
        number="СК-2025/07",
        title="Технадзор реконструкции котельной №4",
        customer="АО «Теплосети»",
        signed_on=date(today.year, 3, 1),
        valid_until=date(today.year, 12, 31),
        status="active",
    )
    session.add_all([contract1, contract2])
    session.flush()

    site1 = Site(
        name="ЖК Северный, корпус 3",
        address="г. Тюмень, ул. Северная, 12",
        contract=contract1,
        responsible_name="Иванов И.И.",
        responsible_phone="+7 900 000-00-01",
    )
    site2 = Site(
        name="ЖК Северный, паркинг",
        address="г. Тюмень, ул. Северная, 12/2",
        contract=contract1,
        responsible_name="Петров П.П.",
        responsible_phone="+7 900 000-00-02",
    )
    site3 = Site(
        name="Котельная №4",
        address="г. Тюмень, пр. Заводской, 5",
        contract=contract2,
        responsible_name="Сидоров С.С.",
        responsible_phone="+7 900 000-00-03",
    )
    session.add_all([site1, site2, site3])
    session.flush()

    kits = {
        site1: [
            ("Нивелир оптический", 1),
            ("Тахеометр электронный", 1),
            ("Рейка нивелирная", 2),
            ("Рулетка измерительная металлическая", 2),
            ("Штангенциркуль", 1),
            ("Шаблон сварщика УШС-3", 1),
            ("Склерометр (молоток Шмидта)", 1),
            ("Термогигрометр", 1),
            ("Уровень строительный", 2),
        ],
        site2: [
            ("Нивелир оптический", 1),
            ("Рулетка измерительная металлическая", 1),
            ("Измеритель защитного слоя бетона", 1),
            ("Склерометр (молоток Шмидта)", 1),
            ("Правило контрольное 2 м", 1),
        ],
        site3: [
            ("Тахеометр электронный", 1),
            ("Адгезиметр отрывной", 1),
            ("Толщиномер покрытий", 1),
            ("Шаблон сварщика УШС-3", 2),
            ("Набор щупов", 1),
            ("Пирометр (бесконтактный термометр)", 1),
            ("Мегаомметр", 1),
        ],
    }
    for site, items in kits.items():
        for type_name, qty in items:
            session.add(
                KitItem(site_id=site.id, type_id=_type_id(session, type_name), required_qty=qty)
            )
    session.flush()

    # инв. номер, наименование, тип, модель, серийный, дней до конца поверки (None — поверки нет)
    instruments_spec = [
        ("СКИ-001", "Нивелир оптический Sokkia B40", "Нивелир оптический", "B40A", "0043211", 200),
        ("СКИ-002", "Нивелир оптический ADA Basis", "Нивелир оптический", "Basis", "A00121", 25),
        ("СКИ-003", "Тахеометр Leica TS06", "Тахеометр электронный", "TS06 plus", "1590233", 120),
        ("СКИ-004", "Тахеометр Sokkia iM-52", "Тахеометр электронный", "iM-52", "IM52-771", -14),
        ("СКИ-005", "Рейка нивелирная 5 м", "Рейка нивелирная", "RGK TS-5", "TS5-0091", 400),
        ("СКИ-006", "Рейка нивелирная 5 м", "Рейка нивелирная", "RGK TS-5", "TS5-0092", 400),
        ("СКИ-007", "Рулетка 30 м", "Рулетка измерительная металлическая", "Р30У3К", "30-1102", 150),
        ("СКИ-008", "Рулетка 30 м", "Рулетка измерительная металлическая", "Р30У3К", "30-1103", 18),
        ("СКИ-009", "Рулетка 10 м", "Рулетка измерительная металлическая", "Р10УЗК", "10-2201", 300),
        ("СКИ-010", "Штангенциркуль ШЦ-I 150", "Штангенциркуль", "ШЦ-I-150-0.05", "SC-4410", 90),
        ("СКИ-011", "Шаблон сварщика УШС-3", "Шаблон сварщика УШС-3", "УШС-3", "USH-1201", 60),
        ("СКИ-012", "Шаблон сварщика УШС-3", "Шаблон сварщика УШС-3", "УШС-3", "USH-1202", None),
        ("СКИ-013", "Склерометр ОМШ-1", "Склерометр (молоток Шмидта)", "ОМШ-1", "OMSH-330", 210),
        ("СКИ-014", "Измеритель прочности ИПС-МГ4.03", "Измеритель прочности бетона ударно-импульсный", "ИПС-МГ4.03", "MG4-8811", 45),
        ("СКИ-015", "Измеритель защитного слоя ИПА-МГ4.01", "Измеритель защитного слоя бетона", "ИПА-МГ4.01", "IPA-2231", 260),
        ("СКИ-016", "Адгезиметр ПСО-5МГ4", "Адгезиметр отрывной", "ПСО-5МГ4", "PSO-1180", 95),
        ("СКИ-017", "Толщиномер покрытий МТ-2007", "Толщиномер покрытий", "МТ-2007", "MT-0455", -40),
        ("СКИ-018", "Термогигрометр ТКА-ПКМ", "Термогигрометр", "ТКА-ПКМ 20", "TKA-6612", 170),
        ("СКИ-019", "Пирометр Testo 830-T2", "Пирометр (бесконтактный термометр)", "830-T2", "T830-9091", 20),
        ("СКИ-020", "Набор щупов №2", "Набор щупов", "Щуп-2 кл.1", "SH-0034", 130),
        ("СКИ-021", "Уровень строительный 1000 мм", "Уровень строительный", "УС1-III-1000", "US-7001", 500),
        ("СКИ-022", "Уровень строительный 600 мм", "Уровень строительный", "УС1-III-600", "US-7002", 500),
        ("СКИ-023", "Мегаомметр ЭС0210/1", "Мегаомметр", "ЭС0210/1", "ES-1123", 80),
        ("СКИ-024", "Правило контрольное 2 м", "Правило контрольное 2 м", "ПК-2000", "PK-2000-11", None),
    ]

    created: dict[str, Instrument] = {}
    for inv_no, name, type_name, model, serial, days_left in instruments_spec:
        instrument = Instrument(
            inventory_no=inv_no,
            name=name,
            type_id=_type_id(session, type_name),
            model=model,
            serial_no=serial,
            manufacturer=None,
            status="warehouse",
            purchase_date=today - timedelta(days=900),
        )
        session.add(instrument)
        session.flush()
        created[inv_no] = instrument

        if days_left is not None:
            valid_until = today + timedelta(days=days_left)
            interval = instrument.type.verification_interval_months
            performed_on = valid_until - timedelta(days=interval * 30)
            add_verification(
                session,
                instrument.id,
                performed_on=performed_on,
                valid_until=valid_until,
                certificate_no=f"С-{inv_no[-3:]}/{performed_on.year}",
                organization="ФБУ «Тюменский ЦСМ»",
            )

    session.flush()

    placement = {
        site1: ["СКИ-001", "СКИ-003", "СКИ-005", "СКИ-006", "СКИ-007", "СКИ-010", "СКИ-011", "СКИ-013", "СКИ-018", "СКИ-021"],
        site2: ["СКИ-009", "СКИ-015"],
        site3: ["СКИ-016", "СКИ-020", "СКИ-019", "СКИ-023"],
    }
    for site, inv_numbers in placement.items():
        for inv_no in inv_numbers:
            issue_instrument(
                session,
                created[inv_no].id,
                site.id,
                happened_on=today - timedelta(days=30),
                person=site.responsible_name,
                doc_no=f"АКТ-{site.id:02d}/{inv_no[-3:]}",
                ignore_verification=True,
            )

    # прибор на поверке — типовая ситуация
    created["СКИ-002"].status = "verification"
    session.flush()
