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
    seed_demo(session)
    security.create_user(
        session,
        "tester",
        "test-password",
        security.Role.ADMIN,
        display_name="Тестовый администратор",
        require_permission=False,
    )
    session.commit()

    with TestClient(app) as test_client:
        response = test_client.post(
            "/login",
            data={"login": "tester", "password": "test-password"},
            follow_redirects=False,
        )
        assert response.status_code == 303, "вход не удался — дальше проверять нечего"
        yield test_client


@pytest.fixture()
def anon_client(session):
    """Клиент без входа — для проверки самой заставы."""
    seed_demo(session)
    security.create_user(
        session,
        "tester",
        "test-password",
        security.Role.ADMIN,
        require_permission=False,
    )
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
