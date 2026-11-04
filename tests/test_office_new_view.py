"""«Новый вид» of the office (lane port-office-rest, the prototype's role «Офис»): with the office group's flag on for the viewer «Чаты»,
«Каталог» (the list, its table, the card of a position), CRM (the clients and the card), «Задачи» and «Что нового» are drawn from
templates/office2/; off — the same pages as before. The forms keep their routes, fields and results; a point never gets them."""
from __future__ import annotations

import re

import pytest

from panel_helpers import login, post
from test_chat_deals import Fake
from test_own_price import NOW, _app
from test_roles import PASSWORD, sign_in
from tires_sync import chat_deals as deals, chats, clients, connections, order_points as points, orders, staging, store, tasks, users

PAGES = ["/chats", "/chats/{chat}", "/catalog", "/catalog?own=1", "/catalog/table", "/catalog/item/100", "/clients", "/clients/{client}",
         "/tasks", "/tasks?tab=all", "/whats-new"]


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


@pytest.fixture
def w(tmp_path):
    """Two feed accounts (own prices), chats of the first with their orders and clients, a point with «Свой остаток (ОТХ)», two tasks, a
    deleted order of a client (its «удалён» in the purchases), and the people: the chats' manager, the partner, a point's manager."""
    app, conn, (first, second) = _app(tmp_path)
    orders.ensure_pipeline(conn, NOW)
    connections.set_modes(conn, first, True, True, NOW, "o")
    connections.set_chats_send(conn, first, True, NOW, "o")
    fake = Fake()
    fake.add("c1", "Татьяна", [("in", "Здравствуйте, это актуально?"), ("out", "Да"), ("in", "Можно в субботу? +7 900 000-01-17")])
    fake.add("c2", "Иван", [("out", "Добрый день!")], item=222, updated=1_791_900_100)
    chats.sync_account(conn, fake, first, NOW)
    deals.process_all(conn, NOW, auto_create=True)
    for c in chats.list_chats(conn):
        clients.attach_chat(conn, c["id"], c["order_id"], NOW, "o")
    pid = points.save_point(conn, {"name": "Янино", "manager_name": "Ольга", "channel": "panel"})
    for login_, role in (("chat1", "chats"), ("zaman", "partner"), ("pt1", "point")):
        users.add_user(conn, login_, PASSWORD, NOW, role=role, point_id=pid if role == "point" else None)
    tatyana = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Татьяна")
    client_id = clients.client_of_chat(conn, tatyana["id"])["id"]
    tasks.add(conn, kind="callback", title="Уточнить размер", due_at="2026-11-20T15:00", note="после обеда", now=NOW, author="owner",
              assignee="owner", client_id=client_id)
    tasks.add(conn, kind="other", title="Проверить остатки", due_at="2026-11-21T10:00", note="", now=NOW, author="owner", assignee="zaman")
    conn.commit()
    owner = app.test_client()
    tok = login(owner)
    assert post(owner, "/chats/%d/create-deal" % tatyana["id"], tok, f="", acc="", q="").status_code == 302
    gone = orders.create_card(conn, dict(client_name="Татьяна"), NOW, "o")
    orders.add_item(conn, gone, goods_id="100", name="Ikon Test Model 205/55 R16", brand_key="IKON TYRES", season="winter", diameter=16,
                    territory="1", qty=4, sale_unit=6200, supplier_price=5000, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="o")
    clients._link_order(conn, client_id, gone, "manual", NOW)
    conn.commit()
    assert post(owner, "/orders/%d/delete" % gone, tok).status_code == 302
    assert clients.deleted_orders(conn, client_id)
    return dict(app=app, conn=conn, owner=owner, tok=tok, chat=tatyana["id"], client=client_id, pid=pid, accounts=(first, second))


def flag(w, state):
    store.set_state(w["conn"], "ui_new.office", state)
    w["conn"].commit()


def person(w, who):
    client = w["app"].test_client()
    sign_in(client, w["conn"], who)
    return client


def address(w, page):
    return page.format(chat=w["chat"], client=w["client"])


def is_new(html: str) -> bool:
    return "office2-pages.css" in html and re.search(r'<body class="[^"]*\bo2\b', html) is not None


