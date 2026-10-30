"""«Обзор» (the owner 2026-10-30): a short walk over the panel at the first sign-in, one set of steps per kind of role, shown on the role's
start page; every step lights an element of that page and says in a line what it is for. «Пропустить» and the last step remember it in the
person's account (user_pref «tour»), not in the browser; «Пройти обзор заново» (the user's menu) forgets it and opens the start page.
All the texts of the tour are here. A step names the section it is about (`need`): a role that cannot open the section never sees the step
(tests/test_tour.py: every step of a role points at an element of its start page). A step about a part the page draws only with data
(`when`: a return, a card, the catalog of a client set up) is left out while that part is not there; a tour left with nothing but «можно
пройти заново» is not shown at all (and not marked: it comes when there is something to show).

The markup (_tour.html) works without a script: a radio button per step, the light and the window drawn by CSS (the client's catalog has
no script at all, its CSP); tour.js of the staff's frame only scrolls to the element and puts the window next to it."""
from __future__ import annotations

import re

from .. import user_prefs, users

PREF = "tour"
VERSION = 1
# a CSS selector of our own pages (the links of the menu by their address), written into an inline <style>: no «<», nothing could close it
SELECTOR = re.compile(r'^[a-z0-9 .#>:\[\]=_"/-]+$')

AGAIN = {"sel": "[data-tour=again]", "need": "self", "title": "Обзор можно пройти заново",
         "text": "Эта кнопка в вашем меню снова покажет обзор. Он больше не появится сам: отметка хранится в вашем аккаунте."}

STEPS = {
    # the owner, the partner, the manager of the chats, the observer: «/» (Главная)
    "staff": [
        {"sel": "[data-tour=attention]", "need": "home", "title": "Главная",
         "text": "Сводка дня: заказы, чаты, деньги. В блоке «Требует внимания» — то, что нужно сделать сейчас, с кнопкой перехода."},
        {"sel": '.sb-link[href="/orders"]', "need": "orders", "title": "Заказы",
         "text": "Все заказы по этапам. В карточке заказа — клиент, шины, сообщения и кнопки следующего шага."},
        {"sel": '.sb-link[href="/chats"]', "need": "chats", "title": "Чаты",
         "text": "Переписка с клиентами Авито. Отсюда отвечают клиенту и заводят заказ из чата."},
        {"sel": '.sb-link[href="/catalog"]', "need": "catalog", "title": "Каталог",
         "text": "Все шины поставщиков: что в фиде, цены, остатки. Здесь включают и выключают позиции."},
        {"sel": '.sb-link[href="/settings"]', "need": "setup", "title": "Настройки",
         "text": "Наценки, порог остатка, бренды. Правки сначала копятся в черновике, фид меняется только после «Применить»."},
        {"sel": '.sb-link[href="/history"]', "need": "setup", "title": "История",
         "text": "Кто, когда и что поменял: настройки, объявления, точки. Применённую версию настроек можно вернуть."},
        AGAIN,
    ],
    # the manager of a point: «/my-point» (Рабочее место)
    "point": [
        {"sel": '.sb-link[href="/my-point"]', "need": "mypoint", "title": "Рабочее место",
         "text": "Ваша стартовая страница: заказы, которые лежат на точке и едут к вам."},
        {"sel": ".mp-tabs", "need": "mypoint", "when": "point", "title": "Вкладки",
         "text": "Заказы по статусам. Цифра — сколько заказов во вкладке; красным — то, что ждёт вас."},
        {"sel": ".mp-search", "need": "mypoint", "when": "point", "title": "Поиск",
         "text": "Найти заказ точки по номеру заказа или перемещения, телефону или имени клиента."},
        {"sel": ".mp-block > .mp-card:first-of-type", "need": "mypoint", "when": "cards", "title": "Карточка заказа",
         "text": "Шины приехали — «Приехал». Клиент забрал — «Выдал». «Подробнее» — перемещение, доставка и срок хранения."},
        {"sel": ".mp-returns", "need": "mypoint", "when": "returns", "title": "Возврат поставщику",
         "text": "Шины, которые забирает поставщик. Водитель приезжает с актом МХ-3: подпишите акт, отдайте шины и отметьте это кнопкой под актом."},
        AGAIN,
    ],
    # the client (a buyer): «/shop» (Каталог); the cart and «Мои заказы» (the owner 2026-10-30)
    "client": [
        {"sel": "[data-tour=catalog]", "need": "shop", "when": "catalog", "title": "Каталог",
         "text": "Шины, которые есть в наличии, с вашими ценами за штуку. Сортировка — над таблицей."},
        {"sel": ".shop-filters", "need": "shop", "when": "catalog", "title": "Фильтры",
         "text": "Размеры через пробел, бренд, сезон, шипы. «Показать» применяет фильтры, «Сбросить» убирает. Они запоминаются в вашем аккаунте."},
        {"sel": "#f-wh", "need": "shop", "when": "catalog", "title": "Склад 1 и Склад 2",
         "text": "Шины лежат на двух складах. Выберите склад, чтобы видеть только его; в таблице склад указан у каждой шины."},
        {"sel": "[data-tour=cart]", "need": "shop", "when": "catalog", "title": "Корзина и заказ",
         "text": "«В корзину» в строке шины добавляет её с количеством. В корзине поправьте количество, выберите точку или доставку и "
                 "оформите заказ — менеджер свяжется. Статус заказов — в «Мои заказы»."},
        AGAIN,
    ],
}
assert all(SELECTOR.match(s["sel"]) for steps in STEPS.values() for s in steps), "a selector of the tour that <style> cannot hold"
# `when`: what of the page (_tour.html reads it from the page's own values) a step needs
WHEN = ("point", "cards", "returns", "catalog")
# where the tour of a kind is shown (the endpoint of its start page)
START = {"staff": "index", "point": "mypoint_page", "client": "shop_page"}


