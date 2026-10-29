"""Buttons by rights (owner, 2026-10-21: «Кнопки без права — делай»): a page shows only the actions the person may do. Every form that posts
and every link to an action page on the main pages, for every role, must be one the server lets this role send (the server still answers 403)."""
from __future__ import annotations

import re
from html import unescape

import pytest
from flask import g
from werkzeug.exceptions import Forbidden, MethodNotAllowed, NotFound

import panel_helpers
from panel_helpers import login, make_app, post
from test_calls import FakeClient as FakeCalls, call
from test_chat_deals import Fake
from test_order_search import REF, UNLOAD
from tires_sync import calls, chats, clients, connections, order_points as points, store, users
from tires_sync.settings_store import confirm_draft, create_draft
from tires_sync.panel import common

NOW = "2026-10-07T12:00:00"
ROLES = ["owner", "partner", "chats", "viewer", "point"]
# links that open a page whose only job is an action: shown only to who may send it
ACTION_LINKS = {"/orders/new": "order_new", "/orders/bulk": "orders_bulk_run", "/orders/cleanup": "orders_cleanup_run",
                "/feed/update": "feed_update_confirm", "/economics/sales/new": "economics_sale_add"}


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """One app with an order card (of a point), a chat and a client; a signed-in client per role."""
    monkeypatch.setattr(panel_helpers, "UNLOAD", UNLOAD)
    monkeypatch.setattr(panel_helpers, "REF_TEXT", REF)
    app = make_app(tmp_path)
    owner = app.test_client()
    tok = login(owner)
    owner.get("/orders")                                                              # the pipeline
    conn = store.connect(app.config["DB_PATH"])
    pid = points.save_point(conn, {"name": "Гамма", "manager_name": "А", "channel": "vk"})
    r = post(owner, "/orders/new", tok, client_name="Иван", client_phone="+79110000000", item_goods_id=["100"], item_qty=["4"])
    assert r.status_code == 302, r.get_data(as_text=True)[:300]
    card = int(re.search(r"/orders/(\d+)$", r.headers["Location"]).group(1))
    conn.execute("UPDATE order_card SET point_id = ? WHERE id = ?", (pid, card))
    conn.commit()
    confirm_draft(conn, create_draft(conn, {"overrides": {"1:999": "on"}}, NOW), NOW, "owner")           # a manual rule without a position
    account = connections.create_account(conn, "Основной", NOW, "o")
    conn.execute("UPDATE account SET chats_enabled = 1 WHERE id = ?", (account,))
    connections.set_chats_send(conn, account, True, NOW, "o")
    conn.execute("UPDATE point SET avito_account_id = ? WHERE id = ?", (account, pid))   # the point works with the chats of its account (2026-10-21)
    conn.commit()
    fake = Fake()
    fake.add("c1", "Татьяна", [("in", "Здравствуйте, есть такие шины?")])
    chats.sync_account(conn, fake, account, NOW)
    chat = chats.list_chats(conn)[0]["id"]
    calls.sync_account(conn, FakeCalls([[call(1, at="2026-10-07T10:00:00+03:00")]]), account, NOW, lambda s: None)   # a call of the point's account
    client_row = conn.execute("SELECT id FROM client LIMIT 1").fetchone()
    people = {"owner": owner}
    for role in ROLES[1:]:
        users.add_user(conn, role + "1", "длинный-пароль-1", NOW, role=role, point_id=pid if role == "point" else None)
        c = app.test_client()
        with c.session_transaction() as session:
            session.update(user=role + "1", epoch=store.get_session_epoch(conn, role + "1"), csrf="tok")
        people[role] = c
    pages = sorted(r.rule for r in app.url_map.iter_rules() if "GET" in r.methods and not r.arguments and r.endpoint not in common.OPEN
                   and not r.rule.endswith((".xlsx", ".csv", ".json")))
    pages += ["/orders?view=list", "/orders/%d" % card, "/chats/%d" % chat, "/catalog/preview/100"]
    if client_row:
        pages.append("/clients/%d" % client_row[0])
    return app, people, pages, {"card": card, "chat": chat}


def allowed_to_post(app, role: str, path: str) -> bool:
    """What the server says to this role posting there: the real check_access, not a copy of it."""
    with app.test_request_context(path, method="POST"):
        g.user_info = {"login": role, "role": role, "point_id": 1 if role == "point" else None, "display_name": ""}
        common.session_valid, saved = (lambda: True), common.session_valid
        try:
            common.check_access()
            return True
        except Forbidden:
            return False
        finally:
            common.session_valid = saved


def endpoint_of(app, path: str):
    try:
        return app.url_map.bind("localhost").match(path.split("?")[0], method="POST")[0]
    except (NotFound, MethodNotAllowed):
        return None


def posted_actions(page: str):
    """The addresses every form posting on the page sends to (the form's action and the buttons' formaction)."""
    found = []
    for form in re.finditer(r"<form\b[^>]*>", page):
        tag = form.group(0)
        if not re.search(r'method="post"', tag, re.I):
            continue
        action = re.search(r'action="([^"]*)"', tag)
        if action and action.group(1).startswith("/"):
            found.append(unescape(action.group(1)))
    found += [unescape(a) for a in re.findall(r'formaction="(/[^"]*)"', page)]
    return found


