"""Веб-приложение учёта средств контроля и измерений (СКИ)."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import (
    attachments,
    backup,
    bot,
    max_api,
    notifications,
    security,
    services,
    session_cookie,
)
from app.database import get_session, init_db
from app.models import (
    ATTACHMENT_KINDS,
    BotInvite,
    BotLink,
    AUDIT_TYPES,
    Attachment,
    USER_ROLES,
    User,
    AuditLog,
    CATEGORIES,
    REQUEST_STATUSES,
    CONTRACT_STATUSES,
    INSTRUMENT_STATUSES,
    MOVEMENT_ACTIONS,
    SITE_STATUSES,
    VERIFICATION_KINDS,
    VERIFICATION_RESULTS,
    ChangeRequest,
    Contract,
    Instrument,
    InstrumentType,
    KitItem,
    KitTemplate,
    KitTemplateItem,
    Movement,
    Site,
)
from app.security import AccessDenied, CurrentUser, Permission, SecurityError
from app.services import BusinessError

BASE_DIR = Path(__file__).resolve().parent

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Учёт СКИ", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals.update(
    CATEGORIES=CATEGORIES,
    INSTRUMENT_STATUSES=INSTRUMENT_STATUSES,
    CONTRACT_STATUSES=CONTRACT_STATUSES,
    SITE_STATUSES=SITE_STATUSES,
    VERIFICATION_KINDS=VERIFICATION_KINDS,
    VERIFICATION_RESULTS=VERIFICATION_RESULTS,
    MOVEMENT_ACTIONS=MOVEMENT_ACTIONS,
    REQUEST_STATUSES=REQUEST_STATUSES,
    APPROVER_TITLE=services.APPROVER_TITLE,
    verification_state=services.verification_state,
    today=date.today,
)


# --------------------------------------------------------------------------
# Вспомогательное
# --------------------------------------------------------------------------


def parse_date(value: str | None, default: date | None = None) -> date | None:
    if not value:
        return default
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


def parse_decimal(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    return float(value.replace(",", ".").replace(" ", ""))


def parse_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    return int(value)


def redirect(url: str, *, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    separator = "&" if "?" in url else "?"
    if msg:
        url = f"{url}{separator}msg={msg}"
    elif err:
        url = f"{url}{separator}err={err}"
    return RedirectResponse(url, status_code=303)


def render(request: Request, template: str, **context) -> HTMLResponse:
    context.setdefault("msg", request.query_params.get("msg"))
    context.setdefault("err", request.query_params.get("err"))
    context.setdefault("section", current_section(request))
    actor = getattr(request.state, "user", None)
    context.setdefault("user", actor)
    context.setdefault("unread", getattr(request.state, "unread", 0))
    return templates.TemplateResponse(request, template, context)


def current_section(request: Request) -> str:
    """Какой раздел открыт — для подсветки в шапке.

    Считаем по первому куску пути, а не по полному совпадению: карточка
    прибора `/instruments/17` обязана подсвечивать «Приборы» так же, как
    и список. Иначе человек, провалившись в карточку, теряет из виду,
    где находится.
    """
    first = request.url.path.strip("/").split("/")[0]
    return first or "dashboard"


# --------------------------------------------------------------------------
# Вход и права
# --------------------------------------------------------------------------

#: Куда пускают без входа: сама страница входа, выход, статика и первичная
#: настройка. Список закрытый — всё остальное требует входа.
PUBLIC_PATHS = {"/login", "/logout", "/setup"}


def optional_user(request: Request, db: Session) -> CurrentUser | None:
    """Кто сейчас работает, или None.

    Роль читается из базы каждый раз, а не берётся из печенья: иначе
    администратор, отключивший человека или понизивший роль, ждал бы
    истечения чужого сеанса.
    """
    user_id = session_cookie.read(request.cookies.get(session_cookie.COOKIE_NAME))
    if user_id is None:
        return None
    found = db.get(User, user_id)
    if found is None or not found.is_active:
        return None
    return security.snapshot(found)


def require_user(request: Request, db: Session = Depends(get_session)) -> CurrentUser:
    """Требовать вошедшего. Без него — на страницу входа."""
    found = optional_user(request, db)
    if found is None:
        raise HTTPException(status_code=401, detail="Требуется вход")
    return found


def guard(permission: Permission):
    """Зависимость-застава: требует конкретное право.

    Проверка стоит на маршруте, а не только в шаблоне: спрятанная кнопка
    не есть право — форму можно отправить и мимо экрана (урок Б-1 «Заявок»).
    """

    def check(actor: CurrentUser = Depends(require_user)) -> CurrentUser:
        security.require(actor, permission)
        return actor

    return check


def _needs_setup() -> bool:
    """Нужен ли первичный запуск — когда учётных записей ещё нет."""
    from app.database import SessionLocal

    session = SessionLocal()
    try:
        return security.is_first_run(session)
    finally:
        session.close()


def _set_session_cookie(response, user_id: int) -> None:
    """Положить печенье сеанса.

    `httponly` — чтобы его не достал скрипт на странице; `samesite=lax` —
    чтобы чужой сайт не отправил форму от имени вошедшего. `secure` не
    ставим: система живёт по http внутри локальной сети конторы, и с этим
    флагом печенье просто не сохранилось бы.
    """
    response.set_cookie(
        session_cookie.COOKIE_NAME,
        session_cookie.issue(user_id),
        max_age=session_cookie.MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
    )


@app.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request, db: Session = Depends(get_session)):
    """Первичная настройка: завести первого администратора."""
    if not security.is_first_run(db):
        return RedirectResponse("/login", status_code=303)
    return render(request, "setup.html")


@app.post("/setup")
def setup_submit(
    login: str = Form(...),
    display_name: str = Form(""),
    password: str = Form(...),
    password2: str = Form(""),
    db: Session = Depends(get_session),
):
    """Завести первого администратора. Работает только на пустой базе."""
    if not security.is_first_run(db):
        return RedirectResponse("/login", status_code=303)
    try:
        security.validate_password(password, password2)
        created = security.create_user(
            db,
            login,
            password,
            security.Role.ADMIN,
            display_name=display_name,
            require_permission=False,
        )
        db.commit()
    except SecurityError as exc:
        return RedirectResponse(f"/setup?err={exc}", status_code=303)

    response = RedirectResponse("/?msg=Добро пожаловать", status_code=303)
    _set_session_cookie(response, created.id)
    return response


def _demo_users(db: Session) -> list[tuple[str, str]]:
    """Демонстрационные записи, если они заведены.

    Подсказку с паролями показываем только тогда, когда такие записи
    действительно существуют: на рабочей установке её не будет вовсе.
    """
    from app.seed import DEMO_USERS

    found = []
    for login, title, _role in DEMO_USERS:
        user = security.find_user(db, login)
        # Пароль совпадает с логином — значит, запись так и осталась
        # демонстрационной. Сменили пароль — подсказка про неё исчезает.
        if user and user.is_active and security.verify_password(login, user.password_hash):
            found.append((login, title))
    return found


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/", db: Session = Depends(get_session)):
    """Страница входа."""
    if security.is_first_run(db):
        return RedirectResponse("/setup", status_code=303)
    return render(request, "login.html", next=next, demo_users=_demo_users(db))


@app.post("/login")
def login_submit(
    login: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_session),
):
    """Проверить логин и пароль."""
    try:
        found = security.authenticate(db, login, password)
        db.commit()
    except SecurityError as exc:
        db.commit()  # неудачная попытка тоже записана в журнал
        return RedirectResponse(f"/login?err={exc}", status_code=303)

    # Возвращаем только на свои страницы: чужой адрес в next превратил бы
    # вход в переадресацию на любой сайт по ссылке из письма.
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(target, status_code=303)
    _set_session_cookie(response, found.id)
    return response


@app.get("/logout")
def logout():
    """Выйти из системы."""
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(session_cookie.COOKIE_NAME)
    return response


@app.get("/users", response_class=HTMLResponse)
def users_list(
    request: Request,
    actor: CurrentUser = Depends(guard(Permission.USER_MANAGE)),
    db: Session = Depends(get_session),
):
    """Учётные записи. Виден только администратору."""
    return render(request, "users.html", users=security.list_users(db), roles=USER_ROLES)


@app.post("/users/create")
def users_create(
    login: str = Form(...),
    password: str = Form(...),
    role: str = Form("keeper"),
    display_name: str = Form(""),
    email: str = Form(""),
    actor: CurrentUser = Depends(guard(Permission.USER_MANAGE)),
    db: Session = Depends(get_session),
):
    """Завести учётную запись."""
    try:
        security.create_user(
            db, login, password, role, display_name=display_name, email=email, actor=actor
        )
        db.commit()
    except SecurityError as exc:
        return RedirectResponse(f"/users?err={exc}", status_code=303)
    return RedirectResponse("/users?msg=Учётная запись заведена", status_code=303)


@app.post("/users/{user_id}/active")
def users_set_active(
    user_id: int,
    active: str = Form("1"),
    actor: CurrentUser = Depends(guard(Permission.USER_MANAGE)),
    db: Session = Depends(get_session),
):
    """Включить или отключить учётную запись."""
    try:
        security.set_active(db, user_id, active == "1", actor=actor)
        db.commit()
    except SecurityError as exc:
        return RedirectResponse(f"/users?err={exc}", status_code=303)
    return RedirectResponse("/users?msg=Готово", status_code=303)


@app.post("/users/{user_id}/role")
def users_change_role(
    user_id: int,
    role: str = Form(...),
    actor: CurrentUser = Depends(guard(Permission.USER_MANAGE)),
    db: Session = Depends(get_session),
):
    """Сменить роль."""
    try:
        security.change_role(db, user_id, role, actor=actor)
        db.commit()
    except SecurityError as exc:
        return RedirectResponse(f"/users?err={exc}", status_code=303)
    return RedirectResponse("/users?msg=Роль изменена", status_code=303)


@app.post("/users/{user_id}/password")
def users_set_password(
    user_id: int,
    password: str = Form(...),
    password2: str = Form(""),
    actor: CurrentUser = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Задать пароль: свой — любой, чужой — только администратор."""
    try:
        security.set_password(db, user_id, password, confirmation=password2, actor=actor)
        db.commit()
    except SecurityError as exc:
        return RedirectResponse(f"/users?err={exc}", status_code=303)
    return RedirectResponse("/users?msg=Пароль изменён", status_code=303)


