"""The managers of the points work in the panel (the owner 2026-10-26): what a point sees is its own and nothing of another point or another
Avito account — by the address, through the JSON of the search and the feeds; no CRM at all (the owner 2026-10-29; until then its own part
of a client, 2026-10-26), no money on the board, the
invitation of a point leads to «Моя точка» and an invitation for a point that is gone is refused."""
from __future__ import annotations

import re

import pytest

from panel_helpers import login, make_app, post
from test_roles import PASSWORD, _csrf, person
from order_fixtures import card_of_phone
from tires_sync import clients, connections, order_points as points, orders, store, users

NOW = "2026-10-07T12:00:00"
NBSP = " "
PHONE_SHARED, PHONE_B_ONLY, PHONE_FOREIGN, PHONE_CALLER = "+79001110001", "+79002220002", "+79003330003", "+79004440004"


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


def _item(conn, card, name, price, qty=4, cost=3100):
    orders.add_item(conn, card, goods_id="g%d" % card, name=name, brand_key="nokian", season="winter", diameter=16, territory="1", qty=qty,
                    sale_unit=price, supplier_price=cost, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="o")


def _chat(conn, account, peer, name, order_id=None):
    row = conn.execute("INSERT INTO avito_chat (account_id, chat_id, peer_id, peer_name, order_id, created_ts) VALUES (?,?,?,?,?,0)",
                       (account, "c-%s" % peer, peer, name, order_id)).lastrowid
    clients.attach_chat(conn, row, order_id, NOW, "o")
    return row


def _call(conn, account, phone):
    n = conn.execute("SELECT COUNT(*) FROM avito_call").fetchone()[0]
    row = conn.execute("INSERT INTO avito_call (account_id, call_id, call_at, buyer_phone, answered, talk_seconds) VALUES (?,?,?,?,1,65)",
                       (account, 5000 + n, "2026-10-07T10:%02d:00" % n, phone)).lastrowid
    clients.attach_call(conn, row, NOW, "calls", own=set())
    return row


def _sale(conn, card, total):
    conn.execute("INSERT INTO sale (day, goods_id, brand_key, season, diameter, name, ad_kind, pieces, price_total, sale_total, cost_total, bonus_total, "
                 "monthly_total, fee_total, promo_total, margin_total, note, source, created, author) "
                 "VALUES ('2026-10-07','g','nokian','winter',16,'x','',4,?,?,0,0,0,0,0,0,?,'order',?,'o')", (total, total, "Заказ №%d" % card, NOW))


