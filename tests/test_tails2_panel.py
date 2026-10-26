"""Заход «tails2», дорожка tails-panel: хвосты аудита защиты панели (П1–П10)."""
import sqlite3

import pytest

from tires_sync import store, transport


def test_login_attempt_indexes_come_to_an_old_base_without_touching_its_rows(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE login_attempt (id INTEGER PRIMARY KEY AUTOINCREMENT, ip TEXT NOT NULL, at TEXT NOT NULL)")
    old.execute("INSERT INTO login_attempt (ip, at) VALUES ('1.1.1.1', '2026-10-25T01:00:00')")
    old.commit()
    old.close()
    conn = store.connect(path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'login_attempt'")}
    assert {"login_attempt_ip_at", "login_attempt_at"} <= names
    assert conn.execute("SELECT ip, at FROM login_attempt").fetchall() == [("1.1.1.1", "2026-10-25T01:00:00")]
    plan = " ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN SELECT COUNT(*) FROM login_attempt WHERE ip = ? AND at >= ?", ("1", "2")))
    assert "login_attempt_ip_at" in plan
    plan = " ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN SELECT COUNT(*) FROM login_attempt WHERE at >= ?", ("2",)))
    assert "login_attempt_at" in plan


@pytest.mark.parametrize("url", ["file:///etc/hosts", "ftp://127.0.0.1/x", "data:text/plain,secret"])
def test_the_transport_opens_no_file_ftp_or_data_address(url):
    with pytest.raises(OSError) as err:
        transport.urllib_transport("GET", url, {}, None)
    assert "unknown url type" in str(err.value)


def test_the_transport_keeps_http_https_proxies_and_no_redirects():
    kinds = {type(h).__name__ for h in transport._OPENER.handlers}
    assert {"HTTPHandler", "HTTPSHandler", "_NoRedirect", "HTTPErrorProcessor", "HTTPDefaultErrorHandler"} <= kinds
    assert not kinds & {"FileHandler", "FTPHandler", "CacheFTPHandler", "DataHandler", "HTTPRedirectHandler"}


# П1: the address of WebMIM in the form leads to a public server only (SSRF from the panel into our own network).
from cryptography.fernet import Fernet  # noqa: E402

from panel_helpers import login, make_app, post  # noqa: E402
from tires_sync import connections, share_photos  # noqa: E402
from tires_sync.panel import routes_connections  # noqa: E402
from tires_sync.secretbox import SecretBox  # noqa: E402

REAL_PUBLIC_ADDRESS = routes_connections.public_address
NAMES = {"mimtest.svrauto.ru": "93.184.216.34", "localhost": "127.0.0.1", "inner.example": "192.168.1.5", "mixed.example": "10.0.0.7"}


def fake_dns(host, port, proto=0):
    import ipaddress
    import socket
    if host in NAMES:
        found = [NAMES[host]] + (["93.184.216.35"] if host == "mixed.example" else [])
        return [(2, 1, 6, "", (ip, port)) for ip in found]
    try:
        return [(2, 1, 6, "", (str(ipaddress.ip_address(host)), port))]
    except ValueError:
        raise socket.gaierror("not found") from None


@pytest.fixture
def real_check(monkeypatch):
    monkeypatch.setattr(routes_connections, "public_address", REAL_PUBLIC_ADDRESS)
    monkeypatch.setattr(share_photos.socket, "getaddrinfo", fake_dns)


def webmim_client(tmp_path):
    app = make_app(tmp_path, SECRET_BOX=SecretBox(Fernet.generate_key()))
    client = app.test_client()
    return app, client, login(client)


@pytest.mark.parametrize("url", ["https://127.0.0.1", "https://localhost", "https://10.1.2.3:8443", "https://[::1]", "https://inner.example",
                                 "https://mixed.example", "https://nowhere.example", "https://user:pw@mimtest.svrauto.ru",
                                 "https://mimtest.svrauto.ru:abc", "https://mimtest.svrauto.ru:99999"])     # a broken port: refused, not 500
