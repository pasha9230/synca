"""The cross-supplier merge (tire_match) is prepared switched off: with the switch off the feeds stay byte for byte what they were.

The golden file was taken on `origin/run/2026-10-25-match` (`76e4bb2`) before tire_match existed (set REGENERATE_MATCH_GOLDEN=1 to retake it,
only on a known-good tree). The scenario holds what the merge would change if it leaked: 4tochki is in the feed, it has the owner's example
tire that WebMIM has too, and the two sides are mapped to different Avito models (so the old same_tire does not merge them)."""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

import pytest

from helpers import catalog_xml, commodity_xml, settings_dict
from tires_sync import cli, connections, store
from tires_sync.cli import main
from tires_sync.settings_store import init_settings

NOW = "2026-10-14T10:00:00"
GOLDEN = Path(__file__).with_name("test_tire_match_feed_unchanged_golden.json")
REFERENCE = ('<Shiny>\n<make name="Ikon Tyres">\n<model name="Character Ice 7"/>\n<model name="Nordman 7"/>\n<model name="Test Model"/>\n'
             '</make>\n</Shiny>')

UNLOAD = catalog_xml(
    commodity_xml(NNOMMODIF="100", TERRITORY_RN="1001", NPRICE_DELAYED="4000", NREST="12", SWIDTH="155", SHEIGHT="80", SDIAMETR="13",
                  SLOAD="79", SSPEED="T", SMNFCODE="CI7", SMODEL="Ikon Character Ice 7",
                  SMODIFNAME="Ikon Tyres 155/80/13 T 79 Ikon Character Ice 7 Ш.", SMODIFNAME_ORIG="Ikon Tyres 155/80/13 T 79 Ikon Character Ice 7 Ш."),
    commodity_xml(NNOMMODIF="101", TERRITORY_RN="1001", NPRICE_DELAYED="6000", NREST="5", SWIDTH="215", SMNFCODE="TS2",
                  SMODIFNAME="IKON 215/55/16 T 95 Test Model", SMODIFNAME_ORIG="Ikon 215/55/16 T 95 Test Model"))


def tochki_item(code, name, model, width, height, diameter, load, rest, price, thorn=False):
    return {"code": code, "name": name, "marka": "IKON", "model": model, "season": "w", "thorn": thorn, "type": "car", "quality": 0,
            "img_big_my": "", "img_big_pish": "https://img.4tochki.test/%s.jpg" % code, "price": price, "rest": rest, "width": str(width),
            "height": str(height), "diameter": str(diameter), "load_index": str(load), "speed_index": "T", "puncture": "",
            "strengthening": "false", "omolog": "", "card": True}


class GoldenDay(date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 25)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(cli, "date", GoldenDay)
    monkeypatch.setattr(cli, "moscow_now", lambda: datetime(2026, 10, 25, 3, 0, 0))


def snapshot():
    return {"version": 1, "fetched_at": datetime.utcnow().isoformat(timespec="seconds"), "warehouse_id": 7, "items": [
        tochki_item("TCH1", "IKON 155/80R13 79T Character Ice 7 (Nordman 7) TL (шип.)", "Character Ice 7 (Nordman 7)", 155, 80, 13, 79, 41,
                    3500.0, thorn=True),                     # the owner's example: the same tire as WebMIM 100
        tochki_item("TCH9", "IKON 225/55R16 98T Test Model", "Test Model", 225, 55, 16, 98, 10, 7000.0),    # only 4tochki has it
    ]}


TRIAL_1 = {"accounts": [1], "mode": "no_twin"}           # the 4tochki trial in account 1 without the merge (plan tochki-feed §9, stage 2)
TRIAL_BOTH = {"accounts": [1, 2], "mode": "merge"}       # both accounts and the merge: the scenario of the merge itself, as before the trial


def without_ads(text: str, ids) -> str:
    """The feed text without the ads of these Ids (the rest byte for byte)."""
    for ad_id in ids:
        text = re.sub(r"<Ad><Id>%s</Id>.*?</Ad>" % re.escape(ad_id), "", text, flags=re.S)
    return text


