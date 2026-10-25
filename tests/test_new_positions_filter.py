"""«Новые позиции» do not show the brands that are switched off for publication."""
from __future__ import annotations

from datetime import datetime

from panel_helpers import login, make_app
from tires_sync import new_filter, store
from tires_sync.models import Season


class S:                                                    # the two fields of Settings that the filter reads
    enabled = frozenset({("IKON TYRES", Season.WINTER), ("KUMHO", Season.SUMMER)})
    excluded_brands = frozenset({"KUMHO"})


def test_a_brand_is_disabled_when_it_is_excluded_or_no_season_of_it_is_on():
    assert new_filter.disabled_brands(S, ["IKON TYRES", "KUMHO", "NEXEN", "S2:IKON"]) == {"KUMHO", "NEXEN", "S2:IKON"}


def test_the_filter_drops_goods_of_disabled_brands_and_keeps_the_rest():
    goods = [{"goods_id": "1", "brand_key": "IKON TYRES"}, {"goods_id": "2", "brand_key": "KUMHO"}, {"goods_id": "3", "brand_key": "NEXEN"}]
    assert [g["goods_id"] for g in new_filter.visible(S, goods)] == ["1"]


def test_the_page_and_the_menu_counter_hide_them_but_the_new_brands_stay_listed(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 7, 12, 0, 0))
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    conn.execute("INSERT INTO run (id, started, dry_run, input_sha, summary) VALUES (1, '2026-10-06T10:00:00', 0, 'a', '{}'), (2, '2026-10-07T10:00:00', 0, 'b', '{}')")
    for goods_id, brand in (("11", "IKON TYRES"), ("12", "NEXEN"), ("13", "PIRELLI")):
        conn.execute("INSERT INTO seen_goods (goods_id, brand_key, model_raw, name, first_run) VALUES (?, ?, 'm', ?, 2)", (goods_id, brand, "Шина-" + brand.replace(" ", "")))
    conn.execute("INSERT INTO seen_brand (brand_key, first_run) VALUES ('NEXEN', 2)")
    conn.commit()
    page = client.get("/new").get_data(as_text=True)
    assert "Шина-IKONTYRES" in page and "Шина-PIRELLI" in page and "Шина-NEXEN" not in page and "Новые позиции (2)" in page
    assert "Новые бренды (1)" in page                                                       # a new brand is still shown to be decided on
    assert 'class="sb-count">2<' in client.get("/orders").get_data(as_text=True).replace(" ", "").replace("\n", "") or "sb-count" in client.get("/orders").get_data(as_text=True)
