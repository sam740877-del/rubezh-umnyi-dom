r"""Сторож доступа: чужого не видно, чужим не распорядиться.

Какое обещание стережём
------------------------

«У каждого свой вход, и каждый может ровно то, что ему положено». Кладовщик
выдаёт приборы, но не решает заявки; главный инженер решает заявки, но не
ведёт склад; учётные записи заводит только администратор.

Как обещание ломается в жизни
------------------------------

Ровно одним способом — **проверкой права в шаблоне вместо операции**. Дефект
Б-1 «Заявок»: карточка пользователя не давала создать мастера без привязки,
но ядро её не требовало, и «любой путь мимо карточки — импорт, миграция,
правка базы руками — открывал бы утечку молча». Спрятанная кнопка не есть
право: форму отправляют и мимо экрана.

Второй способ — **fail-open в проверке**: когда неизвестная роль или пустое
действующее лицо проваливаются мимо всех веток и получают доступ. У «Заявок»
это стоило того, что мастер видел заявки всей конторы.

Чем доказано
-------------

Запуском: настоящий вход настоящим паролем, затем попытка сделать то, на что
права нет. Проверяется ответ сервера и состояние базы — не текст страницы.
"""

from __future__ import annotations

from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app import security
from app.main import app
from app.models import Instrument, User
from app.security import AccessDenied, Permission, Role
from app.seed import seed_demo


#: Пароль демонстрационных записей совпадает с логином (см. app/seed.py).
DEMO_PASSWORD = {"admin": "admin", "keeper": "keeper", "chief": "chief"}


@pytest.fixture()
def users(session):
    """Три роли, по одному человеку на каждую.

    Берём демонстрационные записи, которые заводит `seed_demo`: так сторож
    заодно проверяет, что они действительно работают и что роли у них
    расставлены верно.
    """
    seed_demo(session)
    session.commit()
    return session


def login_as(client: TestClient, login: str) -> None:
    """Войти под указанным логином."""
    response = client.post(
        "/login",
        data={"login": login, "password": DEMO_PASSWORD[login]},
        follow_redirects=False,
    )
    assert response.status_code == 303, f"вход под {login} не удался"


# --------------------------------------------------------------------------
# Матрица прав
# --------------------------------------------------------------------------


def test_keeper_cannot_decide_requests() -> None:
    """Кладовщик не решает заявки — их согласует главный инженер (ответ 46)."""
    assert not security.can(Role.KEEPER, Permission.REQUEST_DECIDE)


def test_chief_does_not_run_the_warehouse() -> None:
    """Главный инженер не выдаёт приборы: выдачу оформляет МОЛ (ответ 43)."""
    assert not security.can(Role.CHIEF, Permission.INSTRUMENT_ISSUE)


def test_only_admin_manages_users() -> None:
    """Учётные записи заводит только администратор."""
    assert security.can(Role.ADMIN, Permission.USER_MANAGE)
    assert not security.can(Role.KEEPER, Permission.USER_MANAGE)
    assert not security.can(Role.CHIEF, Permission.USER_MANAGE)


def test_unknown_role_gets_no_extra_rights() -> None:
    """Неизвестная роль не падает и не даёт прав администратора.

    Приём донора: `parse_role` неизвестного значения возвращает самую
    безопасную роль, а не самую сильную.
    """
    assert not security.can("кто-то-новый", Permission.USER_MANAGE)


def test_no_actor_means_no_rights() -> None:
    """Без действующего лица прав нет никаких — fail-closed.

    Именно здесь ломался донор: пустая привязка проваливалась мимо всех
    веток и получала доступ ко всему.
    """
    with pytest.raises(AccessDenied):
        security.require(None, Permission.INSTRUMENT_ISSUE)


# --------------------------------------------------------------------------
# Застава на маршрутах, а не в шаблоне
# --------------------------------------------------------------------------


def test_anonymous_is_sent_to_login(users) -> None:
    """Без входа систему не видно вовсе."""
    with TestClient(app) as client:
        response = client.get("/instruments", follow_redirects=False)
        assert response.status_code == 303
        assert "/login" in response.headers["location"]