def trial_golden(trial=None) -> dict:
    """The golden feeds (taken when 4tochki went into every account with no entry rule) as the trial builds them with the merge off: in an
    account of the trial the 4tochki side of the pair is out (WebMIM 100 has that tire: strict entry, plan §4б; it used to be a second ad,
    Nordman 7) and the one 4tochki ad left changes twice, both meant: the WebMIM picture of the same model (Test Model, WebMIM 101) instead
    of the supplier's (owner 2026-10-27), and the next pickup point of the rotation (one ad less before it); out of the trial every 4tochki
    ad is out. Everything else byte for byte."""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    accounts = (trial or TRIAL_1)["accounts"]
    out = {}
    for number, (name, suffix) in enumerate((("Основной", ""), ("Партнёр-2", "_a2")), start=1):
        if number not in accounts:
            out[name] = without_ads(golden[name], ["s2-TCH1" + suffix, "s2-TCH9" + suffix])
            continue
        text = without_ads(golden[name], ["s2-TCH1" + suffix])
        ad = re.search(r"<Ad><Id>s2-TCH9%s</Id>.*?</Ad>" % suffix, text, re.S).group(0)
        now = ad.replace('url="https://img.4tochki.test/TCH9.jpg"', 'url="https://example.test/1.jpg"').replace(
            "<SellerAddressID>P1</SellerAddressID>", "<SellerAddressID>P3</SellerAddressID>")
        assert now != ad
        out[name] = text.replace(ad, now)
    return out


def prepare(tmp_path, match_switch=None, trial=None):
    conn = store.connect(tmp_path / "t.db")
    priced = {"enabled": [["IKON TYRES", "winter"], ["S2:IKON", "winter"]],
              "pricing": {"IKON TYRES|winter": {"pct_small": -3.0, "pct_large": -3.0}, "S2:IKON|winter": {"pct_small": 5.0, "pct_large": 5.0}}}
    init_settings(conn, settings_dict(territory_rn="1001", min_diameter=13, **priced), NOW)
    connections.create_account(conn, "Основной", NOW, "owner")
    connections.create_account(conn, "Партнёр-2", NOW, "owner")
    two = connections.create_supplier(conn, "4Точки", NOW, "owner")
    connections.set_config(conn, "tochki", 0, dict({"login": "api-user", "supplier_id": two, "feed": True},
                                                       **(trial or (TRIAL_BOTH if match_switch else TRIAL_1))), NOW, "owner")
    if match_switch is not None:
        connections.set_config(conn, "tire_match", 0, {"feed": match_switch}, NOW, "owner")
    store.upsert_brand_map(conn, "IKON", "Ikon Tyres")
    store.upsert_mapping(conn, "Ikon Tyres", "Ikon Character Ice 7", "Character Ice 7")
    store.upsert_mapping(conn, "Ikon Tyres", "Character Ice 7 (Nordman 7)", "Nordman 7")       # the other side: another Avito model
    conn.commit()
    conn.close()
    (tmp_path / "unload.xml").write_bytes(UNLOAD)
    (tmp_path / "tochki-snapshot.json").write_text(json.dumps(snapshot(), ensure_ascii=False), encoding="utf-8")
    (tmp_path / "reference.txt").write_text(REFERENCE, encoding="utf-8")
    return ["--db", str(tmp_path / "t.db"), "--unload", str(tmp_path / "unload.xml"),
            "--reference", str(tmp_path / "reference.txt"), "--feed-root", str(tmp_path / "feed")]


def build(tmp_path, match_switch=None, trial=None) -> dict:
    assert main(["run-cycle"] + prepare(tmp_path, match_switch, trial)) == 0
    conn = store.connect(tmp_path / "t.db")
    out = {}
    for account in connections.list_accounts(conn):
        token = account["feed_token"]
        out[account["name"]] = (tmp_path / "feed" / token / "next" / "avito_feed.xml").read_text(encoding="utf-8").replace(token, "TOKEN")
    conn.close()
    return out


@pytest.mark.parametrize("switch", [None, False])
def test_feeds_are_unchanged_with_the_match_switch_off(tmp_path, switch):
    """The golden feeds less what the trial keeps out (trial_golden): account 1 in the trial, account 2 out of it."""
    built = build(tmp_path, switch)
    assert built == trial_golden()
    assert "<Id>s2-TCH9</Id>" in built["Основной"] and "<Model>Nordman 7</Model>" not in built["Основной"]
    assert memory_rows(tmp_path) == (0, 0)          # off: nothing of the merge is written either