@pytest.fixture
def w(tmp_path):
    """Two Avito accounts and their points А (Руставели) and Б (Юнона). The client «Общий» bought at both points (one phone), has a second
    phone known to point Б only (a join), a chat and a call with each account. «Чужой» is point Б's only; «Звонивший» called account А only."""
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    acc_a, acc_b = (connections.create_account(conn, n, NOW, "o") for n in ("Аккаунт-Руставели", "Аккаунт-Юнона"))
    pa = points.save_point(conn, {"name": "Руставели", "manager_name": "А", "channel": "vk", "avito_account_id": str(acc_a)})
    pb = points.save_point(conn, {"name": "Юнона", "manager_name": "Б", "channel": "vk", "avito_account_id": str(acc_b)})
    order_a = card_of_phone(conn, {"client_name": "Общий", "client_phone": PHONE_SHARED, "point_id": str(pa)}, NOW, "o")
    order_b = card_of_phone(conn, {"client_name": "Общий", "client_phone": PHONE_SHARED, "point_id": str(pb)}, NOW, "o")
    order_b2 = card_of_phone(conn, {"client_name": "Общий второй", "client_phone": PHONE_B_ONLY, "point_id": str(pb)}, NOW, "o")
    order_x = card_of_phone(conn, {"client_name": "Чужой Иван", "client_phone": PHONE_FOREIGN, "point_id": str(pb)}, NOW, "o")
    _item(conn, order_a, "Nokian Своя-шина 205/55 R16", 5000)
    _item(conn, order_b, "Nokian Шина-Юноны 205/55 R16", 7777, qty=2)
    _item(conn, order_x, "Nokian Шина-Чужого 205/55 R16", 6666)
    shared = clients.client_of_order(conn, order_a)["id"]
    assert clients.client_of_order(conn, order_b)["id"] == shared
    clients.merge(conn, shared, clients.client_of_order(conn, order_b2)["id"], NOW, "o")
    chat_a = _chat(conn, acc_a, "pa", "Чат-Руставели", order_a)
    chat_b = _chat(conn, acc_b, "pb", "Чат-Юноны", order_b)
    chat_x = _chat(conn, acc_b, "px", "Чат-Чужого", order_x)
    call_a = _call(conn, acc_a, PHONE_SHARED)
    call_b = _call(conn, acc_b, PHONE_SHARED)
    call_caller = _call(conn, acc_a, PHONE_CALLER)
    _sale(conn, order_a, 20000)
    _sale(conn, order_b, 15554)
    for card in (order_a, order_x):
        orders.add_event(conn, card, NOW, "система", "point", "Красная запись заказа №%d" % card, level="red")
    conn.commit()
    caller = clients.client_of_call(conn, call_caller)
    foreign = clients.client_of_order(conn, order_x)["id"]
    manager, _ = person(app, "point", pa, login_="rust")
    return dict(app=app, conn=conn, acc_a=acc_a, acc_b=acc_b, pa=pa, pb=pb, order_a=order_a, order_b=order_b, order_b2=order_b2, order_x=order_x,
                shared=shared, foreign=foreign, caller=caller, chat_a=chat_a, chat_b=chat_b, chat_x=chat_x, call_a=call_a, call_b=call_b,
                manager=manager)


def _text(response):
    return response.get_data(as_text=True)


# ---- 1. by the address ------------------------------------------------------------------------------------------------------------

def test_a_point_opens_nothing_of_another_point_by_the_address(w):
    m, conn = w["manager"], w["conn"]
    for url in ("/orders/%d" % w["order_b"], "/orders/%d" % w["order_x"], "/chats/%d" % w["chat_b"], "/chats/%d" % w["chat_x"],
                "/calls/%d/record" % w["call_b"], "/clients/%d" % w["foreign"]):
        assert m.get(url).status_code == 403, url
    other_event = conn.execute("SELECT id FROM order_event WHERE order_id = ? AND level = 'red'", (w["order_x"],)).fetchone()[0]
    assert m.get("/alerts/%d/go" % other_event).status_code == 403
    for url, data in (("/orders/%d/save" % w["order_b"], {"client_name": "x"}), ("/orders/%d/move" % w["order_b"], {"stage_id": "1"}),
                      ("/orders/%d/note" % w["order_b"], {"text": "x"}), ("/orders/%d/delete" % w["order_b"], {}),
                      ("/chats/%d/send" % w["chat_b"], {"text": "x"}), ("/chats/%d/status" % w["chat_b"], {"status": "closed"}),
                      ("/calls/%d/handoff" % w["call_b"], {}), ("/calls/%d/order" % w["call_b"], {}), ("/alerts/%d/ack" % other_event, {}),
                      ("/clients/%d/save" % w["shared"], {"name": "x"}), ("/clients/%d/tag" % w["shared"], {"tag": "x", "on": "1"}),
                      ("/clients/%d/merge" % w["shared"], {"other": str(w["foreign"])}),
                      ("/clients/%d/unmerge/%d" % (w["shared"], w["foreign"]), {}),
                      ("/clients/%d/split-order/%d" % (w["shared"], w["order_a"]), {})):
        assert m.post(url, data=dict(data, csrf="tok")).status_code == 403, url
    assert orders.get_card(conn, w["order_b"])["client_name"] == "Общий"
    assert m.get("/clients/duplicates").status_code == 403