@pytest.mark.parametrize("role", ROLES)
def test_every_form_on_the_main_pages_is_one_the_role_may_send(world, role):
    app, people, pages, _ = world
    wrong = []
    opened = 0
    for url in pages:
        r = people[role].get(url)
        if r.status_code != 200:
            continue
        opened += 1
        page = r.get_data(as_text=True)
        for action in posted_actions(page):
            if endpoint_of(app, action) is None:
                continue
            if not allowed_to_post(app, role, action):
                wrong.append("%s → %s (%s)" % (url, action, endpoint_of(app, action)))
        forms = set(re.findall(r'<form\b[^>]*\bid="([^"]+)"', page))
        for target in re.findall(r'<button\b[^>]*\bform="([^"]+)"', page) if "<html" in page else []:     # a fragment: its forms are on the page                   # a button of a form that is not on the page does nothing
            if target not in forms:
                wrong.append("%s → a button of the missing form %s" % (url, target))
        if "data-tags" in page and "data-tag-in" not in page:                                # app.js needs the field of the tags (a read-only window has none)
            wrong.append("%s → data-tags without data-tag-in" % url)
        for href, endpoint in ACTION_LINKS.items():
            if re.search(r'href="%s[?"]' % re.escape(href) if not href.endswith("=") else r'href="%s' % re.escape(href), page) and not allowed_to_post(app, role, _post_path(app, endpoint)):
                wrong.append("%s → link %s" % (url, href))
    assert opened >= 2
    assert wrong == [], "\n".join(wrong)


def _post_path(app, endpoint):
    rule = next(r for r in app.url_map.iter_rules() if r.endpoint == endpoint and "POST" in r.methods)
    return re.sub(r"<[^>]+>", "1", rule.rule)


def test_can_post_says_what_the_server_says_for_every_role_and_every_page(world):
    app = world[0]
    for role in ROLES:
        for endpoint in common.SECTION_OF:
            if not any("POST" in r.methods for r in app.url_map.iter_rules() if r.endpoint == endpoint):
                continue
            with app.test_request_context("/"):
                g.user_info = {"login": role, "role": role, "point_id": 1 if role == "point" else None, "display_name": ""}
                helper = common.can_post(endpoint)
            assert helper == allowed_to_post(app, role, _post_path(app, endpoint)), (role, endpoint)


# what each role sees: (page, the text of the button or the form address) → who has it (the rest must not)
SEEN = [
    ("/orders", 'href="/orders/new"', {"owner", "partner", "chats"}),                                   # «Новый заказ»
    ("/orders", 'href="/orders/bulk"', {"owner", "partner"}),                                           # «Закрыть разом» (право «массово»)
    ("/orders", 'draggable="true"', {"owner", "partner", "chats", "point"}),                            # moving a card on the board
    ("/orders/{card}", 'action="/orders/{card}/delete"', {"owner", "partner"}),                         # «Удалить заказ» (право «удалять»)
    ("/orders/{card}", 'action="/orders/{card}/save"', {"owner", "partner", "chats"}),                  # the window can be saved
    ("/orders/{card}", 'Только просмотр: менять заказ', {"viewer", "point"}),
    ("/orders/{card}", 'action="/orders/{card}/note"', {"owner", "partner", "chats", "point"}),         # the point writes notes
    ("/orders/{card}", 'action="/orders/{card}/wants/add"', {"owner", "partner", "chats"}),
    ("/chats/{chat}", 'action="/chats/{chat}/send"', {"owner", "partner", "chats", "point"}),           # «Отправить клиенту» (право «отправлять»; the point: its account, 2026-10-21)
    ("/chats/{chat}", 'action="/chats/{chat}/delete"', {"owner", "partner"}),
    ("/chats/{chat}", 'action="/chats/{chat}/create-deal"', {"owner", "partner", "chats", "point"}),
    ("/chats/{chat}", 'Только просмотр: отвечать клиентам', {"viewer"}),
    ("/catalog", 'action="/feed/update/fetch"', {"owner"}),                                             # publishing the feed (право «публиковать»)
    ("/catalog", 'action="/catalog/stage"', {"owner"}),
    ("/catalog/item/100", 'action="/catalog/item/100/own-price"', {"owner"}),                           # своя цена позиции: только владелец (2026-10-29)
    ("/catalog/item/100", "Своя цена по аккаунтам", {"owner", "partner"}),                                # партнёр видит, не правит
    ("/catalog", 'formaction="/shares/new"', {"owner", "partner", "chats"}),                            # a link for the client
    ("/feeds", 'href="/feed/update"', {"owner"}),
    ("/", 'href="/feed/update"', {"owner"}),
    ("/deleted", 'href="/orders/cleanup"', {"owner", "partner"}),
]


@pytest.mark.parametrize("role", ROLES)
def test_each_role_sees_the_buttons_it_has_the_right_for_and_not_the_others(world, role):
    _, people, _, ids = world
    for url, mark, who in SEEN:
        url, mark = url.format(**ids), mark.format(**ids)
        r = people[role].get(url)
        shown = r.status_code == 200 and mark in r.get_data(as_text=True)
        assert shown == (role in who), (role, url, mark, r.status_code)