def test_a_webmim_address_inside_our_network_or_unknown_is_refused(tmp_path, real_check, url):
    app, client, tok = webmim_client(tmp_path)
    post(client, "/connections/webmim", tok, base_url=url, mode="login", login="u")
    assert "ведёт во внутреннюю сеть" in client.get("/connections/webmim").get_data(as_text=True)
    conn = store.connect(app.config["DB_PATH"])
    assert "base_url" not in connections.get_config(conn, "webmim", 0)


def test_the_check_of_a_private_webmim_address_saves_and_asks_nothing(tmp_path, real_check, monkeypatch):
    asked = []
    monkeypatch.setattr(routes_connections, "WebmimClient", lambda *a, **k: asked.append(a))
    app, client, tok = webmim_client(tmp_path)
    post(client, "/connections/webmim/check", tok, base_url="https://inner.example", mode="login", login="u")
    assert "ведёт во внутреннюю сеть" in client.get("/connections/webmim").get_data(as_text=True) and asked == []
    assert "base_url" not in connections.get_config(store.connect(app.config["DB_PATH"]), "webmim", 0)


def test_a_public_webmim_address_is_saved(tmp_path, real_check):
    app, client, tok = webmim_client(tmp_path)
    post(client, "/connections/webmim", tok, base_url="https://mimtest.svrauto.ru", mode="login", login="u")
    assert "ведёт во внутреннюю сеть" not in client.get("/connections/webmim").get_data(as_text=True)
    conn = store.connect(app.config["DB_PATH"])
    assert connections.get_config(conn, "webmim", 0)["base_url"] == "https://mimtest.svrauto.ru"


# П5: the alert about guessing — one sender of two workers, a busy base never makes the sign-in 500, a refused Telegram is retried soon.
import re  # noqa: E402
import sqlite3 as _sqlite3  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

from tires_sync.panel import common  # noqa: E402


def csrf_of(client) -> str:
    return re.search(r'name="csrf" value="([0-9a-f]+)"', client.get("/login").get_data(as_text=True)).group(1)