def test_the_own_things_open(w):
    m = w["manager"]
    for url in ("/orders/%d" % w["order_a"], "/chats/%d" % w["chat_a"], "/my-point"):
        assert m.get(url).status_code == 200, url


# ---- 2. through the JSON ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("q", [PHONE_SHARED, PHONE_B_ONLY, PHONE_FOREIGN, "Чужой", "Общий", "Юнона", "Шина-Юноны"])
def test_the_search_json_gives_nothing_of_another_point(w, q):
    found = w["manager"].get("/search.json", query_string={"q": q}).get_json()
    urls = {i["url"] for g in found["groups"] for i in g["items"]}
    text = repr(found["groups"])                                                          # (the answer repeats the query itself)
    for foreign in ("/orders/%d" % w["order_b"], "/orders/%d" % w["order_b2"], "/orders/%d" % w["order_x"], "/chats/%d" % w["chat_b"],
                    "/chats/%d" % w["chat_x"], "/clients/%d" % w["foreign"]):
        assert foreign not in urls, (q, foreign)
    assert "Чужой" not in text and "Юнона" not in text and PHONE_B_ONLY[2:] not in re.sub(r"\D", "", text.replace(PHONE_SHARED[2:], ""))


def test_the_search_by_number_finds_the_own_order_and_never_the_foreign_one(w):
    m = w["manager"]
    for q in ("№%d" % w["order_x"], "Ч-%d" % w["chat_x"], "#%d" % w["order_b"]):
        assert m.get("/search.json", query_string={"q": q}).get_json()["groups"] == [], q
    own = m.get("/search.json", query_string={"q": "№%d" % w["order_a"]}).get_json()
    assert "/orders/%d" % w["order_a"] in repr(own)


def test_the_search_finds_no_client_for_a_point(w):
    for q in (PHONE_SHARED, PHONE_B_ONLY, "Общий"):
        found = w["manager"].get("/search.json", query_string={"q": q}).get_json()
        assert [g for g in found["groups"] if g["key"] == "clients"] == [], q                 # no «CRM» (the owner 2026-10-29)


def test_the_feeds_and_the_file_are_the_points_own_whatever_p_says(w):
    m = w["manager"]
    feed = m.get("/alerts/feed").get_json()
    assert all(i.get("order_id") in (w["order_a"], None) for i in feed["items"])
    assert "Чужой" not in repr(m.get("/my-point/feed?p=%d" % w["pb"]).get_json())
    board = _text(m.get("/orders?p=%d" % w["pb"]))
    assert "Чужой Иван" not in board and "Общий второй" not in board and "№%d" % w["order_a"] in board
    r = m.get("/orders/export.xlsx?p=%d" % w["pb"])
    if r.status_code == 200 and r.mimetype.endswith("sheet"):                              # openpyxl installed
        import io
        import openpyxl
        sheet = openpyxl.load_workbook(io.BytesIO(r.data)).active
        ids = {row[0] for row in sheet.iter_rows(min_row=2, values_only=True)}
        assert ids == {w["order_a"]}


# ---- 3. no CRM for a point (the owner 2026-10-29) ------------------------------------------------------------------------------------

def test_a_point_has_no_crm_its_own_clients_included(w):
    m = w["manager"]
    for url in ("/clients", "/clients?q=%s" % PHONE_SHARED, "/clients/%d" % w["shared"], "/clients/%d" % w["caller"], "/clients/%d" % w["foreign"],
                "/clients/duplicates"):
        assert m.get(url).status_code == 403, url
    for page in (_text(m.get("/orders/%d" % w["order_a"])), _text(m.get("/chats/%d" % w["chat_a"])), _text(m.get("/my-point"))):
        assert 'href="/clients' not in page                                                   # no link to a client card, no menu item


