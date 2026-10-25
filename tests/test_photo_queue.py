from tires_sync import photo_library as pl
from tires_sync import store
from tires_sync.candidates import Candidate
from tires_sync.models import Season
from tests.helpers import make_offer

NOW = "2026-10-20T10:00:00"


def cands(*offers):
    return {o.goods_id: Candidate(o.goods_id, o, 1000, "B", o.model_raw) for o in offers}   # catalog name per model, like the real catalog


def offer(i, **kw):
    return make_offer(goods_id=str(i), **kw)


def test_only_winter_r15_stock4():
    ok = cands(offer(1, stock=4), offer(2, stock=3, model_raw="B"), offer(3, stock=10, season=Season.SUMMER, model_raw="C"),
               offer(4, diameter=14, model_raw="D"))
    plans = pl.needed_sets(ok, 4)
    assert len(plans) == 1 and plans[0].key.model_key == pl.set_key_for(offer(1)).model_key


def test_order_studded_then_positions_then_stock():
    ok = cands(offer(1, studded=True, model_raw="A"),
               offer(2, model_raw="B"), offer(3, model_raw="B", width=215), offer(4, model_raw="B", width=225),
               offer(5, studded=True, model_raw="C"), offer(6, studded=True, model_raw="C", width=215))
    plans = pl.needed_sets(ok, 4)
    assert [(p.key.studded, p.positions) for p in plans] == [(True, 2), (True, 1), (False, 3)]


def test_ref_url_most_frequent_and_tie():
    ok = cands(offer(1, image="B"), offer(2, image="A"), offer(3, image="A"))
    assert pl.needed_sets(ok, 4)[0].ref_url == "A"
    ok = cands(offer(1, image="B"), offer(2, image="A"))
    assert pl.needed_sets(ok, 4)[0].ref_url == "A"


def test_size_hint_most_frequent():
    ok = cands(offer(1), offer(2), offer(3, width=215, height=60))
    assert pl.needed_sets(ok, 4)[0].size_hint == "205/55 R16"


def test_unknown_season_needs_decision():
    ok = cands(offer(1, season=Season.ALL, model_raw="A"), offer(2, season=Season.UNKNOWN, model_raw="B"))
    assert pl.needed_sets(ok, 4) == []
    plans = pl.needed_sets(ok, 4, seasons=frozenset({Season.WINTER, Season.ALL, Season.UNKNOWN}))
    assert len(plans) == 2 and all(p.state == "needs_decision" and p.reason for p in plans)


def test_no_ref_photo_skipped():
    assert pl.needed_sets(cands(offer(1, image=""), offer(2, image="")), 4) == []


