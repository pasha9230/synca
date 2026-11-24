"""ТЗ №79 of the owner (2026-11-24), CRM: the list of the clients with its filters — where the client first came from (an Avito account,
VK, a point, a call, by hand), a name or none, the orders (but the cancelled) 0 / 1 / 2 and more, the dates «появился в базе» / «последний
заказ» from–to —, the search (the name, a phone in any writing, an article from four characters, a WebMIM number, a transfer number, the
order's number, the text of the chats), all of them together; the view kept in the address, the way to «Возможные дубли» and back keeps
it (the tab «Клиенты» of «Новый вид» was lost there), the pages, no query per row, a point never gets the filters."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlencode

import pytest

from panel_helpers import login, make_app, post
from order_fixtures import card_of_phone
from tires_sync import clients, orders, store, users

NOW = "2026-11-24T12:00:00"


@pytest.fixture
def conn(tmp_path):
    c = store.connect(str(tmp_path / "t.db"))
    orders.ensure_pipeline(c, NOW)
    return c


def _order(conn, phone, name="Иван", at=NOW):
    card = card_of_phone(conn, {"client_name": name, "client_phone": phone}, at, "o")
    conn.execute("UPDATE order_card SET created_at = ? WHERE id = ?", (at, card))
    conn.commit()
    return card


def _cancel(conn, card):
    conn.execute("UPDATE order_card SET stage_id = (SELECT id FROM order_stage WHERE code = 'cancel') WHERE id = ?", (card,))
    conn.commit()


def _ts(text):
    """A moment of Moscow as Avito's unix time."""
    return int(datetime.fromisoformat(text + "+03:00").astimezone(timezone.utc).timestamp())


def _chat(conn, peer, name, account, started, texts=()):
    row = conn.execute("INSERT INTO avito_chat (account_id, chat_id, peer_id, peer_name, created_ts) VALUES (?,?,?,?,?)",
                       (account, "c-" + peer, peer, name, _ts(started))).lastrowid
    for text in texts:
        _message(conn, row, text)
    client_id = clients.attach_chat(conn, row, None, NOW, "o")
    conn.commit()
    return client_id, row


def _message(conn, chat_row, text):
    n = conn.execute("SELECT COUNT(*) FROM avito_message").fetchone()[0]
    conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction, text) VALUES (?,?,?,?,?,?)",
                 (chat_row, "m%d" % n, NOW, n, "in", text))
    conn.commit()


def _call(conn, phone, at):
    n = conn.execute("SELECT COUNT(*) FROM avito_call").fetchone()[0]
    row = conn.execute("INSERT INTO avito_call (account_id, call_id, call_at, buyer_phone, answered) VALUES (1, ?, ?, ?, 1)",
                       (5000 + n, at, phone)).lastrowid
    client_id = clients.attach_call(conn, row, at, "calls", own=set())
    conn.commit()
    return client_id


def _world(conn) -> dict:
    """Иван: one order typed by hand (11-01) with an article, a WebMIM number and a transfer; a client with no name: its first order a
    handoff of VK (11-10), a second (11-12), a third cancelled (11-15); Татьяна: a chat of the account 2 started 10-01 (the client made
    only now), no order; a caller with no name (09-15), no order."""
    ivan_order = _order(conn, "+79001112233", "Иван", "2026-11-01T10:00:00")
    conn.execute("UPDATE order_card SET webmim_number = '7654321', transfer_number = '26R-004417' WHERE id = ?", (ivan_order,))
    conn.execute("INSERT INTO order_item (order_id, goods_id, code, name, qty, supplier_price, sale_unit) VALUES (?, '100', 'ART-55821X', "
                 "'Ikon Autograph Snow 3 205/55 R16', 4, 5000, 6200)", (ivan_order,))
    first = _order(conn, "+79005556677", "", "2026-11-10T10:00:00")
    conn.execute("INSERT INTO order_handoff_source (order_id, kind) VALUES (?, 'vk')", (first,))
    _order(conn, "+79005556677", "", "2026-11-12T10:00:00")
    _cancel(conn, _order(conn, "+79005556677", "", "2026-11-15T10:00:00"))
    tanya, chat = _chat(conn, "p1", "Татьяна", 2, "2026-10-01T12:00:00", ["Нужны шины Nokian Hakkapeliitta на зиму", "размер 4417?"])
    caller = _call(conn, "+79007778899", "2026-09-15T10:00:00")
    conn.commit()
    return dict(ivan=clients.client_of_order(conn, ivan_order)["id"], ivan_order=ivan_order, nameless=clients.client_of_order(conn, first)["id"],
                tanya=tanya, chat=chat, caller=caller)


