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


def test_menus_open_by_click_not_only_hover(client) -> None:
    """Списки в шапке раскрываются нажатием, а не только наведением.

    Найдено владельцем на живом стенде: нажатие не делало НИЧЕГО —
    списки открывались лишь настоящим наведением мыши. А на кнопку
    тянутся нажать; на телефоне и планшете наведения нет вовсе,
    и там разделы были бы недоступны совсем.

    Проверяем разметку, а не поведение браузера: без обработчика
    и без правила `.menu.open` нажатие снова станет пустым.
    """
    page = client.get("/").text
    css = client.get("/static/style.css").text

    assert "menu > button" in page or ".menu > button" in page, (
        "нет обработчика нажатия по кнопкам списков"
    )
    assert "classList.toggle('open')" in page, "нажатие не переключает список"

    # Раскрытия по наведению быть НЕ ДОЛЖНО: два способа спорили друг
    # с другом. Escape снимал класс, но список висел, пока курсор
    # оставался над кнопкой; наведение раскрывало список само; а если
    # открыть один кнопкой и провести мышью над соседним, оба
    # показывались разом и накладывались.
    assert ".menu:hover .drop" not in css, (
        "осталось раскрытие по наведению — оно спорит с раскрытием по нажатию"
    )
    assert "Escape" in page, "список не закрывается по Escape"
    assert ".menu.open .drop" in css, (
        "в стилях нет правила для раскрытого списка — нажатие ничего не покажет"
    )

    # Обрезки у навигации быть НЕ ДОЛЖНО. Из-за неё дефект и жил: класс
    # проставлялся, список рисовался — и тут же срезался высотой полосы
    # в 31 точку. Человек видел пустоту и решал, что кнопка не работает.
    # `overflow-x: auto` режет и по вертикали, отдельного «только вбок» нет.
    import re

    правило = re.search(r"header\.top nav \{[^}]*\}", css)
    assert правило, "не нашлось правило навигации"
    # Комментарии вырезаем: слово «overflow» стоит и в объяснении, почему
    # обрезки быть не должно, — искать его в тексте значит ловить себя же.
    без_комментариев = re.sub(r"/\*.*?\*/", "", правило.group(0), flags=re.S)
    assert "overflow" not in без_комментариев, (
        "у навигации снова стоит overflow — выпадающие списки будут срезаны"
    )


def test_dark_theme_is_available(client) -> None:
    """Тему можно переключить, и она не оставляет светлых пятен.

    Цвета живут переменными: тёмная тема подменяет значения, вёрстка
    остаётся прежней. Правило одно — новый цвет заводится в палитре,
    иначе в тёмной теме он останется светлым и выпадет из общего вида.
    """
    import re

    page = client.get("/").text
    css = client.get("/static/style.css").text

    # Проверяем САМИ ЦВЕТА, а не наличие строки: `data-theme="dark"`
    # встречается в файле восемь раз, и вырезание палитры такую проверку
    # не роняло — подлог проходил мимо неё.
    assert re.search(r'\[data-theme="dark"\]\s*\{[^}]*--bg\s*:', css), (
        "нет тёмной палитры: переменные цветов не переопределены"
    )
    assert "switchTheme" in page, "нет переключателя"
    assert "localStorage" in page, "выбор темы не запоминается"

    # Иконки набора Lucide, а не эмодзи: эмодзи рисуются шрифтом системы,
    # выглядят по-разному на разных машинах и не красятся цветом темы.
    assert 'stroke="currentColor"' in page, "иконка темы не наследует цвет"
    assert "🌙" not in page and "☀" not in page, "остались эмодзи вместо иконок"


def test_no_light_colours_leak_into_dark_theme(client) -> None:
    """Подложки и рамки заданы переменными, а не числами.

    Найдено при разборе: подсветка открытого раздела была белой подложкой
    с тёмным текстом. В тёмной теме подложка потемнела, а текст остался
    тёмным — раздел стал нечитаем.
    """
    import re

    css = client.get("/static/style.css").text
    # Вырезаем палитры: там числа законны.
    без_палитры = re.sub(r'(:root|\[data-theme="dark"\])\s*\{[^}]*\}', "", css)

    # Ищем подложки и рамки, заданные числом.
    прямые = re.findall(
        r"(?:background|border(?:-color)?)\s*:[^;]*?(#[0-9a-fA-F]{3,6})", без_палитры
    )
    # Красный значок непрочитанных и рамка демо-подсказки на светлой
    # странице входа — намеренные исключения.
    лишние = {c.lower() for c in прямые} - {"#d9534f", "#b9c9dc", "#fff"}

    assert not лишние, f"цвета мимо палитры — в тёмной теме станут пятнами: {лишние}"