def snap(w, client, pages=PAGES):
    out = {}
    for page in pages:
        r = client.get(address(w, page))
        assert r.status_code == 200, page
        html = re.sub(r'value="[0-9a-f]{24}"', "", r.get_data(as_text=True))           # the per-render tokens of a reply
        out[page] = re.sub(r'data-sig="[^"]*"', "", html)
    return out


# --- off: exactly the old pages ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["", "off"])
def test_off_the_owner_keeps_the_old_pages(w, state):
    if state:
        flag(w, state)
    for page, html in snap(w, w["owner"]).items():
        assert not is_new(html) and "office2/" not in html and 'class="o2' not in html, page
    assert 'class="chats-layout"' in snap(w, w["owner"], ["/chats/{chat}"])["/chats/{chat}"]


def test_off_written_is_the_same_markup_as_nothing_written(w):
    snap(w, w["owner"])                                                    # the first look marks things seen (the tour, «Что нового»)
    before = snap(w, w["owner"])
    flag(w, "off")
    assert snap(w, w["owner"]) == before
    flag(w, "owner_only")
    chat1 = person(w, "chat1")
    pages = ["/chats", "/chats/{chat}", "/tasks", "/whats-new"]
    snap(w, chat1, pages)
    seen = snap(w, chat1, pages)
    flag(w, "off")
    assert snap(w, chat1, pages) == seen                                    # «только у владельца»: the chats' manager has the old ones


def test_owner_only_is_the_owner_and_the_partner_and_all_is_everybody_of_the_office(w):
    flag(w, "owner_only")
    assert is_new(w["owner"].get("/chats").get_data(as_text=True))
    assert is_new(person(w, "zaman").get("/chats").get_data(as_text=True))
    assert not is_new(person(w, "chat1").get("/chats").get_data(as_text=True))
    flag(w, "all")
    assert is_new(person(w, "chat1").get("/chats").get_data(as_text=True))


@pytest.mark.parametrize("state", ["owner_only", "all"])
@pytest.mark.parametrize("point_state", ["off", "all"])
def test_a_point_never_gets_the_office_screens(w, state, point_state):
    flag(w, state)
    store.set_state(w["conn"], "ui_new.point", point_state)                 # its own «Новый вид» on too: still its own screens
    w["conn"].commit()
    point = person(w, "pt1")
    for page in ("/chats", "/tasks", "/whats-new"):
        r = point.get(page)
        html = r.get_data(as_text=True)
        assert r.status_code == 200 and not is_new(html) and "office2-pages.css" not in html, page
        assert ("point2.css" in html) == (point_state == "all"), page


# --- on: every screen of the new view ----------------------------------------------------------------------------------------------

def test_on_every_office_screen_is_the_new_one(w):
    flag(w, "all")
    pages = snap(w, w["owner"])
    for page, html in pages.items():
        if page != "/catalog/table":                                       # the table is a part of the page (app.js puts it into #items)
            assert is_new(html), page
    assert 'class="card o2-cat-table"' in pages["/catalog/table"] and "Название · код" in pages["/catalog/table"]