def guess(client, tok, n, prefix="10.0"):
    return client.post("/login", data={"csrf": tok, "login": "", "password": "bad"},
                       headers={"X-Forwarded-For": "%s.%d.%d" % (prefix, n // 4, n % 4)}).status_code


def test_of_two_workers_only_one_takes_the_hour_of_the_alert(tmp_path):
    first, second = store.connect(tmp_path / "t.db"), store.connect(tmp_path / "t.db")
    assert store.claim_state(first, "k", "2026-10-07T12:00:00", "2026-10-07T11:00:00") is True
    assert store.claim_state(second, "k", "2026-10-07T12:00:01", "2026-10-07T11:00:01") is False
    assert store.get_state(second, "k") == "2026-10-07T12:00:00"
    assert store.claim_state(second, "k", "2026-10-07T13:00:01", "2026-10-07T12:00:01") is True      # an hour later


def test_an_alert_sent_by_the_other_worker_for_this_series_is_not_sent_again(tmp_path, monkeypatch):
    """The owner 2026-10-27: once a series, not once an hour; the series of the other worker is still going (its failures are in the window)."""
    from tires_sync import notify
    said = []
    monkeypatch.setattr(notify, "notifier_for", lambda conn, box, http=None: said.append)
    app = make_app(tmp_path, TRUST_PROXY=1)
    conn = store.connect(app.config["DB_PATH"])
    for n in range(common.LOGIN_ALERT_FAILURES + 1):
        store.add_login_failure(conn, "10.5.0.%d" % n, "2026-10-07T11:50:00")
    store.set_state(conn, common.LOGIN_ALERT_KEY, "2026-10-07T11:50:00")
    client = app.test_client()
    tok = csrf_of(client)
    for n in range(common.LOGIN_ALERT_FAILURES + 3):
        guess(client, tok, n)
    assert said == []


def test_a_busy_base_during_the_alert_answers_401_and_the_failure_is_counted(tmp_path, monkeypatch):
    def busy(*args):
        raise _sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(store, "claim_state", busy)
    app = make_app(tmp_path, TRUST_PROXY=1)
    client = app.test_client()
    tok = csrf_of(client)
    codes = {guess(client, tok, n) for n in range(common.LOGIN_ALERT_FAILURES + 2)}
    assert codes == {401}
    assert store.connect(app.config["DB_PATH"]).execute("SELECT COUNT(*) FROM login_attempt").fetchone()[0] == common.LOGIN_ALERT_FAILURES + 2


def test_a_refused_telegram_is_tried_again_in_five_minutes_not_in_an_hour(tmp_path, monkeypatch):
    from tires_sync import notify
    tries = []

    def down(text):
        tries.append(text)
        raise RuntimeError("telegram is down")
    monkeypatch.setattr(notify, "notifier_for", lambda conn, box, http=None: down)
    clock = [datetime(2026, 10, 7, 12, 0, 0)]
    app = make_app(tmp_path, TRUST_PROXY=1, NOW=lambda: clock[0])
    client = app.test_client()
    tok = csrf_of(client)
    for n in range(common.LOGIN_ALERT_FAILURES + 2):
        guess(client, tok, n)
    assert len(tries) == 1
    clock[0] += timedelta(minutes=4)
    guess(client, tok, 1, prefix="10.7")
    assert len(tries) == 1                                                                 # not on every failed sign-in
    clock[0] += timedelta(minutes=2)
    guess(client, tok, 2, prefix="10.7")
    assert len(tries) == 2


def test_a_sent_alert_is_not_repeated_while_its_series_goes_on(tmp_path, monkeypatch):
    from tires_sync import notify
    said = []
    monkeypatch.setattr(notify, "notifier_for", lambda conn, box, http=None: said.append)
    clock = [datetime(2026, 10, 7, 12, 0, 0)]
    app = make_app(tmp_path, TRUST_PROXY=1, NOW=lambda: clock[0])
    client = app.test_client()
    tok = csrf_of(client)
    for n in range(common.LOGIN_ALERT_FAILURES + 2):
        guess(client, tok, n)
    clock[0] += timedelta(minutes=6)
    guess(client, tok, 1, prefix="10.7")
    assert len(said) == 1


# П6: a wrong current password is a class of its own, not the word «Текущий» in the message.
def test_change_own_password_raises_wrong_password_for_a_wrong_current_one(tmp_path):
    from tires_sync import users
    conn = store.connect(tmp_path / "t.db")
    users.add_user(conn, "staff1", "длинный-пароль-1", "2026-10-07T10:00:00", role="viewer")
    with pytest.raises(users.WrongPassword):
        users.change_own_password(conn, "staff1", "не-тот-пароль-1", "новый-пароль-22", "2026-10-07T10:01:00")
    with pytest.raises(users.UserError) as err:                                     # a weak new password is not a wrong current one
        users.change_own_password(conn, "staff1", "длинный-пароль-1", "1", "2026-10-07T10:01:00")
    assert not isinstance(err.value, users.WrongPassword)


@pytest.mark.parametrize("error, counted", [("WrongPassword", True), ("UserError", False)])
def test_the_panel_counts_the_class_not_the_words_of_the_message(tmp_path, monkeypatch, error, counted):
    from tires_sync import users
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    users.add_user(conn, "staff1", "длинный-пароль-1", "2026-10-07T10:00:00", role="viewer")
    client = app.test_client()
    client.post("/login", data={"csrf": csrf_of(client), "login": "staff1", "password": "длинный-пароль-1"})
    tok = re.search(r'name="csrf" value="([0-9a-f]+)"', client.get("/account/password").get_data(as_text=True)).group(1)

    def refuse(*args, **kwargs):
        raise getattr(users, error)("Текущий пароль: другой текст" if error == "UserError" else "Не подошёл.")
    monkeypatch.setattr(users, "change_own_password", refuse)
    post(client, "/account/password", tok, old="x", new="новый-пароль-22", new2="новый-пароль-22")
    assert store.connect(app.config["DB_PATH"]).execute("SELECT COUNT(*) FROM login_attempt").fetchone()[0] == int(counted)


# П7: behind nginx the panel takes the scheme from X-Forwarded-Proto; HSTS is nginx's alone (tests/test_panel_sec.py, deploy/nginx-panel.conf).
@pytest.mark.parametrize("sent, seen", [("https", "https"), (None, "http")])
def test_behind_a_trusted_proxy_the_panel_takes_the_scheme_from_nginx(tmp_path, sent, seen):
    app = make_app(tmp_path, TRUST_PROXY=1)
    schemes = []

    def inner(environ, start_response):
        schemes.append(environ["wsgi.url_scheme"])
        start_response("200 OK", [])
        return [b""]
    app.wsgi_app.app = inner                                                     # what Flask would get from ProxyFix
    app.test_client().get("/login", headers={"X-Forwarded-Proto": sent} if sent else {})
    assert schemes == [seen]



def test_nginx_config_gives_the_hsts_line_to_put_into_the_certbot_block_of_443():
    """The repo file has no server block of 443 (certbot keeps it on the server): the line is a commented instruction, never active here."""
    from pathlib import Path
    conf = (Path(__file__).resolve().parents[1] / "deploy" / "nginx-panel.conf").read_text(encoding="utf-8")
    assert re.search(r'^\s*# add_header Strict-Transport-Security "max-age=31536000" always;$', conf, re.M)
    assert not re.search(r"^\s*add_header Strict-Transport-Security", conf, re.M)


def _nginx_map(conf: str, name: str):
    """The regex lines of one map of the config as (python regex, value), in their order (nginx checks regexes in the order of appearance)."""
    body = re.search(r"map \S+ \$%s \{(.*?)\n\}" % name, conf, re.S).group(1)
    pairs = re.findall(r'^\s*"~(.+?)"\s+(\S+);', body, re.M)
    return [(re.compile(rx.replace("(?<", "(?P<")), value.strip('"')) for rx, value in pairs]


def _nginx_apply(pairs, text: str) -> str:
    for rx, value in pairs:
        found = rx.search(text)
        if found:
            return re.sub(r"\$\{?(\w+)\}?", lambda m: found.group(m.group(1)), value)
    return text


@pytest.mark.parametrize("uri, logged", [
    ("/feed/" + "a" * 32 + "/avito_feed.xml", "/feed/***/avito_feed.xml"), ("/img/" + "b" * 32 + "/123.jpg", "/img/***/123.jpg"),
    ("/hook/avito/secret123", "/hook/avito/***"), ("/invite/tok123", "/invite/***"), ("/orders/5", "/orders/5"),
    ("/feed/" + "c" * 32, "/feed/***"), ("/img/" + "d" * 32, "/img/***")])
def test_nginx_journal_never_writes_the_tokens_of_the_address(uri, logged):
    from pathlib import Path
    conf = (Path(__file__).resolve().parents[1] / "deploy" / "nginx-panel.conf").read_text(encoding="utf-8")
    assert "$panel_uri $server_protocol" in conf
    assert _nginx_apply(_nginx_map(conf, "panel_uri"), uri) == logged


def test_nginx_referer_of_an_invitation_has_no_token_and_no_query():
    from pathlib import Path
    conf = (Path(__file__).resolve().parents[1] / "deploy" / "nginx-panel.conf").read_text(encoding="utf-8")
    pairs = _nginx_map(conf, "panel_referer")
    assert _nginx_apply(pairs, "https://panel.example/invite/tok123") == "https://panel.example/invite/***"
    assert _nginx_apply(pairs, "https://panel.example/orders?q=+7900") == "https://panel.example/orders"


def test_nginx_invitation_has_its_own_limit():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "deploy"
    conf = (root / "nginx-panel.conf").read_text(encoding="utf-8")
    block = re.search(r"location \^~ /invite/ \{(.*?)\}", conf, re.S).group(1)
    assert "limit_req zone=invite" in block and "error_log /dev/null crit" in block
    assert "zone=invite:10m" in (root / "nginx-limits.conf").read_text(encoding="utf-8")
    assert re.search(r"location \^~ /hook/ \{[^}]*access_log off;", conf)
    assert re.search(r"location \^~ /img/ \{[^}]*error_log /dev/null crit;", conf)


# П9: the journal speaks Russian for the labels of the shares, the rates and the inquiries.
@pytest.mark.parametrize("action, detail, text", [
    ("share_labels", {"from": {"owner": "", "partner": ""}, "to": {"owner": "Павлу", "partner": ""}},
     "Подписи долей в отчёте по заказам: «Павлу» и «по умолчанию»"),
    ("order_rate", {"name": "point_fee", "valid_from": "2026-10-25", "from": 500.0, "to": 650.5},
     "Ставка «Выплата точке за заказ» с 2026-10-25: 500 → 650.5 ₽"),
    ("order_rate", {"name": "point_fee", "valid_from": "2026-10-25", "from": 12345.67, "to": 100000},
     "Ставка «Выплата точке за заказ» с 2026-10-25: 12345.67 → 100000 ₽"),
    ("order_rate", {"name": "owner_share", "valid_from": "2026-10-25", "from": 0.5, "to": 0.6},
     "Ставка «Доля собственника» с 2026-10-25: 50 → 60 %"),
    ("order_rate", {"name": "handoff_fee", "valid_from": "2026-11-01", "from": None, "to": 300},
     "Ставка «За переданного клиента» с 2026-11-01: — → 300 ₽"),
    ("inquiry", {"client": 7, "from": "new", "to": "work", "why": "открыт чат"}, "Обращение клиента №7: новое → в работе (открыт чат)"),
    ("inquiry", {"client": 8, "from": "", "to": "new", "why": ""}, "Обращение клиента №8: — → новое"),
    ("inquiry_failed", {"event": "call", "id": 12, "tries": 4, "why": "database is locked"},
     "Обращение не обработано: звонок, попыток 4 (database is locked)"),
])
def test_journal_texts_of_labels_rates_and_inquiries(action, detail, text):
    from tires_sync.panel import texts
    assert texts.audit_text({"action": action, "detail": detail}) == text


def test_the_journal_page_shows_the_text_of_a_new_share_label(tmp_path):
    from tires_sync import order_report
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    order_report.set_share_labels(conn, "Павлу", "Ивану", "2026-10-07T11:00:00", "owner")
    conn.commit()
    client = app.test_client()
    login(client)
    page = client.get("/history").get_data(as_text=True)
    assert "Подписи долей в отчёте по заказам: «Павлу» и «Ивану»" in page and "· share_labels<" not in page


def test_a_busy_journal_never_costs_the_alert_message(tmp_path, monkeypatch):
    from tires_sync import notify
    said = []
    monkeypatch.setattr(notify, "notifier_for", lambda conn, box, http=None: said.append)
    real = store.write_audit

    def busy(conn, at, author, action, detail):
        if action == "alert":
            raise _sqlite3.OperationalError("database is locked")
        return real(conn, at, author, action, detail)
    monkeypatch.setattr(store, "write_audit", busy)
    client = make_app(tmp_path, TRUST_PROXY=1).test_client()
    tok = csrf_of(client)
    codes = {guess(client, tok, n) for n in range(common.LOGIN_ALERT_FAILURES + 2)}
    assert codes == {401} and len(said) == 1