def ids(conn, **args):
    return [r["id"] for r in clients.list_page(conn, clients.list_filter(args))["rows"]]


def test_every_filter_alone(conn):
    w = _world(conn)
    assert ids(conn) == [w["caller"], w["tanya"], w["nameless"], w["ivan"]]                       # nothing chosen: everybody, the newest first
    assert ids(conn, nm="yes") == [w["tanya"], w["ivan"]] and ids(conn, nm="no") == [w["caller"], w["nameless"]]
    assert ids(conn, ord="0") == [w["caller"], w["tanya"]] and ids(conn, ord="1") == [w["ivan"]]
    assert ids(conn, ord="2") == [w["nameless"]]                                                   # three orders, one cancelled: 2
    assert ids(conn, src="a2") == [w["tanya"]] and ids(conn, src="a1") == [] and ids(conn, src="call") == [w["caller"]]
    assert ids(conn, src="vk") == [w["nameless"]] and ids(conn, src="manual") == [w["ivan"]] and ids(conn, src="point") == []
    assert ids(conn, df="2026-09-01", dt="2026-09-30") == [w["caller"]]
    assert ids(conn, df="2026-10-01", dt="2026-10-01") == [w["tanya"]]                             # the chat's start, both ends included
    assert ids(conn, dk="last", df="2026-11-12", dt="2026-11-12") == [w["nameless"]]
    assert ids(conn, dk="last", df="2026-11-13") == []                                             # the cancelled order of 11-15 is no «last order»
    assert ids(conn, dk="last", dt="2026-11-05") == [w["ivan"]]


def test_the_filters_and_the_search_act_together(conn):
    w = _world(conn)
    assert ids(conn, nm="no", ord="2") == [w["nameless"]] and ids(conn, src="vk", ord="0") == []
    assert ids(conn, q="Иван", ord="0") == [] and ids(conn, q="Иван", ord="1", src="manual", nm="yes") == [w["ivan"]]
    assert ids(conn, q="Татьяна", df="2026-11-01") == []                                            # she came in October
    assert ids(conn, tag=clients.HANDED) == [w["nameless"]] and ids(conn, tag=clients.HANDED, nm="yes") == []


@pytest.mark.parametrize("query", ["Иван", "иван", "8 (900) 111-22-33", "+7 900 111 22 33", "9001112233", "8900111", "900-111", "art-5582",
                                   "ART-55821X", "7654321", "26R-004417", "26r-004417", "snow 3"])
def test_the_search_finds_ivan_by_every_field(conn, query):
    w = _world(conn)
    assert ids(conn, q=query) == [w["ivan"]], query


def test_the_search_by_the_order_number_the_chat_and_what_it_never_finds(conn):
    w = _world(conn)
    assert ids(conn, q=str(w["ivan_order"])) == [w["ivan"]]
    assert ids(conn, q="№%d" % w["ivan_order"]) == [w["ivan"]]
    assert ids(conn, q="hakkapeliitta") == [w["tanya"]] and ids(conn, q="НА ЗИМУ") == [w["tanya"]]
    assert ids(conn, q="art") == []                                                                # an article: four characters and more
    assert ids(conn, q="4417") == [w["ivan"]]          # a number: the transfer ending in it, never a chat with the digits (prices, sizes)
    assert ids(conn, q="+7 900 555") == [w["nameless"]] and ids(conn, q="7778") == [w["caller"]]


def test_the_chat_search_looks_in_the_last_messages_only(conn, monkeypatch):
    w = _world(conn)
    other, chat = _chat(conn, "p2", "Пётр", 1, "2026-11-20T12:00:00", ["добрый день"])
    monkeypatch.setattr(clients, "CHAT_SEARCH_LAST", 1)                                            # only the very last message is read
    assert ids(conn, q="hakkapeliitta") == [] and ids(conn, q="добрый") == [other]
    conn.execute("UPDATE avito_chat SET last_text = 'Hakkapeliitta есть?' WHERE id = ?", (w["chat"],))   # the last message of every chat is
    assert ids(conn, q="hakkapeliitta") == [w["tanya"]]                                                  # read whatever its age