@app.middleware("http")
async def login_wall(request: Request, call_next):
    """Пускать в систему только вошедших.

    Застава общая, а не на каждом маршруте: забыть повесить её на новый
    экран куда легче, чем вписать путь в PUBLIC_PATHS. Урок Б-1 «Заявок»
    ровно об этом — любой путь мимо заставы открывает дыру молча.

    Заодно кладёт снимок пользователя в `request.state`, чтобы каждый
    шаблон знал, кто работает, без лишнего запроса.
    """
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static"):
        return await call_next(request)

    from app.database import SessionLocal

    session = SessionLocal()
    try:
        actor = optional_user(request, session)
    finally:
        session.close()

    if actor is None:
        if _needs_setup():
            return RedirectResponse("/setup", status_code=303)
        return RedirectResponse(f"/login?next={path}", status_code=303)

    request.state.user = actor
    # Счётчик берём той же сессией, что и пользователя: отдельный запрос
    # на каждую страницу ради значка — плата, которую видно на списках.
    session = SessionLocal()
    try:
        request.state.unread = notifications.unread_count(session, actor.id)
    finally:
        session.close()
    return await call_next(request)


@app.exception_handler(AccessDenied)
async def access_denied_handler(request: Request, exc: AccessDenied):
    """Не хватает прав — говорим по-человечески, а не пятисотой ошибкой."""
    return RedirectResponse(f"/?err={exc}", status_code=303)


