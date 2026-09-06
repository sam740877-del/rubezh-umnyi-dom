#!/usr/bin/env python3
"""Управление базой данных учёта СКИ.

Примеры:
    python manage.py init         — создать таблицы
    python manage.py seed-types   — заполнить справочник типов приборов
    python manage.py seed-demo    — залить демонстрационные данные
    python manage.py stats        — сводка в консоли
    python manage.py run          — запустить веб-приложение
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.database import DEFAULT_DB_PATH, init_db, session_scope
from app.seed import seed_demo, seed_types
from app.services import dashboard


def cmd_init(_: argparse.Namespace) -> None:
    init_db()
    print(f"База готова: {DEFAULT_DB_PATH}")


def cmd_seed_types(_: argparse.Namespace) -> None:
    init_db()
    with session_scope() as session:
        added = seed_types(session)
    print(f"Добавлено типов приборов: {added}")


def cmd_seed_demo(_: argparse.Namespace) -> None:
    init_db()
    with session_scope() as session:
        try:
            seed_demo(session)
        except ValueError as error:
            sys.exit(f"Ошибка: {error}")
    print("Демо-данные загружены: 2 договора, 3 участка, 24 прибора.")


def cmd_reset(args: argparse.Namespace) -> None:
    path = Path(DEFAULT_DB_PATH)
    if not args.yes:
        answer = input(f"Удалить базу {path}? Данные будут потеряны [y/N]: ").strip().lower()
        if answer != "y":
            sys.exit("Отменено")
    path.unlink(missing_ok=True)
    init_db()
    print("База пересоздана пустой.")


def cmd_stats(_: argparse.Namespace) -> None:
    init_db()
    with session_scope() as session:
        data = dashboard(session)
        print(f"Дата: {data['today']:%d.%m.%Y}")
        print(f"Приборов в учёте: {data['total_instruments']}")
        print(f"  на участках: {data['by_status'].get('in_use', 0)}, на складе: {data['by_status'].get('warehouse', 0)}")
        print(f"Договоров действует: {data['active_contracts']}, участков: {data['active_sites']}")
        print(f"Поверка просрочена: {len(data['expired'])}, истекает: {len(data['expiring'])}, нет данных: {len(data['missing'])}")
        print("\nУкомплектованность участков:")
        for report in data["site_reports"]:
            flag = "OK " if report.is_complete else "!! "
            print(f"  {flag}{report.site.name}: {report.fact_total}/{report.required_total} ({report.percent}%)")
            for row in report.rows:
                if row.deficit:
                    print(f"       не хватает {row.deficit} шт. — {row.type.name}")


def cmd_run(args: argparse.Namespace) -> None:
    import uvicorn

    init_db()
    print(f"Откройте в браузере: http://{args.host}:{args.port}")
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)



def cmd_bot(args) -> None:
    """Запустить бота: бесконечный опрос MAX.

    Отдельной командой, а не внутри веб-сервера: опрос держит соединение
    открытым десятками секунд, и мешать этим обработке запросов не стоит.
    На сервере конторы это будет вторая служба рядом с первой.
    """
    import time

    from app import bot, max_api
    from app.database import session_scope

    if not max_api.is_configured():
        print(
            "Не задан токен бота. Положите его в переменную окружения "
            "MAX_BOT_TOKEN и запустите снова."
        )
        return

    client = max_api.MaxClient()
    marker = None
    print("Бот запущен. Остановить — Ctrl+C.")

    while True:
        try:
            with session_scope() as session:
                handled, marker = bot.poll_once(session, client, marker)
                delivered = bot.deliver_pending(session, client)
            if handled or delivered:
                print(f"обработано событий: {handled}, доставлено сообщений: {delivered}")
        except KeyboardInterrupt:
            print("Бот остановлен.")
            return
        except Exception as exc:
            # Опрос не должен умирать от одной ошибки сети: пауза
            # и снова. Иначе бот замолкает до перезапуска вручную,
            # а узнают об этом мастера на объекте.
            print(f"Сбой опроса: {exc}. Повтор через 15 с.")
            time.sleep(15)


def main() -> None:
    parser = argparse.ArgumentParser(description="Учёт СКИ — управление")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="создать таблицы").set_defaults(func=cmd_init)
    sub.add_parser("seed-types", help="заполнить справочник типов").set_defaults(func=cmd_seed_types)
    sub.add_parser("seed-demo", help="залить демонстрационные данные").set_defaults(func=cmd_seed_demo)
    sub.add_parser("stats", help="сводка в консоли").set_defaults(func=cmd_stats)

    reset = sub.add_parser("reset", help="удалить и пересоздать базу")
    reset.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    reset.set_defaults(func=cmd_reset)

    run = sub.add_parser("run", help="запустить веб-приложение")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8000)
    run.add_argument("--reload", action="store_true")
    run.set_defaults(func=cmd_run)

    sub.add_parser("bot", help="запустить бота в MAX").set_defaults(func=cmd_bot)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