def memory_rows(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    rows = (conn.execute("SELECT COUNT(*) FROM tire_pair").fetchone()[0], conn.execute("SELECT COUNT(*) FROM tire_match_switch").fetchone()[0])
    conn.close()
    return rows


UNLOAD_WITHOUT_WEBMIM = catalog_xml(
    commodity_xml(NNOMMODIF="101", TERRITORY_RN="1001", NPRICE_DELAYED="6000", NREST="5", SWIDTH="215", SMNFCODE="TS2",
                  SMODIFNAME="IKON 215/55/16 T 95 Test Model", SMODIFNAME_ORIG="Ikon 215/55/16 T 95 Test Model"))


def publish_then_lose_webmim(tmp_path, forget=False) -> dict:
    """Cycle 1 with the switch on (the pair is one WebMIM ad, published), then the WebMIM row leaves the unload and cycle 2 runs."""
    args = prepare(tmp_path, True, TRIAL_BOTH)
    assert main(["run-cycle"] + args) == 0
    conn = store.connect(tmp_path / "t.db")
    for account in connections.list_accounts(conn):
        store.commit_pending(conn, account["id"], (tmp_path / "feed" / account["feed_token"] / "next" / "avito_feed.xml").read_bytes())
    if forget:                                                             # a copy of the test base: the memory taken away
        conn.execute("DELETE FROM tire_pair")
        conn.commit()
    conn.close()
    (tmp_path / "unload.xml").write_bytes(UNLOAD_WITHOUT_WEBMIM)
    assert main(["run-cycle"] + args) == 0
    conn = store.connect(tmp_path / "t.db")
    out = {a["name"]: (tmp_path / "feed" / a["feed_token"] / "next" / "avito_feed.xml").read_text(encoding="utf-8")
           for a in connections.list_accounts(conn)}
    conn.close()
    return out

def test_the_switch_on_merges_the_pair_into_the_webmim_ad(tmp_path):
    """The scenario does guard the merge: with the switch on the pair is one ad (WebMIM first, though 4tochki has more stock)."""
    built = build(tmp_path, True, TRIAL_BOTH)
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert built["Основной"] != golden["Основной"]
    assert "<Id>100</Id>" in built["Основной"] and "<Id>s2-TCH1</Id>" not in built["Основной"] and "<Id>s2-TCH9</Id>" in built["Основной"]
    assert "<Id>100_a2</Id>" in built["Партнёр-2"] and "s2-TCH1" not in built["Партнёр-2"]
    for text in built.values():
        assert "<Model>Nordman 7</Model>" not in text
    conn = store.connect(tmp_path / "t.db")
    assert [r[:2] for r in store.load_tire_pairs(conn)] == [("100", "s2-TCH1")]        # on: the pair is remembered
    assert conn.execute("SELECT COUNT(*) FROM tire_match_switch").fetchone()[0] == 0
    conn.close()


def test_on_the_remembered_pair_goes_on_under_the_webmim_id_when_the_webmim_row_leaves(tmp_path):
    built = publish_then_lose_webmim(tmp_path)
    assert "<Id>100</Id>" in built["Основной"] and "<Id>s2-TCH1</Id>" not in built["Основной"]
    assert "<Id>100_a2</Id>" in built["Партнёр-2"] and "s2-TCH1" not in built["Партнёр-2"]
    for text in built.values():
        assert "<Model>Character Ice 7</Model>" in text and "<Model>Nordman 7</Model>" not in text
    conn = store.connect(tmp_path / "t.db")
    assert store.last_match_switches(conn, 1)["100"][:2] == ("100", "s2-TCH1")
    assert store.last_match_switches(conn, 2)["100_a2"][:2] == ("100", "s2-TCH1")
    conn.close()


def test_without_the_memory_the_strict_entry_keeps_the_4tochki_twin_out(tmp_path):
    """The memory taken away: the 4tochki side used to come out under a new Id (the known gap of match-core, a repeat for Avito). The
    strict entry of the trial (plan §4б) knows the tire from WebMIM's published ad: it does not go in under an Id of its own."""
    built = publish_then_lose_webmim(tmp_path, forget=True)
    assert "s2-TCH1" not in built["Основной"] and "<Model>Nordman 7</Model>" not in built["Основной"]


def test_match_report_counts_the_pair_and_writes_nothing(tmp_path, capsys):
    args = prepare(tmp_path, trial=TRIAL_BOTH)
    conn = store.connect(tmp_path / "t.db")
    before = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0], conn.execute("SELECT COUNT(*) FROM connection").fetchone()[0]
    conn.close()
    assert main(["match-report", "--db", args[1], "--unload", args[3], "--reference", args[5]]) == 0
    out = capsys.readouterr().out
    assert "склейка в фиде: выключена" in out
    assert "пар (групп одной шины у разных поставщиков): 1, из них модели Авито расходятся: 1" in out
    # without the merge the 4tochki side no longer is a second ad (strict entry, plan §4б): the merge changes no count, it only holds the pair
    assert "аккаунт №1 «Основной», как сейчас: объявлений 3 → 3; ушли бы из фида 0, пришли бы 0" in out
    assert "аккаунт №1 «Основной», если включить пробу 4 точек (режим merge): объявлений 3 → 3" in out
    assert "   4 точки: позиций 2, в фиде 1; не вошли: same_tire 1" in out
    assert "картинка WebMIM той же модели 1, картинка 4 точек 0" in out
    conn = store.connect(tmp_path / "t.db")
    assert (conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0], conn.execute("SELECT COUNT(*) FROM connection").fetchone()[0]) == before
    assert conn.execute("SELECT COUNT(*) FROM published_state").fetchone()[0] == 0
    assert "сохранённых пар (запомнены циклом при включённой склейке): 0" in out


