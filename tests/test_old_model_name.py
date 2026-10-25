"""The former name of a model (the owner 2026-10-24, task 2: «Ikon Autograph Ice 9 — преемник Nokian Hakkapeliitta 9»): kept in the base
by (Avito brand, Avito model), edited on the mappings page, put into the ad text by the variable {old_model} only. No account template has
the variable, so the feeds stay byte for byte what they were until the owner writes it into a template himself."""
from __future__ import annotations

import json
from datetime import date

import pytest

from helpers import make_offer, make_settings
from panel_helpers import login, make_app, post
from tires_sync import connections, store, users
from tires_sync.candidates import Candidate
from tires_sync.cli import main
from tires_sync.description import ALLOWED, DEFAULT_TEMPLATE, PLACEHOLDER_HELP, render_description, validate_template
from tires_sync.feed import render_ad
from tires_sync.mappings import MappingError
from tires_sync.panel import common
from tires_sync.panel.routes_mappings import save_old_model_name
import test_tochki_feed_unchanged as golden_case
from test_tochki_feed_unchanged import GOLDEN, prepare

NOW = "2026-10-25T10:00:00"
REF = {"Ikon Tyres": frozenset({"Test Model", "Autograph Ice 9"}), "Pirelli": frozenset({"Test Model"})}
LINE = "▸ Преемник модели: {old_model}"
TEMPLATE = DEFAULT_TEMPLATE.replace("▸ Модель: {model}", "▸ Модель: {model}\n" + LINE)


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


def cand(model="Test Model"):
    offer = make_offer()
    return Candidate(offer.goods_id, offer, 4810, "Ikon Tyres", model)


# --- the table -------------------------------------------------------------------------------------------------------------------------

