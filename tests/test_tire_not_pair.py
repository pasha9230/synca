"""«Это не пара» (tire_match.not_a_pair): the owner says two positions of different suppliers are not one tire; they are never merged, even
with both in the unload, and the memory of pairs does not use them either. With the merge off the feeds are the golden ones byte for byte."""
from __future__ import annotations

import json
import re

import pytest

import test_tire_match_feed_unchanged as golden
from panel_helpers import post
from test_tire_match import mim, mim_offers, tochki_offers, tochki_row
from test_tire_match_feed_unchanged import UNLOAD_WITHOUT_WEBMIM, memory_rows, prepare, publish_then_lose_webmim
from test_tire_pair_forget import feeds, page_app, published_pair, signed_in
from tires_sync import connections, store, tire_match, users
from tires_sync.cli import main

AT = "2026-10-25T09:00:00"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    from datetime import datetime
    from tires_sync import cli
    monkeypatch.setattr(cli, "date", golden.GoldenDay)
    monkeypatch.setattr(cli, "moscow_now", lambda: datetime(2026, 10, 25, 3, 0, 0))


def counts(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    out = {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in ("tire_pair", "tire_not_pair", "tire_match_switch", "audit")}
    conn.close()
    return out


def say_not_a_pair(tmp_path, a="100", b="s2-TCH1", undo=False):
    conn = store.connect(tmp_path / "t.db")
    done = tire_match.not_a_pair(conn, a, b, "owner", AT, undo=undo)
    conn.close()
    return done


def cycle(tmp_path, args) -> dict:
    """One draft cycle; the feeds of the accounts with their tokens hidden (as the golden ones are kept)."""
    assert main(["run-cycle"] + args) == 0
    conn = store.connect(tmp_path / "t.db")
    out = {}
    for account in connections.list_accounts(conn):
        token = account["feed_token"]
        out[account["name"]] = (tmp_path / "feed" / token / "next" / "avito_feed.xml").read_text(encoding="utf-8").replace(token, "TOKEN")
    conn.close()
    return out


def three_suppliers():
    """One tire at WebMIM row 1 (100), another WebMIM row (s3-200) and 4tochki (s2-TCH1)."""
    return {**mim_offers(mim()), **mim_offers(mim(number="200"), supplier=3), **tochki_offers(tochki_row())}


# --- the finding of pairs and the memory -----------------------------------------------------------------------------------------

def test_the_pair_said_not_to_be_one_is_never_related():
    offers = {**mim_offers(mim()), **tochki_offers(tochki_row())}
    assert tire_match.pairs(offers, tochki_supplier=2).groups == [("100", "s2-TCH1")]
    result = tire_match.pairs(offers, tochki_supplier=2, not_pairs={("100", "s2-TCH1")})
    assert result.groups == [] and result.refused == [] and result.ambiguous == []
    assert set(result.identities) == {"100", "s2-TCH1"}                   # both still known: only not related


def test_a_group_the_two_still_meet_in_through_a_third_position_is_not_merged():
    offers = three_suppliers()
    assert tire_match.pairs(offers, tochki_supplier=2).groups == [("100", "s2-TCH1", "s3-200")]
    result = tire_match.pairs(offers, tochki_supplier=2, not_pairs={("100", "s2-TCH1")})
    assert result.groups == [] and result.refused == [("100", "s2-TCH1", "s3-200")]
    assert tire_match.found_pairs(result) == []                          # nothing of it is remembered
    both = tire_match.pairs(offers, tochki_supplier=2, not_pairs={("100", "s2-TCH1"), ("s2-TCH1", "s3-200")})
    assert both.groups == [("100", "s3-200")] and both.refused == []      # 4tochki apart from both: the WebMIM rows still pair


def stored(here, gone, ident_of):
    a, b = sorted((here, gone))
    return (a, b, ident_of[a], ident_of[b], AT)


def test_the_memory_does_not_join_a_gone_side_to_a_group_with_a_position_apart_from_it():
    offers_all = three_suppliers()
    idents = {g: tire_match.identity_text(i) for g, i in tire_match.pairs(offers_all, tochki_supplier=2).identities.items()}
    here = {k: v for k, v in offers_all.items() if k != "s2-TCH1"}         # 4tochki has left the unload, its pair with s3-200 remembered
    memory = [stored("s3-200", "s2-TCH1", idents)]
    plain = tire_match.pairs(here, tochki_supplier=2)
    tire_match.remember(plain, memory, here)
    assert plain.groups == [("100", "s2-TCH1", "s3-200")]                 # without the mark the gone side joins the group of 100 too
    apart = {("100", "s2-TCH1")}
    marked = tire_match.pairs(here, tochki_supplier=2, not_pairs=apart)
    tire_match.remember(marked, memory, here, not_pairs=apart)
    assert marked.groups == [("100", "s3-200")] and marked.remembered == []


def test_the_memory_does_not_join_a_side_here_to_the_group_of_a_gone_side_apart_from_it():
    offers_all = three_suppliers()
    idents = {g: tire_match.identity_text(i) for g, i in tire_match.pairs(offers_all, tochki_supplier=2).identities.items()}
    here = {k: v for k, v in offers_all.items() if k != "s2-TCH1"}
    memory = sorted([stored("100", "s2-TCH1", idents), stored("s3-200", "s2-TCH1", idents)])
    apart = {("100", "s3-200")}                                           # the two WebMIM rows are not one tire: they meet only via the memory
    marked = tire_match.pairs(here, tochki_supplier=2, not_pairs=apart)
    assert marked.groups == []
    tire_match.remember(marked, memory, here, not_pairs=apart)
    assert marked.groups == [("100", "s2-TCH1")]                          # s3-200 does not join 100 through the gone 4tochki side
    unmarked = tire_match.pairs(here, tochki_supplier=2)
    tire_match.remember(unmarked, memory, here)
    assert unmarked.groups == [("100", "s2-TCH1", "s3-200")]              # control: without the mark the gone side joins both


def test_a_refused_group_is_unclear_for_the_memory():
    offers_all = {**three_suppliers(), **mim_offers(mim(number="300"), supplier=4)}
    idents = {g: tire_match.identity_text(i) for g, i in tire_match.pairs(offers_all, tochki_supplier=2).identities.items()}
    here = {k: v for k, v in offers_all.items() if k != "s4-300"}
    apart = {("100", "s2-TCH1")}
    marked = tire_match.pairs(here, tochki_supplier=2, not_pairs=apart)
    assert marked.refused == [("100", "s2-TCH1", "s3-200")]
    tire_match.remember(marked, [stored("s3-200", "s4-300", idents)], here, not_pairs=apart)
    assert marked.groups == [] and marked.remembered == []


def test_the_memory_does_not_use_a_remembered_pair_said_not_to_be_one(tmp_path):
    published_pair(tmp_path)
    assert say_not_a_pair(tmp_path, "s2-TCH1", "100") is True             # the order of the codes does not matter
    conn = store.connect(tmp_path / "t.db")
    assert tire_match.usable_pairs(conn) == [] and store.load_tire_pairs(conn)[0][:2] == ("100", "s2-TCH1")
    conn.close()


# --- the feeds --------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("switch", [None, False])
def test_with_the_merge_off_a_mark_leaves_the_feeds_as_the_golden_ones(tmp_path, switch):
    args = prepare(tmp_path, switch)
    assert say_not_a_pair(tmp_path) is True
    built = cycle(tmp_path, args)
    assert built == golden.trial_golden()                                 # the golden feeds as the trial builds them (account 1 only)
    assert memory_rows(tmp_path) == (0, 0)


def test_with_the_merge_on_the_pair_said_not_to_be_one_is_two_ads_as_with_the_merge_off(tmp_path):
    args = prepare(tmp_path, True)
    assert say_not_a_pair(tmp_path) is True
    built = cycle(tmp_path, args)
    assert built == golden.trial_golden(golden.TRIAL_BOTH)                # byte for byte the feeds of the merge off (both accounts in the trial)
    assert memory_rows(tmp_path) == (0, 0)                                # not remembered either
    assert say_not_a_pair(tmp_path, undo=True) is True
    built = cycle(tmp_path, args)
    assert "<Id>100</Id>" in built["Основной"] and "<Id>s2-TCH1</Id>" not in built["Основной"]      # taken back: merged again
    assert memory_rows(tmp_path) == (1, 0)


def test_a_published_pair_said_not_to_be_one_is_not_carried_by_the_memory(tmp_path):
    args = published_pair(tmp_path)
    assert say_not_a_pair(tmp_path) is True
    (tmp_path / "unload.xml").write_bytes(UNLOAD_WITHOUT_WEBMIM)
    assert main(["run-cycle"] + args) == 0
    built = feeds(tmp_path)
    # as with no memory at all: the 4tochki side used to come out under a new Id; the strict entry of the trial keeps it out (plan §4б)
    assert "s2-TCH1" not in built["Основной"] and "<Model>Nordman 7</Model>" not in built["Основной"]
    assert counts(tmp_path)["tire_match_switch"] == 0


def test_no_mark_once_an_ad_has_gone_over_between_the_two(tmp_path):
    publish_then_lose_webmim(tmp_path)                                    # the ad 100 went over to s2-TCH1 (noted)
    before = counts(tmp_path)
    assert before["tire_match_switch"] == 2
    conn = store.connect(tmp_path / "t.db")
    with pytest.raises(ValueError, match="уже переходило"):
        tire_match.not_a_pair(conn, "100", "s2-TCH1", "owner", AT)
    conn.close()
    assert counts(tmp_path) == before


def test_no_mark_once_an_ad_has_gone_over_to_or_from_either_of_them(tmp_path):
    prepare(tmp_path)
    conn = store.connect(tmp_path / "t.db")
    store.add_match_switch(conn, 1, "100", "100", "s3-200", AT)          # a group of three: the ad 100 went over to another WebMIM row
    before = counts(tmp_path)
    for a, b in (("100", "s2-TCH1"), ("s3-200", "s2-TCH1")):              # either mark takes the whole group apart
        with pytest.raises(ValueError, match="уже переходило от 100 к s3-200"):
            tire_match.not_a_pair(conn, a, b, "owner", AT)
    assert counts(tmp_path) == before                                     # refused: nothing written
    assert tire_match.not_a_pair(conn, "101", "s2-TCH1", "owner", AT) is True       # positions with no change: the mark is free
    assert store.not_tire_pairs(conn) == {("101", "s2-TCH1"): (AT, "owner")}
    conn.close()


def test_no_mark_once_an_ad_has_gone_over_between_two_others_of_the_group(tmp_path):
    """A group of four (remembered pairs link them): the ad went over s3-Y → s4-Z; a mark of 100 | s2-X would take the whole group apart."""
    prepare(tmp_path)
    conn = store.connect(tmp_path / "t.db")
    store.add_tire_pairs(conn, [("100", "s3-Y", "i", "i"), ("s2-X", "s3-Y", "i", "i"), ("s3-Y", "s4-Z", "i", "i")], AT)
    store.add_match_switch(conn, 2, "100_a2", "s3-Y", "s4-Z", AT)
    audit = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    with pytest.raises(ValueError, match="уже переходило от s3-Y к s4-Z"):
        tire_match.not_a_pair(conn, "100", "s2-X", "owner", AT)
    assert conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == audit and store.not_tire_pairs(conn) == {}
    assert tire_match.not_a_pair(conn, "101", "s2-TCH1", "owner", AT) is True       # not linked to that group: free
    conn.close()


# --- the mark itself ---------------------------------------------------------------------------------------------------------------

def test_the_mark_is_only_added_and_takes_two_positions_of_different_suppliers(tmp_path):
    prepare(tmp_path)
    conn = store.connect(tmp_path / "t.db")
    for a, b in (("100", "100"), ("100", "101"), ("s2-TCH1", "s2-TCH9"), ("", "s2-TCH1")):
        with pytest.raises(ValueError):
            tire_match.not_a_pair(conn, a, b, "owner", AT)
    assert tire_match.not_a_pair(conn, "100", "s2-TCH1", "owner", AT, undo=True) is False      # no mark to take back
    assert tire_match.not_a_pair(conn, "100", "s2-TCH1", "owner", AT) is True
    assert tire_match.not_a_pair(conn, " s2-TCH1 ", "100", "owner", AT) is False               # so already
    assert store.not_tire_pairs(conn) == {("100", "s2-TCH1"): (AT, "owner")}
    assert tire_match.not_a_pair(conn, "100", "s2-TCH1", "owner", AT, undo=True) is True
    assert store.not_tire_pairs(conn) == {}
    rows = conn.execute("SELECT code_a, code_b, apart FROM tire_not_pair ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("100", "s2-TCH1", 1), ("100", "s2-TCH1", 0)]
    actions = [(r[0], json.loads(r[1])) for r in conn.execute("SELECT action, detail FROM audit WHERE action LIKE 'match_not_pair%' ORDER BY id")]
    assert actions == [("match_not_pair", {"code_a": "100", "code_b": "s2-TCH1"}), ("match_not_pair_undo", {"code_a": "100", "code_b": "s2-TCH1"})]
    conn.close()


def test_the_command_marks_takes_back_refuses_and_match_report_shows_it(tmp_path, capsys):
    args = published_pair(tmp_path)
    db = args[1]
    capsys.readouterr()
    assert main(["match-not-pair", "--db", db, "100", "s2-TCH1"]) == 0
    assert "запрет записан" in capsys.readouterr().out
    assert main(["match-not-pair", "--db", db, "100", "s2-TCH1"]) == 0
    assert "запрет уже есть" in capsys.readouterr().out
    before = counts(tmp_path)
    assert main(["match-report", "--db", db, "--unload", args[3], "--reference", args[5]]) == 0
    out = capsys.readouterr().out
    assert counts(tmp_path) == before                                     # the report writes nothing
    assert "пар (групп одной шины у разных поставщиков): 0" in out
    assert "сохранённых пар (запомнены циклом при включённой склейке): 1, используется 0 из 1" in out
    assert re.search(r"  100 \| s2-TCH1, найдена \S+ \(запрет: это не пара\)", out)
    assert "запретов «это не пара» (владелец: не склеивать, даже если обе в выгрузке): 1" in out
    assert re.search(r"  100 \| s2-TCH1, \S+ UTC, владелец", out)
    assert "групп, не склеенных из-за запрета внутри: 0" in out
    assert main(["match-not-pair", "--db", db, "100", "s2-TCH1", "--undo"]) == 0
    assert "запрет снят" in capsys.readouterr().out
    assert main(["match-not-pair", "--db", db, "100", "s2-TCH1", "--undo"]) == 0
    assert "запрета и так нет" in capsys.readouterr().out
    assert main(["match-not-pair", "--db", db, "100", "101"]) == 2        # one supplier
    assert counts(tmp_path)["tire_not_pair"] == 2


# --- the page ----------------------------------------------------------------------------------------------------------------------

def test_only_the_owner_says_not_a_pair_the_others_see(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    prepare(tmp_path)
    app = page_app(tmp_path)
    for role in ("partner", "chats", "viewer"):
        client = signed_in(app, role)
        page = client.get("/pairs")
        assert page.status_code == 200, role
        assert 'action="/pairs/not-pair"' not in page.get_data(as_text=True), role
        assert post(client, "/pairs/not-pair", "tok", a="100", b="s2-TCH1").status_code == 403, role
    assert counts(tmp_path)["tire_not_pair"] == 0
    owner = signed_in(app, "owner")
    before = counts(tmp_path)
    html = owner.get("/pairs").get_data(as_text=True)
    assert counts(tmp_path) == before                                     # the page writes nothing
    assert 'action="/pairs/not-pair"' in html and "Это не пара" in html and "в фидах ничего не изменится" in html
    assert "одна модель Авито" not in html                                # the sides are mapped to two Avito models here
    assert owner.post("/pairs/not-pair", data={"a": "100", "b": "s2-TCH1"}).status_code in (400, 403)     # no CSRF token
    assert counts(tmp_path)["tire_not_pair"] == 0
    r = post(owner, "/pairs/not-pair", "tok", a="100", b="s2-TCH1")
    assert r.status_code == 302 and r.headers["Location"].endswith("/pairs")
    html = owner.get("/pairs").get_data(as_text=True)
    assert "Не пары (запрет владельца)" in html and "Это пара" in html and "25.10.2026 12:00:00" in html     # 09:00 UTC of the panel's clock
    assert re.search(r"Пары сейчас <span class=\"badge badge--neutral\">0</span>", html)
    journal = owner.get("/history").get_data(as_text=True)
    assert "Пары поставщиков: «это не пара» 100 | s2-TCH1 (не склеиваются)" in journal
    r = post(owner, "/pairs/not-pair", "tok", a="100", b="s2-TCH1", undo="1")
    assert r.status_code == 302
    assert "Запрет «это не пара» снят: 100 | s2-TCH1" in owner.get("/history").get_data(as_text=True)
    r = post(owner, "/pairs/not-pair", "tok", a="100", b="101")
    assert r.status_code == 302 and counts(tmp_path)["tire_not_pair"] == 2              # refused: a message, nothing written


def test_the_page_marks_a_remembered_pair_said_not_to_be_one(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    published_pair(tmp_path)
    say_not_a_pair(tmp_path)
    html = signed_in(page_app(tmp_path), "owner").get("/pairs").get_data(as_text=True)
    assert "запрет: это не пара" in html and "используется 0 из 1" in html and "держит объявление" not in html
    assert "память эту пару и так не использует: забывание скажется только после снятия запрета" in html


def test_the_point_manager_cannot_say_not_a_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    prepare(tmp_path)
    app = page_app(tmp_path)
    conn = store.connect(tmp_path / "t.db")
    from tires_sync import order_points
    pid = order_points.save_point(conn, {"name": "Гамма", "manager_name": "А", "channel": "vk"})
    users.add_user(conn, "point1", "длинный-пароль-1", AT, role="point", point_id=pid)
    point = app.test_client()
    with point.session_transaction() as session:
        session.update(user="point1", epoch=store.get_session_epoch(conn, "point1"), csrf="tok")
    conn.close()
    assert post(point, "/pairs/not-pair", "tok", a="100", b="s2-TCH1").status_code == 403
    assert counts(tmp_path)["tire_not_pair"] == 0


def test_the_page_warns_when_both_sides_have_one_avito_model(tmp_path, monkeypatch):
    """One Avito model: the old rule «one tire, one ad» (same_tire) keeps them one ad whatever the mark says; the button says so."""
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    prepare(tmp_path)
    conn = store.connect(tmp_path / "t.db")
    store.upsert_mapping(conn, "Ikon Tyres", "Character Ice 7 (Nordman 7)", "Character Ice 7")
    conn.commit()
    conn.close()
    html = signed_in(page_app(tmp_path), "owner").get("/pairs").get_data(as_text=True)
    assert "У обеих одна модель Авито «Character Ice 7»" in html