def kind(role: str) -> str:
    return role if role in ("point", "client") else "staff"


def steps_for(role: str) -> list:
    """The steps of the role: only those about a section the role may open."""
    return [s for s in STEPS[kind(role)] if users.level(role, s["need"]) >= users.VIEW]


def on_page(steps: list, has: dict) -> list:
    """The steps whose element the page draws now (`has`: WHEN → bool); [] when only «можно пройти заново» is left."""
    steps = [s for s in steps if not s.get("when") or has.get(s["when"])]
    return steps if any(s is not AGAIN for s in steps) else []


def done(conn, login: str) -> bool:
    return user_prefs.get(conn, login, PREF) is not None


def mark_done(conn, login: str, how: str, now: str) -> None:
    user_prefs.put(conn, login, PREF, {"v": VERSION, "how": how, "at": now}, now)     # «v»: which tour was passed (not compared yet)


def forget(conn, login: str) -> None:
    user_prefs.drop(conn, login, PREF)


def register(app) -> None:
    from flask import redirect, request, session

    from .common import current_user, get_conn, now_iso, safe_path

    @app.context_processor
    def tour_context():
        """`tour`: the steps to show on this page (the role's start page, the tour not passed yet), else []."""
        def tour(has=None):
            login = session.get("user")
            if not login or request.method != "GET":
                return []
            role = current_user()["role"]
            if request.endpoint != START[kind(role)] or done(get_conn(), login):
                return []
            return on_page(steps_for(role), has or {})
        return {"tour": tour}

    @app.route("/tour/done", methods=["POST"])
    def tour_done():
        """«Пропустить» or the last step: not shown again; back to the page it was on."""
        mark_done(get_conn(), current_user()["login"], "skip" if request.form.get("how") == "skip" else "end", now_iso())
        return redirect(safe_path(request.form.get("next", "")) or "/")

    @app.route("/tour/again", methods=["POST"])
    def tour_again():
        """«Пройти обзор заново»: forget the mark and open the start page of the role (the tour is shown there)."""
        forget(get_conn(), current_user()["login"])
        return redirect({"staff": "/", "point": "/my-point", "client": "/shop"}[kind(current_user()["role"])])
