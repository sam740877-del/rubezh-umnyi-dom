"""Дымовые тесты веб-интерфейса: страницы открываются, формы работают."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import security
from app.database import SessionLocal
from app.main import app
from app.seed import seed_demo


@pytest.fixture()
def client(session):
    """Клиент, вошедший администратором.

    Без входа система теперь никуда не пускает — это и есть смысл заставы.
    Вход делается настоящим: через страницу `/login`, а не подкладыванием
    печенья, чтобы дымовые тесты заодно стерегли саму процедуру входа.
    """
    seed_demo(session)  # заодно заводит демонстрационные учётные записи
    session.commit()

    with TestClient(app) as test_client:
        response = test_client.post(
            "/login",
            data={"login": "admin", "password": "admin"},
            follow_redirects=False,
        )
        assert response.status_code == 303, "вход не удался — дальше проверять нечего"
        yield test_client


@pytest.fixture()
def anon_client(session):
    """Клиент без входа — для проверки самой заставы."""
    seed_demo(session)
    session.commit()
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize(
    "url",
    ["/", "/instruments", "/sites", "/contracts", "/verifications", "/movements", "/types"],
)
def test_pages_open(client, url):
    response = client.get(url)
    assert response.status_code == 200
    assert "Учёт СКИ" in response.text


def test_instrument_detail_and_filters(client):
    assert client.get("/instruments?q=СКИ-001").status_code == 200
    assert client.get("/instruments?problem=1").status_code == 200
    assert client.get("/instruments/1").status_code == 200


def test_site_detail_shows_completeness(client):
    response = client.get("/sites/1")
    assert response.status_code == 200
    assert "укомплектованность" in response.text


def test_csv_exports(client):
    for url in ["/export/instruments.csv", "/export/site-1.csv", "/export/verifications.csv"]:
        response = client.get(url)
        assert response.status_code == 200
        assert response.text.startswith("﻿")


def test_issue_flow_via_forms(client):
    with SessionLocal() as db:
        from app.models import Instrument
        from app.services import verification_state

        free = [
            i
            for i in db.query(Instrument).filter(Instrument.status == "warehouse")
            if not verification_state(i).is_problem
        ]
        assert free, "в демо-данных есть свободный прибор с действующей поверкой"
        instrument_id = free[0].id

    response = client.post(
        f"/instruments/{instrument_id}/issue",
        data={"site_id": "1", "happened_on": date.today().isoformat(), "person": "Иванов"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "err=" not in response.headers["location"]

    response = client.post(
        f"/instruments/{instrument_id}/return",
        data={"happened_on": date.today().isoformat(), "new_status": "warehouse"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "err=" not in response.headers["location"]


def test_duplicate_inventory_number_rejected(client):
    """Повтор инвентарного номера отвергается, а введённое НЕ теряется.

    Раньше здесь была переадресация на пустую форму: человек набирал
    десяток полей, ошибался в номере — и всё стиралось. Заполнять заново
    из-за одной строки обидно, и это заметила приёмка.
    """
    response = client.post(
        "/instruments/new",
        data={
            "inventory_no": "СКИ-001",
            "name": "Дубль",
            "type_id": "1",
            "serial_no": "ЗАВ-9999",
            "manufacturer": "Завод-изготовитель",
        },
        follow_redirects=False,
    )

    assert response.status_code == 200, "форма должна вернуться, а не переадресовать"
    assert "уже занят" in response.text, "не сказано, что не так"
    # Введённое на месте — иначе человек набирает всё заново.
    assert "Дубль" in response.text
    assert "ЗАВ-9999" in response.text
    assert "Завод-изготовитель" in response.text


# --------------------------------------------------------------------------
# Найдено приёмкой интерфейса
# --------------------------------------------------------------------------


def test_missing_page_is_human_not_json(client) -> None:
    """Несуществующий адрес показывает страницу, а не голый JSON.

    Приёмка: `{"detail":"Not Found"}` без шапки, меню и пути назад —
    тупик, из которого выбираются только кнопкой «назад» в браузере.
    """
    response = client.get("/такой-страницы-точно-нет")

    assert response.status_code == 404, "код ответа должен остаться настоящим"
    assert "Такой страницы нет" in response.text
    assert "На сводку" in response.text, "нет пути назад"
    assert "detail" not in response.text[:200], "ответ всё ещё похож на JSON"


def test_summary_labels_do_not_fight_the_number(client) -> None:
    """Подписи на сводке не спорят с числом.

    Приёмка: «3 действующих участков», «1 заявок на согласовании» —
    читается как черновик. Подписи сделаны не зависящими от числа.
    """
    page = client.get("/").text

    assert "действующих участков" not in page
    assert "заявок на согласовании" not in page
    assert "участков действует" in page
    assert "заявок ждёт решения" in page


def test_model_is_not_repeated_in_the_name(client, session) -> None:
    """Модель не дописывается, если она уже есть в названии.

    Приёмка: «Тахеометр Sokkia iM-52 iM-52». В реестре заказчика модель
    часто входит в наименование, и слепое склеивание её удваивает.
    """
    from app.models import Instrument, InstrumentType

    kind = session.query(InstrumentType).first()
    session.add(
        Instrument(
            inventory_no="ДУБЛЬ-1",
            name="Нивелир Sokkia B40",
            model="Sokkia B40",
            type_id=kind.id,
            status="warehouse",
        )
    )
    session.commit()

    page = client.get("/warehouse").text

    # Сравниваем по ВИДИМОМУ тексту, а не по разметке: между названием
    # и моделью стоит <span>, и проверка на «Sokkia B40 Sokkia B40»
    # подряд ничего не ловила — подлог проходил мимо неё.
    import re

    visible = re.sub(r"<[^>]+>", "", page)
    visible = re.sub(r"\s+", " ", visible)

    assert "Нивелир Sokkia B40" in visible
    assert "Sokkia B40 Sokkia B40" not in visible, "модель удвоилась"


def test_new_instrument_form_has_no_preselected_type(client) -> None:
    """В форме нового прибора тип не выбран заранее.

    Приёмка: по умолчанию стоял первый по алфавиту, и прибор легко
    сохранить с чужим типом — а тип задаёт межповерочный интервал.
    """
    page = client.get("/instruments/new").text

    assert "— выберите тип —" in page, "нет пустого первого пункта"


def test_catalogs_can_be_corrected(client, session) -> None:
    """Справочники правятся, а не только заполняются.

    Приёмка: типы, договоры и участки были только на запись. Межповерочный
    интервал задаёт срок действия поверки для всех приборов типа —
    ошиблись при заведении, жили с ошибкой. Договор нельзя было закрыть
    или продлить, у участка сменить ответственного.
    """
    from app.models import Contract, InstrumentType, Site

    kind = session.query(InstrumentType).first()
    response = client.post(
        f"/types/{kind.id}/edit",
        data={
            "name": kind.name,
            "category": kind.category,
            "requires_verification": "1",
            "verification_interval_months": "24",
            "notes": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.expire_all()
    assert session.get(InstrumentType, kind.id).verification_interval_months == 24

    contract = session.query(Contract).first()
    response = client.post(
        f"/contracts/{contract.id}/edit",
        data={
            "number": contract.number,
            "title": contract.title,
            "customer": contract.customer,
            "signed_on": "",
            "valid_until": "",
            "status": "closed",
            "notes": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.expire_all()
    assert session.get(Contract, contract.id).status == "closed"

    site = session.query(Site).first()
    response = client.post(
        f"/sites/{site.id}/edit",
        data={
            "name": site.name,
            "address": "",
            "responsible_name": "Новый ответственный",
            "responsible_phone": "",
            "status": "mothballed",
            "annex_no": "",
            "annex_date": "",
            "notes": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.expire_all()
    changed = session.get(Site, site.id)
    assert changed.responsible_name == "Новый ответственный"
    assert changed.status == "mothballed"


def test_template_item_can_be_removed(client, session) -> None:
    """Позицию типового комплекта можно убрать.

    Приёмка: удаления не было вовсе — ошибочно добавленный тип оставался
    навсегда и попадал на каждый участок, куда применяли комплект.
    """
    from app.models import KitTemplate, KitTemplateItem

    template = session.query(KitTemplate).filter(KitTemplate.items.any()).first()
    assert template is not None, "в демо-данных есть комплект с позициями"
    item = template.items[0]
    item_id = item.id

    response = client.post(
        f"/templates/{template.id}/item/{item_id}/delete", follow_redirects=False
    )

    assert response.status_code == 303
    session.expire_all()
    assert session.get(KitTemplateItem, item_id) is None, "позиция осталась"


def test_template_with_items_is_not_deleted_by_accident(client, session) -> None:
    """Комплект с позициями не удаляется одним нажатием.

    Иначе случайное нажатие стирает работу целиком. Сперва уберите
    позиции — тогда видно, что удаляешь.
    """
    from app.models import KitTemplate

    template = session.query(KitTemplate).filter(KitTemplate.items.any()).first()
    template_id = template.id

    response = client.post(f"/templates/{template_id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert "err=" in response.headers["location"]
    session.expire_all()
    assert session.get(KitTemplate, template_id) is not None, "комплект удалён разом"