@app.exception_handler(HTTPException)
async def unauthorized_handler(request: Request, exc: HTTPException):
    """401 от require_user превращаем в переход на страницу входа."""
    if exc.status_code == 401:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    raise exc


# --------------------------------------------------------------------------
# Вложения
# --------------------------------------------------------------------------


@app.post("/attachments/{target_type}/{target_id}")
async def attachment_upload(
    target_type: str,
    target_id: int,
    file: UploadFile = File(...),
    kind: str = Form("other"),
    notes: str = Form(""),
    back: str = Form(""),
    actor: CurrentUser = Depends(guard(Permission.INSTRUMENT_EDIT)),
    db: Session = Depends(get_session),
):
    """Приложить файл к объекту."""
    target = back or f"/instruments/{target_id}"
    try:
        data = await file.read()
        attachments.attach(
            db,
            target_type,
            target_id,
            data=data,
            original_name=file.filename or "файл",
            kind=kind,
            source="web",
            uploaded_by=actor.name,
            notes=notes.strip() or None,
        )
        db.commit()
    except attachments.AttachmentError as exc:
        return redirect(target, err=str(exc))
    return redirect(target, msg="Документ приложен")


@app.get("/attachments/{attachment_id}/download")
def attachment_download(
    attachment_id: int,
    actor: CurrentUser = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Отдать файл вложения.

    Имя, под которым файл скачивается, берём исходное: человек искал
    «Свидетельство о поверке.pdf», а не «certificate_2026-09-06_...».
    """
    record = db.get(Attachment, attachment_id)
    if record is None:
        return redirect("/instruments", err="Вложение не найдено")

    path = attachments.to_full_path(record.stored_path)
    if not path.exists():
        # Запись есть, файла нет — это надо сказать прямо, а не отдать
        # пустой ответ: человек ищет документ, который ему нужен сейчас.
        return redirect(
            f"/{record.target_type}s/{record.target_id}",
            err=f"Файл «{record.original_name}» не найден в хранилище",
        )
    return FileResponse(path, filename=record.original_name)


@app.post("/attachments/{attachment_id}/delete")
def attachment_delete(
    attachment_id: int,
    back: str = Form(""),
    actor: CurrentUser = Depends(guard(Permission.INSTRUMENT_EDIT)),
    db: Session = Depends(get_session),
):
    """Открепить документ. Файл в хранилище остаётся."""
    record = db.get(Attachment, attachment_id)
    target = back or (
        f"/instruments/{record.target_id}" if record else "/instruments"
    )
    try:
        attachments.delete(db, attachment_id, deleted_by=actor.name)
        db.commit()
    except attachments.AttachmentError as exc:
        return redirect(target, err=str(exc))
    return redirect(target, msg="Документ откреплён")


# --------------------------------------------------------------------------
# Уведомления
# --------------------------------------------------------------------------


@app.get("/notifications", response_class=HTMLResponse)
def notifications_view(
    request: Request,
    unread_only: str = "",
    actor: CurrentUser = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Ящик уведомлений вошедшего."""
    return render(
        request,
        "notifications.html",
        entries=notifications.for_user(db, actor.id, only_unread=bool(unread_only)),
        filters={"unread_only": unread_only},
    )


@app.post("/notifications/read")
def notifications_mark_read(
    actor: CurrentUser = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Отметить все свои уведомления прочитанными."""
    count = notifications.mark_read(db, actor.id)
    db.commit()
    return redirect("/notifications", msg=f"Отмечено прочитанными: {count}")


@app.post("/notifications/scan")
def notifications_scan(
    actor: CurrentUser = Depends(guard(Permission.AUDIT_VIEW)),
    db: Session = Depends(get_session),
):
    """Пройти по парку и разослать напоминания о поверках.

    Пока запускается кнопкой. На этапе 2 то же самое будет делать
    расписание на сервере — механизм от этого не меняется.
    """
    created = notifications.scan_verifications(db)
    db.commit()
    if created:
        return redirect("/notifications", msg=f"Создано уведомлений: {created}")
    return redirect("/notifications", msg="Новых напоминаний нет — всё уже разослано")


# --------------------------------------------------------------------------
# Резервные копии
# --------------------------------------------------------------------------


@app.get("/backups", response_class=HTMLResponse)
def backups_view(
    request: Request,
    actor: CurrentUser = Depends(guard(Permission.USER_MANAGE)),
):
    """Состояние резервных копий. Видит администратор."""
    return render(
        request,
        "backups.html",
        backups=backup.list_backups(),
        warning=backup.staleness(),
        folder=backup.backup_dir(),
    )


@app.post("/backups/create")
def backups_create(
    actor: CurrentUser = Depends(guard(Permission.USER_MANAGE)),
    db: Session = Depends(get_session),
):
    """Снять копию базы сейчас."""
    try:
        info = backup.create_backup(db)
        db.commit()
    except backup.BackupError as exc:
        return redirect("/backups", err=str(exc))
    return redirect("/backups", msg=f"Копия снята: {info.size_text}")


# --------------------------------------------------------------------------
# Бот в MAX
# --------------------------------------------------------------------------


@app.get("/bot", response_class=HTMLResponse)
def bot_view(
    request: Request,
    actor: CurrentUser = Depends(guard(Permission.USER_MANAGE)),
    db: Session = Depends(get_session),
):
    """Приглашения и подключённые. Видит администратор."""
    return render(
        request,
        "bot.html",
        configured=max_api.is_configured(),
        ttl_days=bot.INVITE_TTL_DAYS,
        now=datetime.now(),
        invites=db.scalars(
            select(BotInvite).order_by(BotInvite.created_at.desc()).limit(50)
        ).all(),
        links=db.scalars(
            select(BotLink).order_by(BotLink.is_active.desc(), BotLink.linked_at.desc())
        ).all(),
        sites=db.scalars(
            select(Site).where(Site.status == "active").order_by(Site.name)
        ).all(),
        users=security.list_users(db, only_active=True),
    )


@app.post("/bot/invite")
def bot_invite_create(
    site_id: str = Form(""),
    user_id: str = Form(""),
    intended_for: str = Form(""),
    actor: CurrentUser = Depends(guard(Permission.USER_MANAGE)),
    db: Session = Depends(get_session),
):
    """Выпустить код-приглашение."""
    try:
        invite = bot.create_invite(
            db,
            site_id=int(site_id) if site_id else None,
            user_id=int(user_id) if user_id else None,
            intended_for=intended_for,
            created_by=actor.name,
        )
        db.commit()
    except bot.BotError as exc:
        return redirect("/bot", err=str(exc))
    return redirect("/bot", msg=f"Код: {invite.code} — продиктуйте его человеку")


@app.post("/bot/links/{link_id}/revoke")
def bot_link_revoke(
    link_id: int,
    actor: CurrentUser = Depends(guard(Permission.USER_MANAGE)),
    db: Session = Depends(get_session),
):
    """Отключить доступ в бот."""
    try:
        bot.revoke_link(db, link_id, actor=actor.name)
        db.commit()
    except bot.BotError as exc:
        return redirect("/bot", err=str(exc))
    return redirect("/bot", msg="Доступ отключён")


def csv_response(filename: str, header: list[str], rows: list[list]) -> StreamingResponse:
    """CSV с BOM и «;» — открывается в Excel без плясок с кодировкой."""
    buffer = io.StringIO()
    buffer.write("﻿")
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------
# Главная
# --------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_session)):
    return render(request, "index.html", data=services.dashboard(db))


# --------------------------------------------------------------------------
# Приборы
# --------------------------------------------------------------------------


@app.get("/warehouse", response_class=HTMLResponse)
def warehouse_view(
    request: Request,
    type_id: str = "",
    only: str = "",
    db: Session = Depends(get_session),
):
    """Склад: что лежит и что из этого готово к выдаче."""
    report = services.warehouse_report(db, type_id=int(type_id) if type_id else None)

    rows = report.rows
    if only == "ready":
        rows = [r for r in rows if r.can_issue]
    elif only == "blocked":
        rows = [r for r in rows if not r.can_issue]

    return render(
        request,
        "warehouse.html",
        report=report,
        rows=rows,
        types=db.scalars(select(InstrumentType).order_by(InstrumentType.name)).all(),
        filters={"type_id": type_id, "only": only},
    )


@app.get("/instruments", response_class=HTMLResponse)
def instruments_list(
    request: Request,
    q: str = "",
    status: str = "",
    type_id: str = "",
    site_id: str = "",
    problem: str = "",
    db: Session = Depends(get_session),
):
    query = select(Instrument).options(
        selectinload(Instrument.type),
        selectinload(Instrument.verifications),
        selectinload(Instrument.current_site),
    )
    if q:
        pattern = f"%{q.strip()}%"
        query = query.where(
            or_(
                Instrument.inventory_no.ilike(pattern),
                Instrument.name.ilike(pattern),
                Instrument.serial_no.ilike(pattern),
                Instrument.model.ilike(pattern),
            )
        )
    if status:
        query = query.where(Instrument.status == status)
    if type_id:
        query = query.where(Instrument.type_id == int(type_id))
    if site_id:
        query = query.where(Instrument.current_site_id == int(site_id))

    items = db.scalars(query.order_by(Instrument.inventory_no)).all()
    if problem:
        items = [i for i in items if services.verification_state(i).is_problem]

    return render(
        request,
        "instruments.html",
        instruments=items,
        types=db.scalars(select(InstrumentType).order_by(InstrumentType.name)).all(),
        sites=db.scalars(select(Site).order_by(Site.name)).all(),
        filters={"q": q, "status": status, "type_id": type_id, "site_id": site_id, "problem": problem},
    )


@app.get("/instruments/new", response_class=HTMLResponse)
def instrument_new_form(request: Request, db: Session = Depends(get_session)):
    return render(
        request,
        "instrument_form.html",
        instrument=None,
        types=db.scalars(select(InstrumentType).order_by(InstrumentType.name)).all(),
    )


@app.post("/instruments/new")
def instrument_create(
    inventory_no: str = Form(...),
    name: str = Form(...),
    type_id: int = Form(...),
    model: str = Form(""),
    manufacturer: str = Form(""),
    serial_no: str = Form(""),
    manufactured_year: str = Form(""),
    status: str = Form("warehouse"),
    purchase_date: str = Form(""),
    price: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_session),
    actor: CurrentUser = Depends(guard(Permission.INSTRUMENT_EDIT)),
):
    instrument = Instrument(
        inventory_no=inventory_no.strip(),
        name=name.strip(),
        type_id=type_id,
        model=model.strip() or None,
        manufacturer=manufacturer.strip() or None,
        serial_no=serial_no.strip() or None,
        manufactured_year=parse_int(manufactured_year),
        status=status,
        purchase_date=parse_date(purchase_date),
        price=parse_decimal(price),
        notes=notes.strip() or None,
    )
    db.add(instrument)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return redirect("/instruments/new", err=f"Инвентарный номер {inventory_no} уже занят")
    return redirect(f"/instruments/{instrument.id}", msg="Прибор добавлен")


@app.get("/instruments/{instrument_id}", response_class=HTMLResponse)
def instrument_detail(request: Request, instrument_id: int, db: Session = Depends(get_session)):
    instrument = db.get(Instrument, instrument_id)
    if instrument is None:
        return redirect("/instruments", err="Прибор не найден")
    return render(
        request,
        "instrument_detail.html",
        instrument=instrument,
        state=services.verification_state(instrument),
        sites=db.scalars(select(Site).where(Site.status == "active").order_by(Site.name)).all(),
        types=db.scalars(select(InstrumentType).order_by(InstrumentType.name)).all(),
        attachments=attachments.for_target(db, "instrument", instrument_id),
        attachment_kinds=ATTACHMENT_KINDS,
    )


@app.post("/instruments/{instrument_id}/edit")
def instrument_edit(
    instrument_id: int,
    name: str = Form(...),
    type_id: int = Form(...),
    model: str = Form(""),
    manufacturer: str = Form(""),
    serial_no: str = Form(""),
    manufactured_year: str = Form(""),
    status: str = Form(...),
    purchase_date: str = Form(""),
    price: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_session),
    actor: CurrentUser = Depends(guard(Permission.INSTRUMENT_EDIT)),
):
    instrument = db.get(Instrument, instrument_id)
    if instrument is None:
        return redirect("/instruments", err="Прибор не найден")
    if status != "in_use" and instrument.current_site_id is not None:
        return redirect(
            f"/instruments/{instrument_id}",
            err="Прибор числится на участке — оформите возврат, чтобы сменить статус",
        )
    if status == "in_use" and instrument.current_site_id is None:
        return redirect(
            f"/instruments/{instrument_id}",
            err="Статус «На участке» ставится только через выдачу на участок",
        )

    instrument.name = name.strip()
    instrument.type_id = type_id
    instrument.model = model.strip() or None
    instrument.manufacturer = manufacturer.strip() or None
    instrument.serial_no = serial_no.strip() or None
    instrument.manufactured_year = parse_int(manufactured_year)
    instrument.status = status
    instrument.purchase_date = parse_date(purchase_date)
    instrument.price = parse_decimal(price)
    instrument.notes = notes.strip() or None
    db.commit()
    return redirect(f"/instruments/{instrument_id}", msg="Изменения сохранены")


@app.post("/instruments/{instrument_id}/issue")
def instrument_issue(
    instrument_id: int,
    site_id: int = Form(...),
    happened_on: str = Form(""),
    person: str = Form(""),
    doc_no: str = Form(""),
    notes: str = Form(""),
    ignore_verification: str = Form(""),
    db: Session = Depends(get_session),
    actor: CurrentUser = Depends(guard(Permission.INSTRUMENT_ISSUE)),
):
    try:
        services.issue_instrument(
            db,
            instrument_id,
            site_id,
            happened_on=parse_date(happened_on, date.today()),
            person=person.strip() or None,
            doc_no=doc_no.strip() or None,
            notes=notes.strip() or None,
            ignore_verification=bool(ignore_verification),
        )
        db.commit()
    except BusinessError as error:
        db.rollback()
        return redirect(f"/instruments/{instrument_id}", err=str(error))
    return redirect(f"/instruments/{instrument_id}", msg="Прибор выдан на участок")


@app.post("/instruments/{instrument_id}/return")
def instrument_return(
    instrument_id: int,
    happened_on: str = Form(""),
    person: str = Form(""),
    doc_no: str = Form(""),
    notes: str = Form(""),
    new_status: str = Form("warehouse"),
    db: Session = Depends(get_session),
    actor: CurrentUser = Depends(guard(Permission.INSTRUMENT_RETURN)),
):
    try:
        services.return_instrument(
            db,
            instrument_id,
            happened_on=parse_date(happened_on, date.today()),
            person=person.strip() or None,
            doc_no=doc_no.strip() or None,
            notes=notes.strip() or None,
            new_status=new_status,
        )
        db.commit()
    except BusinessError as error:
        db.rollback()
        return redirect(f"/instruments/{instrument_id}", err=str(error))
    return redirect(f"/instruments/{instrument_id}", msg="Прибор возвращён с участка")


@app.post("/instruments/{instrument_id}/verification")
def instrument_add_verification(
    instrument_id: int,
    performed_on: str = Form(...),
    kind: str = Form("verification"),
    valid_until: str = Form(""),
    certificate_no: str = Form(""),
    organization: str = Form(""),
    result: str = Form("ok"),
    cost: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_session),
    actor: CurrentUser = Depends(guard(Permission.VERIFICATION_ADD)),
):
    try:
        services.add_verification(
            db,
            instrument_id,
            performed_on=parse_date(performed_on),
            kind=kind,
            valid_until=parse_date(valid_until),
            certificate_no=certificate_no.strip() or None,
            organization=organization.strip() or None,
            result=result,
            cost=parse_decimal(cost),
            notes=notes.strip() or None,
        )
        db.commit()
    except BusinessError as error:
        db.rollback()
        return redirect(f"/instruments/{instrument_id}", err=str(error))
    return redirect(f"/instruments/{instrument_id}", msg="Поверка зарегистрирована")


# --------------------------------------------------------------------------
# Договоры
# --------------------------------------------------------------------------


@app.get("/contracts", response_class=HTMLResponse)
def contracts_list(request: Request, db: Session = Depends(get_session)):
    contracts = db.scalars(
        select(Contract).options(selectinload(Contract.sites)).order_by(Contract.number)
    ).all()
    return render(request, "contracts.html", contracts=contracts)


@app.post("/contracts/new")
def contract_create(
    number: str = Form(...),
    title: str = Form(...),
    customer: str = Form(...),
    signed_on: str = Form(""),
    valid_until: str = Form(""),
    status: str = Form("active"),
    notes: str = Form(""),
    db: Session = Depends(get_session),
    actor: CurrentUser = Depends(guard(Permission.CONTRACT_EDIT)),
):
    contract = Contract(
        number=number.strip(),
        title=title.strip(),
        customer=customer.strip(),
        signed_on=parse_date(signed_on),
        valid_until=parse_date(valid_until),
        status=status,
        notes=notes.strip() or None,
    )
    db.add(contract)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return redirect("/contracts", err=f"Договор {number} уже есть в базе")
    return redirect("/contracts", msg="Договор добавлен")


# --------------------------------------------------------------------------
# Участки и комплектность
# --------------------------------------------------------------------------


@app.get("/sites", response_class=HTMLResponse)
def sites_list(request: Request, db: Session = Depends(get_session)):
    sites = db.scalars(select(Site).options(selectinload(Site.contract)).order_by(Site.name)).all()
    reports = {site.id: services.site_completeness(db, site.id) for site in sites}
    return render(
        request,
        "sites.html",
        sites=sites,
        reports=reports,
        contracts=db.scalars(select(Contract).order_by(Contract.number)).all(),
    )


@app.post("/sites/new")
def site_create(
    name: str = Form(...),
    contract_id: int = Form(...),
    address: str = Form(""),
    responsible_name: str = Form(""),
    responsible_phone: str = Form(""),
    status: str = Form("active"),
    annex_no: str = Form(""),
    annex_date: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_session),
    actor: CurrentUser = Depends(guard(Permission.SITE_EDIT)),
):
    site = Site(
        name=name.strip(),
        contract_id=contract_id,
        address=address.strip() or None,
        responsible_name=responsible_name.strip() or None,
        responsible_phone=responsible_phone.strip() or None,
        status=status,
        annex_no=annex_no.strip() or None,
        annex_date=parse_date(annex_date),
        notes=notes.strip() or None,
    )
    db.add(site)
    db.commit()
    return redirect(f"/sites/{site.id}", msg="Участок создан")


@app.get("/sites/{site_id}", response_class=HTMLResponse)
def site_detail(request: Request, site_id: int, db: Session = Depends(get_session)):
    try:
        report = services.site_completeness(db, site_id)
    except BusinessError as error:
        return redirect("/sites", err=str(error))
    return render(
        request,
        "site_detail.html",
        report=report,
        site=report.site,
        types=db.scalars(select(InstrumentType).order_by(InstrumentType.name)).all(),
        templates=db.scalars(
            select(KitTemplate).options(selectinload(KitTemplate.items)).order_by(KitTemplate.name)
        ).all(),
        free_instruments=db.scalars(
            select(Instrument)
            .options(selectinload(Instrument.type), selectinload(Instrument.verifications))
            .where(Instrument.current_site_id.is_(None), Instrument.status == "warehouse")
            .order_by(Instrument.inventory_no)
        ).all(),
    )


@app.post("/sites/{site_id}/kit")
def site_kit_add(
    site_id: int,
    type_id: int = Form(...),
    required_qty: int = Form(1),
    notes: str = Form(""),
    db: Session = Depends(get_session),
    actor: CurrentUser = Depends(guard(Permission.KIT_EDIT)),
):
    existing = db.scalar(
        select(KitItem).where(KitItem.site_id == site_id, KitItem.type_id == type_id)
    )
    if existing:
        existing.required_qty = required_qty
        existing.notes = notes.strip() or None
        db.commit()
        return redirect(f"/sites/{site_id}", msg="Позиция комплекта обновлена")

    db.add(
        KitItem(site_id=site_id, type_id=type_id, required_qty=required_qty, notes=notes.strip() or None)
    )
    db.commit()
    return redirect(f"/sites/{site_id}", msg="Позиция добавлена в комплект")


@app.post("/sites/{site_id}/kit/{item_id}/delete")
def site_kit_delete(
    site_id: int,
    item_id: int,
    db: Session = Depends(get_session),
    actor: CurrentUser = Depends(guard(Permission.KIT_EDIT)),
):
    item = db.get(KitItem, item_id)
    if item and item.site_id == site_id:
        db.delete(item)
        db.commit()
    return redirect(f"/sites/{site_id}", msg="Позиция удалена")


@app.post("/sites/{site_id}/issue")
def site_issue(
    site_id: int,
    instrument_id: int = Form(...),
    happened_on: str = Form(""),
    person: str = Form(""),
    doc_no: str = Form(""),
    ignore_verification: str = Form(""),
    db: Session = Depends(get_session),
    actor: CurrentUser = Depends(guard(Permission.INSTRUMENT_ISSUE)),
):
    try:
        services.issue_instrument(
            db,
            instrument_id,
            site_id,
            happened_on=parse_date(happened_on, date.today()),
            person=person.strip() or None,
            doc_no=doc_no.strip() or None,
            ignore_verification=bool(ignore_verification),
        )
        db.commit()
    except BusinessError as error:
        db.rollback()
        return redirect(f"/sites/{site_id}", err=str(error))
    return redirect(f"/sites/{site_id}", msg="Прибор выдан на участок")


# --------------------------------------------------------------------------
# Типы, поверки, журнал
# --------------------------------------------------------------------------


@app.get("/types", response_class=HTMLResponse)
def types_list(request: Request, db: Session = Depends(get_session)):
    types = db.scalars(
        select(InstrumentType).options(selectinload(InstrumentType.instruments)).order_by(
            InstrumentType.category, InstrumentType.name
        )
    ).all()
    return render(request, "types.html", types=types)


@app.post("/types/new")
def type_create(
    name: str = Form(...),
    category: str = Form("other"),
    requires_verification: str = Form(""),
    verification_interval_months: int = Form(12),
    notes: str = Form(""),
    db: Session = Depends(get_session),
    actor: CurrentUser = Depends(guard(Permission.CATALOG_EDIT)),
):
    db.add(
        InstrumentType(
            name=name.strip(),
            category=category,
            requires_verification=bool(requires_verification),
            verification_interval_months=verification_interval_months,
            notes=notes.strip() or None,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return redirect("/types", err=f"Тип «{name}» уже есть в справочнике")
    return redirect("/types", msg="Тип добавлен")


@app.get("/verifications", response_class=HTMLResponse)
def verifications_view(request: Request, days: int = 60, db: Session = Depends(get_session)):
    rows = services.expiring_instruments(db, days=days)
    return render(request, "verifications.html", rows=rows, days=days)


@app.get("/movements", response_class=HTMLResponse)
def movements_view(
    request: Request,
    site_id: str = "",
    instrument_id: str = "",
    db: Session = Depends(get_session),
):
    query = select(Movement).options(
        selectinload(Movement.instrument), selectinload(Movement.site)
    )
    if site_id:
        query = query.where(Movement.site_id == int(site_id))
    if instrument_id:
        query = query.where(Movement.instrument_id == int(instrument_id))
    movements = db.scalars(
        query.order_by(Movement.happened_on.desc(), Movement.id.desc()).limit(500)
    ).all()
    return render(
        request,
        "movements.html",
        movements=movements,
        sites=db.scalars(select(Site).order_by(Site.name)).all(),
        filters={"site_id": site_id, "instrument_id": instrument_id},
    )


@app.get("/audit", response_class=HTMLResponse)
def audit_view(
    request: Request,
    audit_type: str = "",
    who: str = "",
    db: Session = Depends(get_session),
    actor: CurrentUser = Depends(guard(Permission.AUDIT_VIEW)),
):
    """Журнал действий: кто и что делал в системе.

    Виден администратору и главному инженеру. Кладовщику журнал не нужен:
    он в нём действующее лицо, а не проверяющий.
    """
    query = select(AuditLog)
    if audit_type:
        query = query.where(AuditLog.audit_type == audit_type)
    if who.strip():
        query = query.where(AuditLog.actor.icontains(who.strip()))
    entries = db.scalars(
        query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(500)
    ).all()
    return render(
        request,
        "audit.html",
        entries=entries,
        audit_types=AUDIT_TYPES,
        filters={"audit_type": audit_type, "who": who},
    )


# --------------------------------------------------------------------------
# Выгрузки CSV
# --------------------------------------------------------------------------


@app.get("/export/instruments.csv")
def export_instruments(db: Session = Depends(get_session)):
    instruments = db.scalars(
        select(Instrument)
        .options(
            selectinload(Instrument.type),
            selectinload(Instrument.verifications),
            selectinload(Instrument.current_site),
        )
        .order_by(Instrument.inventory_no)
    ).all()
    rows = []
    for i in instruments:
        state = services.verification_state(i)
        rows.append(
            [
                i.inventory_no,
                i.name,
                i.type.name,
                i.model or "",
                i.serial_no or "",
                i.status_label,
                i.current_site.name if i.current_site else "",
                i.verification_valid_until.strftime("%d.%m.%Y") if i.verification_valid_until else "",
                state.label,
            ]
        )
    return csv_response(
        "instruments.csv",
        ["Инв. №", "Наименование", "Тип", "Модель", "Зав. №", "Статус", "Участок", "Поверка до", "Состояние поверки"],
        rows,
    )


@app.get("/export/site-{site_id}.csv")
def export_site(site_id: int, db: Session = Depends(get_session)):
    report = services.site_completeness(db, site_id)
    rows = []
    for row in report.rows:
        rows.append(
            [
                row.type.name,
                row.required_qty,
                row.fact_qty,
                row.deficit,
                ", ".join(i.inventory_no for i in row.instruments),
            ]
        )
    for instrument in report.extra:
        rows.append([f"{instrument.type.name} (вне комплекта)", 0, 1, 0, instrument.inventory_no])
    return csv_response(
        f"site-{site_id}-completeness.csv",
        ["Тип прибора", "Требуется", "Факт", "Дефицит", "Инвентарные номера"],
        rows,
    )


@app.get("/export/verifications.csv")
def export_verifications(days: int = 60, db: Session = Depends(get_session)):
    rows = []
    for instrument, state in services.expiring_instruments(db, days=days):
        rows.append(
            [
                instrument.inventory_no,
                instrument.name,
                instrument.current_site.name if instrument.current_site else "",
                instrument.verification_valid_until.strftime("%d.%m.%Y")
                if instrument.verification_valid_until
                else "",
                state.label,
                state.days_left if state.days_left is not None else "",
            ]
        )
    return csv_response(
        "verifications.csv",
        ["Инв. №", "Наименование", "Участок", "Поверка до", "Состояние", "Дней осталось"],
        rows,
    )


# --------------------------------------------------------------------------
# Типовые комплекты
# --------------------------------------------------------------------------


@app.get("/templates", response_class=HTMLResponse)
def templates_list(request: Request, db: Session = Depends(get_session)):
    items = db.scalars(
        select(KitTemplate).options(selectinload(KitTemplate.items)).order_by(KitTemplate.name)
    ).all()
    return render(
        request,
        "kit_templates.html",
        templates_list=items,
        types=db.scalars(select(InstrumentType).order_by(InstrumentType.name)).all(),
    )


@app.post("/templates/new")
def template_create(
    name: str = Form(...), notes: str = Form(""), db: Session = Depends(get_session),
    actor: CurrentUser = Depends(guard(Permission.KIT_EDIT)),
):
    db.add(KitTemplate(name=name.strip(), notes=notes.strip() or None))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return redirect("/templates", err=f"Комплект «{name}» уже есть")
    return redirect("/templates", msg="Типовой комплект создан")


@app.post("/templates/{template_id}/item")
def template_add_item(
    template_id: int,
    type_id: int = Form(...),
    required_qty: int = Form(1),
    db: Session = Depends(get_session),
    actor: CurrentUser = Depends(guard(Permission.KIT_EDIT)),
):
    existing = db.scalar(
        select(KitTemplateItem).where(
            KitTemplateItem.template_id == template_id, KitTemplateItem.type_id == type_id
        )
    )
    if existing:
        existing.required_qty = required_qty
    else:
        db.add(
            KitTemplateItem(template_id=template_id, type_id=type_id, required_qty=required_qty)
        )
    db.commit()
    return redirect("/templates", msg="Позиция сохранена")


@app.post("/sites/{site_id}/apply-template")
def site_apply_template(
    site_id: int, template_id: int = Form(...), db: Session = Depends(get_session),
    actor: CurrentUser = Depends(guard(Permission.KIT_EDIT)),
):
    try:
        changed = services.apply_kit_template(db, site_id, template_id)
        db.commit()
    except BusinessError as error:
        db.rollback()
        return redirect(f"/sites/{site_id}", err=str(error))
    return redirect(f"/sites/{site_id}", msg=f"Комплект применён, позиций добавлено: {changed}")


# --------------------------------------------------------------------------
# Заявки на перемещение
# --------------------------------------------------------------------------


@app.get("/requests", response_class=HTMLResponse)
def requests_list(request: Request, db: Session = Depends(get_session)):
    items = db.scalars(
        select(ChangeRequest)
        .options(
            selectinload(ChangeRequest.instrument),
            selectinload(ChangeRequest.from_site),
            selectinload(ChangeRequest.to_site),
        )
        .order_by(ChangeRequest.status != "new", ChangeRequest.requested_on.desc())
        .limit(200)
    ).all()
    return render(
        request,
        "requests.html",
        requests=items,
        sites=db.scalars(select(Site).where(Site.status == "active").order_by(Site.name)).all(),
        placed_instruments=db.scalars(
            select(Instrument)
            .options(selectinload(Instrument.current_site))
            .where(Instrument.current_site_id.is_not(None))
            .order_by(Instrument.inventory_no)
        ).all(),
    )


@app.post("/requests/new")
def request_create(
    instrument_id: int = Form(...),
    to_site_id: int = Form(...),
    requested_by: str = Form(...),
    reason: str = Form(""),
    db: Session = Depends(get_session),
):
    try:
        services.create_change_request(
            db,
            instrument_id,
            to_site_id,
            requested_by,
            reason=reason.strip() or None,
        )
        db.commit()
    except BusinessError as error:
        db.rollback()
        return redirect("/requests", err=str(error))
    return redirect("/requests", msg="Заявка подана и ждёт согласования")


@app.post("/requests/{request_id}/approve")
def request_approve(
    request_id: int,
    decided_by: str = Form(...),
    comment: str = Form(""),
    ignore_verification: str = Form(""),
    db: Session = Depends(get_session),
    actor: CurrentUser = Depends(guard(Permission.REQUEST_DECIDE)),
):
    try:
        services.approve_change_request(
            db,
            request_id,
            decided_by,
            comment=comment.strip() or None,
            ignore_verification=bool(ignore_verification),
        )
        db.commit()
    except BusinessError as error:
        db.rollback()
        return redirect("/requests", err=str(error))
    return redirect("/requests", msg="Заявка согласована, прибор перемещён")


@app.post("/requests/{request_id}/reject")
def request_reject(
    request_id: int,
    decided_by: str = Form(...),
    comment: str = Form(""),
    db: Session = Depends(get_session),
    actor: CurrentUser = Depends(guard(Permission.REQUEST_DECIDE)),
):
    try:
        services.reject_change_request(db, request_id, decided_by, comment)
        db.commit()
    except BusinessError as error:
        db.rollback()
        return redirect("/requests", err=str(error))
    return redirect("/requests", msg="Заявка отклонена")
