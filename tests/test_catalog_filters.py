"""The owner's catalog (the owner 2026-10-29): several sizes through spaces and whole sizes as the client catalog reads them, several brands
and «Кроме брендов» (the view only), the brand off and on the feed from the catalog for the owner only (the change of «Исключения», into the
working draft, nothing applied at once), and the filters remembered."""
from __future__ import annotations

import re

import pytest

from helpers import catalog_xml, commodity_xml
from panel_helpers import login, make_app, post
from tires_sync import order_points as points, staging, store, user_prefs, users
from tires_sync.settings_store import get_active
from urllib.parse import quote

NEIZV = quote("НЕИЗВЕСТНЫЙ")

T = "2026-10-07T10:00:00"


def tire(code, w, h, d, brand=None, name="IKON"):
    return commodity_xml(NNOMMODIF=code[1:], SMNFCODE=code, **({"SMARKA": brand} if brand else {}), SWIDTH=str(w), SHEIGHT=str(h), SDIAMETR=str(d), NREST="12",
                         SMODIFNAME="%s %d/%d/%d T 94 Test Model" % (name, w, h, d), SMODIFNAME_ORIG="%s %d/%d/%d T 94 Test Model" % (name, w, h, d))


ROWS = (tire("C1", 225, 55, 16), tire("C2", 205, 55, 16), tire("C3", 195, 60, 17), tire("C4", 205, 60, 17), tire("C5", 225, 45, 18),
        tire("C6", 205, 55, 16, brand="Лауфенн", name="LAUFENN"), tire("C7", 195, 65, 15, brand="Неизвестный", name="UNKNOWN"))


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


def setup(tmp_path):
    app = make_app(tmp_path)
    owner = app.test_client()
    tok = login(owner)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*ROWS))
    return app, owner, tok, store.connect(app.config["DB_PATH"])


def codes(html):
    return sorted(set(re.findall(r">(C\d)<", html.split('id="items"')[1])))


def as_user(app, conn, login_, role, point_id=None):
    users.add_user(conn, login_, "длинный-пароль-1", T, role=role, point_id=point_id)
    c = app.test_client()
    with c.session_transaction() as session:
        session.update(user=login_, epoch=store.get_session_epoch(conn, login_), csrf="tok")
    return c


def test_several_widths_profiles_and_radii_through_spaces_give_every_combination(tmp_path):
    app, owner, tok, conn = setup(tmp_path)
    get = lambda q: codes(owner.get("/catalog" + q).get_data(as_text=True))
    assert get("?w=225+205+195") == ["C1", "C2", "C3", "C4", "C5", "C6", "C7"]
    assert get("?w=225+205&h=55+60&d=16+17") == ["C1", "C2", "C4", "C6"]
    assert get("?w=195+205&h=60&d=17") == ["C3", "C4"]


def test_whole_sizes_are_read_as_in_the_client_catalog_and_go_with_the_fields(tmp_path):
    app, owner, tok, conn = setup(tmp_path)
    get = lambda q: codes(owner.get("/catalog" + q).get_data(as_text=True))
    assert get("?size=205/55R16+225/45R18") == ["C2", "C5", "C6"]
    assert get("?size=205/55/16+195/60+r17") == ["C2", "C3", "C6"]
    assert get("?size=205/55R16+225/45R18&w=225") == ["C5"]                  # both: the fields and the sizes
    assert get("?size=чепуха") == ["C1", "C2", "C3", "C4", "C5", "C6", "C7"]  # nothing understood: no filter
    assert 'name="size" value="205/55R16 225/45R18"' in owner.get("/catalog?size=205/55R16+225/45R18").get_data(as_text=True)


def test_several_brands_and_brands_left_out_are_the_view_only(tmp_path):
    app, owner, tok, conn = setup(tmp_path)
    get = lambda q: codes(owner.get("/catalog" + q).get_data(as_text=True))
    assert get("?b=LAUFENN&b=" + NEIZV) == ["C6", "C7"]
    assert get("?xb=IKON+TYRES") == ["C6", "C7"]
    assert get("?b=IKON+TYRES&b=LAUFENN&xb=LAUFENN") == ["C1", "C2", "C3", "C4", "C5"]
    page = owner.get("/catalog?b=LAUFENN&b=" + NEIZV + "&o=stock").get_data(as_text=True)
    assert 'name="b" value="LAUFENN" checked' in page and 'name="xb" value="LAUFENN" aria' in page
    sort = re.search(r'href="(/catalog\?[^"]*o=price[^"]*)"', page).group(1)       # the links of the page keep every brand
    assert sort.count("b=") == 2
    assert staging.summary(conn)["draft_id"] is None                                  # nothing in the draft: the feed is not touched


