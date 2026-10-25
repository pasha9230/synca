"""The tails of the chats and the sends (run 2026-10-25-tails1, lane tails-chats): each test names its tail (Ч1…Ч11, Ф12)."""
from __future__ import annotations

import pytest

from test_chat_send_refusal import Refusing, error_of, picture
from test_stuck_sending import NOW, Sender, env, journal, status  # noqa: F401  (env is a fixture)
from tires_sync import chats, connections
from tires_sync.avito import AvitoError


class NoId(Sender):
    """Avito answers the question «who are we» with a 200 that has no id."""

    def __init__(self):
        super().__init__()
        self.uploads = []

    def get_self(self, retry=True):
        return {"id": None, "name": "Кабинет"}

    def upload_image(self, user_id, name, data):
        self.uploads.append(user_id)
        return "img-1"

    def send_image(self, user_id, chat_id, image_id):
        self.sent.append((user_id, image_id))
        return {"id": "p-out"}


def _unknown_cabinet(conn, chat_row):
    account = chats.get_chat(conn, chat_row)["account_id"]
    connections.forget_identity(conn, account)
    return account


# ---- Ч1: get_self 200 without the id ------------------------------------------------------------------------------------------------------

def test_ch1_a_cabinet_without_an_id_is_avitos_odd_answer_and_nothing_is_remembered(env):
    conn, chat_row, card = env
    account = _unknown_cabinet(conn, chat_row)
    with pytest.raises(AvitoError) as raised:
        connections.cabinet_user_id(conn, NoId(), account)
    assert raised.value.status == 200 and raised.value.message == connections.NO_CABINET_ID
    assert not connections.get_avito_identity(conn, account)["id"]


def test_ch1_a_reply_is_not_sent_to_accounts_none_and_may_go_again(env):
    conn, chat_row, card = env
    _unknown_cabinet(conn, chat_row)
    sender = NoId()
    with pytest.raises(chats.ChatError) as raised:
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert not isinstance(raised.value, chats.ChatSendUnknown)
    assert "Авито не отдал данные кабинета (код 200: в ответе нет Id кабинета)" in str(raised.value)
    assert sender.sent == [] and status(conn)[0] == "failed"
    chats.send_reply(conn, Sender(), chat_row, "Добрый день", "tok-1", NOW, "owner")         # «failed»: the same press goes once the id comes
    assert status(conn)[0] == "sent"


def test_ch1_a_photo_is_not_uploaded_to_accounts_none(env):
    conn, chat_row, card = env
    _unknown_cabinet(conn, chat_row)
    sender = NoId()
    with pytest.raises(chats.ChatError) as raised:
        chats.send_image(conn, sender, chat_row, picture(), "tok-1", NOW, "owner")
    assert not isinstance(raised.value, chats.ChatSendUnknown)
    assert sender.uploads == [] and sender.sent == [] and status(conn)[0] == "failed"


# ---- Ч2: a failure of our own while the cabinet's id is asked ---------------------------------------------------------------------------