def test_the_owner_still_sees_the_whole_client(w):
    owner = w["app"].test_client()
    login(owner)
    page = _text(owner.get("/clients/%d" % w["shared"]))
    assert "Шина-Юноны" in page and "Чат-Юноны" in page and "Склеенные" in page and "Nokian Своя-шина" in page


def test_a_client_of_another_point_merged_away_or_missing_is_not_opened(w):
    m, conn = w["manager"], w["conn"]
    assert m.get("/clients/999999").status_code in (403, 404)
    gone = clients.client_of_order(conn, w["order_b2"])                                   # merged into «Общий»: its own links moved
    assert gone is None or gone["id"] == w["shared"]
    merged = conn.execute("SELECT id FROM client WHERE merged_into = ?", (w["shared"],)).fetchone()[0]
    assert m.get("/clients/%d" % merged).status_code == 403


# ---- 4. money -------------------------------------------------------------------------------------------------------------------

def test_the_board_of_a_point_shows_no_sums_and_the_card_shows_the_clients_prices_without_the_purchase(w):
    m = w["manager"]
    for view in ("board", "list"):
        page = _text(m.get("/orders?v=%s" % view))
        assert "Сумма в работе" not in page and "20%s000" % NBSP not in page and "₽" not in page, view
    card = _text(m.get("/orders/%d" % w["order_a"]))
    assert re.search(r'value="5000(\.0+)?"', card) and "20%s000" % NBSP in card            # the client's prices: the point takes the cash
    assert "Закупка" not in card and "3%s100" % NBSP not in card and "12%s400" % NBSP not in card and "прибыл" not in card.lower()
    owner = w["app"].test_client()
    login(owner)
    assert "Сумма в работе" in _text(owner.get("/orders")) and "20%s000" % NBSP in _text(owner.get("/orders"))


# ---- 5. the orders of accounts 1 and 2: seen when the order names the point ------------------------------------------------------------

def test_an_order_of_another_account_is_the_points_when_it_names_the_point(w):
    conn, m = w["conn"], w["manager"]
    main = connections.create_account(conn, "Основной", NOW, "o")
    named = card_of_phone(conn, {"client_name": "С основного", "point_id": str(w["pa"])}, NOW, "o")
    unnamed = card_of_phone(conn, {"client_name": "Без точки"}, NOW, "o")
    chat = _chat(conn, main, "pm", "Чат-основного", named)
    conn.commit()
    assert m.get("/orders/%d" % named).status_code == 200 and "С основного" in _text(m.get("/orders"))
    assert m.get("/orders/%d" % unnamed).status_code == 403 and "Без точки" not in _text(m.get("/orders"))     # postponed (п. 5): unchanged
    assert m.get("/chats/%d" % chat).status_code == 403                                   # the chat of the other account stays closed
    assert m.get("/clients/%d" % clients.client_of_order(conn, named)["id"]).status_code == 403          # no CRM (the owner 2026-10-29)


# ---- 6. the invitation ------------------------------------------------------------------------------------------------------------

def test_the_invitation_of_a_point_leads_to_my_point(w):
    app, conn = w["app"], w["conn"]
    owner = app.test_client()
    tok = login(owner)
    assert post(owner, "/users/invite", tok, role="point", point_id=str(w["pb"]), display_name="Юля").status_code == 302
    link = re.search(r'value="(http://localhost/invite/[^"]+)"', _text(owner.get("/users"))).group(1).replace("http://localhost", "")
    guest = app.test_client()
    assert "Менеджер точки" in _text(guest.get(link))
    assert guest.post(link, data={"csrf": _csrf(guest), "login": "yulia", "password": PASSWORD, "password2": PASSWORD}).status_code == 302
    assert guest.get(link).status_code == 404                                              # once
    assert guest.post("/login", data={"csrf": _csrf(guest), "login": "yulia", "password": PASSWORD}).status_code == 302
    home = guest.get("/")
    assert home.status_code == 302 and home.headers["Location"].endswith("/my-point")
    assert users.get_role(conn, "yulia")["point_id"] == w["pb"]
    for url in ("/catalog", "/economics", "/connections/webmim", "/users", "/orders/report"):
        assert guest.get(url).status_code == 403, url
    users.set_active(conn, "yulia", False, NOW, "owner")
    fresh = app.test_client()
    assert fresh.post("/login", data={"csrf": _csrf(fresh), "login": "yulia", "password": PASSWORD}).status_code == 401