def test_the_filters_of_sizes_and_brands_are_remembered(tmp_path):
    app, owner, tok, conn = setup(tmp_path)
    here = {"Referer": "http://localhost/catalog"}
    owner.get("/catalog?size=205/55R16&b=LAUFENN&b=IKON+TYRES&xb=" + NEIZV, headers=here)
    assert user_prefs.get(conn, "owner", "f.catalog") == {"size": ["205/55R16"], "b": ["LAUFENN", "IKON TYRES"], "xb": ["НЕИЗВЕСТНЫЙ"]}
    r = owner.get("/catalog", headers={"Referer": "http://localhost/orders"})
    assert r.status_code == 302
    page = owner.get(r.headers["Location"]).get_data(as_text=True)
    assert codes(page) == ["C2", "C6"] and "Фильтр включён: 3" in page


def test_the_owner_takes_a_brand_off_the_feed_from_the_catalog_into_the_draft_without_applying_it(tmp_path):
    app, owner, tok, conn = setup(tmp_path)
    page = owner.get("/catalog").get_data(as_text=True)
    assert "data-feed-brands" in page and 'data-brand-off="IKON TYRES"' in page
    before = get_active(conn)[1].get("excluded_brands") or []
    r = post(owner, "/connections/suppliers/1/excluded", tok, brand="IKON TYRES", next="/catalog?b=IKON+TYRES")
    assert r.status_code == 302 and r.headers["Location"].endswith("/catalog?b=IKON+TYRES")
    assert staging.summary(conn)["changes"]["excluded_brands"] == ["IKON TYRES"]
    assert (get_active(conn)[1].get("excluded_brands") or []) == before                  # not applied
    page = owner.get("/catalog?b=IKON+TYRES").get_data(as_text=True)
    assert "Не применено: 1 правка" in page and "data-settings-bar" in page and ">Применить</button>" in page
    assert "снятие не применено" in page and 'data-brand-on="IKON TYRES"' in page
    pending = owner.get("/settings/pending").get_json()
    assert pending["open"] and "выйдет 5" in pending["line"]                              # «было → стало» in the bar
    post(owner, "/connections/suppliers/1/excluded", tok, remove="IKON TYRES", next="/catalog")      # back: the draft is empty again
    assert staging.summary(conn)["draft_id"] is None


def test_the_brand_change_goes_back_only_to_the_catalog(tmp_path):
    app, owner, tok, conn = setup(tmp_path)
    for asked in ("//evil.example/catalog", "/history", "https://evil.example/catalog"):
        r = post(owner, "/connections/suppliers/1/excluded", tok, brand="LAUFENN", next=asked)
        assert r.headers["Location"].endswith("/settings/exclusions#excluded-1"), asked


def test_only_the_owner_sees_the_brands_of_the_feed_and_a_point_or_a_client_never_sees_the_catalog(tmp_path):
    app, owner, tok, conn = setup(tmp_path)
    partner = as_user(app, conn, "partner1", "partner")
    page = partner.get("/catalog").get_data(as_text=True)
    assert page and "data-feed-brands" not in page and "data-brand-off" not in page and 'name="b"' in page   # the view filters are for all
    assert partner.post("/connections/suppliers/1/excluded", data={"csrf": "tok", "brand": "LAUFENN", "next": "/catalog"}).status_code == 403
    viewer = as_user(app, conn, "viewer1", "viewer")
    assert "data-feed-brands" not in viewer.get("/catalog").get_data(as_text=True)
    pid = points.save_point(conn, {"name": "Юнона", "manager_name": "А", "channel": "panel"})
    assert as_user(app, conn, "point1", "point", point_id=pid).get("/catalog").status_code == 403
    assert as_user(app, conn, "client1", "client").get("/catalog").status_code == 403
    assert staging.summary(conn)["draft_id"] is None
