"""«Изменить цену просмотра» of «Аналитика рекламы» (ads_bid_set, routes_ads_bid; the owner 2026-11-13): who may, the bounds of Avito on the
server, «Подтверждаю» for a jump or a big limit, one write per form, a 429 never repeated, the control read into ad_bid, a mismatch told,
«Вернуть прежнюю», the journal, and no write from a client without allow_write. Avito is a fake transport: no request leaves the test."""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from cryptography.fernet import Fernet

from panel_helpers import login, make_app
from tires_sync import ads_bid_set, connections, order_points as points, store, users
from tires_sync.avito import AvitoClient, WriteDisabled
from tires_sync.secretbox import SecretBox

NOW = datetime(2026, 11, 13, 9, 0, 0)            # UTC: 12:00 in Moscow
AD = 4242
PAGE = "/economics/ads/bid/1/%d" % AD


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


class FakeAvito:
    """The transport of the client: the token, getBids, getPromotionsByItemIds (the manual price as last set, or `shown`), setManual."""

    def __init__(self, bid=350, limit=30000, mode="manual", set_status=200):
        self.bid, self.limit, self.mode, self.set_status = bid, limit, mode, set_status
        self.shown = None                      # (bid, limit) Avito shows after a write instead of what was set
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.avito.ru", 1)[1]
        data = json.loads(body.decode()) if body and path != "/token/" else None
        self.calls.append((method, path, data))
        if path == "/token/":
            return 200, {}, b'{"access_token": "t", "expires_in": 3600}'
        if path == "/cpxpromo/1/getBids/%d" % AD:
            return 200, {}, json.dumps({"actionTypeID": 5, "manual": {
                "bidPenny": self.bid, "recBidPenny": 500, "minBidPenny": 100, "maxBidPenny": 3000,
                "bids": [{"valuePenny": v, "minForecast": v // 50, "maxForecast": v // 25} for v in (200, 300, 350, 400, 500, 900, 1500)]}}).encode()
        if path == "/cpxpromo/1/getPromotionsByItemIds":
            if self.mode == "auto":
                entry = {"itemID": AD, "actionTypeID": 5, "autoPromotion": {"budgetPenny": 50000}}
            else:
                bid, limit = self.shown or (self.bid, self.limit)
                entry = {"itemID": AD, "actionTypeID": 5, "manualPromotion": {"bidPenny": bid, "limitPenny": limit}}
            return 200, {}, json.dumps({"items": [entry]}).encode()
        if path == "/cpxpromo/1/setManual":
            if self.set_status != 200:
                return self.set_status, {"retry-after": "1"}, b'{"error": {"code": 429, "message": "too many requests"}}'
            self.bid, self.limit, self.mode = data["bidPenny"], data["limitPenny"], "manual"
            return 200, {}, b'{"success": true}'
        return 404, {}, b"{}"

    def writes(self):
        return [c for c in self.calls if c[1] == "/cpxpromo/1/setManual"]

    def reads(self):
        return [c for c in self.calls if c[1].startswith("/cpxpromo/") and c[1] != "/cpxpromo/1/setManual"]


def setup(tmp_path, http=None):
    http = http or FakeAvito()
    box = SecretBox(Fernet.generate_key())
    app = make_app(tmp_path, NOW=lambda: NOW, SECRET_BOX=box, AVITO_HTTP=http)
    conn = store.connect(app.config["DB_PATH"])
    connections.create_account(conn, "Основной", "2026-11-01T10:00:00", "o")
    for name in ("client_id", "client_secret"):
        connections.set_secret(conn, box, "avito", 1, name, "x", "2026-11-01T10:00:00", "o")
    conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, title, seen_at) VALUES (1, ?, 'G1', 'Nokian Hakka 205/55 R16', '2026-11-11')", (AD,))
    conn.execute("INSERT INTO ad_spend_day (account_id, avito_id, day, spend, views, contacts) VALUES (1, ?, '2026-11-09', 1234.5, 500, 7)", (AD,))
    conn.execute("INSERT INTO ad_spend_day (account_id, avito_id, day) VALUES (1, 0, '2026-11-09')")
    conn.commit()
    return app, conn, http


def owner_of(app):
    client = app.test_client()
    login(client)
    return client


def as_user(app, conn, login_name, role, point_id=None):
    users.add_user(conn, login_name, "длинный-пароль-1", "2026-11-01T10:00:00", role=role, point_id=point_id)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user"], s["epoch"], s["csrf"] = login_name, store.get_session_epoch(conn, login_name), "tok"
    return client


def opened(client):
    """The window as a person opens it (the fresh read) and its form token."""
    page = client.get(PAGE)
    assert page.status_code == 200
    text = page.get_data(as_text=True)
    return text, text.split('name="token" value="', 1)[1].split('"', 1)[0]


def send(client, token, url=PAGE, csrf="tok", **form):
    data = dict(form, token=token)
    if csrf:
        data["csrf"] = csrf
    return client.post(url, data=data, follow_redirects=True)


def journal(conn):
    return [(at, author, json.loads(detail)) for at, author, detail in
            conn.execute("SELECT at, author, detail FROM audit WHERE action = ? ORDER BY id", (ads_bid_set.AUDIT,))]


# ---- who may ----------------------------------------------------------------------------------------------------------------------------

def test_the_owner_opens_the_window_with_a_fresh_read_and_sees_the_button(tmp_path):
    app, conn, http = setup(tmp_path)
    owner = owner_of(app)
    text, _ = opened(owner)
    assert [c[1] for c in http.reads()] == ["/cpxpromo/1/getBids/%d" % AD, "/cpxpromo/1/getPromotionsByItemIds"]
    assert http.writes() == []
    assert "своя 3,50 ₽" in text and "лимит 300,00&nbsp;₽ в день" in text and "от 1,00&nbsp;₽ до 30,00&nbsp;₽" in text
    assert text.count('name="pick" value="') == 6                    # five nearest options, the own value; the recommended is among them
    assert 'value="5.00"' in text and 'name="own"' in text and 'name="limit"' in text and "Подтверждаю" in text
    assert "ДРР объявления за 30 дней" in text
    row = conn.execute("SELECT mode, bid, day_limit, min_bid, max_bid FROM ad_bid WHERE avito_id = ?", (AD,)).fetchone()
    assert tuple(row) == ("manual", 3.5, 300.0, 1.0, 30.0)
    assert "Изменить цену просмотра" in owner.get("/economics/ads").get_data(as_text=True)


def test_the_partner_may_change(tmp_path):
    app, conn, http = setup(tmp_path)
    partner = as_user(app, conn, "partner1", "partner")
    _, token = opened(partner)
    assert "Изменить цену просмотра" in partner.get("/economics/ads").get_data(as_text=True)
    send(partner, token, pick="4.00", limit="300")
    assert len(http.writes()) == 1


@pytest.mark.parametrize("role", ["viewer", "chats"])
def test_the_others_get_403_and_no_button(tmp_path, role):
    app, conn, http = setup(tmp_path)
    other = as_user(app, conn, "u" + role, role)
    assert other.get(PAGE).status_code == 403
    for url in (PAGE, PAGE + "/undo", "/economics/ads/bid-threshold"):
        assert other.post(url, data={"csrf": "tok", "token": "a" * 32, "pick": "4.00", "limit": "300"}).status_code == 403
    assert http.calls == []


def test_a_point_gets_403(tmp_path):
    app, conn, http = setup(tmp_path)
    point_id = points.save_point(conn, {"name": "Юнона", "manager_name": "А", "channel": "panel"})
    point = as_user(app, conn, "p1", "point", point_id)
    assert point.get(PAGE).status_code == 403
    assert point.post(PAGE, data={"csrf": "tok", "token": "a" * 32, "pick": "4.00", "limit": "300"}).status_code == 403
    assert http.calls == []


def test_without_csrf_nothing_is_written(tmp_path):
    app, conn, http = setup(tmp_path)
    owner = owner_of(app)
    _, token = opened(owner)
    assert send(owner, token, csrf="", pick="4.00", limit="300").status_code == 400
    assert send(owner, token, csrf="wrong", pick="4.00", limit="300").status_code == 400
    assert http.writes() == [] and journal(conn) == []


# ---- the checks on the server -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["0,50", "31", "-2"])
def test_a_value_outside_the_bounds_is_refused_by_the_server(tmp_path, value):
    app, conn, http = setup(tmp_path)
    owner = owner_of(app)
    _, token = opened(owner)
    text = send(owner, token, pick="own", own=value, limit="300", confirm="1").get_data(as_text=True)
    assert http.writes() == [] and journal(conn) == []
    assert "вне границ Авито" in text or "больше нуля" in text


def test_a_jump_of_more_than_half_wants_the_box(tmp_path):
    app, conn, http = setup(tmp_path)
    owner = owner_of(app)
    _, token = opened(owner)
    text = send(owner, token, pick="own", own="5,30", limit="300").get_data(as_text=True)           # 3.50 → 5.30: +51 %
    assert http.writes() == [] and "Подтверждаю" in text and "больше чем на 50 %" in text
    text = send(owner, token, pick="own", own="1,70", limit="300").get_data(as_text=True)           # −51 %
    assert http.writes() == []
    send(owner, token, pick="own", own="5,25", limit="300")                                          # +50 % exactly: no box needed
    assert [w[2]["bidPenny"] for w in http.writes()] == [525]
    _, token = opened(owner)
    send(owner, token, pick="own", own="15", limit="300", confirm="1")
    assert [w[2]["bidPenny"] for w in http.writes()] == [525, 1500]


def test_a_limit_above_the_threshold_wants_the_box(tmp_path):
    app, conn, http = setup(tmp_path)
    owner = owner_of(app)
    _, token = opened(owner)
    text = send(owner, token, pick="4.00", limit="1500").get_data(as_text=True)
    assert http.writes() == [] and "выше порога" in text
    send(owner, token, pick="4.00", limit="1000")                                                   # the threshold itself is allowed
    assert [w[2]["limitPenny"] for w in http.writes()] == [100000]


def test_the_threshold_is_a_setting_of_the_section(tmp_path):
    app, conn, http = setup(tmp_path)
    owner = owner_of(app)
    owner.post("/economics/ads/bid-threshold", data={"csrf": "tok", "threshold": "2000", "back": PAGE})
    assert ads_bid_set.threshold(conn) == 2000.0
    _, token = opened(owner)
    send(owner, token, pick="4.00", limit="1500")
    assert len(http.writes()) == 1
    assert conn.execute("SELECT author, action FROM audit WHERE action = ?", (ads_bid_set.THRESHOLD_AUDIT,)).fetchone() is not None


def test_no_limit_is_refused(tmp_path):
    app, conn, http = setup(tmp_path)
    owner = owner_of(app)
    _, token = opened(owner)
    text = send(owner, token, pick="4.00", limit="").get_data(as_text=True)
    assert http.writes() == [] and "Сколько тратить в день" in text


# ---- once -------------------------------------------------------------------------------------------------------------------------------

def test_a_double_press_writes_once(tmp_path):
    app, conn, http = setup(tmp_path)
    owner = owner_of(app)
    _, token = opened(owner)
    send(owner, token, pick="4.00", limit="300")
    text = send(owner, token, pick="4.00", limit="300").get_data(as_text=True)
    assert len(http.writes()) == 1 and "второй раз не отправляю" in text
    send(owner, token, pick="9.00", limit="300", confirm="1")                                     # the same form with other values: still once
    assert len(http.writes()) == 1
    _, other = opened(owner)                                                                       # a new form, the same values within a minute
    send(owner, other, pick="4.00", limit="300")
    assert len(http.writes()) == 1 and len(journal(conn)) == 1


def test_a_429_is_told_and_never_repeated(tmp_path):
    app, conn, http = setup(tmp_path, FakeAvito(set_status=429))
    owner = owner_of(app)
    _, token = opened(owner)
    reads = len(http.reads())
    text = send(owner, token, pick="4.00", limit="300").get_data(as_text=True)
    assert len(http.writes()) == 1 and len(http.reads()) == reads                                  # no repeat, no control read
    assert "Авито просит подождать" in text
    (_, _, detail), = journal(conn)
    assert detail["ok"] is False and detail["answer"].startswith("429")
    http.set_status = 200                                                                           # a new press later goes through
    _, token = opened(owner)
    send(owner, token, pick="4.00", limit="300")
    assert len(http.writes()) == 2


def test_the_panel_keeps_to_20_requests_a_minute(tmp_path):
    app, conn, http = setup(tmp_path)
    owner = owner_of(app)
    for _ in range(10):
        opened(owner)                                                                              # 2 requests each: 20
    assert len(http.reads()) == 20
    text = owner.get(PAGE).get_data(as_text=True)
    assert len(http.reads()) == 20 and "Авито просит подождать" in text and 'name="pick"' not in text


# ---- the control read -------------------------------------------------------------------------------------------------------------------

def test_the_control_read_updates_ad_bid_and_the_window(tmp_path):
    app, conn, http = setup(tmp_path)
    owner = owner_of(app)
    _, token = opened(owner)
    text = send(owner, token, pick="4.00", limit="250").get_data(as_text=True)
    method, path, body = http.writes()[0]
    assert body == {"itemID": AD, "actionTypeID": 5, "bidPenny": 400, "limitPenny": 25000}
    assert http.calls[-1][1] == "/cpxpromo/1/getPromotionsByItemIds"                              # read back right after the write
    row = conn.execute("SELECT bid, day_limit, taken_at FROM ad_bid WHERE avito_id = ?", (AD,)).fetchone()
    assert tuple(row) == (4.0, 250.0, "2026-11-13T09:00:00")
    assert "Цена просмотра на Авито: 4,00 ₽, лимит 250,00 ₽ в день." in text and "своя 4,00 ₽" in text


def test_a_mismatch_with_avito_is_told(tmp_path):
    http = FakeAvito()
    http.shown = (380, 25000)
    app, conn, _ = setup(tmp_path, http)
    owner = owner_of(app)
    _, token = opened(owner)
    text = send(owner, token, pick="4.00", limit="250").get_data(as_text=True)
    assert "Авито показывает не то, что ставили" in text and "3,80 ₽" in text
    assert conn.execute("SELECT bid FROM ad_bid WHERE avito_id = ?", (AD,)).fetchone()[0] == 3.8
    (_, _, detail), = journal(conn)
    assert detail["ok"] is False and detail["got"] == {"mode": "manual", "bid": 3.8, "day_limit": 250.0}


# ---- «Вернуть прежнюю» ------------------------------------------------------------------------------------------------------------------

def test_undo_puts_back_the_price_and_the_limit_before_the_change(tmp_path):
    app, conn, http = setup(tmp_path)
    owner = owner_of(app)
    _, token = opened(owner)
    send(owner, token, pick="4.00", limit="250")
    text, _ = opened(owner)
    assert "Вернуть прежнюю" in text and "цена 3,50&nbsp;₽, лимит 300,00&nbsp;₽" in text
    undo_token = text.split('action="%s/undo"' % PAGE, 1)[1].split('name="token" value="', 1)[1].split('"', 1)[0]
    page = send(owner, undo_token, url=PAGE + "/undo").get_data(as_text=True)
    assert [(w[2]["bidPenny"], w[2]["limitPenny"]) for w in http.writes()] == [(400, 25000), (350, 30000)]
    assert "Цена просмотра на Авито: 3,50 ₽, лимит 300,00 ₽ в день." in page
    assert [d["kind"] for _, _, d in journal(conn)] == ["set", "undo"]


def test_undo_after_auto_says_auto_cannot_come_back(tmp_path):
    http = FakeAvito(mode="auto")
    app, conn, _ = setup(tmp_path, http)
    owner = owner_of(app)
    _, token = opened(owner)
    send(owner, token, pick="4.00", limit="250")
    assert http.mode == "manual"
    text, _ = opened(owner)
    assert "был режим «авто»: через API Авито его не вернуть" in text
    assert "поставит цену, прочитанную тогда" in text                       # getBids gave 3.50 then


def test_undo_without_a_change_is_refused(tmp_path):
    app, conn, http = setup(tmp_path)
    owner = owner_of(app)
    opened(owner)
    text = send(owner, "b" * 32, url=PAGE + "/undo").get_data(as_text=True)
    assert "возвращать нечего" in text and http.writes() == []


# ---- the journal ------------------------------------------------------------------------------------------------------------------------

def test_the_journal_line(tmp_path):
    app, conn, http = setup(tmp_path)
    owner = owner_of(app)
    _, token = opened(owner)
    send(owner, token, pick="4.00", limit="250")
    (at, author, detail), = journal(conn)
    assert at == "2026-11-13T12:00:00" and author == "owner"                  # Moscow time (panel.MOSCOW_AUDIT)
    assert detail["account_id"] == 1 and detail["account"] == "Основной" and detail["avito_id"] == AD
    assert detail["was"] == {"mode": "manual", "bid": 3.5, "day_limit": 300.0}
    assert detail["want"] == {"bid": 4.0, "limit": 250.0}
    assert detail["got"] == {"mode": "manual", "bid": 4.0, "day_limit": 250.0} and detail["ok"] is True
    assert "success" in detail["answer"]
    assert not {"client_id", "client_secret", "Authorization"} & set(json.dumps(detail).split('"'))     # no key in the journal


# ---- the client --------------------------------------------------------------------------------------------------------------------------

def test_a_read_only_client_never_writes():
    http = FakeAvito()
    client = AvitoClient("id", "secret", http=http)
    with pytest.raises(WriteDisabled):
        client.set_manual_bid(AD, 5, 400, 25000)
    assert http.calls == []


def test_set_manual_bid_is_sent_once_on_a_429():
    http = FakeAvito(set_status=429)
    client = AvitoClient("id", "secret", http=http, allow_write=True, sleep=lambda s: pytest.fail("no wait, no repeat"))
    from tires_sync.avito import RateLimited
    with pytest.raises(RateLimited):
        client.set_manual_bid(AD, 5, 400, 25000)
    assert len(http.writes()) == 1


@pytest.mark.parametrize("args", [(AD, 5, 0, 100), (AD, 5, 100, 0), (AD, 0, 100, 100), (AD, 5, 1.5, 100), (AD, 5, True, 100)])
def test_set_manual_bid_wants_positive_whole_kopecks(args):
    http = FakeAvito()
    with pytest.raises(ValueError):
        AvitoClient("id", "secret", http=http, allow_write=True).set_manual_bid(*args)
    assert http.calls == []
