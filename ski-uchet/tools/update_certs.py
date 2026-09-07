r"""Обновить набор доверенных сертификатов.

Запуск:  python tools/update_certs.py

Зачем это нужно
----------------

MAX выпускает сертификат своего сервера у РОССИЙСКОГО удостоверяющего
центра — «Russian Trusted Sub CA» Минцифры. В обычных наборах корневых
сертификатов (тех, что идут с Python и браузерами) его нет, и соединение
падает с «CERTIFICATE_VERIFY_FAILED: unable to get local issuer
certificate».

Сертификат при этом настоящий: просто подписан центром, которого мир
за пределами России не знает. Лечится добавлением корневого сертификата
Минцифры к обычному набору — это и делает утилита.

Чего здесь нет и почему
------------------------

Отключения проверки (`verify=False`). Это «решение» встречается в чужих
советах чаще правильного, и оно хуже болезни: без проверки любой, кто
встанет между нами и MAX, сможет читать и подменять сообщения. А бот
носит акты приёмки, фото повреждений и решения по заявкам.

Когда запускать
----------------

Сертификаты истекают — корневой Минцифры выдан до 2032 года,
промежуточный меняется чаще. Если бот вдруг перестал соединяться
с «CERTIFICATE_VERIFY_FAILED» — сперва запустите это.
"""
from __future__ import annotations

import sys
from pathlib import Path

import certifi
import httpx

КОРЕНЬ = Path(__file__).resolve().parent.parent

#: Откуда берём. Официальный адрес Госуслуг — не зеркало и не чей-то
#: репозиторий: корневой сертификат берут только из первоисточника,
#: иначе смысл проверки теряется.
ИСТОЧНИКИ = (
    "https://gu-st.ru/content/Other/doc/russian_trusted_root_ca.cer",
    "https://gu-st.ru/content/Other/doc/russian_trusted_sub_ca.cer",
)


def main() -> int:
    куда = КОРЕНЬ / "certs"
    куда.mkdir(exist_ok=True)

    части = [Path(certifi.where()).read_text(encoding="utf-8")]

    for адрес in ИСТОЧНИКИ:
        имя = адрес.rsplit("/", 1)[1]
        try:
            ответ = httpx.get(адрес, timeout=30, follow_redirects=True)
            ответ.raise_for_status()
        except httpx.HTTPError as ошибка:
            print(f"не удалось скачать {имя}: {ошибка}")
            return 1

        текст = ответ.text.strip()
        if "BEGIN CERTIFICATE" not in текст:
            print(f"{имя}: пришло не похожее на сертификат — не берём")
            return 1

        части.append(текст)
        print(f"{имя}: получен, {len(ответ.content)} байт")

    набор = куда / "bundle.pem"
    набор.write_text("\n".join(части) + "\n", encoding="utf-8")
    print(f"\nнабор собран: {набор} ({набор.stat().st_size // 1024} КБ)")

    # Проверяем делом, а не верим на слово: набор без проверки — бумажка.
    try:
        with httpx.Client(verify=str(набор), timeout=30) as клиент:
            клиент.get("https://platform-api2.max.ru/", timeout=20)
        print("проверено: MAX проходит проверку сертификата")
    except httpx.HTTPStatusError:
        # Код ответа не важен — важно, что соединение состоялось.
        print("проверено: MAX проходит проверку сертификата")
    except httpx.HTTPError as ошибка:
        print(f"набор собран, но MAX всё равно не проходит: {ошибка}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