def test_the_chats_are_two_columns_and_keep_the_hooks_of_app_js(w):
    flag(w, "all")
    html = w["owner"].get("/chats/%d" % w["chat"]).get_data(as_text=True)
    assert 'class="chats-layout o2-cl has-cur"' in html and 'class="chats-list o2-cl-list"' in html and 'class="chats-main o2-cl-pane"' in html
    assert html.count('class="chat-row o2-cl-row') == 2 and 'o2-cl-row is-active' in html
    assert 'id="chats-live"' in html and 'id="chats-order"' in html and 'id="chats-offline"' in html and "data-chats-relist" in html
    lst = html.split('class="chats-list o2-cl-list"', 1)[1].split('<section class="chats-main', 1)[0]
    bar = lst.split('class="chats-filter o2-cl-top"', 1)[1].split('<a class="chat-row', 1)[0]
    assert bar.count("ml-auto") == 1 and 'class="ml-auto">Всего: 2' in bar         # «Обновить список» swaps the count by the first .ml-auto
    assert bar.endswith('</span></div>\n</div>\n')                                 # and puts the fresh rows right after the bar
    assert 'class="o2-chip is-on"' in html and ">Ждут ответа</a>" in html                      # the state as chips
    assert 'class="card-head chat-head o2-cl-head"' in html and 'class="chat-thread o2-cl-msgs"' in html
    assert 'class="chats-deal o2-cl-panel" id="chats-deal"' in html and "data-deal-fold" in html and "data-deal-unfold" in html
    assert 'action="/chats/%d/send"' % w["chat"] in html and "data-photo-form" in html and 'name="reply_token"' in html
    assert 'action="/chats/%d/assign"' % w["chat"] in html and 'action="/chats/%d/status"' % w["chat"] in html
    assert 'action="/chats/%d/stage"' % w["chat"] in html and "data-item-find" in html and 'action="/chats/%d/wait"' % w["chat"] in html
    assert "Можно в субботу?" in html
    tasks_block = html.split('<div class="o2-panel-tasks">', 1)[1].split("</section>", 1)[0]          # «Задачи» of the chat, folded: the chat stays in sight
    assert 'class="o2-toggle" type="checkbox" id="o2-tasks-open" aria-label' in tasks_block            # no <details>, not checked: see the next line
    assert "<details" not in tasks_block       # an open <details> is `open` in innerHTML: the live refill of app.js would wipe what is typed here
    assert 'action="/tasks/new"' in tasks_block and 'name="chat_row" value="%d"' % w["chat"] in tasks_block
    assert 'name="back" value="/chats/%d"' % w["chat"] in tasks_block


def test_a_late_task_of_the_chat_shows_on_the_folded_button(w):
    from datetime import datetime
    flag(w, "all")
    tasks.add(w["conn"], kind="other", title="Ответить про шипы", due_at="2026-11-20T15:00", note="", now=NOW, author="owner", assignee="owner", chat_row=w["chat"])
    w["conn"].commit()
    late = '<span class="badge badge--err" title="Просроченные">1</span>'
    assert "Задачи (1)" in w["owner"].get("/chats/%d" % w["chat"]).get_data(as_text=True)
    assert late not in w["owner"].get("/chats/%d" % w["chat"]).get_data(as_text=True)
    w["app"].config["NOW"] = lambda: datetime(2026, 12, 1, 12, 0, 0)
    assert late in w["owner"].get("/chats/%d" % w["chat"]).get_data(as_text=True)


def test_the_catalog_has_the_filters_in_rows_the_season_tags_and_the_bulk_panels(w):
    flag(w, "all")
    html = w["owner"].get("/catalog").get_data(as_text=True)
    assert 'class="filters o2-filters" data-autosubmit' in html and html.count('class="o2-frow') == 2
    for field in ('name="w"', 'name="size"', 'name="q"', 'name="s"', 'name="t"', 'name="st"', 'name="own"', 'name="b"', 'name="xb"'):
        assert field in html, field
    assert 'class="ico-sun"' in html and 'class="ico-snow"' in html and "o2-stud" in html          # sun, snowflake, the stud
    assert 'class="o2-season o2-season--winter"' in html
    assert 'id="items" data-url=' in html and 'id="stage"' in html and 'id="bulk"' in html and 'id="bulk-price"' in html
    assert 'class="bulkbar o2-bulkbar"' in html and "Своя цена выбранным…" in html and 'formaction="/catalog/own-price/bulk"' in html
    assert 'action="/catalog/own-price/inline"' in html and "data-own-cell" in html and "own_price.js" in html
    assert 'data-preview="100"' in html and 'data-select-row' in html and "data-toggle-sp" in html


def test_the_card_of_a_position_has_its_own_price_and_oth_in_the_side_column(w):
    flag(w, "all")
    html = w["owner"].get("/catalog/item/100").get_data(as_text=True)
    side = html.split('<aside class="o2-item-side">', 1)[1].split("</aside>", 1)[0]
    assert "Своя цена по аккаунтам" in side and 'action="/catalog/item/100/own-price"' in side and "Шина-Колесо" in side
    assert "Свой остаток (ОТХ)" in side and 'action="/catalog/item/100/oth-manual"' in side and "Янино" in side
    assert "Объявление сейчас" in html.split('<aside class="o2-item-side">', 1)[0]


