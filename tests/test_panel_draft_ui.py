from __future__ import annotations

from panel_helpers import UNLOAD, login, make_app, post
from tires_sync.panel import service
from tires_sync.webmim import parse_unload


def draft_page(client, tok, brand, season, small, large):
    post(client, "/settings/pricing", tok, scope="brand", supplier="1", brand=brand,
         **{season + "_form": "1", season + "_small": small, season + "_large": large, season + "_on": "on"})
    r = client.get("/catalog/review")
    assert r.status_code == 302 and "/draft/" in r.headers["Location"]
    return client.get(r.headers["Location"]).get_data(as_text=True)


def test_plural_follows_russian_rules():
    word = lambda n: service.plural(n, "товар", "товара", "товаров")
    assert [word(n) for n in (0, 1, 2, 4, 5, 11, 12, 21, 22, 25, 111)] == [
        "товаров", "товар", "товара", "товара", "товаров", "товаров", "товаров", "товар", "товара", "товаров", "товаров"]


def test_changes_are_described_in_plain_words_not_raw_keys():
    raw = {"enabled": [["IKON TYRES", "winter"]], "pricing": {"IKON TYRES|winter": {"pct_small": -3.82, "pct_large": -4.28}},
           "min_stock": 4, "point_values": {"A": 40, "B": 60}}
    lines = service.describe_changes(raw, {
        "enabled": [["IKON TYRES", "winter"], ["LAUFENN", "summer"]],
        "pricing": {"IKON TYRES|winter": {"pct_small": -3.0, "pct_large": -4.28},
                    "LAUFENN|summer": {"pct_small": 5.0, "pct_large": 5.0}},
        "min_stock": 6, "point_values": {"A": 50, "B": 50}})
    text = "\n".join(lines)
    assert "Включить публикацию: LAUFENN · лето" in text
    assert "Процент IKON TYRES · зима: до R17 −3.82 % → −3 %, от R18 −4.28 % → −4.28 %" in text
    assert "Процент LAUFENN · лето: до R17 не задан → +5 %, от R18 не задан → +5 %" in text
    assert "Минимальный остаток: 4 → 6" in text
    assert "Доли по точкам: A 40, B 60 → A 50, B 50" in text
    assert "enabled" not in text and "pricing" not in text
    off = service.describe_changes(raw, {"enabled": []})
    assert off == ["Выключить публикацию: IKON TYRES · зима"]


def test_draft_page_shows_plain_lines_effects_and_keeps_raw_details(tmp_path):
    client = make_app(tmp_path).test_client()
    tok = login(client)
    html = draft_page(client, tok, "LAUFENN", "winter", "-5", "-6")
    assert "Включить публикацию: LAUFENN · зима" in html
    assert "Процент LAUFENN · зима: до R17 не задан → −5 %" in html
    assert "Появится" in html and "Исчезнет" in html and "Изменится цена" in html
    assert "<details" in html and "enabled" in html and "pricing" in html


def test_funnel_explains_how_many_goods_of_a_new_combination_pass(tmp_path):
    client = make_app(tmp_path).test_client()
    tok = login(client)
    html = draft_page(client, tok, "LAUFENN", "winter", "-5", "-6")
    assert "Что попадёт в фид по новым сочетаниям" in html
    assert "LAUFENN · зима" in html and "из 1 позиции" in html


def test_funnel_lists_the_reasons_goods_do_not_pass():
    parsed = parse_unload(UNLOAD, "T1")
    from panel_helpers import REF_TEXT
    from tires_sync.catalog import parse_reference
    from helpers import make_settings
    ref = parse_reference(REF_TEXT)
    new = make_settings(enabled=[["НЕИЗВЕСТНЫЙ", "winter"]], pricing={"НЕИЗВЕСТНЫЙ|winter": {"pct_small": -1, "pct_large": -1}})
    rows = service.pair_funnel(parsed, ref, {}, {}, new, [("НЕИЗВЕСТНЫЙ", "winter")])
    assert rows and rows[0]["total"] >= 1 and rows[0]["ok"] == 0
    reasons = dict(rows[0]["reasons"])
    assert any("справочнике" in text for text in reasons)


def test_price_changes_show_product_names(tmp_path):
    client = make_app(tmp_path).test_client()
    tok = login(client)
    html = draft_page(client, tok, "IKON TYRES", "winter", "-1", "-1")
    name = next(o.name_orig for o in parse_unload(UNLOAD, "T1").offers.values() if o.brand_key == "IKON TYRES")   # the Latin catalog name, not the supplier's Russian one
    assert "Изменится цена" in html and name in html