def test_an_invitation_or_a_role_for_a_point_that_is_not_there_is_refused(w):
    conn = w["conn"]
    for point in (999, None, 0):
        with pytest.raises(users.UserError):
            users.create_invite(conn, "point", point, "", NOW, "owner")
    gone = points.save_point(conn, {"name": "Янино", "manager_name": "В", "channel": "vk"})
    token = users.create_invite(conn, "point", gone, "", NOW, "owner")
    points.delete_point(conn, gone)
    with pytest.raises(users.UserError):
        users.accept_invite(conn, token, "late1", PASSWORD, NOW)
    assert not users.exists(conn, "late1") and users.find_invite(conn, token, NOW) is not None     # the link is not spent by the refusal
    users.add_user(conn, "vika", PASSWORD, NOW, role="viewer")
    with pytest.raises(users.UserError):
        users.set_role(conn, "vika", "point", 999, "", NOW, "owner")
    owner = w["app"].test_client()
    tok = login(owner)
    post(owner, "/users/invite", tok, role="point", point_id="999", display_name="x")
    assert users.list_invites(conn, NOW) == [i for i in users.list_invites(conn, NOW) if i["point_id"] != 999]


# ---- 7. the matrix ----------------------------------------------------------------------------------------------------------------

def test_the_point_role_matrix(w):
    for section in ("home", "clients", "catalog", "analytics", "setup", "users"):
        assert users.level("point", section) == users.NONE, section
    for right in ("money", "delete", "bulk", "publish", "connections", "supplier"):
        assert not users.has_right("point", right), right
    m = w["manager"]
    for url in ("/catalog", "/economics", "/connections/webmim", "/users", "/orders/report", "/orders/settings", "/shares", "/clients/duplicates"):
        assert m.get(url).status_code == 403, url
    for url in ("/connections/telegram", "/users/invite", "/orders/settings/duty", "/draft/1/confirm"):
        assert m.post(url, data={"csrf": "tok"}).status_code == 403, url


# ---- review 2: the card reads nothing of the whole client; our own numbers are never the client's ------------------------------------

def test_the_points_card_never_reads_the_whole_clients_journal_inquiries_or_tags(w, monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("read the whole client")
    for name in ("inquiries", "client_events", "tags_of", "handed"):
        monkeypatch.setattr(clients, name, never)
    assert w["manager"].get("/clients/%d" % w["shared"]).status_code == 403            # no CRM at all now (the owner 2026-10-29)


def test_our_own_number_typed_into_an_order_is_not_the_clients_phone_for_the_point(w):
    conn, own = w["conn"], "+79006660066"
    conn.execute("UPDATE point SET manager_phone = ? WHERE id = ?", (own, w["pa"]))
    card = card_of_phone(conn, {"client_name": "Свой по нашему номеру", "client_phone": own, "point_id": str(w["pa"])}, NOW, "o")
    if clients.client_of_order(conn, card) is None:                                      # an order typed with our number joins nobody:
        conn.execute("INSERT INTO client_order (client_id, order_id, how, at) VALUES (?,?,'manual',?)", (w["shared"], card, NOW))   # by hand
    conn.commit()
    m = w["manager"]
    assert [g for g in m.get("/search.json", query_string={"q": own}).get_json()["groups"] if g["key"] == "clients"] == []