def test_keeper_cannot_approve_request_through_the_form(users) -> None:
    """Кладовщик не согласует заявку, даже отправив форму напрямую.

    Кнопки на экране он не видит, но это не защита: форму можно отправить
    и мимо экрана. Проверяем именно такой путь.
    """
    from app.services import create_change_request, issue_instrument
    from app.models import Site

    session = users
    instrument = session.query(Instrument).filter(Instrument.status == "warehouse").first()
    sites = session.query(Site).filter(Site.status == "active").limit(2).all()
    issue_instrument(session, instrument.id, sites[0].id, ignore_verification=True)
    request = create_change_request(session, instrument.id, sites[1].id, "Мастер")
    request_id = request.id
    session.commit()

    # Сперва убеждаемся, что заявка вообще согласуема: иначе тест зеленел бы
    # от любой посторонней помехи — например, от просроченной поверки — и
    # снятую заставу пропустил бы. Ровно это здесь и случилось при проверке
    # зубов, поэтому проверка разделена на две.
    with TestClient(app) as client:
        login_as(client, "keeper")
        denied = client.post(
            f"/requests/{request_id}/approve",
            data={"decided_by": "Кладовщик", "ignore_verification": "1"},
            follow_redirects=False,
        )
    assert denied.status_code == 303
    # Адрес приходит закодированным процентами — сравниваем по раскодированному.
    reason = unquote(denied.headers["location"])
    # Текст отказа зависит от раздела и называет, кто им ведает: сверяем
    # по сути («заявки решает главный инженер»), а не по дословной фразе,
    # иначе сторож ломается от каждой правки формулировки.
    assert "главный инженер" in reason.lower(), (
        f"отказ пришёл не по правам, а по другой причине: {reason}"
    )

    session.expire_all()
    assert session.get(type(request), request_id).status == "new", (
        "заявка изменилась, хотя прав не было"
    )

    # А главный инженер ту же заявку согласует — иначе застава просто
    # ломала бы работу вместо того, чтобы разграничивать её.
    with TestClient(app) as client:
        login_as(client, "chief")
        allowed = client.post(
            f"/requests/{request_id}/approve",
            data={"decided_by": "Главный инженер", "ignore_verification": "1"},
            follow_redirects=False,
        )
    assert "err=" not in allowed.headers["location"], (
        f"главному инженеру не дали согласовать заявку: "
        f"{unquote(allowed.headers['location'])}"
    )


def test_keeper_cannot_open_users_page(users) -> None:
    """Кладовщик не попадает на страницу учётных записей."""
    with TestClient(app) as client:
        login_as(client, "keeper")
        response = client.get("/users", follow_redirects=False)

    assert response.status_code == 303
    assert "err=" in response.headers["location"]


def test_admin_can_open_users_page(users) -> None:
    """Администратор туда попадает — иначе застава просто ломает систему."""
    with TestClient(app) as client:
        login_as(client, "admin")
        response = client.get("/users")

    assert response.status_code == 200
    assert "Учётные записи" in response.text


# --------------------------------------------------------------------------
# Пароли и вход
# --------------------------------------------------------------------------


def test_password_is_not_stored_as_written(users) -> None:
    """В базе лежит не пароль, а его свёртка с солью."""
    user = users.query(User).filter(User.login == "admin").one()

    assert "admin" not in user.password_hash
    assert user.password_hash.startswith("pbkdf2_sha256$")
    assert security.verify_password("admin", user.password_hash)
    assert not security.verify_password("другой", user.password_hash)


def test_same_password_gives_different_hashes() -> None:
    """Соль у каждого своя: одинаковые пароли не видны как одинаковые."""
    assert security.hash_password("одинаковый") != security.hash_password("одинаковый")


def test_disabled_user_cannot_log_in(users) -> None:
    """Отключённая запись в систему не пускает."""
    admin = security.snapshot(users.query(User).filter(User.login == "admin").one())
    keeper = users.query(User).filter(User.login == "keeper").one()
    security.set_active(users, keeper.id, False, actor=admin)
    users.commit()

    with TestClient(app) as client:
        response = client.post(
            "/login",
            data={"login": "keeper", "password": "keeper"},
            follow_redirects=False,
        )

    assert "err=" in response.headers["location"]


def test_last_admin_cannot_be_disabled(users) -> None:
    """Последнего администратора отключить нельзя — иначе не войдёт никто."""
    admin_record = users.query(User).filter(User.login == "admin").one()
    admin = security.snapshot(admin_record)

    with pytest.raises(security.SecurityError):
        security.set_active(users, admin_record.id, False, actor=admin)


def test_last_admin_cannot_be_demoted(users) -> None:
    """И понизить в роли тоже нельзя — по той же причине."""
    admin_record = users.query(User).filter(User.login == "admin").one()
    admin = security.snapshot(admin_record)

    with pytest.raises(security.SecurityError):
        security.change_role(users, admin_record.id, Role.KEEPER, actor=admin)


def test_users_are_disabled_not_deleted() -> None:
    """Удаления учётных записей нет вовсе.

    Идентификатор человека — это вся история его действий: кто выдал
    прибор, кто согласовал заявку. Удаление стёрло бы летопись, поэтому
    в модуле есть `set_active`, но нет `delete_user`.
    """
    assert not hasattr(security, "delete_user")


def test_brute_force_is_slowed_down(users) -> None:
    """После нескольких неудач вход закрывается на время."""
    security.reset_login_failures("keeper")
    for _ in range(security.LOGIN_MAX_ATTEMPTS):
        security.register_login_failure("keeper")

    assert security.login_lock_seconds_left("keeper") > 0

    with pytest.raises(security.SecurityError) as exc:
        security.authenticate(users, "keeper", "keeper")
    assert "попыток" in str(exc.value)

    security.reset_login_failures("keeper")


def test_login_is_written_to_the_journal(users) -> None:
    """Вход и неудачная попытка попадают в журнал действий."""
    from app import audit

    security.authenticate(users, "admin", "admin")
    users.commit()

    actions = [e.action for e in audit.recent(users)]
    assert "Вход в систему" in actions

    with pytest.raises(security.SecurityError):
        security.authenticate(users, "admin", "неверный")
    users.commit()

    actions = [e.action for e in audit.recent(users)]
    assert "Неудачная попытка входа" in actions


