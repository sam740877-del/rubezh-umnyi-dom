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
    response = client.post(
        "/instruments/new",
        data={"inventory_no": "СКИ-001", "name": "Дубль", "type_id": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "err=" in response.headers["location"]


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
