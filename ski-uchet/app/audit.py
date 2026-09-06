r"""Журнал действий: кто, когда и что сделал.

Взято у БПО (`C:\bpo\core\audit.py`) вместе с главным правилом (Р9.3):
**сбой аудита никогда не роняет бизнес-операцию.** Прибор обязан выдаться,
даже если журнал не записался. Но и молчать о своём сбое журнал не вправе —
причина уходит в файл `logs/audit-failures.log`. Слова донора: «Потерянная
запись аудита это потерянное доказательство, и узнавать о ней надо сразу,
а не через полгода».

Границу транзакции держит вызывающий: здесь только `session.add` внутри
точки сохранения.

Два отличия от донора, оба вынужденные
---------------------------------------

**Объект хранится ссылкой, а не текстом.** У донора `target` это строка
`id=17`. Урок «Заявок» (миграция 04): по обрезанному тексту «две заявки
с одинаковым названием смешивались, а длинная не находилась вовсе».

**Причина последнего сбоя не хранится в модуле.** У донора это переменная
`last_failure` на уровне модуля — для настольной программы, где человек
один, это годится. Здесь запросы идут одновременно, и такая переменная
превратилась бы в гонку: два запроса затирают причину друг друга, и в
разборе окажется чужая. Поэтому причина возвращается вызывающему и пишется
в файл, а в памяти не живёт.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import AUDIT_BUSINESS, AUDIT_SYSTEM, AuditLog

#: Куда пишется причина несостоявшейся записи. Файл, а не база: журнал
#: срывается чаще всего именно тогда, когда недоступна сама база.
FAILURES_LOG = Path(__file__).resolve().parent.parent / "logs" / "audit-failures.log"

#: Действующее лицо, когда его не назвали. Пустым оно не бывает никогда:
#: запись «неизвестно кто» бесполезна при разборе, а разбор — единственная
#: причина, по которой этот журнал существует (довод донора, core/operator.py).
UNKNOWN_ACTOR = "не определён"


def _append_failure_log(text: str) -> None:
    """Дописать причину сбоя в файл. Сам никогда не падает."""
    try:
        FAILURES_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(FAILURES_LOG, "a", encoding="utf-8") as fp:
            fp.write(f"\n===== {datetime.now().isoformat()} =====\n{text}\n")
    except Exception:
        pass


def write(
    session: Session,
    action: str,
    *,
    actor: str | None = None,
    object_type: str | None = None,
    object_id: int | None = None,
    details: str | None = None,
    audit_type: str = AUDIT_BUSINESS,
) -> AuditLog | None:
    """Записать действие в журнал.

    Действующее лицо (`actor`) передаётся явно и всегда. Глобальной
    переменной «текущий пользователь» здесь нет и не будет: урок «Заявок»
    (R-12) — в одном процессе работают много людей, и глобал дал бы
    «молчаливую порчу от имени не того человека».

    Возвращает запись либо `None`, если записать не вышло. Исключение
    наружу не летит никогда — операция важнее журнала.
    """
    try:
        entry = AuditLog(
            actor=(actor or "").strip() or UNKNOWN_ACTOR,
            audit_type=audit_type or AUDIT_BUSINESS,
            action=action or "",
            object_type=object_type,
            object_id=object_id,
            details=details,
        )
        # Точка сохранения, а не общая транзакция: испорченная запись
        # аудита откатывает только себя. Общий rollback здесь означал бы,
        # что сбой журнала убивает саму операцию — ровно то, что запрещено.
        with session.begin_nested():
            session.add(entry)
        return entry
    except Exception as exc:
        _append_failure_log(
            f"запись аудита не создана — {action} / {object_type}#{object_id}: {exc}"
        )
        return None


def write_system(
    session: Session,
    action: str,
    *,
    object_type: str | None = None,
    object_id: int | None = None,
    details: str | None = None,
) -> AuditLog | None:
    """Системное событие: миграция, резервная копия, автоочистка."""
    return write(
        session,
        action,
        actor="система",
        object_type=object_type,
        object_id=object_id,
        details=details,
        audit_type=AUDIT_SYSTEM,
    )


def recent(session: Session, limit: int = 100) -> list[AuditLog]:
    """Последние записи журнала, новые первыми."""
    return list(
        session.scalars(
            select(AuditLog)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(limit)
        )
    )


def for_object(
    session: Session, object_type: str, object_id: int, limit: int = 50
) -> list[AuditLog]:
    """История по одному объекту — например, по прибору."""
    return list(
        session.scalars(
            select(AuditLog)
            .where(AuditLog.object_type == object_type, AuditLog.object_id == object_id)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(limit)
        )
    )


def object_titles(session: Session, entries: list[AuditLog]) -> dict[tuple[str, int], str]:
    """Человеческие названия объектов журнала: ключ — (вид, номер).

    Журнал хранит ссылку парой `object_type` + `object_id` — это верно
    для базы, но «прибор №13» человеку ничего не говорит. Здесь номера
    превращаются в то, что человек ищет: «СКИ-013 Склерометр ОМШ-1».

    Одним запросом на каждый вид объекта, а не по запросу на строку:
    иначе страница на 500 записей сделала бы 500 запросов.
    """
    from app.models import ChangeRequest, Instrument, Site, User

    wanted: dict[str, set[int]] = {}
    for entry in entries:
        if entry.object_type and entry.object_id:
            wanted.setdefault(entry.object_type, set()).add(entry.object_id)

    titles: dict[tuple[str, int], str] = {}

    if wanted.get("instrument"):
        for item in session.scalars(
            select(Instrument).where(Instrument.id.in_(wanted["instrument"]))
        ):
            titles[("instrument", item.id)] = f"{item.inventory_no} {item.name}"

    if wanted.get("site"):
        for item in session.scalars(select(Site).where(Site.id.in_(wanted["site"]))):
            titles[("site", item.id)] = item.name

    if wanted.get("user"):
        for item in session.scalars(select(User).where(User.id.in_(wanted["user"]))):
            titles[("user", item.id)] = item.display_name or item.login

    if wanted.get("change_request"):
        for item in session.scalars(
            select(ChangeRequest)
            .where(ChangeRequest.id.in_(wanted["change_request"]))
            .options(selectinload(ChangeRequest.instrument))
        ):
            what = item.instrument.inventory_no if item.instrument else "?"
            titles[("change_request", item.id)] = f"Заявка №{item.id} · {what}"

    return titles
