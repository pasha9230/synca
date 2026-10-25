"""«Подключения → Авито»: every account with the phone of its cabinet, the phone that goes out in its ads and what is wrong, for the owner to check by eye."""
from __future__ import annotations

from datetime import datetime

from panel_helpers import login, make_app
from tires_sync import connections, store

NOW = "2026-10-14T12:00:00"


def feed(tmp_path, token, *phones, folder=""):
    root = tmp_path / "feed" / token / folder
    root.mkdir(parents=True, exist_ok=True)
    ads = "".join("<Ad><Id>%d</Id><ContactPhone>%s</ContactPhone></Ad>" % (n, p) for n, p in enumerate(phones))
    (root / "avito_feed.xml").write_text('<Ads formatVersion="3">%s</Ads>' % ads, encoding="utf-8")


def setup(tmp_path):
    (tmp_path / "feed").mkdir()
    app = make_app(tmp_path, FEED_ROOT=str(tmp_path / "feed"), NOW=lambda: datetime(2026, 10, 14, 12, 0, 0))
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    return app, client, conn


def page(client, account_id):
    return client.get("/connections/avito/%d" % account_id).get_data(as_text=True)


def test_the_table_shows_the_phone_of_each_cabinet_and_what_goes_out(tmp_path):
    app, client, conn = setup(tmp_path)
    a = connections.create_account(conn, "Основной", NOW, "o")
    b = connections.create_account(conn, "Партнёр", NOW, "o")
    connections.set_avito_identity(conn, a, 1, "Основной", "79000000011")
    connections.set_avito_identity(conn, b, 2, "Партнёр", "79000000012")
    feed(tmp_path, connections.get_account(conn, a)["feed_token"], "+79000000011", "+7 900 000-00-11")
    feed(tmp_path, connections.get_account(conn, b)["feed_token"], "+7 900 000-00-13")                      # the author's number of the file
    html = page(client, a)
    block = html.split("Сверка телефонов", 1)[1].split("</section>", 1)[0]
    assert "+7 900 000-00-11" in block and "+7 900 000-00-12" in block
    assert "совпадает" in block and "в объявлениях другой номер" in block and "+7 900 000-00-13" in block


def test_a_connected_account_without_the_phone_is_marked_and_a_shared_number_is_a_warning(tmp_path, bare_accounts):
    app, client, conn = setup(tmp_path)
    a = connections.create_account(conn, "Один", NOW, "o")
    b = connections.create_account(conn, "Два", NOW, "o")
    c = connections.create_account(conn, "Три", NOW, "o")
    for n in (a, b, c):
        conn.execute("INSERT INTO secret (kind, owner_id, name, ciphertext, updated) VALUES ('avito', ?, 'client_id', 'x', ?)", (n, NOW))
    conn.commit()
    connections.set_avito_identity(conn, a, 1, "Один", "79000000012")
    connections.set_avito_identity(conn, b, 2, "Два", "79000000012")
    block = page(client, a).split("Сверка телефонов", 1)[1].split("</section>", 1)[0]
    assert "номер кабинета не определён" in block                                       # «Три»: keys, no phone
    assert block.count("тот же номер у") == 2                                           # «Один» and «Два» name each other


def test_without_a_feed_the_table_says_so_and_html_is_escaped(tmp_path):
    app, client, conn = setup(tmp_path)
    a = connections.create_account(conn, "<b>x</b>", NOW, "o")
    block = page(client, a).split("Сверка телефонов", 1)[1].split("</section>", 1)[0]
    assert "<b>x</b>" not in block and "фид не опубликован" in block


def test_the_same_address_in_two_accounts_is_a_warning(tmp_path):
    app, client, conn = setup(tmp_path)
    a = connections.create_account(conn, "Один", NOW, "o")
    b = connections.create_account(conn, "Два", NOW, "o")
    c = connections.create_account(conn, "Три", NOW, "o")
    row = lambda i, text: [{"name": "Точка", "address_id": i, "enabled": True, "share": 100, "address_text": text}]
    connections.save_addresses(conn, a, "percent", None, row("101", "Санкт-Петербург, Условный пр-т, 125"), NOW, "o", address_mode="text")
    connections.save_addresses(conn, b, "percent", None, row("102", "санкт-петербург,  условный пр-т, 125"), NOW, "o", address_mode="text")
    connections.save_addresses(conn, c, "percent", None, row("103", "Санкт-Петербург, пр-т Образцова, 21к1"), NOW, "o", address_mode="text")
    block = page(client, a).split("Сверка телефонов", 1)[1].split("</section>", 1)[0]
    assert block.count("тот же адрес в другом аккаунте") == 2                            # «Один» and «Два» name each other; «Три» is fine