def test_the_column_counts_the_orders_as_the_filter_and_like_signs_are_letters(conn):
    """The review: «Заказов» of a row is the filter's count (the cancelled left out), in the list and in the top search (list_clients);
    «%» and «_» of a query are themselves, never «anything»."""
    w = _world(conn)
    rows = {r["id"]: r["orders"] for r in clients.list_page(conn, clients.list_filter({}))["rows"]}
    assert rows == {w["ivan"]: 1, w["nameless"]: 2, w["tanya"]: 0, w["caller"]: 0}
    assert [r["orders"] for r in clients.list_clients(conn, "+79005556677", fold=True)] == [2]
    for query in ("_", "%", "%%", "_ван", "Ив%"):
        assert ids(conn, q=query) == [], query
    clients.save(conn, w["ivan"], "Иван_100%", "", NOW, "owner")
    conn.commit()
    assert ids(conn, q="_100%") == [w["ivan"]] and ids(conn, q="н_1") == [w["ivan"]] and ids(conn, q="н 1") == []


def test_the_parameters_are_checked_and_kept_in_the_address():
    f = clients.list_filter({"q": " Иван ", "src": "a12", "nm": "no", "ord": "2", "dk": "last", "df": "2026-11-30", "dt": "2026-11-01", "p": "3"})
    assert (f.query, f.origin, f.named, f.orders, f.date_kind, f.date_from, f.date_to, f.page) == \
        ("Иван", "a12", "no", "2", "last", "2026-11-01", "2026-11-30", 3)                            # the dates the other way round: swapped
    assert f.pairs() == [("q", "Иван"), ("src", "a12"), ("nm", "no"), ("ord", "2"), ("dk", "last"), ("df", "2026-11-01"),
                         ("dt", "2026-11-30"), ("p", "3")]
    bad = clients.list_filter({"src": "http://example.com", "nm": "x", "ord": "5", "dk": "zz", "df": "2026-13-45", "dt": "вчера", "p": "-3"})
    assert bad.pairs() == [] and not bad.active and bad.page == 1 and bad.date_kind == "first"
    assert clients.list_filter({}).pairs() == [] and clients.list_filter({"p": "абв"}).page == 1


def test_the_pages_and_no_query_per_row(conn, monkeypatch):
    monkeypatch.setattr(clients, "LIST_PAGE", 2)
    made = [clients.client_of_order(conn, _order(conn, "+7900123%04d" % (n * 37 + 1000), "Клиент %d" % n))["id"] for n in range(5)]
    found = clients.list_page(conn, clients.list_filter({"p": "2"}))
    assert [r["id"] for r in found["rows"]] == made[::-1][2:4] and (found["total"], found["page"], found["pages"]) == (5, 2, 3)
    assert clients.list_page(conn, clients.list_filter({"p": "99"}))["page"] == 3                     # past the last: the last

    def statements(args) -> int:
        seen = []
        conn.set_trace_callback(seen.append)
        try:
            clients.list_page(conn, clients.list_filter(args))
        finally:
            conn.set_trace_callback(None)
        return len(seen)

    every = {"q": "клиент", "nm": "yes", "ord": "1", "src": "manual", "df": "2026-01-01", "dt": "2026-12-31"}
    monkeypatch.setattr(clients, "LIST_PAGE", 100)
    few = statements(every)
    for n in range(5, 40):
        _order(conn, "+7900123%04d" % (n * 37 + 1000), "Клиент %d" % n)
    assert statements(every) == few                                                                 # 40 clients read with as many queries as 5


# ---- the pages of the panel ------------------------------------------------------------------------------------------------------------

