"""One catalog model written two ways by the supplier (WS71 / WS71 SUV) is one photo set; the other spelling is an alias."""
from tires_sync import photo_library as pl
from tires_sync import store
from tires_sync.candidates import Candidate
from tests.helpers import make_offer

NOW = "2026-10-21T10:00:00"


def cand(i, raw, avito="WinterCraft WS71", **kw):
    o = make_offer(goods_id=str(i), model_raw=raw, brand_raw="Kumho", brand_key="KUMHO", **kw)
    return Candidate(o.goods_id, o, 1000, "Kumho", avito)


def ok(*cands):
    return {c.goods_id: c for c in cands}


def test_two_spellings_of_one_catalog_model_make_one_set():
    plans = pl.needed_sets(ok(cand(1, "WS71"), cand(2, "WS71 SUV", width=215), cand(3, "WS71 SUV", width=225)), 4)
    assert len(plans) == 1
    p = plans[0]
    assert p.key.model_key == "WS71SUV" and p.positions == 3          # the spelling with more positions leads
    assert [a.text() for a in p.aliases] == ["kumho|WS71|winter|n|r15_18"]


def test_different_catalog_models_or_classes_stay_apart():
    plans = pl.needed_sets(ok(cand(1, "WS71"), cand(2, "WS71 SUV", avito="WinterCraft WS71 SUV"), cand(3, "WS71", diameter=19)), 4)
    assert len(plans) == 3 and all(not p.aliases for p in plans)


def test_sync_keeps_the_key_that_already_has_frames(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    plans = pl.needed_sets(ok(cand(1, "WS71")), 4)                  # yesterday: one spelling only
    pl.sync_sets(conn, plans, NOW)
    old = plans[0].key.text()
    conn.execute("INSERT INTO photo (sha256, ext, source, created) VALUES ('x','png','gen',?)", (NOW,))
    conn.execute("INSERT INTO set_frame (set_key, kind, photo_id, status, created) VALUES (?,'three_quarter',1,'approved',?)", (old, NOW))
    plans = pl.needed_sets(ok(cand(1, "WS71"), cand(2, "WS71 SUV", width=215), cand(3, "WS71 SUV", width=225)), 4)
    pl.sync_sets(conn, plans, NOW)
    needed = [r[0] for r in conn.execute("SELECT key FROM photo_set WHERE state='needed'")]
    assert needed == [old]
    assert pl.aliases(conn) == {"kumho|WS71SUV|winter|n|r15_18": old}


def test_duplicate_set_is_retired_and_its_queued_job_dropped(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    pl.sync_sets(conn, pl.needed_sets(ok(cand(1, "WS71", avito="A")), 4), NOW)
    pl.sync_sets(conn, pl.needed_sets(ok(cand(1, "WS71", avito="A"), cand(2, "WS71 SUV", avito="B")), 4), NOW)
    pl.enqueue(conn, NOW)
    assert conn.execute("SELECT COUNT(*) FROM image_job WHERE status='queued'").fetchone()[0] == 2
    pl.sync_sets(conn, pl.needed_sets(ok(cand(1, "WS71"), cand(2, "WS71 SUV")), 4), NOW)   # catalog now says one model
    assert conn.execute("SELECT COUNT(*) FROM photo_set WHERE state='needed'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM image_job WHERE status='queued'").fetchone()[0] == 1


def test_alias_offer_gets_the_photos_of_its_set(tmp_path):
    import test_feed_own_photos as f
    conn, first, second = f.env(tmp_path)
    links = f.library(conn, first, second)
    other = "ikon tyres|OTHERSPELLING|winter|n|r15_18"
    conn.execute("UPDATE set_frame SET set_key=?", (other,))       # the set lives under the other spelling
    conn.execute("INSERT INTO set_alias (alias_key, set_key) VALUES (?,?)", (f.KEY, other))
    conn.execute("INSERT INTO photo_set (key, state, in_feed) VALUES (?, 'needed', 1)", (other,))   # the set the owner sent to the feed
    conn.commit()
    own = pl.own_photos_for(conn, first, f.BASE)
    urls = pl.image_urls(own, f.OFFER, False, "https://supplier/1.jpg")
    assert urls[:3] == [links[first]["tq"], links[first]["set4"], links[first]["x1"]]


def _frame(conn, key, status="approved", kind="three_quarter", when=NOW):
    conn.execute("INSERT OR IGNORE INTO photo (id, sha256, ext, source, created) VALUES (1,'x','png','gen',?)", (NOW,))
    conn.execute("INSERT INTO set_frame (set_key, kind, photo_id, status, created, decided) VALUES (?,?,1,?,?,?)", (key, kind, status, when, when))


def _needed(conn):
    return [r[0] for r in conn.execute("SELECT key FROM photo_set WHERE state='needed'")]


A, B = "kumho|WS71|winter|n|r15_18", "kumho|WS71SUV|winter|n|r15_18"


def test_approved_frames_beat_a_rejected_one_on_the_leader(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    _frame(conn, B, status="rejected")
    _frame(conn, A)
    pl.sync_sets(conn, pl.needed_sets(ok(cand(1, "WS71"), cand(2, "WS71 SUV", width=215), cand(3, "WS71 SUV", width=225)), 4), NOW)
    assert _needed(conn) == [A] and pl.aliases(conn) == {B: A}


def test_choice_is_stable_when_both_spellings_have_frames(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    _frame(conn, B, when="2026-10-20T09:00:00")
    _frame(conn, A, when="2026-10-20T10:00:00")
    more_a = ok(cand(1, "WS71"), cand(2, "WS71", width=215), cand(3, "WS71 SUV", width=225))
    more_b = ok(cand(1, "WS71"), cand(2, "WS71 SUV", width=215), cand(3, "WS71 SUV", width=225))
    for plan in (more_a, more_b, more_a):
        pl.sync_sets(conn, pl.needed_sets(plan, 4), NOW)
        assert _needed(conn) == [B] and pl.aliases(conn) == {A: B}     # the earlier approval keeps the set


def test_sold_out_spelling_with_frames_keeps_serving(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    pl.sync_sets(conn, pl.needed_sets(ok(cand(1, "WS71")), 4), NOW)
    _frame(conn, A)
    pl.sync_sets(conn, pl.needed_sets(ok(cand(1, "WS71"), cand(2, "WS71 SUV")), 4), NOW)
    assert pl.aliases(conn) == {B: A}
    pl.sync_sets(conn, pl.needed_sets(ok(cand(2, "WS71 SUV")), 4), NOW)           # WS71 sold out today
    assert _needed(conn) == [A] and pl.aliases(conn) == {B: A}


def test_supplier_brands_are_not_merged_by_catalog_name():
    other = make_offer(goods_id="9", model_raw="WS71", brand_raw="Marshal", brand_key="MARSHAL")
    plans = pl.needed_sets(ok(cand(1, "WS71"), Candidate("9", other, 1000, "Kumho", "WinterCraft WS71")), 4)
    assert len(plans) == 2
