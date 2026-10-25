"""Owner, 2026-10-15: every connected Avito account has its own markup by brand, title template and address of the photos."""
from __future__ import annotations

import pytest
from datetime import date

from test_feed_account_rules import NOW, REF, UNLOAD, feed, two_accounts
from tires_sync import connections, cycle


def build(tmp_path, conn, day=14):
    cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, day), tmp_path / "feed")


def by_id(root):
    return {a.findtext("Id").split("_")[0]: a for a in root}


def test_a_brand_markup_changes_the_price_of_that_account_only(tmp_path):
    conn, first, second = two_accounts(tmp_path)
    build(tmp_path, conn)
    base = {k: int(a.findtext("Price")) for k, a in by_id(feed(tmp_path, conn, second)).items()}
    connections.set_brand_deltas(conn, second, {"Ikon Tyres": 3.0}, NOW, "t")
    build(tmp_path, conn, day=15)
    same_first = {k: int(a.findtext("Price")) for k, a in by_id(feed(tmp_path, conn, first)).items()}
    up = {k: int(a.findtext("Price")) for k, a in by_id(feed(tmp_path, conn, second)).items()}
    assert all(up[k] > base[k] for k in base)                                              # the brand of the account got dearer
    assert same_first == {k: int(a.findtext("Price")) for k, a in by_id(feed(tmp_path, conn, first)).items()}
    assert connections.get_brand_deltas(conn, second) == {"IKON TYRES": 3.0} and connections.get_brand_deltas(conn, first) == {}


def test_the_brand_markup_text_is_parsed_and_checked():
    assert connections.parse_brand_deltas("Kumho: 1.5\nikon  tyres = −2\n\nNexen: 0") == {"KUMHO": 1.5, "IKON TYRES": -2.0, "NEXEN": 0.0}
    for bad in ("Kumho", "Kumho: abc", "Kumho: 11", "Kumho: -10.5"):
        with pytest.raises(ValueError):
            connections.parse_brand_deltas(bad)


def test_a_zero_markup_removes_the_brand(tmp_path):
    conn, first, second = two_accounts(tmp_path)
    connections.set_brand_deltas(conn, second, {"Kumho": 1.0, "Nexen": 2.0}, NOW, "t")
    connections.set_brand_deltas(conn, second, {"Kumho": 1.0, "Nexen": 0.0}, NOW, "t")
    assert connections.get_brand_deltas(conn, second) == {"KUMHO": 1.0}


def test_a_title_template_is_for_its_account_and_falls_back_when_it_does_not_fit(tmp_path):
    conn, first, second = two_accounts(tmp_path)
    connections.set_title_template(conn, second, "{brand} {model} {size} {indexes}", NOW, "t")
    build(tmp_path, conn)
    own = {a.findtext("Title") for a in feed(tmp_path, conn, second)}
    std = {a.findtext("Title") for a in feed(tmp_path, conn, first)}
    assert own and all(t.startswith("Ikon Tyres Test Model 205 55 R16") for t in own)
    assert all(t.endswith(" в наличии") for t in std)                                      # the other account keeps the standard title
    for bad in ("{nope}", "x" * 121, "a\x01b"):
        with pytest.raises(ValueError):
            connections.set_title_template(conn, second, bad, NOW, "t")
    connections.set_title_template(conn, second, "", NOW, "t")
    assert connections.get_title_template(conn, second) == ""


def test_a_template_that_makes_a_title_longer_than_the_limit_gives_the_standard_one():
    from helpers import make_offer, make_settings
    from tires_sync.candidates import Candidate
    from tires_sync.feed import render_ad
    c = Candidate("1", make_offer(goods_id="1"), 4810, "Ikon Tyres", "Test Model")
    ad = render_ad(c, 4810, "P1", make_settings(), date(2026, 10, 14), title_template="{brand} {model} " * 8)
    assert ad.findtext("Title").endswith(" в наличии") and len(ad.findtext("Title")) <= 70


def test_an_account_can_have_its_own_address_for_photos(tmp_path):
    conn, first, second = two_accounts(tmp_path, base="https://panel.example")
    connections.set_photo_base(conn, second, "https://img.second.example/p/", NOW, "t")
    build(tmp_path, conn)
    token = connections.get_account(conn, second)["feed_token"]
    assert all(a.find("Images/Image").get("url").startswith("https://img.second.example/p/img/%s/" % token) for a in feed(tmp_path, conn, second))
    assert all(a.find("Images/Image").get("url").startswith("https://panel.example/img/") for a in feed(tmp_path, conn, first))
    for bad in ("http://img.example", "https://img.example/a b", "https://img.example/?x=1", "img.example"):
        with pytest.raises(ValueError):
            connections.set_photo_base(conn, second, bad, NOW, "t")


