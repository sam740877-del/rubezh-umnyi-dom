r"""Настройки, которые правит человек, а не программист.

Устройство взято у БПО (`C:\bpo\core\settings.py`) вместе с главным
правилом, записанным там дословно:

    «Испорченное руками значение („месяц" вместо числа) должно
    превращаться в значение по умолчанию, а не в падение программы
    на запуске: настройка — не то место, ради которого клиент останется
    без программы.»

Поэтому чтение здесь **никогда не бросает исключение**. Не нашлось,
пусто, буквы вместо числа, отрицательное число — берётся умолчание.

Зачем это заведено сейчас
--------------------------

Горизонт предупреждения о поверке был зашит числом 30 в двух местах:
`services.WARN_DAYS` и `notifications.WARN_DAYS`. Это уже расхождение —
поправят одно, забудут другое.

А вопрос заказчику 16 («за сколько дней до конца поверки предупреждать»)
открыт с самого начала. Ждать ответа, чтобы поменять константу, — значит
ждать зря: правильный ответ на «сколько дней» это не число, а настройка.
Метролог сам знает свой срок, и он у разных контор разный.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.models import Setting

#: За сколько дней до конца поверки прибор считается «истекающим».
#: Тридцать — то, с чем система жила до появления настройки.
WARN_DAYS = "warn_days"

#: Через сколько дней положено снимать резервную копию.
BACKUP_INTERVAL_DAYS = "backup_interval_days"

#: Название конторы — в шапке документов и выгрузок. Пока не используется,
#: но заведено сразу: это первое, что спрашивает второй заказчик.
COMPANY_NAME = "company_name"

#: Умолчания и человеческие названия. Ключ → (значение, название, пояснение).
#: Одним местом: у донора это `constants.py`, здесь незачем разносить.
DEFAULTS: dict[str, tuple[str, str, str]] = {
    WARN_DAYS: (
        "30",
        "Предупреждать о поверке за, дней",
        "За сколько дней до конца поверки прибор попадает в «истекающие» "
        "и о нём приходит напоминание.",
    ),
    BACKUP_INTERVAL_DAYS: (
        "1",
        "Резервная копия раз в, дней",
        "Как часто система напоминает, что пора снять копию. "
        "Ноль отключает напоминания.",
    ),
    COMPANY_NAME: (
        "",
        "Название организации",
        "Подставляется в шапку выгрузок и печатных форм.",
    ),
}


def get(session: Session, key: str) -> str:
    """Значение настройки строкой. Не нашлось — умолчание."""
    row = session.get(Setting, key)
    if row is not None and row.value is not None:
        return row.value
    return DEFAULTS.get(key, ("", "", ""))[0]


def get_int(session: Session, key: str, minimum: int | None = None) -> int:
    """Числовая настройка. Кривое значение не роняет программу.

    Настройку могли править руками в базе: буквы вместо числа — не повод
    отказать в работе, а повод взять умолчание.

    `minimum` отсекает бессмысленное: горизонт предупреждения в минус
    сорок дней — это не настройка, а опечатка.
    """
    def умолчание() -> int:
        try:
            return int(DEFAULTS.get(key, ("0",))[0])
        except ValueError:
            return 0

    try:
        значение = int(str(get(session, key)).strip())
    except (TypeError, ValueError):
        return умолчание()

    if minimum is not None and значение < minimum:
        return умолчание()
    return значение


def set_value(
    session: Session, key: str, value: str, *, actor: str | None = None
) -> Setting:
    """Записать настройку. Границу транзакции держит вызывающий."""
    row = session.get(Setting, key)
    было = row.value if row is not None else DEFAULTS.get(key, ("",))[0]

    if row is None:
        row = Setting(key=key)
        session.add(row)

    row.value = str(value).strip()
    row.changed_at = datetime.now()
    row.changed_by = actor
    session.flush()

    if было != row.value:
        # Настройка меняет поведение системы для всех — это событие
        # того же рода, что и правка справочника, и место ему в журнале.
        audit.write(
            session,
            "Изменена настройка",
            actor=actor,
            object_type="setting",
            details=f"{DEFAULTS.get(key, ('', key))[1]}: «{было}» → «{row.value}»",
        )
    return row


def all_settings(session: Session) -> list[dict]:
    """Все настройки для экрана: ключ, название, пояснение, значение."""
    сохранённые = {
        row.key: row for row in session.scalars(select(Setting))
    }
    итог = []
    for ключ, (умолчание, название, пояснение) in DEFAULTS.items():
        row = сохранённые.get(ключ)
        итог.append(
            {
                "key": ключ,
                "title": название,
                "note": пояснение,
                "value": row.value if row is not None else умолчание,
                "default": умолчание,
                "changed_at": row.changed_at if row is not None else None,
                "changed_by": row.changed_by if row is not None else None,
            }
        )
    return итог


def warn_days(session: Session) -> int:
    """Горизонт предупреждения о поверке.

    Отдельной функцией, а не чтением ключа на местах: горизонт нужен
    и в расчёте состояния поверки, и в рассылке напоминаний. Одно место —
    один ответ; прежде число 30 стояло в двух файлах и уже разъезжалось.
    """
    return get_int(session, WARN_DAYS, minimum=1)
