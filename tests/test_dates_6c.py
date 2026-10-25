"""The joint of the dates (6A × 6B): the time of a run is written in UTC and shown in Moscow time everywhere — the main page, «Фиды» and
«Новые позиции» show one time, also across midnight; a reversal of a manual sale is UTC, of an order's sale Moscow time."""
from __future__ import annotations

import json
import re

from panel_helpers import login, make_app
from tires_sync import sales, store

UTC_RUN = "2026-10-20T22:30:00"                      # 21.10 01:30 in Moscow


def test_one_run_shows_one_moscow_time_on_every_page(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    conn.execute("INSERT INTO run (started, dry_run, input_sha, summary) VALUES (?, 0, 'x', ?)", (UTC_RUN, json.dumps({"publish": 1})))
    conn.commit()
    client = app.test_client()
    login(client)
    for url in ("/", "/feeds", "/new"):
        page = client.get(url).get_data(as_text=True)
        assert re.search(r"21\.10(\.2026)? 01:30", page), url
        assert not re.search(r"20\.10(\.2026)? 22:30", page), url


def test_a_reversal_is_shown_by_how_it_was_written(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    common = dict(goods_id="g", brand_key="nokian", season="winter", diameter=16, name="Nokian", ad_kind="single", pieces=4, sale_total=20000,
                  supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, account_id=None, point="", now="2026-10-07T10:00:00", author="o",
                  today="2026-10-07")
    manual = sales.add_sale(conn, day="2026-10-06", note="вручную", source="manual", **common)
    of_order = sales.add_sale(conn, day="2026-10-06", note="Заказ №7", source="order", **common)
    sales.reverse_sale(conn, manual, "2026-10-06T22:30:00", "owner")                           # the panel writes UTC: 07.10 01:30 Moscow
    sales.reverse_sale(conn, of_order, "2026-10-06T22:30:00", "owner", from_order=True)        # an order writes Moscow time: as it is
    client = app.test_client()
    login(client)
    page = client.get("/economics/sales?from=2026-10-01&to=2026-10-31&rev=1").get_data(as_text=True)
    titles = re.findall(r'title="Сторнировал owner, ([^"]+)"', page)
    assert len(titles) == 2 and any(re.search(r"07\.10(\.2026)? 01:30", t) for t in titles) \
        and any(re.search(r"06\.10(\.2026)? 22:30", t) for t in titles), titles


def test_the_readiness_times_are_moscow_time_too(tmp_path):
    from tires_sync import connections
    from tires_sync.panel import service
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    connections.record_check(conn, "webmim", 0, True, "ключ получен", UTC_RUN)                    # the panel writes UTC
    (tmp_path / "unload.xml.meta.json").write_text(json.dumps({"fetched_at": UTC_RUN, "items": 4}))
    client = app.test_client()
    login(client)
    client.get("/")
    from tires_sync.panel.data import load_merged, load_reference
    from tires_sync.settings_store import active_settings
    settings = active_settings(conn)
    text = " ".join(s["detail"] for s in service.readiness(conn, app.config["UNLOAD_PATH"], load_merged(conn, settings, app.config["UNLOAD_PATH"]),
                                                             load_reference(app.config["REFERENCE_PATH"]), settings))
    assert text.count("21.10.2026 01:30:00") == 2 and "22:30" not in text
