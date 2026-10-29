"""The tails of build 1 of the role «Клиент» (plan client-role, report client1): the server checks the person sent as responsible or on
duty against the list the form offers (a forged POST never names a client), and the server's user-add takes a role."""
from __future__ import annotations

import io

import pytest

from panel_helpers import post
from test_client_terms import owner_of
from test_shop import make_shop
from tires_sync import cli, orders, store, users

NOW = "2026-10-29T12:00:00"
PASSWORD = "длинный-пароль-1"


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


# --- a client is never responsible nor on duty -----------------------------------------------------------------------------------------

def test_a_forged_post_never_makes_a_client_responsible_or_on_duty(tmp_path):
    app, buyer, conn, _ = make_shop(tmp_path)
    users.add_user(conn, "olya", PASSWORD, NOW, role="chats")
    owner, tok = owner_of(app)
    before = conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0]
    post(owner, "/orders/new", tok, client_name="Иван", client_phone="", responsible="buyer")
    assert conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == before
    post(owner, "/orders/new", tok, client_name="Иван", responsible="olya")
    card_id, who = conn.execute("SELECT id, responsible FROM order_card ORDER BY id DESC").fetchone()
    assert who == "olya"
    post(owner, "/orders/%d/save" % card_id, tok, responsible="buyer")
    post(owner, "/orders/%d/move" % card_id, tok, stage_id=str(orders.list_stages(conn)[1]["id"]), responsible="buyer")
    assert conn.execute("SELECT responsible FROM order_card WHERE id = ?", (card_id,)).fetchone() == ("olya",)
    post(owner, "/orders/settings/responsible", tok, responsible="buyer")
    assert orders.default_responsible(conn) == ""
    post(owner, "/orders/settings/responsible", tok, responsible="olya")
    assert orders.default_responsible(conn) == "olya"
    from tires_sync import order_manager as manager
    post(owner, "/orders/settings/duty", tok, day0="buyer", start="9", end="18")
    post(owner, "/orders/settings/duty", tok, today="buyer", start="9", end="18")
    duty = manager.duty_settings(conn)
    assert "buyer" not in duty["week"].values() and duty["today"].get("login") != "buyer"
    post(owner, "/orders/settings/duty", tok, day0="olya", today="olya", start="9", end="18")
    assert manager.duty_settings(conn)["week"][0] == "olya"


# --- the server's command ---------------------------------------------------------------------------------------------------------------

def test_user_add_takes_a_role(tmp_path, monkeypatch, capsys):
    db = tmp_path / "t.db"
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert cli.main(["user-add", "--login", "shop1", "--password-stdin", "--role", "client", "--name", "ООО Шины", "--db", str(db)]) == 0
    conn = store.connect(db)
    assert users.get_role(conn, "shop1") == {"role": "client", "active": True, "point_id": None, "display_name": "ООО Шины"}
    assert "Клиент" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    with pytest.raises(SystemExit):
        cli.main(["user-add", "--login", "shop2", "--password-stdin", "--role", "boss", "--db", str(db)])
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert cli.main(["user-add", "--login", "anna", "--password-stdin", "--db", str(db)]) == 0
    assert users.get_role(conn, "anna")["role"] == "owner"
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert cli.main(["user-add", "--login", "pt", "--password-stdin", "--role", "point", "--point-id", "99", "--db", str(db)]) == 2
    assert not users.exists(conn, "pt")                                                 # no such point