# --------------------------------------------------------------------------
# Защита от кривого ввода
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "/instruments?type_id=абв",
        "/instruments?site_id=--",
        "/instruments?type_id=-5",
        "/instruments?type_id=999999999999999999999",
        "/warehouse?type_id=x",
        "/movements?site_id=нет",
        "/movements?instrument_id=1;DROP TABLE",
        "/verifications?days=абв",
        "/verifications?days=-100",
        "/verifications?days=99999999999",
        "/audit?audit_type=выдуманный",
        "/instruments/0",
        "/instruments/999999",
        "/sites/-1",
    ],
)
def test_broken_input_does_not_crash(client, url) -> None:
    """Кривое значение в адресе не роняет страницу.

    Правило донора (БПО, `core/settings.py`): «испорченное руками значение
    должно превращаться в значение по умолчанию, а не в падение программы».

    Найдено запуском, а не догадкой: четыре страницы отдавали пятисотую
    ошибку от одной буквы в адресе. Букву наберут случайно, скопируют
    из письма с переносом строки или подставят нарочно — во всех трёх
    случаях человек должен увидеть страницу, а не поломку.
    """
    response = client.get(url)

    assert response.status_code < 500, (
        f"страница упала от кривого значения: {response.status_code}"
    )
    # И не голым JSON, как отвечал FastAPI на неразобранное число.
    assert "detail" not in response.text[:120], (
        "вместо страницы показаны внутренности"
    )


def test_settings_are_editable(client, session) -> None:
    """Горизонт предупреждения правится, а не зашит числом.

    Прежде 30 дней стояло константой в двух файлах — `services` и
    `notifications`. Два места с одним смыслом уже начинали разъезжаться,
    а вопрос заказчику «за сколько дней предупреждать» открыт с самого
    начала. Правильный ответ на него — не число, а настройка.
    """
    from app import settings as app_settings

    assert app_settings.warn_days(session) == 30, "не то значение по умолчанию"

    response = client.post(
        "/settings",
        data={"warn_days": "45", "backup_interval_days": "1", "company_name": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.expire_all()

    assert app_settings.warn_days(session) == 45, "настройка не сохранилась"


def test_broken_setting_falls_back_to_default(client, session) -> None:
    """Испорченная настройка не роняет систему.

    Значение могли править руками в базе. Буквы вместо числа — не повод
    отказать в работе: «настройка не то место, ради которого клиент
    останется без программы».
    """
    from app import settings as app_settings

    app_settings.set_value(session, app_settings.WARN_DAYS, "месяц")
    session.commit()

    assert app_settings.warn_days(session) == 30, "кривое значение не заменилось"
    assert client.get("/verifications").status_code == 200
    assert client.get("/").status_code == 200


def test_absurd_setting_is_refused_with_explanation(client) -> None:
    """Бессмысленное значение не принимается, и человеку сказано почему.

    Молча взять умолчание за спиной у человека — хуже отказа: он решит,
    что сохранил, и уйдёт.
    """
    response = client.post(
        "/settings",
        data={"warn_days": "-40", "backup_interval_days": "1", "company_name": ""},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "err=" in response.headers["location"], "отказ прошёл молча"


# --------------------------------------------------------------------------
# Перечень СИ — документ для заказчика
# --------------------------------------------------------------------------


def test_instrument_list_shows_what_customer_asks_for(client) -> None:
    """Перечень СИ отвечает на вопрос заказчика, а не на наш.

    Заказчик спрашивает: какие приборы работают на моём объекте и до
    какого срока они поверены. Это НЕ сверка комплектности («сколько
    нужно и сколько есть») — там другой вопрос и другой документ.

    Номер свидетельства обязателен: без него строка «поверен до» ничем
    не подтверждена, и заказчик её не примет.
    """
    page = client.get("/sites/1/instrument-list").text

    assert "Перечень средств измерений, применяемых на объекте" in page
    for столбец in ("Инв. №", "Зав. №", "Поверен до", "Свидетельство"):
        assert столбец in page, f"нет столбца «{столбец}»"

    # Реквизиты договора — документ без них не документ.
    assert "Договор:" in page
    assert "Составлен:" in page


def test_instrument_list_warns_about_expired(client, session) -> None:
    """Просроченная поверка в перечне названа прямо.

    Перечень с просроченной поверкой заказчику лучше не отдавать,
    и узнать об этом надо до печати, а не от заказчика.
    """
    from app.models import Instrument
    from app.services import issue_instrument

    просроченный = (
        session.query(Instrument)
        .filter(Instrument.status == "warehouse")
        .first()
    )
    issue_instrument(session, просроченный.id, 1, ignore_verification=True)
    session.commit()

    page = client.get("/sites/1/instrument-list").text

    assert "лучше не отдавать" in page, "не предупредили о просроченных"


def test_instrument_list_prints_as_document(client) -> None:
    """На бумагу идёт документ, а не снимок экрана.

    Шапка системы и кнопки при печати не нужны, а тёмная тема съедает
    картридж и читается хуже.
    """
    css = client.get("/static/style.css").text

    assert "@media print" in css, "нет стилей печати"
    assert "header.top, .no-print" in css, "шапка системы попадёт на бумагу"
    assert "display: table-header-group" in css, (
        "заголовок таблицы не повторяется — вторая страница станет "
        "набором чисел без объяснения"
    )


def test_instrument_list_csv_has_same_data(client) -> None:
    """Тот же перечень выгружается таблицей — когда нужен не документ, а данные."""
    response = client.get("/export/site-1-list.csv")

    assert response.status_code == 200
    assert response.text.startswith("﻿"), "нет метки кодировки для Excel"
    assert "Свидетельство" in response.text