def test_ch2_a_failure_of_our_own_is_not_no_answer_and_the_log_says_where(env, monkeypatch, caplog):
    import logging
    conn, chat_row, card = env

    secret = "Добрый день"

    def broken(*a, **k):
        raise KeyError(secret)                                           # whatever its text, it never reaches the log (the stack: code lines)
    monkeypatch.setattr(chats.connections, "cabinet_user_id", broken)
    with caplog.at_level(logging.WARNING, logger=chats.log.name):
        with pytest.raises(chats.ChatError) as raised:
            chats.send_reply(conn, Sender(), chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert not isinstance(raised.value, chats.ChatSendUnknown)
    assert "(непредвиденная ошибка KeyError)" in str(raised.value) and "нет ответа" not in str(raised.value)
    assert status(conn)[0] == "failed" and error_of(conn) == "непредвиденная ошибка (KeyError)"
    logged = [r.getMessage() for r in caplog.records]
    assert any("непредвиденная ошибка (KeyError)" in line and "broken" in line for line in logged)       # the stack: where it broke
    assert not any("Добрый день" in line for line in logged)


def test_ch2_the_network_is_still_no_answer(env, monkeypatch):
    conn, chat_row, card = env

    def cut(*a, **k):
        raise ConnectionResetError("reset")
    monkeypatch.setattr(chats.connections, "cabinet_user_id", cut)
    with pytest.raises(chats.ChatError) as raised:
        chats.send_reply(conn, Sender(), chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert "(нет ответа)" in str(raised.value) and error_of(conn) == "нет ответа (ConnectionResetError)"


# ---- Ч3.1: Avito resting after failures is not «no answer» in the record ---------------------------------------------------------------

def test_ch3_a_pause_of_avito_is_kept_as_a_pause_not_as_no_answer(env, monkeypatch):
    from tires_sync import reliability
    conn, chat_row, card = env

    def resting(*a, **k):
        raise reliability.ServiceUnavailable("avito", 1_791_900_900.0, "HTTP 503")
    monkeypatch.setattr(chats.connections, "cabinet_user_id", resting)
    with pytest.raises(chats.ChatError):
        chats.send_reply(conn, Sender(), chat_row, "Добрый день", "tok-1", NOW, "owner")
    record = error_of(conn)
    assert record.startswith("Авито: пауза после сбоев подряд до") and "нет ответа" not in record and status(conn)[0] == "failed"


def test_ch3_a_pause_at_the_send_itself_is_named_too(env):
    from tires_sync import reliability
    conn, chat_row, card = env
    with pytest.raises(chats.ChatError):
        chats.send_reply(conn, Refusing(reliability.ServiceUnavailable("avito", 1_791_900_900.0, "HTTP 503")), chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert error_of(conn).startswith("Авито: пауза после сбоев подряд до")


# ---- Ч3.2: a 404 or a 400 is not helped by a repeat (the lead 2026-10-25) ----------------------------------------------------------------

@pytest.mark.parametrize("code, hint", [(404, "Чат у Авито не найден (удалён или закрыт): повтор не поможет."),
                                        (400, "Авито не принял запрос: повтор не поможет — напишите владельцу.")])
def test_ch3_a_404_or_a_400_says_a_repeat_will_not_help_with_avitos_text(env, code, hint):
    from test_chat_send_refusal import refusal
    conn, chat_row, card = env
    with pytest.raises(chats.ChatError) as raised:
        chats.send_reply(conn, Refusing(refusal(code, {"error": {"message": "nope"}})), chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert str(raised.value) == "Авито не принял сообщение (код %d: nope). %s" % (code, hint)
    assert "Повторите через минуту" not in str(raised.value) and status(conn)[0] == "failed"      # what may be repeated is unchanged


def test_ch3_a_photo_refused_with_a_404_says_the_same(env):
    from test_chat_send_refusal import refusal
    conn, chat_row, card = env
    with pytest.raises(chats.ChatError) as raised:
        chats.send_image(conn, Refusing(refusal(404, {"error": {"message": "chat not found"}})), chat_row, picture(), "tok-1", NOW, "owner")
    assert str(raised.value) == "Авито не принял сообщение с фото (код 404: chat not found). Чат у Авито не найден (удалён или закрыт): повтор не поможет."


# ---- Ч4: our text quoted by Avito in JSON escapes or HTML entities --------------------------------------------------------------------

TEXT = "Иван, ваш заказ ждёт по адресу Новая ул., 1"


@pytest.mark.parametrize("body", [
    '{"errors": [{"field": "message.text", "value": %s}]}' % __import__("json").dumps(TEXT),               # \uXXXX, no field «message»
    "<html><body><p>Invalid: %s</p></body></html>" % "".join("&#%d;" % ord(ch) for ch in TEXT),           # a proxy's page in entities
    "<html><body><p>Invalid: &quot;%s&quot;</p></body></html>" % TEXT.replace(" ", "&nbsp;"),
])
def test_ch4_an_escaped_quote_of_our_text_is_hidden_everywhere(env, caplog, body):
    import logging
    from test_chat_send_refusal import refusal
    conn, chat_row, card = env
    with caplog.at_level(logging.WARNING, logger=chats.log.name):
        with pytest.raises(chats.ChatError) as raised:
            chats.send_reply(conn, Refusing(refusal(400, body)), chat_row, TEXT, "tok-1", NOW, "owner")
    assert "(код 400: текст ответа скрыт, в нём наше сообщение)" in str(raised.value)
    seen = [str(raised.value), error_of(conn)] + journal(conn, card) + [r.getMessage() for r in caplog.records]
    assert not any(part in line for line in seen for part in ("u0418", "&#1048;", "Новая ул", "ваш заказ"))


def test_ch4_an_answer_without_our_text_is_still_shown():
    from test_chat_send_refusal import refusal
    err = refusal(400, '{"errors": [{"text": %s}]}' % __import__("json").dumps("Неверный формат"))
    assert chats._avito_said(err, TEXT).startswith("код 400: {")                                              # shown as it came


# ---- Ч5: pin_phone with an empty list of the cabinet's numbers --------------------------------------------------------------------------

def test_ch5_no_number_is_pinned_while_the_cabinets_list_is_not_read(env):
    conn, chat_row, card = env
    account = chats.get_chat(conn, chat_row)["account_id"]
    connections.set_avito_identity(conn, account, 1, "n", "79000000013")              # the chats' tick: the id, no list of numbers
    assert connections.get_avito_identity(conn, account)["phones"] == []
    with pytest.raises(ValueError, match="Проверить подключение"):
        connections.pin_phone(conn, account, "79990000000", NOW, "o")
    assert connections.get_avito_identity(conn, account)["pinned"] is False
    connections.set_avito_identity(conn, account, 1, "n", "79000000013", ["79000000013", "79000000012"])     # the check reads the list
    connections.pin_phone(conn, account, "79000000012", NOW, "o")
    assert connections.get_avito_identity(conn, account)["phone"] == "+79000000012"


# ---- Ч6: a ValueError of the WebMIM settings is not «the master key» ------------------------------------------------------------------

@pytest.mark.parametrize("failure, said", [(ValueError("base_url must use https"), "Настройки WebMIM не подходят"),
                                           ("secret", "проверьте мастер-ключ")])
def test_ch6_the_webmim_addresses_name_the_settings_not_the_master_key(tmp_path, monkeypatch, failure, said):
    from datetime import datetime
    from cryptography.fernet import Fernet
    from panel_helpers import login, make_app
    from tires_sync import webmim_match
    from tires_sync.secretbox import SecretBox, SecretKeyError
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 23, 9, 0, 0), SECRET_BOX=SecretBox(Fernet.generate_key()))
    client = app.test_client()
    login(client)

    def broken(*a, **k):
        raise SecretKeyError("no key") if failure == "secret" else failure
    monkeypatch.setattr(webmim_match, "client_for", broken)
    html = client.get("/connections/points?addresses=1").get_data(as_text=True)
    assert said in html
    assert ("мастер-ключ" in html) == (failure == "secret") and "base_url" not in html


# ---- Ч7: «Нажмите…» in a browser without WebAudio -------------------------------------------------------------------------------------

NOTIFY_HARNESS = r"""
const fs = require("fs");
const [file, audio, permission] = process.argv.slice(2);
function el() { return {textContent: "", hidden: false, addEventListener() {}}; }
const state = el(), button = el(), fresh = el(), on = {};
global.document = {querySelector: (s) => s.includes("enable") ? button : s.includes("state") ? state : fresh,
                   addEventListener: (name, fn) => { on[name] = fn; }};
global.window = {localStorage: {getItem: () => null, setItem() {}}, focus() {}};
if (permission !== "none") { window.Notification = global.Notification = {permission, requestPermission() {}}; }
if (audio === "web") { window.AudioContext = function () { this.state = "suspended"; this.resume = () => ({then() {}}); }; }
if (audio === "broken") { window.AudioContext = function () { throw new Error("no device"); }; }
global.fetch = () => new Promise(() => {});
global.setInterval = () => 0;
new Function(fs.readFileSync(file, "utf8"))();
if (on.click) on.click();
console.log(JSON.stringify(state.textContent));
"""


@pytest.mark.parametrize("audio, permission, shown", [
    ("none", "default", "Этот браузер не даёт звук: будут только уведомления."),
    ("broken", "granted", "Уведомления включены. Этот браузер не даёт звук: будут только уведомления."),
    ("none", "denied", "Уведомления запрещены в настройках браузера, а звук этот браузер не даёт: новое видно только на странице и в колокольчике."),
    ("none", "none", "Этот браузер не показывает уведомления и не даёт звук: новое видно только на странице и в колокольчике."),
    ("web", "granted", "Уведомления включены. Нажмите на страницу, чтобы включить звук."),          # WebAudio: the press is still asked for
])
def test_ch7_without_webaudio_the_page_never_asks_for_a_press(tmp_path, audio, permission, shown):
    import json
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node")
    harness = tmp_path / "harness.js"
    harness.write_text(NOTIFY_HARNESS, encoding="utf-8")
    script = Path(chats.__file__).parent / "panel" / "static" / "notify.js"
    out = subprocess.run([node, str(harness), str(script), audio, permission], capture_output=True, text=True, timeout=30, check=True).stdout
    assert json.loads(out.strip().splitlines()[-1]) == shown


# ---- Ч9: the list of the webhook subscriptions is a read (a POST marked safe) ---------------------------------------------------------

def test_ch9_the_list_of_subscriptions_is_repeated_after_a_5xx_like_the_other_reads():
    from test_reliability import avito
    from tires_sync import avito_webhook
    client, http, sleeps = avito((503, {"error": {"code": 503}}), (200, {"subscriptions": [{"url": "https://p/x", "version": "3"}]}))
    assert avito_webhook.subscriptions(client) == ["https://p/x"]
    assert len(http.calls) == 3 and sleeps == [1.0]                      # the token, the 503, the repeat


# ---- Ч8: the hint of the bot: its pause only once it went, one hint for two events at once ----------------------------------------------

def _hinted(app):
    from test_vk_commands import to_points
    from tires_sync import order_manager as mgr
    return [text for _peer, text in to_points(app) if mgr.HINT in text]


def test_ch8_a_hint_vk_did_not_take_gives_its_pause_back(tmp_path, monkeypatch):
    from http_fakes import ScriptedHttp
    from test_vk_commands import hook, ready
    from tires_sync import vk_callback
    app, client, tok, conn, pid, card = ready(tmp_path)
    monkeypatch.setattr(vk_callback, "_alarm", lambda conn, box, text: None)
    refused = (200, {"error": {"error_code": 901, "error_msg": "Can't send messages"}})
    app.config["VK_HTTP"] = ScriptedHttp(refused, refused, (200, {"response": 7}))
    hook(app, conn, "привет", event="h1")                                   # with the keyboard and without: both refused
    assert _hinted(app) and len(app.config["VK_HTTP"].calls) == 2
    hook(app, conn, "ау", event="h2")                                       # the same minute: the hint goes now
    assert len(app.config["VK_HTTP"].calls) == 3 and len(_hinted(app)) == 3
    hook(app, conn, "ещё", event="h3")                                      # it went: the pause of 30 minutes holds
    assert len(app.config["VK_HTTP"].calls) == 3


def test_ch8_a_failure_of_the_panel_at_the_sending_gives_the_pause_back_too(tmp_path, monkeypatch):
    from test_vk_commands import PEER, hook, ready
    from tires_sync import order_manager as mgr, vk_callback
    app, client, tok, conn, pid, card = ready(tmp_path)
    monkeypatch.setattr(vk_callback, "_alarm", lambda conn, box, text: None)
    monkeypatch.setattr(vk_callback, "_answer", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    hook(app, conn, "привет", event="k1")
    assert not conn.execute("SELECT value FROM app_state WHERE key = ?", (mgr.HINT_KEY % pid,)).fetchone()[0]
    assert mgr.hint(conn, PEER, "2026-10-21T12:01:00") == mgr.HINT


def test_ch8_of_two_events_handled_at_once_only_one_gets_the_hint(tmp_path, monkeypatch):
    from test_vk_commands import PEER, ready
    from tires_sync import order_manager as mgr, store
    app, client, tok, conn, pid, card = ready(tmp_path)
    store.set_state(conn, mgr.HINT_KEY % pid, "2026-10-21T12:00:00")       # the other event took the pause meanwhile
    real = store.get_state
    monkeypatch.setattr(mgr.store, "get_state", lambda c, key: None if key == mgr.HINT_KEY % pid else real(c, key))   # read before it
    assert mgr.hint(conn, PEER, "2026-10-21T12:00:01") == ""
    assert real(conn, mgr.HINT_KEY % pid) == "2026-10-21T12:00:00"


def test_ch8_a_later_hint_is_not_given_back_by_an_earlier_failure(tmp_path):
    from test_vk_commands import PEER, ready
    from tires_sync import order_manager as mgr, store
    app, client, tok, conn, pid, card = ready(tmp_path)
    store.set_state(conn, mgr.HINT_KEY % pid, "2026-10-21T12:40:00")
    mgr.hint_failed(conn, PEER, "2026-10-21T12:00:00")
    assert store.get_state(conn, mgr.HINT_KEY % pid) == "2026-10-21T12:40:00"


# ---- Ч10: «Входящие» name the point of a cut handoff from «Моя точка» without VK ------------------------------------------------------

def test_ch10_a_cut_handoff_from_the_page_lands_in_the_inbox_with_its_point(tmp_path):
    from tires_sync import order_manager as mgr, order_points as points, orders, store
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    pid = points.save_point(conn, {"name": "Гамма", "manager_name": "Б", "channel": "panel"})
    conn.execute("INSERT INTO panel_user_role (login, role, point_id) VALUES ('gamma', 'point', ?)", (pid,))
    for message_id, sender in (("page-1", "панель: gamma"), ("page-2", "панель: nobody")):
        conn.execute("INSERT INTO order_command_log (message_id, at, sender, word, order_id, outcome) VALUES (?, '2026-10-13T11:00:00', ?, 'клиент', NULL, 'обрабатывается')",
                     (message_id, sender))
    conn.commit()
    assert mgr.note_stuck(conn, "2026-10-13T12:00:00", "2026-10-13T11:55:00") == 2
    rows = {r[0]: r[1] for r in conn.execute("SELECT message_id, point_id FROM manager_message")}
    assert rows == {"stuck-page-1": pid, "stuck-page-2": None}                 # a login of no point: as before
    assert sorted(item["point"] for item in mgr.list_inbox(conn)) == ["", "Гамма"]


# ---- Ч11: the answer to a binding that did not go is an alert; a stranger's is the journal only (the lead 2026-10-25) --------------------

def test_ch11_the_answer_to_a_good_binding_code_that_vk_refused_is_an_alert(tmp_path, monkeypatch):
    from http_fakes import ScriptedHttp
    from test_vk_commands import hook, ready
    from tires_sync import order_points as points, vk_callback
    app, client, tok, conn, pid, card = ready(tmp_path)
    said = []
    monkeypatch.setattr(vk_callback, "_alarm", lambda conn, box, text: said.append(text))
    refused = (200, {"error": {"error_code": 901, "error_msg": "Can't send messages"}})
    app.config["VK_HTTP"] = ScriptedHttp(*[refused] * 4)
    other = points.save_point(conn, {"name": "Гамма", "manager_name": "Б", "channel": "vk"})
    hook(app, conn, points.new_bind_code(conn, other), event="b1", sender="777")          # bound before the answer: a point now
    assert points.get_point(conn, other)["vk_peer_id"] == "777"
    assert said == [vk_callback.NOT_ANSWERED % "ВК не принял сообщение (код 901)"]
    hook(app, conn, "ABCDEFGH", event="b2", sender="888")                                # a stranger with a wrong code: the journal only
    assert len(said) == 1
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'vk_answer_failed'").fetchone()[0] == 2


# ---- Ф12: cli._alert — the same text once in six hours, whatever went between, and not twice from two processes ------------------------

def _tg(monkeypatch, *replies):
    from http_fakes import ScriptedHttp
    from test_order_notify import TG_OK
    from tires_sync import cli
    tg = ScriptedHttp(*(replies or [TG_OK] * 10))
    monkeypatch.setattr(cli, "_http", lambda: tg)
    return tg


def test_f12_a_text_said_is_not_said_again_after_another_alert_within_six_hours(tmp_path, monkeypatch):
    from test_order_notify import env as order_env
    from tires_sync import cli
    conn, box = order_env(tmp_path)
    tg = _tg(monkeypatch)
    clock = [1_000_000.0]
    monkeypatch.setattr(cli, "_epoch", lambda: clock[0])
    assert cli._alert(conn, box, "Бот ВК не может ответить точкам") is True
    clock[0] += 600
    assert cli._alert(conn, box, "Google-таблица не отвечает") is True
    clock[0] += 600
    assert cli._alert(conn, box, "Бот ВК не может ответить точкам") is True and len(tg.calls) == 2       # A, B, A: the second A is quiet
    clock[0] += cli.REPEAT_PAUSE_SECONDS
    assert cli._alert(conn, box, "Бот ВК не может ответить точкам") is True and len(tg.calls) == 3       # six hours later: told again


def test_f12_of_two_processes_with_the_same_text_one_sends_it(tmp_path, monkeypatch):
    from test_order_notify import env as order_env
    from tires_sync import cli, store
    conn, box = order_env(tmp_path)
    tg = _tg(monkeypatch)
    monkeypatch.setattr(cli, "_epoch", lambda: 1_000_000.0)
    real = store.get_state

    def stale(c, key):                                                    # the other process read the marks before this one wrote them
        return None if key in ("last_alert_at", "last_alert_text") else real(c, key)
    assert cli._alert(conn, box, "Служба упала") is True
    monkeypatch.setattr(cli.store, "get_state", stale)
    assert cli._alert(conn, box, "Служба упала") is True and len(tg.calls) == 1


def test_f12_a_text_telegram_refused_is_free_again_and_the_marks_do_not_pile_up(tmp_path, monkeypatch):
    from test_order_notify import TG_OK, env as order_env
    from tires_sync import cli
    conn, box = order_env(tmp_path)
    tg = _tg(monkeypatch, (502, {}), TG_OK, TG_OK)
    clock = [1_000_000.0]
    monkeypatch.setattr(cli, "_epoch", lambda: clock[0])
    assert cli._alert(conn, box, "A") is False
    assert cli._alert(conn, box, "A") is True and len(tg.calls) == 2                                      # not said: sent at once
    clock[0] += cli.REPEAT_PAUSE_SECONDS + 1
    assert cli._alert(conn, box, "B") is True
    marks = [r[0] for r in conn.execute("SELECT key FROM app_state WHERE key LIKE ?", (cli.SAID_PREFIX + "%",))]
    assert len(marks) == 1                                                                                 # A's mark, older than six hours, is gone


def test_f12_review_a_failure_after_its_recovery_is_told_again_and_the_recovery_too(tmp_path, monkeypatch):
    """Review 1 (D1): «не отвечает» → «снова отвечает» → «не отвечает» within six hours: each half of the second episode is told, so the
    owner's last message is always the true state."""
    from test_order_notify import env as order_env
    from tires_sync import cli
    conn, box = order_env(tmp_path)
    tg = _tg(monkeypatch)
    clock = [1_000_000.0]
    monkeypatch.setattr(cli, "_epoch", lambda: clock[0])
    down, up = "Tires Sync: Авито не отвечает: 5 сбоев", "Tires Sync: Авито снова отвечает."
    for _episode in range(2):
        assert cli._alert(conn, box, down) is True
        clock[0] += 600
        assert cli._alert(conn, box, up, recovery=True) is True                  # N1: a recovery says so itself (any notice did before)
        clock[0] += 600
    assert [__import__("json").loads(c[3])["text"] for c in tg.calls] == [down, up, down, up]
    assert cli._alert(conn, box, up, recovery=True) is True and len(tg.calls) == 4                       # the same news twice in a row: quiet


def test_f12_review_a_notice_telegram_refused_keeps_the_failures_quiet(tmp_path, monkeypatch):
    from test_order_notify import TG_OK, env as order_env
    from tires_sync import cli
    conn, box = order_env(tmp_path)
    tg = _tg(monkeypatch, TG_OK, (502, {}), TG_OK)
    monkeypatch.setattr(cli, "_epoch", lambda: 1_000_000.0)
    assert cli._alert(conn, box, "сбой") is True
    assert cli._alert(conn, box, "восстановилось", recovery=True) is False
    assert cli._alert(conn, box, "сбой") is True and len(tg.calls) == 2                                  # the recovery was not told: still quiet


def test_n1_other_news_leaves_a_lasting_failure_quiet(tmp_path, monkeypatch):
    """Хвост N1 (match3): news that is not a recovery (new models without a pair, manual ads…) no longer frees the failures' marks, so a
    failure of hours is not told again every cycle next to it."""
    from test_order_notify import env as order_env
    from tires_sync import cli
    conn, box = order_env(tmp_path)
    tg = _tg(monkeypatch)
    clock = [1_000_000.0]
    monkeypatch.setattr(cli, "_epoch", lambda: clock[0])
    down = "Tires Sync: Google-таблица не принимает заказы"
    for news in ("Tires Sync: новые модели не сопоставлены (1): X", "Tires Sync: объявления, сделанные вручную: Y"):
        assert cli._alert(conn, box, down) is True
        clock[0] += 600
        assert cli._alert(conn, box, news, notice=True) is True
        clock[0] += 600
    assert cli._alert(conn, box, down) is True
    assert [__import__("json").loads(c[3])["text"] for c in tg.calls] == [down, "Tires Sync: новые модели не сопоставлены (1): X",
                                                                         "Tires Sync: объявления, сделанные вручную: Y"]


def test_n1_a_service_back_through_the_breakers_alert_frees_the_marks(tmp_path, monkeypatch):
    from test_order_notify import env as order_env
    from tires_sync import cli, reliability
    conn, box = order_env(tmp_path)
    tg = _tg(monkeypatch)
    clock = [1_000_000.0]
    monkeypatch.setattr(cli, "_epoch", lambda: clock[0])
    alert = reliability.owner_alert(lambda: box)
    down, up = "Tires Sync: Авито не отвечает: 5 сбоев", "Tires Sync: Авито снова отвечает."
    alert(conn, down)
    clock[0] += 600
    alert(conn, up, "notice")
    clock[0] += 600
    alert(conn, down)
    assert [__import__("json").loads(c[3])["text"] for c in tg.calls] == [down, up, down]


def test_n1_the_table_back_frees_the_marks_and_its_failure_alert_does_not(tmp_path, monkeypatch):
    from test_order_notify import env as order_env
    from tires_sync import cli, order_sheet
    conn, box = order_env(tmp_path)
    tg = _tg(monkeypatch)
    clock = [1_000_000.0]
    monkeypatch.setattr(cli, "_epoch", lambda: clock[0])
    monkeypatch.setattr(cli, "load_box", lambda key_file: box)
    monkeypatch.setattr(order_sheet, "flush", lambda *a, **k: {})
    monkeypatch.setattr(order_sheet, "alarm_told", lambda *a: None)
    said = [("Tires Sync: таблица не принимает заказы 3 часа", False), (order_sheet.RECOVERED, True)]
    monkeypatch.setattr(order_sheet, "lasting_alarm", lambda conn, now: said.pop(0))
    down = "Tires Sync: Авито не отвечает: 5 сбоев"
    assert cli._alert(conn, box, down) is True
    cli._google_sheet(conn, "key")                                                # the table's own failure: not a recovery
    assert cli._alert(conn, box, down) is True and len(tg.calls) == 2              # still quiet
    cli._google_sheet(conn, "key")                                                # «снова принимает»: a recovery
    assert cli._alert(conn, box, down) is True
    assert [__import__("json").loads(c[3])["text"] for c in tg.calls] == [down, "Tires Sync: таблица не принимает заказы 3 часа",
                                                                         order_sheet.RECOVERED, down]


@pytest.mark.parametrize("ours", ["Да, есть.\nЦена 5000\nЖдём вас", 'Шина "Nokian" 205/55 R16 есть', "Ждём 😊 вас 😊 завтра 😊 к 10"])
def test_ch4_review_short_lines_quotes_slashes_and_emoji_of_ours_are_found_in_a_json_body(ours):
    """Review 1 (D2): a raw JSON body escapes \\n, \\", \\/ and writes an emoji as a surrogate pair: our text is still recognised."""
    import json
    from test_chat_send_refusal import refusal
    body = '{"errors": [{"value": %s}]}' % json.dumps(ours).replace("/", "\\/")
    assert chats._quotes(body, ours)
    assert chats._avito_said(refusal(400, body), ours) == "код 400: текст ответа скрыт, в нём наше сообщение"


def test_ch3_review_a_404_at_the_upload_of_a_photo_names_no_chat(env):
    """Review 1 (D3): the upload has no chat in it; its 404 is «повтор не поможет — напишите владельцу», not «чат не найден»."""
    from test_chat_send_refusal import refusal
    conn, chat_row, card = env
    sender = Refusing(refusal(404, {"error": {"message": "not found"}}))
    sender.at_upload = True
    with pytest.raises(chats.ChatError) as raised:
        chats.send_image(conn, sender, chat_row, picture(), "tok-1", NOW, "owner")
    assert str(raised.value) == "Авито не принял фото (код 404: not found). Авито не принял запрос: повтор не поможет — напишите владельцу."
    assert status(conn)[0] == "failed"


def test_ch10_review_the_point_of_a_written_handoff_comes_first(tmp_path):
    """Review 1 (D4): the owner handed over for a point they picked (their login has no point): the line names the handoff's point."""
    from tires_sync import order_manager as mgr, order_points as points, orders, store
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    pid = points.save_point(conn, {"name": "Гамма", "manager_name": "Б", "channel": "panel"})
    card = orders.create_card(conn, dict(client_name="Т"), NOW, "o")
    conn.execute("INSERT INTO order_command_log (message_id, at, sender, word, order_id, outcome) VALUES ('chat-1', '2026-10-13T11:00:00', 'панель: owner', "
                 "'под заказ', NULL, 'обрабатывается')")
    conn.execute("INSERT INTO order_handoff (order_id, message_id, point_id, created_at) VALUES (?, 'chat-1', ?, '2026-10-13T11:00:00')", (card, pid))
    conn.commit()
    assert mgr.note_stuck(conn, "2026-10-13T12:00:00", "2026-10-13T11:55:00") == 1
    assert [item["point"] for item in mgr.list_inbox(conn)] == ["Гамма"]


def test_ch8_review_without_the_key_of_the_community_the_hint_keeps_its_pause(tmp_path, monkeypatch):
    """Review 1 (D5): with no key every message would try and fail again: the hint is tried once in 30 minutes, as before."""
    from test_vk_commands import hook, ready
    from tires_sync import order_manager as mgr, vk_callback, vk_send
    app, client, tok, conn, pid, card = ready(tmp_path)
    monkeypatch.setattr(vk_callback, "_alarm", lambda conn, box, text: None)
    monkeypatch.setattr(vk_send, "sender_for", lambda *a, **k: None)
    hook(app, conn, "привет", event="n1")
    hook(app, conn, "ау", event="n2")
    assert conn.execute("SELECT value FROM app_state WHERE key = ?", (mgr.HINT_KEY % pid,)).fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'vk_answer_failed'").fetchone()[0] == 1
