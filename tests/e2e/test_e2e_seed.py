"""The base of the screen audit (tests/e2e/seed.py) has an order on every stage and every role signs in; the fake clients refuse.
No browser here: the crawl itself is `python tests/e2e/crawl.py` (tests/e2e/AUDIT-2026-11-08.md)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import seed  # noqa: E402
import serve  # noqa: E402

from tires_sync import orders, store  # noqa: E402
from tires_sync.panel import create_app  # noqa: E402


@pytest.fixture
def seeded(tmp_path):
    return tmp_path, seed.build(tmp_path)


def test_every_stage_of_the_board_has_an_order_and_the_split_one_has_two_webmim_numbers(seeded):
    root, out = seeded
    conn = store.connect(out["db"])
    codes = {orders.get_card(conn, cid)["stage"]["code"] for cid in out["orders"].values()}
    assert {"new", "talk", "placed", "moving", "at_point", "oth", "return_given", "return", "done", "cancel"} <= codes
    assert conn.execute("SELECT count(*) FROM order_mim_extra").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM avito_chat").fetchone()[0] == 8
    assert {r for (r,) in conn.execute("SELECT DISTINCT role FROM panel_user_role")} == {"partner", "chats", "viewer", "point", "client"}
    assert {store.get_state(conn, "ui_new.%s" % g) for g in seed.UI_GROUPS} == {"all"}


@pytest.mark.parametrize("login,page", [("owner", "/orders"), ("office1", "/chats"), ("point1", "/my-point"), ("client1", "/shop")])
def test_each_role_signs_in_and_opens_its_page_with_no_outside_call(seeded, login, page):
    root, out = seeded
    app = create_app(dict(serve.config(root), TESTING=True))
    client = app.test_client()
    before = len(serve.OUTSIDE)
    password = seed.OWNER_PASSWORD if login == "owner" else seed.PASSWORD
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/login").get_data(as_text=True)).group(1)
    assert client.post("/login", data={"csrf": csrf, "login": "" if login == "owner" else login, "password": password}).status_code == 302
    r = client.get(page)
    assert r.status_code == 200 and "ui-new" in r.get_data(as_text=True) and len(serve.OUTSIDE) == before


def test_a_fake_client_refuses_and_is_counted():
    before = len(serve.OUTSIDE)
    with pytest.raises(OSError):
        serve.fake("avito_http")("GET", "https://example.com/", {}, None)
    assert len(serve.OUTSIDE) == before + 1
