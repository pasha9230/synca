"""The «Чаты» section: list, a chat with its deal, statuses, linking, account modes and the menu counter."""
from __future__ import annotations

import re
from datetime import datetime

from panel_helpers import login, make_app, post
from test_chat_deals import Fake
from tires_sync import chat_deals as deals, chats, connections, orders, store
from tires_sync.panel.routes_chats import FILTERS

NOW = "2026-10-13T12:00:00"


def setup(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 13, 9, 0, 0))
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    account = connections.create_account(conn, "Основной", NOW, "o")
    connections.set_modes(conn, account, True, True, NOW, "o")
    fake = Fake()
    fake.add("c1", "Татьяна", [("in", "Здравствуйте, это актуально? <b>жирный</b>")])
    fake.add("c2", "Иван", [("out", "Добрый день!")], item=222, updated=1_791_900_100)
    chats.sync_account(conn, fake, account, NOW)
    deals.process_all(conn, NOW, auto_create=True)
    return app, client, tok, conn, account


def test_the_chats_page_needs_a_login_and_lists_chats_by_the_name_of_the_client_only(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    assert app.test_client().get("/chats").status_code == 302
    page = client.get("/chats").get_data(as_text=True)
    assert "Татьяна" in page and "Иван" in page
    rows = page.split('class="chat-row')[1:]
    assert rows and all("Заказ №" not in r.split("</a>")[0] and "badge--neutral" not in r.split("</a>")[0] for r in rows)       # the list: dot, name, time, message
    assert all('class="strong chat-name"' in r for r in rows) and "chat-dot" in page
    assert "<b>жирный</b>" not in page and "&lt;b&gt;жирный&lt;/b&gt;" in page                           # the client's text never becomes markup
    assert "Чаты" in page and 'href="/chats"' in page


def test_a_chat_opens_with_the_conversation_the_deal_block_and_the_stage_switch(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    row = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Татьяна")
    page = client.get("/chats/%d" % row["id"]).get_data(as_text=True)
    assert "Здравствуйте, это актуально?" in page and "Заказ №%d" % row["order_id"] in page and "/orders/%d" % row["order_id"] in page
    assert "Сделка" not in page and "сделк" not in page                                     # one word for the order (owner's decision 2026-10-21)
    assert 'name="stage_id"' in page and "Отправка ответов выключена" in page
    assert client.get("/chats/99999").status_code == 404


def test_filters_unread_without_a_deal_and_account(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    ivan = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Иван")
    assert ivan["order_id"] is None                                                                   # only our own words: no deal
    page = client.get("/chats?f=nodeal").get_data(as_text=True)
    assert "Иван" in page and "Татьяна" not in page
    page = client.get("/chats?f=unread").get_data(as_text=True)
    assert "Татьяна" in page and "Иван" not in page
    assert "Татьяна" in client.get("/chats?acc=%d" % account).get_data(as_text=True)
    assert "Татьяна" not in client.get("/chats?acc=999").get_data(as_text=True)


def _menus(page):
    """{the label of a filter button: [(item, href, on)]} of the list's filter row."""
    found = {}
    for label, body in re.findall(r'<details class="menu chats-filter-menu"><summary class="btn[^"]*">([^<]*)</summary>(.*?)</details>', page, re.S):
        found[label] = [(item, href, bool(on)) for on, href, item in re.findall(r'<a class="menu-item( is-on)?" href="([^"]*)"[^>]*>([^<]*)</a>', body)]
    return found


def test_the_state_and_the_account_are_two_buttons_that_combine_and_keep_their_parameters(tmp_path):
    """Owner's word 2026-10-24 (5): the filters split into «Состояние» and «Аккаунт» (was one «Фильтр» button, ab73f62); links, no selects."""
    app, client, tok, conn, account = setup(tmp_path)
    page = client.get("/chats").get_data(as_text=True)
    assert 'name="f"' not in page and 'name="acc"' not in page and "Показать" not in page                      # no selects, no «Показать»
    menus = _menus(page)
    assert list(menus) == ["Состояние: Открытые"]                                                               # one account: no «Аккаунт» button
    assert menus["Состояние: Открытые"] == [(v, "/chats" + ("?f=" + k if k else ""), k == "") for k, v in FILTERS.items()]
    assert list(FILTERS.values()) == ["Открытые", "Ждут ответа", "Не прочитанные", "Без заказа", "Не требуют ответа", "Закрытые"]
    assert "Всего: 2" in page and 'href="/deleted?k=chat"' in page
    second = connections.create_account(conn, "Партнёр-2", NOW, "o")
    connections.set_modes(conn, second, True, True, NOW, "o")
    menus = _menus(client.get("/chats?f=unread&acc=%d&q=Тат" % account).get_data(as_text=True))
    assert list(menus) == ["Состояние: Не прочитанные", "Аккаунт: Основной"]
    states = {item: (href, on) for item, href, on in menus["Состояние: Не прочитанные"]}
    assert states["Закрытые"] == ("/chats?f=closed&amp;acc=%d&amp;q=%%D0%%A2%%D0%%B0%%D1%%82" % account, False)       # a state keeps the account and the search
    assert states["Не прочитанные"][1] and [on for _, on in states.values()].count(True) == 1
    accounts = {item: (href, on) for item, href, on in menus["Аккаунт: Основной"]}
    assert accounts == {"Все аккаунты": ("/chats?f=unread&amp;q=%D0%A2%D0%B0%D1%82", False),                  # an account keeps the state and the search
                        "Основной": ("/chats?f=unread&amp;acc=%d&amp;q=%%D0%%A2%%D0%%B0%%D1%%82" % account, True),
                        "Партнёр-2": ("/chats?f=unread&amp;acc=%d&amp;q=%%D0%%A2%%D0%%B0%%D1%%82" % second, False)}
    assert list(_menus(client.get("/chats").get_data(as_text=True))) == ["Состояние: Открытые", "Аккаунт: Все аккаунты"]


def test_a_stale_account_in_the_address_shows_in_the_filter_and_can_be_dropped(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    menus = _menus(client.get("/chats?acc=999").get_data(as_text=True))
    assert list(menus) == ["Состояние: Открытые", "Аккаунт: аккаунт №999"]
    assert ("Все аккаунты", "/chats", False) in menus["Аккаунт: аккаунт №999"]


def _say(conn, chat_row, at, direction="in"):
    ts = int(datetime.fromisoformat(at).timestamp())
    conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction, kind, text) VALUES (?,?,?,?,?,'text','x')",
                 (chat_row, "w-%s-%s" % (at, direction), at, ts, direction))
    conn.execute("UPDATE avito_chat SET last_dir = ?, last_at = ? WHERE id = ?", (direction, at, chat_row))


def _names(page):
    listing = page.split('class="card chats-list"')[1].split("</section>")[0]
    return re.findall(r'<span class="strong chat-name">([^<]*)</span>', listing)


def test_the_state_waits_for_an_answer_shows_the_open_chats_whose_client_waits(tmp_path):
    """«Ждут ответа»: the same clock as the badge «ждёт N мин» (an open chat, a message of the client after the last answer of a person,
    the chat newer than the first read of its account)."""
    app, client, tok, conn, account = setup(tmp_path)
    tanya = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Татьяна")
    ivan = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Иван")
    for row in (tanya, ivan):
        conn.execute("DELETE FROM avito_message WHERE chat_row = ?", (row["id"],))
    _say(conn, tanya["id"], "2026-10-13T11:35:00")
    _say(conn, ivan["id"], "2026-10-13T11:00:00")
    _say(conn, ivan["id"], "2026-10-13T11:10:00", "out")                                         # Иван is answered
    store.set_state(conn, "chats_baseline_%d" % account, "2026-10-13T09:00:00")
    conn.commit()
    page = client.get("/chats?f=wait").get_data(as_text=True)
    assert _names(page) == ["Татьяна"] and "Всего: 1" in page and "ждёт 25 мин" in page
    assert _names(client.get("/chats?f=wait&acc=%d" % account).get_data(as_text=True)) == ["Татьяна"]          # it combines with the account
    assert _names(client.get("/chats?f=wait&acc=999").get_data(as_text=True)) == []
    for status in ("closed", "no_reply"):                                                         # a closed chat or «не требует ответа» does not wait
        conn.execute("UPDATE avito_chat SET status = ? WHERE id = ?", (status, tanya["id"]))
        conn.commit()
        assert _names(client.get("/chats?f=wait").get_data(as_text=True)) == []
    conn.execute("UPDATE avito_chat SET status = 'open' WHERE id = ?", (tanya["id"],))
    store.set_state(conn, "chats_baseline_%d" % account, "2026-10-13T12:00:00")                  # the chat is older than the first read: the past
    conn.commit()
    assert _names(client.get("/chats?f=wait").get_data(as_text=True)) == []
    resp = post(client, "/chats/%d/status" % tanya["id"], tok, status="closed", f="wait", acc=str(account))
    assert "f=wait" in resp.headers["Location"] and "acc=%d" % account in resp.headers["Location"]


def test_the_open_chat_stays_in_the_list_when_it_no_longer_fits_the_state(tmp_path):
    """Owner's word 2026-10-24 (6): the picked chat is not lost. Answered in «Ждут ответа», closed in «Открытые», read in «Не прочитанные»:
    after the reload it stays in its place in the list, highlighted, and «Всего» counts only the chats of the state."""
    app, client, tok, conn, account = setup(tmp_path)
    tanya = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Татьяна")
    ivan = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Иван")
    for row in (tanya, ivan):
        conn.execute("DELETE FROM avito_message WHERE chat_row = ?", (row["id"],))
    _say(conn, tanya["id"], "2026-10-13T11:00:00")
    _say(conn, ivan["id"], "2026-10-13T11:35:00")
    store.set_state(conn, "chats_baseline_%d" % account, "2026-10-13T09:00:00")
    conn.commit()
    assert _names(client.get("/chats?f=wait").get_data(as_text=True)) == ["Иван", "Татьяна"]
    _say(conn, tanya["id"], "2026-10-13T11:40:00", "out")                                         # answered: Татьяна no longer waits
    conn.execute("UPDATE avito_chat SET last_at = '2026-10-13T11:00:00' WHERE id = ?", (tanya["id"],))   # her place in the list is kept
    conn.commit()
    assert _names(client.get("/chats?f=wait").get_data(as_text=True)) == ["Иван"]                  # not open: only the chats of the state
    page = client.get("/chats/%d?f=wait" % tanya["id"]).get_data(as_text=True)
    assert _names(page) == ["Иван", "Татьяна"] and "Всего: 1" in page
    assert re.search(r'<a class="chat-row is-active[^"]*" href="/chats/%d\?f=wait">' % tanya["id"], page)
    conn.execute("UPDATE avito_chat SET status = 'closed' WHERE id = ?", (ivan["id"],))                # closed in «Открытые»
    conn.commit()
    page = client.get("/chats/%d" % ivan["id"]).get_data(as_text=True)
    assert _names(page) == ["Иван", "Татьяна"] and "Всего: 1" in page
    assert _names(client.get("/chats/%d?acc=999" % ivan["id"]).get_data(as_text=True)) == []          # the account filter still holds
    assert _names(client.get("/chats/%d?q=Татьяна" % ivan["id"]).get_data(as_text=True)) == ["Татьяна"]   # and the search
    conn.execute("UPDATE avito_chat SET unread = 0")
    conn.commit()
    assert _names(client.get("/chats/%d?f=unread" % tanya["id"]).get_data(as_text=True)) == ["Татьяна"]


def test_the_list_keeps_its_place_after_a_reload_and_after_update_list():
    """app.js: the scroll of the list is kept in this tab (sessionStorage, by state, account and search) when the page is loaded again
    (a click on a chat, a form, F5); the open chat is brought into sight moving the list only; «Обновить список» redraws the list in place."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel"
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    place = js[js.index("the list keeps its place"):js.index("the order column folds away")]
    assert 'sessionStorage.getItem(KEY)' in place and 'sessionStorage.setItem(KEY' in place and 'addEventListener("pagehide"' in place
    assert place.count("try {") == 2 and place.count("catch (e)") == 2                                # no storage: the list as before
    assert "live.dataset.place" in place and "all[place] = list.scrollTop" in place                    # by the list as the server reads it
    assert "list.innerHTML" not in place and "after(bar).forEach" in place                              # «Обновить список»: the search and the menus stay
    assert "scrollIntoView" not in place and "list.scrollTop +=" in place                            # only the list moves, never the page
    assert 'closest("[data-chats-relist]")' in place and "e.preventDefault()" in place and 'getElementById("chats-offline")' in place
    assert 'href="" data-chats-relist>Обновить список</a>' in (root / "templates" / "chats.html").read_text(encoding="utf-8")


def test_the_key_of_the_list_place_is_the_list_as_the_server_reads_it(tmp_path):
    """The address may hold a search with extra spaces or an unknown state: the place of the list is kept by what the server shows."""
    app, client, tok, conn, account = setup(tmp_path)
    tanya = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Татьяна")
    place = lambda url: re.search(r'id="chats-live"[^>]* data-place="([^"]*)"', client.get(url).get_data(as_text=True)).group(1)
    assert place("/chats") == ""
    assert place("/chats?f=wait&acc=%d&q=%%20%%20Тат%%20" % account) == place("/chats/%d?f=wait&acc=%d&q=Тат" % (tanya["id"], account)) \
        == "?f=wait&amp;acc=%d&amp;q=%%D0%%A2%%D0%%B0%%D1%%82" % account
    assert place("/chats?f=nonsense") == ""


def test_a_row_of_the_list_shows_the_name_the_time_and_the_last_message_not_the_ad(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    conn.execute("UPDATE avito_chat SET item_title = 'Ikon Autograph Snow 3 205/55 R16'")
    conn.commit()
    rows = client.get("/chats").get_data(as_text=True).split('class="chat-row')[1:]
    assert rows and all("Ikon Autograph" not in r.split("</a>")[0] and "chat-meta" not in r.split("</a>")[0] for r in rows)
    second = connections.create_account(conn, "Партнёр-2", NOW, "o")
    connections.set_modes(conn, second, True, True, NOW, "o")
    rows = client.get("/chats").get_data(as_text=True).split('class="chat-row')[1:]
    assert all('<div class="small muted chat-last chat-meta">Основной</div>' in r.split("</a>")[0] for r in rows)   # several accounts: the account, small


def test_status_link_unlink_create_and_stage_actions(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    tanya = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Татьяна")
    ivan = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Иван")
    post(client, "/chats/%d/status" % tanya["id"], tok, status="no_reply")
    assert chats.get_chat(conn, tanya["id"])["status"] == "no_reply"
    post(client, "/chats/%d/status" % tanya["id"], tok, status="bogus")                                  # refused
    assert chats.get_chat(conn, tanya["id"])["status"] == "no_reply"
    post(client, "/chats/%d/create-deal" % ivan["id"], tok)
    assert chats.get_chat(conn, ivan["id"])["order_id"] is not None
    post(client, "/chats/%d/unlink" % ivan["id"], tok)
    assert chats.get_chat(conn, ivan["id"])["order_id"] is None and chats.get_chat(conn, ivan["id"])["unlinked"] == 1
    mine = orders.create_card(conn, dict(client_name="Пётр"), NOW, "o")
    post(client, "/chats/%d/link" % ivan["id"], tok, order_id=str(mine))
    assert chats.get_chat(conn, ivan["id"])["order_id"] == mine
    post(client, "/chats/%d/link" % ivan["id"], tok, order_id="9999")                                    # no such deal: a message, nothing changes
    assert chats.get_chat(conn, ivan["id"])["order_id"] == mine
    talk = next(s["id"] for s in orders.list_stages(conn) if s["code"] == "talk")
    post(client, "/chats/%d/stage" % ivan["id"], tok, stage_id=str(talk))
    assert orders.get_card(conn, mine)["stage_id"] == talk
    assert client.post("/chats/%d/status" % tanya["id"], data={"status": "open"}).status_code == 400   # no CSRF token


def test_the_deal_card_lists_its_chats_and_the_menu_shows_unread(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    tanya = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Татьяна")
    page = client.get("/orders/%d" % tanya["order_id"]).get_data(as_text=True)
    assert "Чаты Авито" in page and "/chats/%d" % tanya["id"] in page
    assert 'class="sb-count">1<' in client.get("/orders").get_data(as_text=True).replace(" ", "") or "sb-count" in client.get("/orders").get_data(as_text=True)
    mimic = client.get("/chats").get_data(as_text=True)
    assert "sb-link" in mimic


def test_chats_of_an_account_without_the_chats_mode_stay_out_of_the_list(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    connections.set_modes(conn, account, False, True, NOW, "o")
    assert "Татьяна" not in client.get("/chats").get_data(as_text=True)


def test_account_modes_are_saved_on_the_account_page(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    assert "chats_enabled" in client.get("/connections/avito/%d" % account).get_data(as_text=True)
    post(client, "/connections/accounts/%d/modes" % account, tok, chats_enabled="1")                      # feed_enabled left out: off
    got = connections.get_account(conn, account)
    assert got["chats_enabled"] == 1 and got["feed_enabled"] == 0
    post(client, "/connections/accounts/%d/modes" % account, tok, chats_enabled="1", feed_enabled="1")
    assert connections.get_account(conn, account)["feed_enabled"] == 1


def test_saving_account_modes_returns_to_an_existing_page(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    resp = post(client, "/connections/accounts/%d/modes" % account, tok, chats_enabled="1", feed_enabled="1", chats_send="1")
    assert resp.status_code == 302
    target = resp.headers["Location"]
    assert client.get(target).status_code == 200                                                         # not a 404 after «Сохранить»


def test_the_deal_block_lists_the_earlier_deals_of_the_client_and_the_filter_is_kept_after_an_action(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    tanya = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Татьяна")
    ivan = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Иван")                           # the same Avito user in the test data
    post(client, "/chats/%d/create-deal" % ivan["id"], tok)
    other = chats.get_chat(conn, ivan["id"])["order_id"]
    page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    assert "писал раньше" in page and "/orders/%d" % other in page
    resp = post(client, "/chats/%d/status" % tanya["id"], tok, status="closed", f="unread", acc=str(account))
    assert "f=unread" in resp.headers["Location"] and "acc=%d" % account in resp.headers["Location"]
    post(client, "/chats/merge", tok, from_order=str(other), into_order=str(tanya["order_id"]))               # the manual move of chats stays
    assert chats.get_chat(conn, ivan["id"])["order_id"] == tanya["order_id"]


def test_the_reply_form_is_off_until_the_account_is_allowed_then_it_sends_once(tmp_path):
    from http_fakes import ScriptedHttp
    from tires_sync.secretbox import create_key_file, load_box
    app, client, tok, conn, account = setup(tmp_path)
    key_file = tmp_path / "secret.key"
    create_key_file(key_file)
    box = load_box(key_file)
    connections.set_secret(conn, box, "avito", account, "client_id", "CLIENTID-12345678", NOW, "o")
    connections.set_secret(conn, box, "avito", account, "client_secret", "SECRETVALUE-XYZ", NOW, "o")
    app.config["SECRET_BOX"] = box
    http = ScriptedHttp((200, {"access_token": "t", "expires_in": 3600}),
                        (200, {"id": "m-out", "created": 1_791_900_500, "direction": "out", "type": "text", "content": {"text": "Добрый день"}}))
    app.config["AVITO_HTTP"] = http
    tanya = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Татьяна")
    page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    assert "Отправка ответов выключена" in page and 'name="reply_token"' not in page
    post(client, "/chats/%d/send" % tanya["id"], tok, text="Добрый день", reply_token="t1")                        # refused: off
    assert not [c for c in http.calls if "messages" in c[1] and c[0] == "POST"]
    post(client, "/connections/accounts/%d/modes" % account, tok, chats_enabled="1", feed_enabled="1", chats_send="1")
    assert connections.get_account(conn, account)["chats_send"] == 1
    page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    token = re.search(r'name="reply_token" value="([0-9a-f]+)"', page).group(1)
    post(client, "/chats/%d/send" % tanya["id"], tok, text="Добрый день", reply_token=token)
    post(client, "/chats/%d/send" % tanya["id"], tok, text="Добрый день", reply_token=token)                       # a double press
    assert len([c for c in http.calls if c[0] == "POST" and "/messenger/" in c[1]]) == 1
    assert "Добрый день" in client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    assert client.post("/chats/%d/send" % tanya["id"], data={"text": "x"}).status_code == 400                      # no CSRF token


def test_the_open_page_asks_for_fresh_messages_and_the_signature_follows_the_chat(tmp_path):
    from http_fakes import ScriptedHttp
    from tires_sync.secretbox import create_key_file, load_box
    app, client, tok, conn, account = setup(tmp_path)
    key_file = tmp_path / "secret.key"
    create_key_file(key_file)
    box = load_box(key_file)
    connections.set_secret(conn, box, "avito", account, "client_id", "CLIENTID-12345678", NOW, "o")
    connections.set_secret(conn, box, "avito", account, "client_secret", "SECRETVALUE-XYZ", NOW, "o")
    app.config["SECRET_BOX"] = box
    tanya = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Татьяна")
    page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    sig = re.search(r'id="chats-live" data-sig="([0-9a-f]+)"', page).group(1)
    quiet = ScriptedHttp((200, {"access_token": "t", "expires_in": 3600}), (200, {"chats": [], "meta": {"has_more": False}}))
    app.config["AVITO_HTTP"] = quiet
    first = client.post("/chats/refresh", data={"csrf": tok, "chat": str(tanya["id"])}).get_json()
    assert first["sig"] == sig and len(quiet.calls) == 2                                                       # token and the list: who we are is known; nothing new
    again = client.post("/chats/refresh", data={"csrf": tok, "chat": str(tanya["id"])}).get_json()
    assert len(quiet.calls) == 2 and again["sig"] == sig                                                       # the second press within 15 s asks Avito for nothing
    conn.execute("UPDATE avito_chat SET unread = unread + 1 WHERE id = ?", (tanya["id"],))
    conn.commit()
    assert client.post("/chats/refresh", data={"csrf": tok, "chat": str(tanya["id"])}).get_json()["sig"] != sig        # something came: the page would reload
    assert client.post("/chats/refresh", data={"chat": "1"}).status_code == 400                                 # no CSRF token
    assert app.test_client().post("/chats/refresh", data={"csrf": tok}).status_code in (302, 400)


def _sendable_page(tmp_path, *send_replies):
    from http_fakes import ScriptedHttp
    from tires_sync.secretbox import create_key_file, load_box
    app, client, tok, conn, account = setup(tmp_path)
    key_file = tmp_path / "secret.key"
    create_key_file(key_file)
    box = load_box(key_file)
    connections.set_secret(conn, box, "avito", account, "client_id", "CLIENTID-12345678", NOW, "o")
    connections.set_secret(conn, box, "avito", account, "client_secret", "SECRETVALUE-XYZ", NOW, "o")
    app.config["SECRET_BOX"] = box
    app.config["AVITO_HTTP"] = ScriptedHttp((200, {"access_token": "t", "expires_in": 3600}), *send_replies)
    post(client, "/connections/accounts/%d/modes" % account, tok, chats_enabled="1", feed_enabled="1", chats_send="1")
    tanya = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Татьяна")
    return app, client, tok, conn, tanya


def _token(client, row):
    return re.search(r'name="reply_token" value="([0-9a-f]+)"', client.get("/chats/%d" % row["id"]).get_data(as_text=True)).group(1)


def test_after_an_unknown_outcome_the_text_does_not_come_back_into_the_form(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path, (502, {"error": {"message": "bad gateway"}}))
    post(client, "/chats/%d/send" % tanya["id"], tok, text="Уникальный ответ клиенту", reply_token=_token(client, tanya))
    page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    assert "Неизвестно, ушло ли сообщение" in page
    form = re.search(r"<textarea[^>]*>(.*?)</textarea>", page, re.S)
    assert form is not None and "Уникальный ответ клиенту" not in form.group(1)           # one press of «Отправить» could have sent it twice


def test_after_a_definite_refusal_the_typed_text_is_kept_for_a_retry(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path, (403, {"error": {"message": "forbidden"}}))
    post(client, "/chats/%d/send" % tanya["id"], tok, text="Ответ, который нужно сохранить", reply_token=_token(client, tanya))
    form = re.search(r"<textarea[^>]*>(.*?)</textarea>", client.get("/chats/%d" % tanya["id"]).get_data(as_text=True), re.S)
    assert form is not None and "Ответ, который нужно сохранить" in form.group(1)


def test_a_failed_record_after_a_delivered_message_is_neither_an_error_nor_a_second_send(tmp_path):
    from tires_sync.avito import AvitoError

    class Flaky:
        """The database refuses the status update after the message has left."""
        def __init__(self, inner):
            self.inner = inner

        def execute(self, sql, *a):
            if sql.startswith("UPDATE chat_send_log SET status = ?") and a and a[0][0] == "sent":       # the mark «sent» (chats._mark)
                raise __import__("sqlite3").OperationalError("database is locked")
            return self.inner.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    class Sender:
        calls = 0

        def get_self(self, retry=True):
            return {"id": 1}

        def send_message(self, *a):
            Sender.calls += 1
            return {"id": "m1", "created": 1_791_900_500}
    app, client, tok, conn, tanya = _sendable_page(tmp_path)
    chats.send_reply(Flaky(conn), Sender(), tanya["id"], "Ответ", "tok-1", NOW, "o")           # does not raise
    assert Sender.calls == 1
    import pytest
    with pytest.raises(chats.ChatError):
        chats.send_reply(conn, Sender(), tanya["id"], "Ответ", "tok-1", NOW, "o")              # the same token is not sent again
    assert Sender.calls == 1


def _position_setup(tmp_path, monkeypatch, item_args):
    from tires_sync import order_offers
    from tires_sync.panel import routes_chats
    app, client, tok, conn, account = setup(tmp_path)
    conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, title, seen_at) VALUES (?, 222, 'G7-x4', 't', ?)", (account, NOW))
    conn.commit()
    monkeypatch.setattr(routes_chats, "context", lambda app_: (None, object(), object(), object()))
    monkeypatch.setattr(order_offers, "item_args", item_args)
    ivan = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Иван")
    return client, tok, conn, ivan, account


def test_the_create_deal_button_adds_the_position_of_the_ad_of_the_chat(tmp_path, monkeypatch):
    seen = []

    def item_args(conn_, settings, parsed, ref, goods_id, day, account_id=None):
        seen.append((goods_id, account_id))
        return dict(goods_id=goods_id, code="T7", name="Шина", brand_key="ikon", season="winter", diameter=15, territory="1", sale_unit=5750,
                    supplier_price=4800, terms=None, fee_pct=0, promo_pct=0)
    client, tok, conn, ivan, account = _position_setup(tmp_path, monkeypatch, item_args)
    post(client, "/chats/%d/create-deal" % ivan["id"], tok)
    deal = orders.get_card(conn, chats.get_chat(conn, ivan["id"])["order_id"])
    assert seen == [("G7", account)] and [(i["code"], i["qty"]) for i in deal["items"]] == [("T7", 4)]


def test_the_create_deal_button_still_makes_the_deal_when_the_position_cannot_be_priced(tmp_path, monkeypatch):
    def item_args(*a):
        raise RuntimeError("broken unload")
    client, tok, conn, ivan, _account = _position_setup(tmp_path, monkeypatch, item_args)
    post(client, "/chats/%d/create-deal" % ivan["id"], tok)
    deal = orders.get_card(conn, chats.get_chat(conn, ivan["id"])["order_id"])
    assert deal["items"] == []
    assert any("Позиция не добавлена" in e["text"] for e in orders.list_events(conn, deal["id"]))           # the manager sees why


def test_the_create_deal_button_says_so_when_the_unload_cannot_be_read(tmp_path, monkeypatch):
    from tires_sync.panel import routes_chats
    client, tok, conn, ivan, _account = _position_setup(tmp_path, monkeypatch, lambda *a: {})

    def broken(app_):
        raise RuntimeError("no unload")
    monkeypatch.setattr(routes_chats, "context", broken)
    post(client, "/chats/%d/create-deal" % ivan["id"], tok)
    deal = orders.get_card(conn, chats.get_chat(conn, ivan["id"])["order_id"])
    assert deal["items"] == [] and any("не прочитана" in e["text"] for e in orders.list_events(conn, deal["id"]))


def test_the_title_of_the_ad_in_the_open_chat_leads_to_our_catalog_and_avito_is_a_small_second_link(tmp_path, monkeypatch):
    from tires_sync.panel import routes_chats
    app, client, tok, conn, account = setup(tmp_path)
    conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, title, seen_at) VALUES (?, 222, 'G7-x4', 't', ?)", (account, NOW))
    conn.execute("UPDATE avito_chat SET item_title = 'Ikon 185/55 R15', item_url = 'https://www.avito.ru/x_222' WHERE item_id = '222'")
    conn.commit()
    from helpers import make_offer
    offer = make_offer(goods_id="G7", code="TS1", stock=6)
    monkeypatch.setattr(routes_chats, "context", lambda app_: (None, None, type("P", (), {"offers": {"G7": offer}})(), None))
    connections.set_config(conn, "webmim", 0, {"base_url": "https://mim.example", "mode": "login"}, NOW, "o")
    ivan = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Иван")
    page = client.get("/chats/%d" % ivan["id"]).get_data(as_text=True)
    assert 'class="strong" href="/catalog?q=TS1"' in page and "Ikon 185/55 R15" in page            # the title leads to the catalog
    assert ">В каталоге</a>" in page and "Проверить у поставщика" not in page and "mim.example" not in page
    assert "у поставщика по последней выгрузке: 6 шт." in page
    avito = re.search(r'<a [^>]*href="https://www.avito.ru/x_222"[^>]*>(.*?)</a>', page)
    assert avito and avito.group(1) == "на Авито" and 'class="small muted"' in avito.group(0)       # the Avito page stays, small and second
    assert 'href="https://www.avito.ru/x_222" rel="noopener noreferrer" target="_blank">Ikon' not in page


def test_an_ad_that_is_not_ours_has_the_catalog_button_with_a_search_by_model_and_size_but_no_supplier_button(tmp_path):
    from helpers import catalog_xml
    from test_panel_catalog import ROWS
    app, client, tok, conn, account = setup(tmp_path)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*ROWS))
    conn.execute("UPDATE avito_chat SET item_title = 'Ikon Test Model 215/60 R17 94T', item_url = 'https://www.avito.ru/y' WHERE item_id = '222'")
    conn.commit()
    ivan = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Иван")
    page = client.get("/chats/%d" % ivan["id"]).get_data(as_text=True)
    assert ">В каталоге</a>" in page and "Проверить у поставщика" not in page and "Артикул" not in page
    link = re.search(r'class="strong" href="(/catalog\?[^"]+)"', page).group(1).replace("&amp;", "&")
    assert re.search(r'class="btn btn--secondary btn--xs" href="%s" target="_blank" rel="noopener">В каталоге' % re.escape(link.replace("&", "&amp;")), page)           # opens in a new window: the chat stays
    assert "94" not in link                                                                         # the index of load and speed is dropped from the search
    assert sorted(re.findall(r">(C[123])<", client.get(link).get_data(as_text=True))) == ["C2"]   # the search finds that model and size
    assert 'href="https://www.avito.ru/y"' in page                                                  # the Avito link stays


def test_the_search_of_a_foreign_ad_drops_the_index_after_the_size_and_the_filler_words_only():
    from tires_sync.panel.routes_chats import _title_search
    assert _title_search("Pirelli Formula Ice 225/45 R17 94H") == "Pirelli Formula Ice 225/45R17"
    assert _title_search("Шины 205/55R16 91V Nokian Hakkapeliitta 9") == "205/55R16 Nokian Hakkapeliitta 9"
    assert _title_search("Cordiant 195/65 R15 98/96T XL") == "Cordiant 195/65R15"
    assert _title_search("Nokian Hakkapeliitta 10p SUV 215/55 R17 98T") == "Nokian Hakkapeliitta 10p SUV 215/55R17"
    assert _title_search("Шины 205/55 R16 94Н") == "205/55R16"
    assert _title_search("Зимние шины 205/55 R16 91 V Kama 505") == "205/55R16 Kama 505"
    assert _title_search("Michelin 195/75 R16C 107/105R") == "Michelin 195/75R16"
    assert _title_search("Шины 225/45R17 Pirelli Formula Ice 94Н") == "225/45R17 Pirelli Formula Ice"
    assert _title_search("Pirelli P Zero 245/40 ZR18 97Y XL") == "Pirelli P Zero 245/40R18"
    assert _title_search("Кама 205/55 Р16 91Н") == "Кама 205/55R16"
    assert _title_search("Nokian Hakkapeliitta 9 205/55 R16 комплект 4 шт.") == "Nokian Hakkapeliitta 9 205/55R16"
    assert _title_search("XL Pirelli Run Flat 225/45 R17 94H RF") == "Pirelli 225/45R17"
    assert _title_search("Michelin 225/45/17 94V") == "Michelin 225/45R17"
    assert _title_search("Шина Cordiant 195 R14C 106/104Q") == "Cordiant"
    assert _title_search("Шины 225/45 R17. 94H Pirelli") == "225/45R17 Pirelli"
    assert _title_search("Pirelli 225/45 R17 (4 шт.)") == "Pirelli 225/45R17"
    assert _title_search("Pirelli 225/45 ZR17 XL 94W Run-Flat") == "Pirelli 225/45R17"
    assert _title_search("") == ""


def test_the_open_chats_page_updates_in_place_and_never_reloads_itself(tmp_path):
    """The list of chats must not jump under the manager's hand: no reload of the page, no reordering. (Checked in a real browser by hand on 2026-10-19:
    a message in another chat refreshed its row where it stood, the order stayed, the note offered a fresh list.)"""
    app, client, tok, conn, account = setup(tmp_path)
    js = client.get("/static/app.js").get_data(as_text=True)
    block = js[js.index("«Чаты»"):]
    assert "location.reload" not in block.split("window.setInterval(ask, 15000)")[0]
    assert 'id="chats-order"' in client.get("/chats").get_data(as_text=True)


def test_the_catalog_link_of_a_chat_opens_exactly_that_position_and_shows_its_article(tmp_path):
    from helpers import catalog_xml
    from test_panel_catalog import ROWS
    app, client, tok, conn, account = setup(tmp_path)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*ROWS))
    conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, title, seen_at) VALUES (?, 222, '2-x4', 't', ?)", (account, NOW))
    conn.execute("UPDATE avito_chat SET item_title = 'Ikon 215/60 R17', item_url = 'http://insecure.example/x' WHERE item_id = '222'")
    conn.commit()
    ivan = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Иван")
    page = client.get("/chats/%d" % ivan["id"]).get_data(as_text=True)
    link = re.search(r'class="strong" href="(/catalog\?[^"]+)"', page).group(1).replace("&amp;", "&")
    assert "Артикул C2" in page and "http://insecure.example/x" not in page                         # a non-https page of Avito is not offered
    found = sorted(re.findall(r">(C[123])<", client.get(link).get_data(as_text=True)))
    assert found == ["C2"]                                                                          # the link opens that position and no other


def test_a_position_that_left_the_unload_is_shown_as_not_found_and_has_no_supplier_button(tmp_path, monkeypatch):
    from tires_sync.panel import routes_chats
    app, client, tok, conn, account = setup(tmp_path)
    conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, title, seen_at) VALUES (?, 222, 's2-77-x4', 't', ?)", (account, NOW))
    conn.execute("UPDATE avito_chat SET item_title = 'Ушла' WHERE item_id = '222'")
    conn.commit()
    two = connections.create_supplier(conn, "Второй", NOW, "o")
    connections.set_config(conn, "webmim", connections.supplier_owner(two), {"base_url": "https://two.example", "mode": "login"}, NOW, "o")
    connections.set_config(conn, "webmim", 0, {"base_url": "https://one.example", "mode": "login"}, NOW, "o")
    monkeypatch.setattr(routes_chats, "context", lambda app_: (None, None, type("P", (), {"offers": {}})(), None))
    ivan = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Иван")
    page = client.get("/chats/%d" % ivan["id"]).get_data(as_text=True)
    assert "two.example" not in page and "one.example" not in page and "Проверить у поставщика" not in page and "в последней выгрузке не найден" in page


def _wait_setup(tmp_path, monkeypatch, offers, title="Ikon Tyres 195 65 R15 91T Test Model"):
    from tires_sync.panel import routes_chats
    app, client, tok, conn, account = setup(tmp_path)
    conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, title, seen_at) VALUES (?, 222, 'G7-x4', 't', ?)", (account, NOW))
    conn.execute("UPDATE avito_chat SET item_title = ? WHERE item_id = '222'", (title,))
    conn.commit()
    monkeypatch.setattr(routes_chats, "context", lambda app_: (None, None, type("P", (), {"offers": {o.goods_id: o for o in offers}})(), None))
    ivan = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Иван")
    return client, tok, conn, ivan


def test_the_wait_form_starts_with_the_tire_of_the_ad_from_the_catalog_with_four_pieces(tmp_path, monkeypatch):
    from helpers import make_offer
    client, tok, conn, ivan = _wait_setup(tmp_path, monkeypatch, [make_offer(goods_id="G7", code="TS1", brand_key="IKON TYRES", model_raw="Test Model", width=195, height=65, diameter=15)])
    page = client.get("/chats/%d" % ivan["id"]).get_data(as_text=True)
    assert "Клиент ждёт эту шину" in page and "Заполнено из каталога" in page
    for piece in ('name="brand" value="IKON TYRES"', 'name="width" value="195"', 'name="profile" value="65"', 'name="radius" value="15"', 'name="qty" value="4"'):
        assert piece in page
    assert '<option value="winter" selected>' in page and 'name="any_size" value="1">' in page and 'name="any_size" value="1" checked' not in page     # the ticks are off


def test_the_wait_form_reads_an_ad_that_is_not_in_the_catalog_from_its_title_and_leaves_the_season_to_the_manager(tmp_path, monkeypatch):
    from helpers import make_offer
    client, tok, conn, ivan = _wait_setup(tmp_path, monkeypatch, [make_offer(goods_id="X1", brand_key="IKON TYRES")], title="Ikon Tyres 225 45 R17 94V")
    page = client.get("/chats/%d" % ivan["id"]).get_data(as_text=True)
    assert "Заполнено из названия объявления" in page and 'name="width" value="225"' in page and 'name="brand" value="IKON TYRES"' in page
    assert "<option value=\"\">выберите</option>" in page and "selected>зима" not in page


def test_the_wait_button_makes_the_deal_and_moves_it_and_a_bad_form_only_says_why(tmp_path, monkeypatch):
    client, tok, conn, ivan = _wait_setup(tmp_path, monkeypatch, [])
    form = dict(brand="Nokian", width="225", profile="45", radius="17", qty="6")
    post(client, "/chats/%d/wait" % ivan["id"], tok, **form)                                          # an unknown tire without a season
    assert chats.get_chat(conn, ivan["id"])["order_id"] is None
    assert "Выберите сезон" in client.get("/chats/%d" % ivan["id"]).get_data(as_text=True)
    post(client, "/chats/%d/wait" % ivan["id"], tok, season="summer", **form)
    card = orders.get_card(conn, chats.get_chat(conn, ivan["id"])["order_id"])
    assert card["stage"]["code"] == "wait" and [(w["brand"], w["season"], w["qty"]) for w in card["wants"]] == [("Nokian", "summer", 6)]


def _photo(size=(2400, 1800)) -> bytes:
    import io
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", size, (20, 120, 200)).save(out, "JPEG")
    return out.getvalue()


def _image_token(client, row):
    return re.search(r'name="image_token" value="([0-9a-f]+)"', client.get("/chats/%d" % row["id"]).get_data(as_text=True)).group(1)


def _send_photo(client, row, tok, token, data, name="photo.jpg"):
    import io
    return client.post("/chats/%d/send-image" % row["id"], data={"csrf": tok, "image_token": token, "photo": (io.BytesIO(data), name)},
                       content_type="multipart/form-data")


def test_the_photo_button_sends_one_small_jpeg_once_per_press_and_says_so(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path, (200, {"img-9": {"1600x1200": "https://img.example/9.jpg"}}),
                                                   (200, {"id": "m-img", "created": 1_791_900_500}))
    page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    assert 'action="/chats/%d/send"' % tanya["id"] in page and 'enctype="multipart/form-data"' in page and 'accept="image/*"' in page    # one form: tests/test_chats_send.py; the old route stays for an open page
    token = re.search(r'name="image_token" value="([0-9a-f]+)"', page).group(1)
    assert token != re.search(r'name="reply_token" value="([0-9a-f]+)"', page).group(1)                       # its own mark, not the one of the text form
    assert _send_photo(client, tanya, tok, token, _photo()).status_code == 302
    assert _send_photo(client, tanya, tok, token, _photo()).status_code == 302                                 # a double press
    http = app.config["AVITO_HTTP"]
    uploads = [c for c in http.calls if c[1].endswith("/uploadImages")]
    sends = [c for c in http.calls if c[1].endswith("/messages/image")]
    assert len(uploads) == 1 and len(sends) == 1
    from PIL import Image
    import io
    body = uploads[0][3]
    picture = Image.open(io.BytesIO(body[body.index(b"\xff\xd8"):]))
    assert picture.format == "JPEG" and max(picture.size) == 1600                                              # the server shrinks it too, whatever the browser did
    page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    assert "Фото отправлено клиенту" in page and "📷 Фото отправлено" in page


def test_the_photo_button_is_there_only_when_replies_are_allowed_and_is_refused_otherwise(tmp_path):
    from http_fakes import ScriptedHttp
    app, client, tok, conn, account = setup(tmp_path)
    app.config["AVITO_HTTP"] = http = ScriptedHttp()
    tanya = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Татьяна")
    page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    assert "/send-image" not in page and 'name="image_token"' not in page
    response = _send_photo(client, tanya, tok, "a" * 24, _photo())
    assert response.status_code == 302 and not http.calls                                                     # nothing goes to Avito
    assert client.post("/chats/%d/send-image" % tanya["id"], data={"image_token": "x"}).status_code == 400   # no CSRF token
    assert app.test_client().post("/chats/%d/send-image" % tanya["id"], data={"csrf": tok}).status_code in (302, 400)


def test_a_missing_or_wrong_file_is_refused_with_a_reason_and_nothing_is_sent(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path)
    token = _image_token(client, tanya)
    client.post("/chats/%d/send-image" % tanya["id"], data={"csrf": tok, "image_token": token}, content_type="multipart/form-data")
    assert "Выберите фото" in client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    _send_photo(client, tanya, tok, token, b"%PDF-1.4 not a picture", "doc.pdf")
    assert "не открывается как фотография" in client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    assert not [c for c in app.config["AVITO_HTTP"].calls if "/messenger/" in c[1]]


def test_after_an_unsure_photo_send_the_page_warns_not_to_send_it_again(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path, (200, {"img-9": {"1600x1200": "u"}}), (502, {"error": {"message": "bad gateway"}}))
    token = _image_token(client, tanya)
    _send_photo(client, tanya, tok, token, _photo((800, 600)))
    assert "Неизвестно, ушло ли фото" in client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    _send_photo(client, tanya, tok, token, _photo((800, 600)))                                                  # the same press again
    assert len([c for c in app.config["AVITO_HTTP"].calls if c[1].endswith("/messages/image")]) == 1


def test_a_body_over_the_limit_of_the_panel_gets_a_plain_message_not_an_empty_page(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path)
    app.config["MAX_CONTENT_LENGTH"] = 1024
    response = _send_photo(client, tanya, tok, "a" * 24, _photo())
    assert response.status_code == 413 and "слишком большой" in response.get_data(as_text=True)


def test_the_conversation_shows_the_day_once_and_only_the_time_at_each_message(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    row = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Татьяна")
    page = client.get("/chats/%d" % row["id"]).get_data(as_text=True)
    at = chats.list_messages(conn, row["id"])[0]["at"]
    assert page.count('class="chat-day"') == 1 and ">Сегодня<" in page
    assert '<div class="msg-meta" title="%s.%s.%s %s">%s<' % (at[8:10], at[5:7], at[:4], at[11:19], at[11:16]) in page     # dates by one rule (UX 4.1)


def test_the_dates_of_the_panel_are_short_and_name_the_day(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 13, 22, 30, 0))          # 01:30 of the 14th in Moscow
    f = app.jinja_env.filters
    with app.app_context():
        assert f["day_label"]("2026-10-14T00:10:00") == "Сегодня" and f["day_label"]("2026-10-13T23:59:00") == "Вчера"
        assert f["day_label"]("2026-03-08T10:00:00") == "8 марта" and f["day_label"]("2025-12-31T10:00:00") == "31 декабря 2025"
        assert f["short_dt"]("2026-10-07T15:46:50") == "07.10 15:46" and f["short_dt"]("2025-10-07 15:46:50") == "07.10.2025 15:46"
        assert f["hm"]("2026-10-07T15:46:50") == "15:46" and f["hm"](None) == "" and f["short_dt"]("вчера") == "вчера"


def test_the_rare_actions_of_a_chat_are_in_the_more_menu_and_the_answer_follows_the_conversation(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    row = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Татьяна")
    page = client.get("/chats/%d" % row["id"]).get_data(as_text=True)
    menu = page[page.index('<details class="menu">'):page.index("</details>", page.index('<details class="menu">'))]
    for action in ("/status", "/unlink", "/delete"):
        assert 'action="/chats/%d%s"' % (row["id"], action) in menu                                  # Закрыть, Не требует ответа, Отвязать, Удалить
    thread, composer = page.index('class="chat-thread"'), page.index('class="chat-composer"')
    assert thread < composer and "/status" not in page[thread:composer] and "/wait" not in page[thread:composer]
    deal = page[page.index('class="card chats-deal"'):]
    assert "Заказ №%d" % row["order_id"] in deal and "<dt>Телефон</dt><dd><span class=\"muted\">не указан</span>" in deal
    assert 'action="/chats/%d/wait"' % row["id"] in deal                                              # «Клиент ждёт эту шину» moved next to the deal
    orders.update_card(conn, row["order_id"], {"client_phone": "+79112223344"}, NOW, "o")
    page = client.get("/chats/%d" % row["id"]).get_data(as_text=True)
    assert 'id="deal-phone" value="+7 911 222-33-44" readonly' in page and 'data-copy="deal-phone"' in page    # one look of a phone (UX 4.3, 2026-10-21)


def test_the_list_shows_the_last_message_time_as_day_month(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    row = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Татьяна")
    page = client.get("/chats").get_data(as_text=True)
    at = row["last_at"]
    assert '<span class="small muted ml-auto nowrap" title="%s.%s.%s %s">%s.%s %s</span>' % (at[8:10], at[5:7], at[:4], at[11:19], at[8:10], at[5:7], at[11:16]) in page


def test_a_move_the_order_lacks_data_for_is_refused_out_loud_and_the_deal_block_names_what_is_missing(tmp_path):
    """Order №226: cancelled, no point, no delivery date. The move to «Перемещение на точку» is refused in red with the gaps named,
    the deal block says what is missing before anyone tries, and once the gaps are filled the same move goes through."""
    from tires_sync import order_points as points
    app, client, tok, conn, account = setup(tmp_path)
    ivan = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Иван")
    post(client, "/chats/%d/create-deal" % ivan["id"], tok)
    cid = chats.get_chat(conn, ivan["id"])["order_id"]
    stage = {s["code"]: s["id"] for s in orders.list_stages(conn)}
    orders.add_item(conn, cid, goods_id="1", name="Nokian 205/55 R16", brand_key="nokian", season="winter", diameter=16, territory="1",
                    qty=4, sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="o")
    orders.update_card(conn, cid, {"client_name": "Алексей", "client_phone": "+79990001122", "cancel_reason": "Не заказ"}, NOW, "o")
    orders.move_card(conn, cid, stage["cancel"], NOW, "o", fire_rules=False)
    page = client.get("/chats/%d" % ivan["id"]).get_data(as_text=True)
    assert "не хватает: <b>точка, дата доставки</b>" in page and 'href="/orders/%d">Заполнить в заказе' % cid in page and "дата не указана" in page
    page = client.post("/chats/%d/stage" % ivan["id"], data={"csrf": tok, "stage_id": str(stage["moving"])}, follow_redirects=True).get_data(as_text=True)
    assert orders.get_card(conn, cid)["stage"]["code"] == "cancel"
    assert re.search(r'class="flash flash--err" role="alert">.*?заполните: точка, дата доставки\. Заполните в заказе №%d' % cid, page, re.S)
    post(client, "/connections/points/save", tok, name="Гамма", manager_name="Сергей", channel="vk")
    orders.update_card(conn, cid, {"point_id": str(points.list_points(conn)[0]["id"]), "delivery_date": "2026-10-21"}, NOW, "o")
    page = client.get("/chats/%d" % ivan["id"]).get_data(as_text=True)
    assert "chat-deal-gaps" not in page and "21.10" in page
    page = client.post("/chats/%d/stage" % ivan["id"], data={"csrf": tok, "stage_id": str(stage["moving"])}, follow_redirects=True).get_data(as_text=True)
    assert orders.get_card(conn, cid)["stage"]["code"] == "moving"
    assert "Заказ №%d перенесён в «Перемещение на точку»." % cid in page and "flash--err" not in page


STUB = "Сообщение не поддерживается. Пожалуйста, перейдите в Авито мессенджер https://avito.ru/profile/messenger/channel/u2i-Ab_9~x."


def test_avitos_stub_of_an_unsupported_message_is_a_plain_label_with_the_link_to_the_chat_in_avito(tmp_path):
    """The owner 2026-10-23: «Сообщение не поддерживается…» was shown raw; now a label and «Открыть чат в Авито» (only a link of Avito's messenger)."""
    app, client, tok, conn, account = setup(tmp_path)
    fake = Fake()
    fake.add("c3", "Ольга", [("in", "Добрый день"), ("in", STUB)])
    chats.sync_account(conn, fake, account, NOW)
    row = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Ольга")
    listing = client.get("/chats").get_data(as_text=True)
    olga = next(r for r in listing.split('class="chat-row')[1:] if "Ольга" in r).split("</a>")[0]
    assert "Сообщение без текста: видно только в Авито" in olga and "не поддерживается" not in olga
    page = client.get("/chats/%d" % row["id"]).get_data(as_text=True)
    assert "Клиент прислал сообщение, которое Авито не показывает через API" in page and "Добрый день" in page
    assert '<a class="btn btn--secondary btn--xs" href="https://avito.ru/profile/messenger/channel/u2i-Ab_9~x" target="_blank" rel="noopener noreferrer">Открыть чат в Авито</a>' in page
    assert "Пожалуйста, перейдите" not in page


def test_the_link_of_the_stub_is_only_avitos_messenger_else_the_chat_of_its_id():
    assert chats.unsupported_link(STUB, "c3") == "https://avito.ru/profile/messenger/channel/u2i-Ab_9~x"
    assert chats.unsupported_link("Сообщение не поддерживается. https://evil.example/profile/messenger/channel/x", "u2i-1") == \
        "https://www.avito.ru/profile/messenger/channel/u2i-1"
    assert chats.unsupported_link("Сообщение не поддерживается.", 'x"><script>') == ""          # an id that is no id: no link at all
    assert chats.unsupported_link("Здравствуйте, сообщение не поддерживается?", "c3") is None and chats.unsupported_link("", "c3") is None


# Run «match2», lane small-tails: the kept open chat says so; «Ждут ответа» reads the start of a wait of a chat once.
def test_the_kept_open_chat_is_marked_out_of_the_filter_and_the_count_says_so(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    tanya = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Татьяна")
    conn.execute("UPDATE avito_chat SET status = 'closed' WHERE id = ?", (tanya["id"],))
    conn.commit()
    page = client.get("/chats/%d?f=noreply" % tanya["id"]).get_data(as_text=True)
    assert _names(page) == ["Татьяна"] and "Всего: 0 · открытый чат вне фильтра" in page
    assert page.count(">вне фильтра</span>") == 1
    page = client.get("/chats/%d?f=closed" % tanya["id"]).get_data(as_text=True)     # it fits: no mark
    assert "Всего: 1 ·" in page and "вне фильтра" not in page
    assert "вне фильтра" not in client.get("/chats").get_data(as_text=True)


def test_an_open_chat_beyond_the_limit_of_the_list_stays_unmarked_it_fits_the_state(tmp_path, monkeypatch):
    from tires_sync.panel import routes_chats
    app, client, tok, conn, account = setup(tmp_path)
    monkeypatch.setattr(routes_chats, "LIST_LIMIT", 1)
    first = _names(client.get("/chats").get_data(as_text=True))
    assert len(first) == 1
    other = next(c for c in chats.list_chats(conn) if c["peer_name"] != first[0])
    page = client.get("/chats/%d" % other["id"]).get_data(as_text=True)
    assert len(_names(page)) == 2 and "Всего: 2 ·" in page and "вне фильтра" not in page


def test_waits_for_an_answer_reads_the_start_of_each_wait_once(tmp_path, monkeypatch):
    app, client, tok, conn, account = setup(tmp_path)
    tanya = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Татьяна")
    ivan = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Иван")
    for row in (tanya, ivan):
        conn.execute("DELETE FROM avito_message WHERE chat_row = ?", (row["id"],))
    _say(conn, tanya["id"], "2026-10-13T11:35:00")
    _say(conn, ivan["id"], "2026-10-13T11:00:00")
    _say(conn, ivan["id"], "2026-10-13T11:10:00", "out")
    store.set_state(conn, "chats_baseline_%d" % account, "2026-10-13T09:00:00")
    conn.commit()
    asked, real = [], chats.wait_starts
    monkeypatch.setattr(chats, "wait_starts", lambda c, rows: (asked.extend(list(rows)), real(c, rows))[1])
    page = client.get("/chats/%d?f=wait" % ivan["id"]).get_data(as_text=True)          # Иван is answered: kept, out of the filter
    assert _names(page) == ["Иван", "Татьяна"] or _names(page) == ["Татьяна", "Иван"]
    assert "ждёт 25 мин" in page and "Всего: 1 · открытый чат вне фильтра" in page
    assert sorted(asked) == sorted([tanya["id"], ivan["id"]])                          # each chat once (was: twice)
