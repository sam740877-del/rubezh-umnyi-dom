"""Модели предметной области: договоры, участки, комплекты, приборы, поверки, движение."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _sql_list(values) -> str:
    """Закрытый список значений для CHECK-ограничения.

    Берём ключи прямо из словаря подписей: если статус добавят в словарь,
    но забудут про ограничение, база начнёт отвергать законное значение —
    и это заметят сразу, а не на данных клиента.
    """
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


class Base(DeclarativeBase):
    pass


# --- Справочники значений (храним строками, чтобы БД читалась «глазами») ---

CATEGORIES = {
    "geodesy": "Геодезические приборы",
    "adhesion": "Адгезиметры и прочностной контроль",
    "vik": "Визуально-измерительный контроль (ВИК)",
    "climate": "Климат и влажность",
    "electro": "Электроизмерения",
    "other": "Прочее",
}

INSTRUMENT_STATUSES = {
    "warehouse": "На складе",
    "in_use": "На участке",
    "verification": "На поверке",
    "repair": "В ремонте",
    "written_off": "Списан",
}

# Статусы, при которых прибор нельзя выдать на участок
BLOCKED_FOR_ISSUE = ("verification", "repair", "written_off")

CONTRACT_STATUSES = {"active": "Действует", "suspended": "Приостановлен", "closed": "Закрыт"}
SITE_STATUSES = {"active": "Действует", "mothballed": "Законсервирован", "closed": "Закрыт"}
VERIFICATION_KINDS = {"verification": "Поверка", "calibration": "Калибровка", "maintenance": "ТО"}
VERIFICATION_RESULTS = {"ok": "Годен", "fail": "Брак"}
MOVEMENT_ACTIONS = {"issue": "Выдача", "return": "Возврат"}

REQUEST_STATUSES = {
    "new": "На согласовании",
    "approved": "Согласована",
    "rejected": "Отклонена",
}


class InstrumentType(Base):
    """Тип средства контроля: «Нивелир», «Адгезиметр отрывной», «Штангенциркуль» и т.п."""

    __tablename__ = "instrument_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    category: Mapped[str] = mapped_column(String(32), default="other")
    requires_verification: Mapped[bool] = mapped_column(default=True)
    verification_interval_months: Mapped[int] = mapped_column(Integer, default=12)
    notes: Mapped[str | None] = mapped_column(Text)

    instruments: Mapped[list["Instrument"]] = relationship(back_populates="type")

    __table_args__ = (
        CheckConstraint("verification_interval_months > 0", name="ck_type_interval_positive"),
    )

    @property
    def category_label(self) -> str:
        return CATEGORIES.get(self.category, self.category)

    def __repr__(self) -> str:
        return f"<InstrumentType {self.name}>"


class Contract(Base):
    """Договор с заказчиком — в нём «живёт» комплект приборов."""

    __tablename__ = "contracts"
    __table_args__ = (
        CheckConstraint(
            "status IN " + _sql_list(CONTRACT_STATUSES),
            name="ck_contract_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(80), unique=True)
    title: Mapped[str] = mapped_column(String(255))
    customer: Mapped[str] = mapped_column(String(255))
    signed_on: Mapped[date | None] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="active")
    notes: Mapped[str | None] = mapped_column(Text)

    sites: Mapped[list["Site"]] = relationship(
        back_populates="contract", cascade="all, delete-orphan"
    )

    @property
    def status_label(self) -> str:
        return CONTRACT_STATUSES.get(self.status, self.status)


class Site(Base):
    """Участок строительства (объект) в рамках договора."""

    __tablename__ = "sites"
    __table_args__ = (
        CheckConstraint(
            "status IN " + _sql_list(SITE_STATUSES),
            name="ck_site_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(String(255))
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id", ondelete="CASCADE"))
    responsible_name: Mapped[str | None] = mapped_column(String(160))
    responsible_phone: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="active")
    # Комплект закреплён приложением к договору — храним его реквизиты
    annex_no: Mapped[str | None] = mapped_column(String(80))
    annex_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)

    contract: Mapped[Contract] = relationship(back_populates="sites")
    kit_items: Mapped[list["KitItem"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )
    instruments: Mapped[list["Instrument"]] = relationship(back_populates="current_site")

    @property
    def status_label(self) -> str:
        return SITE_STATUSES.get(self.status, self.status)


class KitItem(Base):
    """Позиция комплекта, требуемого на участке по договору."""

    __tablename__ = "kit_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    type_id: Mapped[int] = mapped_column(ForeignKey("instrument_types.id", ondelete="RESTRICT"))
    required_qty: Mapped[int] = mapped_column(Integer, default=1)
    notes: Mapped[str | None] = mapped_column(Text)

    site: Mapped[Site] = relationship(back_populates="kit_items")
    type: Mapped[InstrumentType] = relationship()

    __table_args__ = (
        UniqueConstraint("site_id", "type_id", name="uq_kit_site_type"),
        CheckConstraint("required_qty > 0", name="ck_kit_qty_positive"),
    )


class Instrument(Base):
    """Конкретный экземпляр прибора (по инвентарному номеру)."""

    __tablename__ = "instruments"
    __table_args__ = (
        CheckConstraint(
            "status IN " + _sql_list(INSTRUMENT_STATUSES),
            name="ck_instrument_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    inventory_no: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    type_id: Mapped[int] = mapped_column(ForeignKey("instrument_types.id", ondelete="RESTRICT"))
    model: Mapped[str | None] = mapped_column(String(160))
    manufacturer: Mapped[str | None] = mapped_column(String(160))
    serial_no: Mapped[str | None] = mapped_column(String(120))
    manufactured_year: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="warehouse")
    current_site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id", ondelete="SET NULL"))
    purchase_date: Mapped[date | None] = mapped_column(Date)
    price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    notes: Mapped[str | None] = mapped_column(Text)

    type: Mapped[InstrumentType] = relationship(back_populates="instruments")
    current_site: Mapped[Site | None] = relationship(back_populates="instruments")
    verifications: Mapped[list["Verification"]] = relationship(
        back_populates="instrument", cascade="all, delete-orphan", order_by="Verification.performed_on.desc()"
    )
    movements: Mapped[list["Movement"]] = relationship(
        back_populates="instrument", cascade="all, delete-orphan", order_by="Movement.happened_on.desc()"
    )

    @property
    def status_label(self) -> str:
        return INSTRUMENT_STATUSES.get(self.status, self.status)

    @property
    def last_verification(self) -> "Verification | None":
        """Последняя годная поверка/калибровка."""
        valid = [v for v in self.verifications if v.result == "ok" and v.valid_until]
        return max(valid, key=lambda v: v.valid_until) if valid else None

    @property
    def verification_valid_until(self) -> date | None:
        last = self.last_verification
        return last.valid_until if last else None

    def __repr__(self) -> str:
        return f"<Instrument {self.inventory_no} {self.name}>"


class Verification(Base):
    """Запись о поверке / калибровке / ТО прибора."""

    __tablename__ = "verifications"
    __table_args__ = (
        CheckConstraint(
            "result IN " + _sql_list(VERIFICATION_RESULTS),
            name="ck_verification_result",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16), default="verification")
    performed_on: Mapped[date] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date)
    certificate_no: Mapped[str | None] = mapped_column(String(120))
    organization: Mapped[str | None] = mapped_column(String(255))
    result: Mapped[str] = mapped_column(String(8), default="ok")
    cost: Mapped[float | None] = mapped_column(Numeric(12, 2))
    notes: Mapped[str | None] = mapped_column(Text)

    instrument: Mapped[Instrument] = relationship(back_populates="verifications")

    @property
    def kind_label(self) -> str:
        return VERIFICATION_KINDS.get(self.kind, self.kind)

    @property
    def result_label(self) -> str:
        return VERIFICATION_RESULTS.get(self.result, self.result)


class Movement(Base):
    """Журнал выдачи прибора на участок и возврата с участка."""

    __tablename__ = "movements"

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"))
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    action: Mapped[str] = mapped_column(String(8))
    happened_on: Mapped[date] = mapped_column(Date)
    person: Mapped[str | None] = mapped_column(String(160))
    doc_no: Mapped[str | None] = mapped_column(String(120))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    instrument: Mapped[Instrument] = relationship(back_populates="movements")
    site: Mapped[Site] = relationship()

    @property
    def action_label(self) -> str:
        return MOVEMENT_ACTIONS.get(self.action, self.action)


class KitTemplate(Base):
    """Типовой комплект: собирается один раз и применяется к новым участкам."""

    __tablename__ = "kit_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    notes: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list["KitTemplateItem"]] = relationship(
        back_populates="template", cascade="all, delete-orphan"
    )

    @property
    def total_qty(self) -> int:
        return sum(item.required_qty for item in self.items)


class KitTemplateItem(Base):
    """Позиция типового комплекта."""

    __tablename__ = "kit_template_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("kit_templates.id", ondelete="CASCADE"))
    type_id: Mapped[int] = mapped_column(ForeignKey("instrument_types.id", ondelete="RESTRICT"))
    required_qty: Mapped[int] = mapped_column(Integer, default=1)

    template: Mapped[KitTemplate] = relationship(back_populates="items")
    type: Mapped[InstrumentType] = relationship()

    __table_args__ = (
        UniqueConstraint("template_id", "type_id", name="uq_template_type"),
        CheckConstraint("required_qty > 0", name="ck_template_qty_positive"),
    )


class ChangeRequest(Base):
    """Заявка мастера на перемещение прибора. Согласует главный инженер."""

    __tablename__ = "change_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN " + _sql_list(REQUEST_STATUSES),
            name="ck_request_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"))
    from_site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id", ondelete="SET NULL"))
    to_site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    requested_by: Mapped[str] = mapped_column(String(160))
    requested_on: Mapped[date] = mapped_column(Date)
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="new")
    decided_by: Mapped[str | None] = mapped_column(String(160))
    decided_on: Mapped[date | None] = mapped_column(Date)
    decision_comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    instrument: Mapped[Instrument] = relationship()
    from_site: Mapped[Site | None] = relationship(foreign_keys=[from_site_id])
    to_site: Mapped[Site] = relationship(foreign_keys=[to_site_id])

    @property
    def status_label(self) -> str:
        return REQUEST_STATUSES.get(self.status, self.status)

    @property
    def is_open(self) -> bool:
        return self.status == "new"


class SchemaVersion(Base):
    r"""Версия схемы базы: одна строка на всю базу.

    Взято у БПО (`C:\bpo\db\models.py`) вместе с обоснованием: до появления
    версии «старая база» находилась только на живых данных клиента, когда
    менять что-либо уже поздно.
    """

    __tablename__ = "schema_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    applied_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


#: Род записи в журнале действий. Деловое событие — то, что сделал человек
#: (выдал прибор, согласовал заявку); системное — то, что сделала программа
#: сама (миграция, резервная копия, автоочистка). Разделение взято у БПО:
#: при разборе происшествия системный шум не должен прятать людские действия.
AUDIT_BUSINESS = "business"
AUDIT_SYSTEM = "system"

AUDIT_TYPES = {
    AUDIT_BUSINESS: "Действие пользователя",
    AUDIT_SYSTEM: "Системное событие",
}


class AuditLog(Base):
    r"""Журнал действий: кто, когда, что сделал и с чем.

    Взято у БПО (`C:\bpo\core\audit.py`) с двумя правками, обе — из разбора
    граблей семьи:

    1. **Объект хранится ссылкой, а не текстом.** У донора `target` это
       строка вида `id=17`. Урок «Заявок» (миграция 04): журнал хранил
       обрезанный текст, и «две заявки с одинаковым названием смешивались,
       а длинная не находилась вовсе». Восстановить связь задним числом
       нельзя честно. Поэтому здесь `object_type` + `object_id`.
    2. **Действующее лицо остаётся строкой, но с заделом на учётные записи.**
       Пока в системе нет входа, пишем имя как есть; появятся учётные
       записи (этап 2) — рядом встанет `actor_id`, а строка сохранится как
       снимок на момент события: человек может уволиться, а летопись
       обязана остаться читаемой.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint(
            "audit_type IN " + _sql_list(AUDIT_TYPES),
            name="ck_audit_type",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    actor: Mapped[str] = mapped_column(String(160))
    audit_type: Mapped[str] = mapped_column(String(16), default=AUDIT_BUSINESS)
    action: Mapped[str] = mapped_column(String(120))
    object_type: Mapped[str | None] = mapped_column(String(40), index=True)
    object_id: Mapped[int | None] = mapped_column(Integer, index=True)
    details: Mapped[str | None] = mapped_column(Text)


#: Роли на сайте. Мастера участков работают через бота в MAX, учётных
#: записей здесь у них нет (ответ заказчика на вопрос 49).
USER_ROLES = {
    "admin": "Администратор",
    "keeper": "Кладовщик",
    "chief": "Главный инженер",
}


class User(Base):
    r"""Учётная запись сотрудника конторы.

    Устройство взято у «Заявок» (`C:\zayavki\db\models.py`) вместе с двумя
    правилами: пользователей **отключают, а не удаляют** (иначе стирается
    учёт по людям — идентификатор человека это вся история его действий),
    и пароль хранится строкой с именем схемы впереди, чтобы смена алгоритма
    не обесценила разом все пароли.
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN " + _sql_list(USER_ROLES), name="ck_user_role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="keeper")
    display_name: Mapped[str | None] = mapped_column(String(160))
    email: Mapped[str | None] = mapped_column(String(160))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)

    @property
    def role_label(self) -> str:
        return USER_ROLES.get(self.role, self.role)