def test_the_table_is_added_to_an_old_base(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    conn.execute("DROP TABLE old_model_name")
    conn.commit()
    conn.close()
    conn = store.connect(tmp_path / "t.db")
    assert store.load_old_model_names(conn) == {}
    conn.close()


def test_saving_changing_and_clearing_a_former_name_with_the_journal(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    text = save_old_model_name(conn, REF, "ikon tyres", "autograph  ice 9", "  Nokian   Hakkapeliitta 9 ", NOW, "owner")
    assert "сохранено" in text
    assert store.load_old_model_names(conn) == {("Ikon Tyres", "Autograph Ice 9"): "Nokian Hakkapeliitta 9"}      # the reference spelling
    assert "Без изменений" in save_old_model_name(conn, REF, "Ikon Tyres", "Autograph Ice 9", "Nokian Hakkapeliitta 9", NOW, "owner")
    save_old_model_name(conn, REF, "Ikon Tyres", "Autograph Ice 9", "Nokian Hakka 9", NOW, "owner")
    assert store.load_old_model_names(conn)[("Ikon Tyres", "Autograph Ice 9")] == "Nokian Hakka 9"
    assert "убрано" in save_old_model_name(conn, REF, "Ikon Tyres", "Autograph Ice 9", "", NOW, "owner")
    assert store.load_old_model_names(conn) == {}
    journal = [json.loads(r[0]) for r in conn.execute("SELECT detail FROM audit WHERE action = 'old_model_name' ORDER BY id")]
    assert [(j["before"], j["after"]) for j in journal] == [("", "Nokian Hakkapeliitta 9"), ("Nokian Hakkapeliitta 9", "Nokian Hakka 9"),
                                                             ("Nokian Hakka 9", "")]
    conn.close()


@pytest.mark.parametrize("brand,model,typed,error", [
    ("Nope", "Test Model", "X", "бренд"),
    ("Ikon Tyres", "Ice 99", "X", "нет у бренда"),
    ("Ikon Tyres", "Test Model", "Nokian\x0bHakka", "управляющие"),
    ("Ikon Tyres", "Test Model", "N" * 101, "длиннее 100"),
    ("Ikon Tyres", "Test Model", "Nokian\u202eHakka", "управляющие"),
    ("Ikon Tyres", "Test Model", "Nokian\u200bHakka", "управляющие"),
])
def test_a_wrong_brand_model_or_text_is_refused(tmp_path, brand, model, typed, error):
    conn = store.connect(tmp_path / "t.db")
    with pytest.raises(MappingError, match=error):
        save_old_model_name(conn, REF, brand, model, typed, NOW, "owner")
    assert store.load_old_model_names(conn) == {}
    conn.close()


def test_a_record_whose_model_left_the_reference_can_still_be_cleared(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.set_old_model_name(conn, "Ikon Tyres", "Gone Model", "Nokian Gone", NOW, "o")
    with pytest.raises(MappingError):
        save_old_model_name(conn, REF, "Ikon Tyres", "Gone Model", "Nokian Other", NOW, "owner")
    assert "убрано" in save_old_model_name(conn, REF, "Ikon Tyres", "Gone Model", " ", NOW, "owner")
    assert store.load_old_model_names(conn) == {}
    conn.close()


# --- the mappings page -----------------------------------------------------------------------------------------------------------------

def test_the_mappings_page_adds_and_shows_a_former_name(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    page = client.get("/mappings").get_data(as_text=True)
    assert 'id="old-names"' in page and 'action="/mappings/old-name"' in page and 'list="old-brands"' in page and '<option value="Ikon Tyres">' in page
    r = post(client, "/mappings/old-name", tok, brand="Ikon Tyres", model="Test Model", old_name="Nokian Test 1")
    assert r.status_code == 302 and r.headers["Location"].endswith("/mappings#old-names")
    page = client.get("/mappings").get_data(as_text=True)
    assert 'value="Nokian Test 1"' in page and "сохранено" in page
    r = post(client, "/mappings/old-name", tok, brand="Ikon Tyres", model="Nope", old_name="X")
    assert "нет у бренда" in client.get(r.headers["Location"]).get_data(as_text=True)
    assert "Nokian Test 1" not in client.get("/catalog").get_data(as_text=True)          # the owner: not shown in the catalog


def test_the_form_needs_the_csrf_token(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    r = client.post("/mappings/old-name", data={"brand": "Ikon Tyres", "model": "Test Model", "old_name": "X"})
    assert r.status_code in (400, 403)
    conn = store.connect(app.config["DB_PATH"])
    assert store.load_old_model_names(conn) == {}
    conn.close()


def test_every_role_may_save_a_former_name_exactly_when_it_may_confirm_a_model(tmp_path):
    from test_buttons_by_rights import ROLES, allowed_to_post
    app = make_app(tmp_path)
    for role in ROLES:
        assert allowed_to_post(app, role, "/mappings/old-name") == allowed_to_post(app, role, "/mappings/model"), role
    assert "save_old_model_name" in common.SECTION_OF and common.SECTION_OF["save_old_model_name"] == common.SECTION_OF["confirm_model"]


def test_a_viewer_is_refused_and_sees_no_button(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    users.add_user(conn, "viewer1", "длинный-пароль-1", NOW, role="viewer")
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user="viewer1", epoch=store.get_session_epoch(conn, "viewer1"), csrf="tok")
    page = client.get("/mappings").get_data(as_text=True)
    assert 'action="/mappings/old-name"' not in page
    r = client.post("/mappings/old-name", data={"csrf": "tok", "brand": "Ikon Tyres", "model": "Test Model", "old_name": "X"})
    assert r.status_code == 403
    assert store.load_old_model_names(conn) == {}
    conn.close()


# --- the description -------------------------------------------------------------------------------------------------------------------

def test_the_variable_is_known_and_explained():
    assert "old_model" in ALLOWED and "old_model" in PLACEHOLDER_HELP and "не выводится" in PLACEHOLDER_HELP["old_model"]
    assert validate_template(TEMPLATE) == []
    assert "{old_model}" not in DEFAULT_TEMPLATE                       # the standard text is not changed: the owner adds it himself


def test_the_line_shows_with_a_former_name_and_is_dropped_without_it():
    names = {("Ikon Tyres", "Test Model"): "Nokian Hakkapeliitta 9"}
    assert "▸ Преемник модели: Nokian Hakkapeliitta 9" in render_description(TEMPLATE, cand(), 4810, names)
    assert "Преемник" not in render_description(TEMPLATE, cand("Autograph Ice 9"), 4810, names)
    assert "Преемник" not in render_description(TEMPLATE, cand(), 4810)
    assert render_description(DEFAULT_TEMPLATE, cand(), 4810, names) == render_description(DEFAULT_TEMPLATE, cand(), 4810)


def test_the_ad_and_its_4_pack_carry_the_former_name():
    names = {("Ikon Tyres", "Test Model"): "Nokian Hakkapeliitta 9"}
    settings = make_settings()
    for pack in (1, 4):
        ad = render_ad(cand(), 4810, "7000001", settings, date(2026, 10, 25), template=TEMPLATE, pack=pack, old_models=names)
        assert "Преемник модели: Nokian Hakkapeliitta 9" in ad.findtext("Description"), pack


def test_the_description_preview_shows_the_former_name(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    account = connections.create_account(conn, "Основной", NOW, "o")
    store.set_old_model_name(conn, "Ikon Tyres", "Test Model", "Nokian Test 1", NOW, "o")
    conn.close()
    page = post(client, "/accounts/%d/description" % account, tok, action="preview", template=TEMPLATE).get_data(as_text=True)
    assert "Преемник модели: Nokian Test 1" in page
    assert "old_model" in client.get("/accounts/%d/description" % account).get_data(as_text=True)      # the list of variables


# --- the feeds -------------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def golden_clock(monkeypatch):
    """The clock of the golden file (as in test_tochki_feed_unchanged)."""
    from datetime import datetime
    from tires_sync import cli
    monkeypatch.setattr(cli, "date", golden_case.GoldenDay)
    monkeypatch.setattr(cli, "moscow_now", lambda: datetime(2026, 10, 21, 3, 0, 0))


def _feeds(tmp_path, names, template=None) -> dict:
    args = prepare(tmp_path)
    conn = store.connect(tmp_path / "t.db")
    for (brand, model), text in names.items():
        store.set_old_model_name(conn, brand, model, text, NOW, "o")
    if template is not None:
        connections.set_description_template(conn, 1, template, NOW, "o")
    conn.close()
    assert main(["run-cycle"] + args) == 0
    conn = store.connect(tmp_path / "t.db")
    out = {}
    for account in connections.list_accounts(conn):
        token = account["feed_token"]
        out[account["name"]] = (tmp_path / "feed" / token / "next" / "avito_feed.xml").read_text(encoding="utf-8").replace(token, "TOKEN")
    conn.close()
    return out


def test_with_former_names_saved_the_feeds_stay_byte_for_byte_with_todays_templates(tmp_path, golden_clock):
    built = _feeds(tmp_path, {("Ikon Tyres", "Test Model"): "Nokian Hakkapeliitta 9"})
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert set(built) == set(golden)
    for key in golden:
        assert built[key] == golden[key], key
    assert all("Hakkapeliitta" not in text for text in built.values())


def test_a_template_with_the_variable_puts_the_former_name_into_that_account_only(tmp_path, golden_clock):
    built = _feeds(tmp_path, {("Ikon Tyres", "Test Model"): "Nokian Hakkapeliitta 9"}, template=TEMPLATE)
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert built["Основной"].count("Преемник модели: Nokian Hakkapeliitta 9") == built["Основной"].count("<Ad>") > 0
    assert built["Партнёр-2"] == golden["Партнёр-2"]
    (tmp_path / "empty").mkdir()
    empty = _feeds(tmp_path / "empty", {}, template=TEMPLATE)
    assert "Преемник" not in empty["Основной"]                           # no record: the line is dropped