def test_crm_the_list_and_the_card_with_the_deleted_order(w):
    flag(w, "all")
    html = w["owner"].get("/clients").get_data(as_text=True)
    assert 'class="card o2-crm-list"' in html and 'href="/clients/duplicates"' in html and 'data-href="/clients/%d"' % w["client"] in html
    assert "Заказы · чаты · звонки" in html and 'name="q"' in html and 'name="tag"' in html
    card = w["owner"].get("/clients/%d" % w["client"]).get_data(as_text=True)
    assert 'class="o2-client-cols"' in card and 'href="#o2-orders"' in card and 'id="o2-orders"' in card and 'id="o2-merge"' in card
    assert 'badge badge--err">удалён</span>' in card                                                     # the purchases keep the deleted
    for action in ("save", "tag", "merge"):
        assert 'action="/clients/%d/%s"' % (w["client"], action) in card, action


def test_tasks_and_whats_new(w):
    flag(w, "all")
    html = w["owner"].get("/tasks?tab=all").get_data(as_text=True)
    assert 'data-open-modal="o2-newtask"' in html and 'id="o2-newtask"' in html and 'action="/tasks/new"' in html
    assert "Уточнить размер" in html and "Проверить остатки" in html and html.count('action="/tasks/') >= 5
    assert '<th>Кому</th>' in html and '<th>Кому</th>' in w["owner"].get("/tasks").get_data(as_text=True)     # «Мне» of the owner has others' too
    assert 'class="wn o2-wn"' in w["owner"].get("/whats-new").get_data(as_text=True)


# --- on: the presses work as before -----------------------------------------------------------------------------------------------

def test_the_chat_presses_work_from_the_new_view(w):
    flag(w, "all")
    o, tok, chat = w["owner"], w["tok"], w["chat"]
    keep = dict(f="", acc="", q="")
    assert post(o, "/chats/%d/assign" % chat, tok, responsible="chat1", **keep).status_code == 302
    assert chats.get_chat(w["conn"], chat)["responsible"] == "chat1"
    assert post(o, "/chats/%d/status" % chat, tok, status="no_reply", **keep).status_code == 302
    assert chats.get_chat(w["conn"], chat)["status"] == "no_reply"
    html = o.get("/chats/%d" % chat).get_data(as_text=True)
    assert is_new(html) and "отвечает " in html and "Открыть чат снова" in html


def test_the_catalog_presses_work_from_the_new_view(w):
    flag(w, "all")
    o, tok = w["owner"], w["tok"]
    r = o.post("/catalog/own-price/inline", data=dict(csrf=tok, g="100", value="6990", do="save", next="/catalog"))
    assert r.status_code in (200, 302)
    assert staging.summary(w["conn"])["changes"].get("price_overrides")
    assert 'data-own-mark' in o.get("/catalog").get_data(as_text=True)                                    # «своя» on the row
    assert post(o, "/catalog/item/100/oth-manual", tok, point=str(w["pid"]), qty="3", note="витрина", action="set").status_code == 302
    side = o.get("/catalog/item/100").get_data(as_text=True).split('<aside class="o2-item-side">', 1)[1]
    assert "витрина" in side and 'value="3"' in side


def test_the_task_and_the_client_presses_work_from_the_new_view(w):
    flag(w, "all")
    o, tok = w["owner"], w["tok"]
    r = post(o, "/tasks/new", tok, kind="other", title="Позвонить поставщику", assignee="owner", date="2026-11-25", time="10:00", note="", back="/tasks")
    assert r.status_code == 302
    html = o.get("/tasks").get_data(as_text=True)
    assert is_new(html) and "Позвонить поставщику" in html
    tid = int(re.search(r'data-task="(\d+)"[^>]*>\n<td class="nowrap"><span class="badge badge--neutral">Другое</span></td>\n<td class="o2-tk-what"><div>Позвонить поставщику',
                        html).group(1))
    assert post(o, "/tasks/%d/done" % tid, tok, result="договорились", back="/tasks").status_code == 302
    assert "Позвонить поставщику" not in o.get("/tasks").get_data(as_text=True)
    assert post(o, "/clients/%d/tag" % w["client"], tok, tag="опт", on="1").status_code == 302
    assert ">опт</span>" in o.get("/clients").get_data(as_text=True)
