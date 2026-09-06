"""Веб-приложение учёта средств контроля и измерений (СКИ)."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import services
from app.database import get_session, init_db
from app.models import (
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
    return templates.TemplateResponse(request, template, context)


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
def site_kit_delete(site_id: int, item_id: int, db: Session = Depends(get_session)):
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
    name: str = Form(...), notes: str = Form(""), db: Session = Depends(get_session)
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
    site_id: int, template_id: int = Form(...), db: Session = Depends(get_session)
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
):
    try:
        services.reject_change_request(db, request_id, decided_by, comment)
        db.commit()
    except BusinessError as error:
        db.rollback()
        return redirect("/requests", err=str(error))
    return redirect("/requests", msg="Заявка отклонена")