def test_the_account_page_saves_the_three_settings(tmp_path):
    from panel_helpers import login, make_app, post
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    from tires_sync import store
    conn = store.connect(app.config["DB_PATH"])
    account = connections.create_account(conn, "Второй", NOW, "o")
    conn.commit()
    page = client.get("/connections/avito/%d" % account)
    assert page.status_code == 200 and "Наценка по брендам" in page.get_data(as_text=True) and "Шаблон заголовка" in page.get_data(as_text=True)
    post(client, "/connections/accounts/%d/brand-pricing" % account, tok, brands="Kumho: 1.5")       # no price rule for this brand in the test settings
    assert connections.get_brand_deltas(store.connect(app.config["DB_PATH"]), account) == {}                       # an unknown brand is not saved silently
    post(client, "/connections/accounts/%d/brand-pricing" % account, tok, brands="Ikon Tyres: 1.5")
    post(client, "/connections/accounts/%d/title" % account, tok, template="{brand} {size}")
    post(client, "/connections/accounts/%d/photo-base" % account, tok, base="https://img.example")
    post(client, "/connections/accounts/%d/photo-style" % account, tok, label="Второй", corner="top-left", border="#112233", crop="2", quality="85")
    conn = store.connect(app.config["DB_PATH"])
    assert connections.get_brand_deltas(conn, account) == {"IKON TYRES": 1.5}
    assert connections.get_title_template(conn, account) == "{brand} {size}"
    assert connections.get_photo_base(conn, account) == "https://img.example"
    assert connections.get_photo_style(conn, account) == {"label": "Второй", "corner": "top-left", "border": "#112233", "crop": 2.0, "quality": 85}
    post(client, "/connections/accounts/%d/title" % account, tok, template="{nope}")
    assert connections.get_title_template(store.connect(app.config["DB_PATH"]), account) == "{brand} {size}"     # a bad template is not saved


def test_brand_names_are_read_as_the_rules_key_them():
    assert connections._clean_brand("S2:Kumho") == "KUMHO" and connections._clean_brand("Кумхо") == "KUMHO"
    assert connections._clean_brand(" ikon   tyres ") == "IKON TYRES" and connections._clean_brand("Айкон Тайерс") == "IKON TYRES"


def test_the_total_delta_of_an_account_and_a_brand_stays_within_ten_points(tmp_path):
    from helpers import make_settings
    conn, first, second = two_accounts(tmp_path)
    connections.set_account_pricing(conn, second, -10.0, None, NOW, "t")
    connections.set_brand_deltas(conn, second, {"Ikon Tyres": -10.0}, NOW, "t")
    base = make_settings()
    eff = connections.effective_settings(conn, second, base)
    key = next(k for k in base.pricing if k[0] == "IKON TYRES")
    assert eff.pricing[key].pct_small == round(base.pricing[key].pct_small - 10.0, 4)          # not −20


def test_the_season_of_a_title_template_is_in_russian():
    from helpers import make_offer, make_settings
    from tires_sync.candidates import Candidate
    from tires_sync.feed import render_ad
    c = Candidate("1", make_offer(goods_id="1"), 4810, "Ikon Tyres", "Test Model")
    ad = render_ad(c, 4810, "P1", make_settings(), date(2026, 10, 14), title_template="{brand} {season}")
    assert ad.findtext("Title") == "Ikon Tyres зимняя"


def test_a_photo_look_changes_the_link_and_a_broken_picture_is_served_as_it_is(tmp_path):
    from tires_sync import photo_style
    conn, first, second = two_accounts(tmp_path)
    connections.set_photo_style(conn, second, {"label": "Два"}, NOW, "t")
    build(tmp_path, conn)
    version = photo_style.version(connections.get_photo_style(conn, second))
    assert all(a.find("Images/Image").get("url").endswith("?s=%s" % version) for a in feed(tmp_path, conn, second))
    assert all("?" not in a.find("Images/Image").get("url") for a in feed(tmp_path, conn, first))
    assert photo_style.process(b"\xff\xd8\xff garbage", {"label": "x"}) is None