def test_match_report_shows_the_remembered_pairs_and_the_last_change_and_writes_nothing(tmp_path, capsys):
    publish_then_lose_webmim(tmp_path)
    capsys.readouterr()
    conn = store.connect(tmp_path / "t.db")
    before = conn.execute("SELECT COUNT(*) FROM tire_pair").fetchone()[0], conn.execute("SELECT COUNT(*) FROM tire_match_switch").fetchone()[0]
    conn.close()
    args = ["--db", str(tmp_path / "t.db"), "--unload", str(tmp_path / "unload.xml"), "--reference", str(tmp_path / "reference.txt")]
    assert main(["match-report"] + args) == 0                              # 3 if anything was written
    out = capsys.readouterr().out
    assert "сохранённых пар (запомнены циклом при включённой склейке): 1" in out
    assert "  100 (нет в выгрузке) | s2-TCH1, найдена " in out
    assert "групп, дополненных памятью (одна сторона ушла из выгрузки): 1" in out
    assert "аккаунт №1 «Основной»: объявлений пар, у которых менялся поставщик: 1" in out
    assert "   Id 100: 100 → s2-TCH1, " in out and "следующая смена не раньше " in out
    assert "ждёт смены поставщика" not in out             # the change of cycle 2 (not published here) is the same one, not a new one
    conn = store.connect(tmp_path / "t.db")
    assert (conn.execute("SELECT COUNT(*) FROM tire_pair").fetchone()[0], conn.execute("SELECT COUNT(*) FROM tire_match_switch").fetchone()[0]) == before
    conn.close()


def test_the_pairs_are_only_added(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    assert store.add_tire_pairs(conn, [("s2-B", "100", "ib", "ia")], "2026-10-20T10:00:00") == 1
    assert store.add_tire_pairs(conn, [("100", "s2-B", "other", "other")], "2026-10-25T10:00:00") == 0     # the first record stays
    assert store.load_tire_pairs(conn) == [("100", "s2-B", "ia", "ib", "2026-10-20T10:00:00")]
    store.add_match_switch(conn, 1, "100", "100", "s2-B", "2026-10-20T10:00:00")
    store.add_match_switch(conn, 1, "100", "s2-B", "100", "2026-10-22T10:00:00")
    assert store.last_match_switches(conn, 1) == {"100": ("s2-B", "100", "2026-10-22T10:00:00")} and store.last_match_switches(conn, 2) == {}
    assert conn.execute("SELECT COUNT(*) FROM tire_match_switch").fetchone()[0] == 2
    conn.close()