def _db(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    plans = pl.needed_sets(cands(offer(1)), 4)
    pl.sync_sets(conn, plans, NOW)
    return conn, plans[0].key.text()


def _jobs(conn):
    return [tuple(r) for r in conn.execute("SELECT set_key, kind, status FROM image_job ORDER BY id")]


def test_enqueue_three_quarter_first(tmp_path):
    conn, key = _db(tmp_path)
    assert pl.enqueue(conn, NOW) == 1
    assert _jobs(conn) == [(key, "three_quarter", "queued")]
    conn.execute("UPDATE image_job SET status='done'")
    conn.execute("INSERT INTO photo (sha256, ext, source, created) VALUES ('x','png','gen',?)", (NOW,))
    conn.execute("INSERT INTO set_frame (set_key, kind, photo_id, status, created) VALUES (?,?,1,'approved',?)", (key, "three_quarter", NOW))
    assert pl.enqueue(conn, NOW) == 1
    assert _jobs(conn)[-1] == (key, "set4", "queued")


def test_enqueue_respects_attempts(tmp_path):
    conn, key = _db(tmp_path)
    conn.execute("INSERT INTO photo (sha256, ext, source, created) VALUES ('x','png','gen',?)", (NOW,))
    for st in ("failed", "failed"):
        conn.execute("INSERT INTO image_job (set_key, kind, status, created, updated) VALUES (?,?,?,?,?)", (key, "three_quarter", st, NOW, NOW))
    conn.execute("INSERT INTO set_frame (set_key, kind, photo_id, status, created) VALUES (?,?,1,'rejected',?)", (key, "three_quarter", NOW))
    assert pl.enqueue(conn, NOW) == 0


def test_enqueue_respects_daily_cap(tmp_path):
    conn, key = _db(tmp_path)
    conn.execute("INSERT INTO image_job (set_key, kind, status, cost_usd, created, updated) VALUES ('other','set4','done',5.0,?,?)", (NOW, NOW))
    assert pl.enqueue(conn, NOW) == 0
    assert pl.enqueue(conn, "2026-10-21T10:00:00") == 1


def test_enqueue_idempotent(tmp_path):
    conn, _ = _db(tmp_path)
    assert pl.enqueue(conn, NOW) == 1
    assert pl.enqueue(conn, NOW) == 0


def _many(n):
    return cands(*[offer(i, model_raw="M%03d" % i) for i in range(n)])


def test_enqueue_cap_bounds_batch(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    pl.sync_sets(conn, pl.needed_sets(_many(100), 4), NOW)
    assert pl.enqueue(conn, NOW, daily_cap_usd=5.0, max_open=1000) == 50
    assert pl.enqueue(conn, NOW, daily_cap_usd=5.0, max_open=1000) == 0


def _approve_three_quarter(conn, key):
    conn.execute("UPDATE image_job SET status='done' WHERE set_key=? AND kind='three_quarter'", (key,))
    conn.execute("INSERT OR IGNORE INTO photo (sha256, ext, source, created) VALUES ('x','png','gen',?)", (NOW,))
    conn.execute("INSERT INTO set_frame (set_key, kind, photo_id, status, created) VALUES (?,'three_quarter',1,'approved',?)", (key, NOW))


def test_enqueue_limits_queue_depth(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    pl.sync_sets(conn, pl.needed_sets(_many(30), 4), NOW)
    assert pl.enqueue(conn, NOW) == pl.MAX_OPEN_JOBS == 10
    assert pl.enqueue(conn, NOW) == 0


def test_enqueue_makes_room_for_higher_priority(tmp_path):
    # a long queue from before the depth limit must not hold back the set4 of the top set
    conn = store.connect(tmp_path / "t.db")
    plans = pl.needed_sets(_many(30), 4)
    pl.sync_sets(conn, plans, NOW)
    assert pl.enqueue(conn, NOW, max_open=1000) == 30
    top, last = plans[0].key.text(), plans[-1].key.text()
    _approve_three_quarter(conn, top)
    assert pl.enqueue(conn, NOW) == 1
    queued = [tuple(r) for r in conn.execute("SELECT set_key, kind FROM image_job WHERE status='queued'")]
    assert len(queued) == 10
    assert (top, "set4") in queued and (last, "three_quarter") not in queued
    assert [k for k, _ in queued if k != top] == [p.key.text() for p in plans[1:10]]


def test_running_jobs_count_toward_depth_and_stay(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    plans = pl.needed_sets(_many(30), 4)
    pl.sync_sets(conn, plans, NOW)
    pl.enqueue(conn, NOW, max_open=1000)
    conn.execute("UPDATE image_job SET status='running' WHERE set_key=?", (plans[29].key.text(),))
    pl.enqueue(conn, NOW)
    statuses = [r[0] for r in conn.execute("SELECT status FROM image_job")]
    assert statuses.count("running") == 1 and statuses.count("queued") == 9


def test_enqueue_follows_plan_order(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    a = offer(1, model_raw="A")
    pl.sync_sets(conn, pl.needed_sets(cands(a), 4), NOW)
    ok = cands(a, offer(2, model_raw="Z", studded=True))          # studded Z outranks A
    pl.sync_sets(conn, pl.needed_sets(ok, 4), NOW)
    assert pl.enqueue(conn, NOW) == 2
    keys = [r[0] for r in conn.execute("SELECT set_key FROM image_job ORDER BY id")]
    assert keys == [p.key.text() for p in pl.needed_sets(ok, 4)]
    assert "|s|" in keys[0]


def test_absent_set_retired_and_queued_jobs_dropped(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    ok = cands(offer(1, model_raw="A"), offer(2, model_raw="B"))
    plans = pl.needed_sets(ok, 4)
    pl.sync_sets(conn, plans, NOW)
    pl.enqueue(conn, NOW)
    gone, kept = plans[1].key.text(), plans[0].key.text()
    conn.execute("UPDATE image_job SET status='running' WHERE set_key=?", (kept,))
    pl.sync_sets(conn, plans[:1], NOW)
    assert conn.execute("SELECT state FROM photo_set WHERE key=?", (gone,)).fetchone()[0] == "retired"
    assert [r[0] for r in conn.execute("SELECT set_key FROM image_job")] == [kept]
    pl.enqueue(conn, NOW)
    assert conn.execute("SELECT COUNT(*) FROM image_job WHERE set_key=?", (gone,)).fetchone()[0] == 0
    pl.sync_sets(conn, plans, NOW)                                 # reappears
    assert conn.execute("SELECT state FROM photo_set WHERE key=?", (gone,)).fetchone()[0] == "needed"


def _cycle_setup(tmp_path, db):
    from datetime import date
    from helpers import catalog_xml, commodity_xml, settings_dict
    from tires_sync import connections, cycle
    from tires_sync.catalog import parse_reference
    from tires_sync.settings_store import active_settings, init_settings
    from tires_sync.webmim import parse_unload
    ref = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')
    conn = store.connect(tmp_path / db)
    init_settings(conn, settings_dict(), NOW)
    connections.create_account(conn, "A", NOW, "owner")
    xml = catalog_xml(commodity_xml())
    res = cycle.run_for_accounts(conn, xml, ref, date(2026, 10, 10), tmp_path / ("feed" + db))
    return conn, ref, parse_unload(xml, "T1"), active_settings(conn), [(r.name, r.ok, r.error, r.feed_count) for r in res]


def test_run_for_accounts_alone_writes_no_sets(tmp_path):
    conn, *_ = _cycle_setup(tmp_path, "a.db")
    assert conn.execute("SELECT COUNT(*) FROM photo_set").fetchone()[0] == 0


def test_cli_hook_writes_sets(tmp_path):
    from tires_sync import cli
    conn, ref, parsed, settings, _ = _cycle_setup(tmp_path, "a.db")
    cli._queue_photo_sets(tmp_path / "a.db", parsed, ref, settings, NOW)
    assert conn.execute("SELECT COUNT(*) FROM photo_set").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM image_job").fetchone()[0] == 1


def test_cli_hook_failure_persists_nothing(tmp_path, monkeypatch):
    from tires_sync import cli
    conn, ref, parsed, settings, before = _cycle_setup(tmp_path, "a.db")

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(pl, "enqueue", boom)
    cli._queue_photo_sets(tmp_path / "a.db", parsed, ref, settings, NOW)        # must not raise
    assert conn.execute("SELECT COUNT(*) FROM photo_set").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM image_job").fetchone()[0] == 0


def test_cli_hook_skips_an_empty_evaluation(tmp_path):
    from helpers import catalog_xml
    from tires_sync import cli
    from tires_sync.webmim import parse_unload
    conn, ref, parsed, settings, _ = _cycle_setup(tmp_path, "a.db")
    cli._queue_photo_sets(tmp_path / "a.db", parsed, ref, settings, NOW)
    cli._queue_photo_sets(tmp_path / "a.db", parse_unload(catalog_xml(), "T1"), ref, settings, NOW)   # a bad unload: no offers at all
    assert [r[0] for r in conn.execute("SELECT state FROM photo_set")] == ["needed"]                  # nothing retired
    assert conn.execute("SELECT COUNT(*) FROM image_job WHERE status='queued'").fetchone()[0] == 1


def test_cli_hook_uses_the_panel_clock(tmp_path, monkeypatch):
    """The panel stamps jobs in UTC (app NOW = datetime.utcnow); the hook must count the day's spend on the same clock."""
    from datetime import datetime as real
    from tires_sync import cli

    class Frozen(real):
        @classmethod
        def utcnow(cls):
            return real(2026, 10, 7, 22, 30)                       # 01:30 next day in Moscow

        @classmethod
        def now(cls, tz=None):
            return real(2026, 10, 8, 1, 30)
    monkeypatch.setattr(cli, "datetime", Frozen)
    conn, ref, parsed, settings, _ = _cycle_setup(tmp_path, "a.db")
    conn.execute("INSERT INTO image_job (set_key, kind, status, cost_usd, created, updated) VALUES ('x','three_quarter','done',5.0,?,?)",
                 ("2026-10-07T21:00:00", "2026-10-07T22:00:00"))          # spent today by the handout, stamped in UTC
    conn.commit()
    cli._queue_photo_sets(tmp_path / "a.db", parsed, ref, settings)
    assert conn.execute("SELECT COUNT(*) FROM image_job WHERE status='queued'").fetchone()[0] == 0      # same day: the cap is reached
    conn.execute("UPDATE image_job SET updated='2026-10-06T22:00:00'")
    conn.commit()
    cli._queue_photo_sets(tmp_path / "a.db", parsed, ref, settings)
    assert [r[0] for r in conn.execute("SELECT created FROM image_job WHERE status='queued'")] == ["2026-10-07T22:30:00"]


def test_stopped_queue_enqueues_nothing(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.set_state(conn, "image_queue_stopped", "1")
    pl.sync_sets(conn, pl.needed_sets(cands(offer(1)), 4), NOW)
    assert pl.enqueue(conn, NOW) == 0
    assert conn.execute("SELECT COUNT(*) FROM image_job").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM photo_set").fetchone()[0] == 1
    store.set_state(conn, "image_queue_stopped", "0")
    assert pl.enqueue(conn, NOW) == 1


def test_cap_and_attempts_defined_once():
    from tires_sync.panel import routes_agent
    assert pl.DAILY_CAP_USD == 5.0 and pl.MAX_ATTEMPTS == 3
    assert not hasattr(routes_agent, "DAILY_CAP_USD")


def test_sync_sets_records_what_is_in_stock(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    plans = pl.needed_sets(cands(offer(1, stock=6), offer(2, stock=10, width=215)), 4)
    pl.sync_sets(conn, plans, NOW)
    assert conn.execute("SELECT positions, stock FROM photo_set").fetchone() == (2, 16)
