"""Left over from the full review: huge numbers in addresses are a 404/empty value, not a 500; chats do not ask Avito who we are on every run."""
from __future__ import annotations

import pytest

from panel_helpers import login, make_app
from tires_sync import chats, connections, store

BIG = "9" * 30


@pytest.mark.parametrize("url", [
    "/orders/%s" % BIG, "/chats/%s" % BIG, "/draft/%s" % BIG, "/connections/avito/%s" % BIG, "/accounts/%s/addresses" % BIG,
    "/connections/webmim/%s" % BIG, "/chats?acc=%s" % BIG, "/economics/sales?a=%s" % BIG, "/orders?point=%s" % BIG,
])
def test_a_huge_number_in_an_address_is_never_a_server_error(tmp_path, url):
    client = make_app(tmp_path).test_client()
    login(client)
    assert client.get(url).status_code in (200, 302, 400, 404)         # never 500


def test_the_post_with_a_huge_supplier_number_is_not_a_server_error(tmp_path):
    client = make_app(tmp_path).test_client()
    tok = login(client)
    resp = client.post("/connections/webmim", data={"csrf": tok, "supplier": BIG, "base_url": "https://x.example", "mode": "login"})
    assert resp.status_code < 500


def test_the_chat_sync_does_not_ask_avito_for_the_cabinet_id_when_it_is_known(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    account = connections.create_account(conn, "A", "2026-10-14T10:00:00", "o")
    connections.set_avito_identity(conn, account, 80000001, "A", "79000000000")

    class Client:
        asked = 0

        def get_self(self, retry=True):
            Client.asked += 1
            return {"id": 80000001, "name": "A", "phone": ""}

        def chats(self, *a, **k):
            return {"chats": []}
    chats.sync_account(conn, Client(), account, "2026-10-14T10:00:00", max_pages=1)
    assert Client.asked == 0
    other = connections.create_account(conn, "B", "2026-10-14T10:00:00", "o")                   # unknown yet: asked once, then remembered
    chats.sync_account(conn, Client(), other, "2026-10-14T10:00:00", max_pages=1)
    chats.sync_account(conn, Client(), other, "2026-10-14T10:05:00", max_pages=1)
    assert Client.asked == 1 and connections.get_avito_identity(conn, other)["id"] == 80000001