# --------------------------------------------------------------------------
# Демонстрационный режим
# --------------------------------------------------------------------------


def test_demo_hint_shows_only_with_demo_users(users) -> None:
    """Подсказка с паролями видна, только пока записи демонстрационные.

    Смысл в том, чтобы на рабочей установке её не было вовсе: подсказанный
    логин с известным паролем — это инструкция для постороннего, а не
    помощь своему (урок Л-14 «Заявок»).
    """
    with TestClient(app) as client:
        response = client.get("/login")

    assert response.status_code == 200
    assert "Демонстрационный режим" in response.text
    assert "admin" in response.text


def test_demo_hint_disappears_after_password_change(users) -> None:
    """Сменили пароль — подсказка про эту запись пропадает.

    Проверяем не текст на экране, а поведение: подсказка привязана к тому,
    совпадает ли пароль с логином, а не к какой-то отдельной пометке,
    которую можно забыть снять.
    """
    session = users
    admin_record = session.query(User).filter(User.login == "admin").one()
    admin = security.snapshot(admin_record)

    for login in ("admin", "keeper", "chief"):
        record = session.query(User).filter(User.login == login).one()
        security.set_password(session, record.id, "рабочий-пароль-2026", actor=admin)
    session.commit()

    with TestClient(app) as client:
        response = client.get("/login")

    assert "Демонстрационный режим" not in response.text, (
        "подсказка с паролями осталась после их смены"
    )


def test_secret_key_is_not_in_the_repository() -> None:
    """Ключ подписи сеансов не хранится в репозитории.

    Зная его, любой подделает себе печенье администратора — вход по
    паролю станет необязательным. Файл однажды туда уже попал, поэтому
    правило получило сторожа, а не только строку в .gitignore.
    """
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.splitlines()

    forbidden = [
        name
        for name in tracked
        if Path(name).name in (".secret_key", ".env", "settings.local.json")
        or name.startswith("logs/")
        or name.startswith("storage/")
        or name.startswith("backups/")
    ]
    assert not forbidden, f"в репозитории лежит то, чему там не место: {forbidden}"


def test_keeper_does_not_see_decision_buttons(users) -> None:
    """Кладовщик не видит кнопок, которые ему нельзя нажимать.

    Найдено приёмкой: кнопки «Согласовать» и «Отклонить» показывались
    всем. Кладовщик нажимал — и его выбрасывало на Сводку с отказом.
    Кнопка приглашала, а за нажатие наказывали.

    Скрытая кнопка при этом НЕ считается защитой: право проверяется
    и на маршруте (см. сторож выше). Здесь стережём другое — чтобы
    человеку не предлагали чужую работу.
    """
    from app.services import create_change_request, issue_instrument
    from app.models import Site

    session = users
    instrument = session.query(Instrument).filter(Instrument.status == "warehouse").first()
    sites = session.query(Site).filter(Site.status == "active").limit(2).all()
    issue_instrument(session, instrument.id, sites[0].id, ignore_verification=True)
    create_change_request(session, instrument.id, sites[1].id, "Мастер")
    session.commit()

    with TestClient(app) as client:
        login_as(client, "keeper")
        page = client.get("/requests").text

    assert "Согласовать" not in page, "кладовщику показали чужую кнопку"
    assert "Решает главный инженер" in page, "не сказано, кто решает заявки"

    with TestClient(app) as client:
        login_as(client, "chief")
        page = client.get("/requests").text

    assert "Согласовать" in page, "у главного инженера пропала его кнопка"


def test_chief_does_not_see_warehouse_buttons(users) -> None:
    """Главному инженеру не показывают рабочее место кладовщика.

    Найдено приёмкой: на Складе ему выводились живые кнопки «Выдать»,
    а в карточке прибора — формы выдачи, возврата и регистрации поверки.
    При этом подзаголовок Склада прямо гласил «которыми кладовщик может
    распорядиться» — страница сама признавала, что она не для него.
    """
    session = users
    instrument = session.query(Instrument).filter(Instrument.status == "warehouse").first()

    with TestClient(app) as client:
        login_as(client, "chief")
        warehouse = client.get("/warehouse").text
        card = client.get(f"/instruments/{instrument.id}").text

    assert "К выдаче" not in warehouse, "инженеру показали кнопку выдачи"
    assert "Выдачу оформляет кладовщик" in warehouse, "не сказано, кто выдаёт"
    assert "Выдать на участок" not in card, "инженеру показали форму выдачи"
    assert "Зарегистрировать" not in card, "инженеру показали регистрацию поверки"

    with TestClient(app) as client:
        login_as(client, "keeper")
        warehouse = client.get("/warehouse").text
        card = client.get(f"/instruments/{instrument.id}").text

    assert "К выдаче" in warehouse, "у кладовщика пропала кнопка выдачи"
    assert "Выдать на участок" in card, "у кладовщика пропала форма выдачи"