def _app(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    return app, conn


def test_the_list_carries_its_view_to_the_duplicates_and_back(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    app, conn = _app(tmp_path)
    w = _world(conn)
    order_b = _order(conn, "+79004445566", "Ваня")                                                 # a pair of «Возможные дубли»: one phone
    b = clients.client_of_order(conn, order_b)["id"]
    conn.execute("UPDATE order_card SET client_phone = '+79001112233' WHERE id = ?", (order_b,))
    conn.commit()
    owner = app.test_client()
    tok = login(owner)
    view = [("q", "Иван"), ("nm", "yes"), ("ord", "1"), ("p", "1")]
    kept = urlencode([("q", "Иван"), ("nm", "yes"), ("ord", "1")])                                 # the first page: no «p»
    html = owner.get("/clients?" + urlencode(view)).get_data(as_text=True)
    assert 'href="/clients/duplicates?%s"' % kept.replace("&", "&amp;") in html and "/clients/%d\"" % w["ivan"] in html
    for name in ("q", "tag", "src", "nm", "ord", "dk", "df", "dt"):
        assert 'name="%s"' % name in html, name
    assert 'selected>Есть имя</option>' in html and "Найдено: 1" in html
    dups = owner.get("/clients/duplicates?" + kept).get_data(as_text=True)
    assert 'href="/clients?%s">← Клиенты</a>' % kept.replace("&", "&amp;") in dups and "Ваня" in dups
    assert 'name="list" value="%s"' % kept.replace("&", "&amp;") in dups
    r = post(owner, "/clients/%d/skip/%d" % (w["ivan"], b), tok, back="duplicates", list=kept)
    assert r.status_code == 302 and r.headers["Location"].endswith("/clients/duplicates?" + kept)
    r = post(owner, "/clients/%d/skip/%d" % (w["ivan"], b), tok, back="duplicates", list="next=http://example.com&src=//example.com")
    assert r.headers["Location"].endswith("/clients/duplicates")                                   # only the list's own values come back
    assert 'href="/clients/duplicates"' in owner.get("/clients").get_data(as_text=True)            # no filter: the bare address, as before


def test_the_filters_are_kept_for_the_menu_but_never_the_page_number(tmp_path, monkeypatch):
    """The review: «CRM» of the menu opens the filters chosen last (page_prefs), always on the first page."""
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    monkeypatch.setattr(clients, "LIST_PAGE", 1)
    app, conn = _app(tmp_path)
    _world(conn)
    owner = app.test_client()
    login(owner)
    same = {"Referer": "http://localhost/clients"}
    assert owner.get("/clients?nm=no", headers=same).status_code == 200
    assert "2 из 2" in owner.get("/clients?nm=no&p=2", headers=same).get_data(as_text=True)     # the second page, chosen on the page
    r = owner.get("/clients", headers={"Referer": "http://localhost/orders"})                     # the menu, from another page
    assert r.status_code == 302 and r.headers["Location"].endswith("/clients?nm=no")
    assert "1 из 2" in owner.get(r.headers["Location"]).get_data(as_text=True)


def test_the_new_view_keeps_the_tabs_on_the_duplicates(tmp_path, monkeypatch):
    """The cause of п. 1: in «Новый вид» «Возможные дубли» was the old page with no tabs — no «Клиенты» to go back by, another look."""
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    app, conn = _app(tmp_path)
    _world(conn)
    store.set_state(conn, "ui_new.office", "all")
    conn.commit()
    owner = app.test_client()
    login(owner)
    html = owner.get("/clients?ord=2&src=vk").get_data(as_text=True)
    assert 'class="card o2-crm-list"' in html and 'href="/clients/duplicates?src=vk&amp;ord=2"' in html and 'class="select o2-crm-f"' in html
    dups = owner.get("/clients/duplicates?src=vk&ord=2").get_data(as_text=True)
    assert "office2-pages.css" in dups and 'class="tabs o2-tabs"' in dups
    assert '<a href="/clients?src=vk&amp;ord=2">Клиенты</a>' in dups
    assert '<a class="is-active" href="/clients/duplicates?src=vk&amp;ord=2" aria-current="page">Возможные дубли</a>' in dups
    plain = owner.get("/clients/duplicates").get_data(as_text=True)
    assert '<a href="/clients">Клиенты</a>' in plain


def test_a_point_has_no_crm_with_the_filters_either(tmp_path, monkeypatch):
    """A point has no «CRM» (the owner 2026-10-29): the filters and the way to the duplicates open nothing to it."""
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    app, conn = _app(tmp_path)
    _world(conn)
    pid = conn.execute("INSERT INTO point (name) VALUES ('Бета')").lastrowid
    users.add_user(conn, "pointman", "длинный-пароль-1", NOW, role="point", point_id=pid)
    conn.commit()
    point = app.test_client()
    with point.session_transaction() as s:
        s["user"], s["epoch"], s["csrf"] = "pointman", store.get_session_epoch(conn, "pointman"), "tok"
    for url in ("/clients?src=vk&ord=2&nm=no&q=Иван", "/clients/duplicates?ord=2"):
        assert point.get(url).status_code == 403, url
    assert point.post("/clients/1/skip/2", data={"csrf": "tok", "back": "duplicates", "list": "ord=2"}).status_code == 403
