"""Second full review: guard alerts, alert dedupe, settings ranges, pack4 holds, address, stale supplier."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import pytest

from helpers import catalog_xml, commodity_xml, settings_dict
from http_fakes import ScriptedHttp
from tires_sync import cli, connections, cycle, store
from tires_sync.catalog import parse_reference
from tires_sync.config import settings_from_dict
from tires_sync.secretbox import create_key_file, load_box
from tires_sync.settings_store import init_settings

NOW = "2026-10-14T10:00:00"
TODAY = date(2026, 10, 14)
REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')
BOT = "123456:ABCdef-SECRET"
OK = (200, {"ok": True, "result": {}})


def goods(n, start=1, **kw):
    return catalog_xml(*[commodity_xml(NNOMMODIF=str(i), NREST="12", SMNFCODE="C%d" % i, **kw) for i in range(start, start + n)])


def env(tmp_path, **settings):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(**settings), NOW)
    connections.create_account(conn, "A", NOW, "owner")
    connections.set_live(conn, 1, True, NOW, "owner")
    key_file = tmp_path / "secret.key"
    create_key_file(key_file)
    box = load_box(key_file)
    connections.set_config(conn, "telegram", 0, {"chat_id": "42"}, NOW, "owner")
    connections.set_secret(conn, box, "telegram", 0, "bot_token", BOT, NOW, "owner")
    return conn, key_file, box


# --- 3: alerts of different content are not swallowed by each other --------------------------------------------------

def test_two_different_alerts_in_one_run_are_both_sent_but_a_repeat_is_not(tmp_path, monkeypatch):
    conn, key_file, box = env(tmp_path)
    http = ScriptedHttp(OK, OK, OK)
    monkeypatch.setattr(cli, "_http", lambda: http)
    cli._alert(conn, box, "поставщик удерживается")
    cli._alert(conn, box, "цикл упал")
    cli._alert(conn, box, "цикл упал")
    assert len(http.calls) == 2


def test_the_services_own_failure_alert_is_skipped_right_after_a_cycle_alert(tmp_path, monkeypatch):
    conn, key_file, box = env(tmp_path)
    http = ScriptedHttp(OK, OK)
    monkeypatch.setattr(cli, "_http", lambda: http)
    cli._alert(conn, box, "цикл упал")
    conn.close()
    assert cli.main(["alert", "--db", str(tmp_path / "t.db"), "--key-file", str(key_file), "--message", "Сбой службы"]) == 0
    assert len(http.calls) == 1


# --- 2: the removal guard is reported and keeps the ads ----------------------------------------------------------------

def published_ten(tmp_path, **settings):
    conn, key_file, box = env(tmp_path, guard_min=2, guard_fraction=0.0, **settings)
    results = cycle.run_for_accounts(conn, goods(10), REF, TODAY, tmp_path / "feed")
    assert results[0].ok
    from tires_sync import publishing
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    assert len(store.load_published(conn, 1)) == 10
    return conn, key_file, box


def test_guard_trip_is_reported_and_start_dates_the_hold(tmp_path):
    conn, key_file, box = published_ten(tmp_path)
    results = cycle.run_for_accounts(conn, goods(2), REF, TODAY + timedelta(days=1), tmp_path / "feed")
    assert results[0].ok and results[0].guard_tripped
    import json
    payload = json.loads(conn.execute("SELECT payload FROM pending_state WHERE account_id = 1").fetchone()[0])
    holds = [i for i in payload if i["action"] == "hold"]
    assert len(holds) == 8 and all(i["first_hold"] == (TODAY + timedelta(days=1)).isoformat() for i in holds)


def test_timer_alerts_when_the_guard_trips(tmp_path, monkeypatch):
    conn, key_file, box = published_ten(tmp_path)
    conn.close()
    (tmp_path / "unload.xml").write_bytes(goods(2))
    (tmp_path / "reference.txt").write_text('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>', encoding="utf-8")
    http = ScriptedHttp(OK, OK)
    monkeypatch.setattr(cli, "_http", lambda: http)
    monkeypatch.setattr(cli, "moscow_now", lambda: datetime(2026, 10, 7, 12, 0))
    args = ["run-cycle", "--publish", "--db", str(tmp_path / "t.db"), "--unload", str(tmp_path / "unload.xml"),
            "--reference", str(tmp_path / "reference.txt"), "--feed-root", str(tmp_path / "feed"), "--key-file", str(key_file)]
    code = cli.main(args)                              # eight positions left the unload: taken off at once, so the mass removal is real and the guard stops it
    assert code == 3
    import json
    assert any("массов" in json.loads(c[3])["text"] for c in http.calls)


# --- 6: ranges ---------------------------------------------------------------------------------------------------------

def test_account_min_stock_must_be_at_least_one(tmp_path):
    conn, _, _ = env(tmp_path)
    with pytest.raises(ValueError):
        connections.set_account_pricing(conn, 1, 0.0, 0, NOW, "owner")
    connections.set_account_pricing(conn, 1, 0.0, 1, NOW, "owner")


@pytest.mark.parametrize("field,value", [("min_stock", 0), ("stale_days", -1), ("portion_limit", -1),
                                          ("guard_min", -1), ("guard_fraction", 1.5), ("guard_fraction", -0.1),
                                          ("date_end_days", 0), ("price_jump_limit", 0), ("min_diameter", 0)])
def test_settings_out_of_range_are_rejected(field, value):
    with pytest.raises(ValueError):
        settings_from_dict(settings_dict(**{field: value}))


def test_zero_days_before_taking_off_is_a_valid_choice():
    assert settings_from_dict(settings_dict(stale_days=0)).stale_days == 0


# --- 4: a switched-off address does not stay on existing ads -------------------------------------------------------------

def feed_of(tmp_path, conn, account=1):
    token = connections.get_account(conn, account)["feed_token"]
    return (tmp_path / "feed" / token / "next" / "avito_feed.xml").read_text(encoding="utf-8")


def test_ads_move_away_from_an_address_that_was_switched_off(tmp_path):
    from tires_sync import publishing
    from tires_sync.settings_store import confirm_draft, create_draft
    conn, _, _ = env(tmp_path)
    cycle.run_for_accounts(conn, goods(6), REF, TODAY, tmp_path / "feed")
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    assert "<SellerAddressID>P1</SellerAddressID>" in feed_of(tmp_path, conn)
    confirm_draft(conn, create_draft(conn, {"point_values": {"Q1": 50, "Q2": 50}}, NOW), NOW, "owner")
    cycle.run_for_accounts(conn, goods(6), REF, TODAY, tmp_path / "feed")
    text = feed_of(tmp_path, conn)
    assert "P1" not in text and "P2" not in text and "P3" not in text
    assert text.count("<SellerAddressID>Q1</SellerAddressID>") == 3 and text.count("<SellerAddressID>Q2</SellerAddressID>") == 3


# --- 5: pack4 ads are held together with the base ad ----------------------------------------------------------------------

def test_a_held_ad_keeps_its_pack4_twin(tmp_path):
    from tires_sync import publishing
    conn, _, _ = env(tmp_path, pack4=True, guard_min=5)
    cycle.run_for_accounts(conn, goods(4), REF, TODAY, tmp_path / "feed")
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    first = feed_of(tmp_path, conn)
    assert first.count("<Id>") == 8 and "<Id>1-x4</Id>" in first
    jumped = catalog_xml(
        commodity_xml(NNOMMODIF="1", NREST="12", SMNFCODE="C1", NPRICE_DELAYED="по запросу"),
        *[commodity_xml(NNOMMODIF=str(i), NREST="12", SMNFCODE="C%d" % i) for i in (2, 3, 4)])
    results = cycle.run_for_accounts(conn, jumped, REF, TODAY + timedelta(days=1), tmp_path / "feed")
    assert results[0].ok and results[0].counts.get("hold") == 1                       # bad data: held, not taken off (the wait is 3 days here)
    text = feed_of(tmp_path, conn)
    assert text.count("<Id>") == 8 and "<Id>1-x4</Id>" in text and "<Id>1</Id>" in text


# --- 8: a stale unload of a secondary supplier is not used for anything ------------------------------------------------

def test_a_stale_secondary_unload_holds_everything_of_that_supplier(tmp_path):
    from datetime import datetime
    from test_multi_supplier_cycle import ONE, TWO, files, prepared
    conn = prepared(tmp_path)
    connections.set_live(conn, 1, True, NOW, "owner")
    conn.close()
    args = files(tmp_path, ONE, TWO)
    assert cli.main(["run-cycle", "--publish"] + args) == 0
    # a new position appears in the stale second unload: it must not be published
    (tmp_path / "unload-2.xml").write_bytes(catalog_xml(
        commodity_xml(NNOMMODIF="5", TERRITORY_RN="2002", NPRICE_DELAYED="5000"),
        commodity_xml(NNOMMODIF="6", TERRITORY_RN="2002", NPRICE_DELAYED="5000", SMNFCODE="C6")))
    old = (datetime.utcnow() - timedelta(days=30)).isoformat(timespec="seconds")
    (tmp_path / "unload-2.xml.meta.json").write_text('{"fetched_at": "%s", "items": 2}' % old, encoding="utf-8")
    assert cli.main(["run-cycle", "--publish"] + args) == 3
    conn = store.connect(tmp_path / "t.db")
    assert set(store.load_published(conn, 1)) == {"5", "s2-5"}


# --- 7: one writer at a time --------------------------------------------------------------------------------------------

def test_feed_lock_is_exclusive(tmp_path):
    from tires_sync import publishing
    with publishing.feed_lock(tmp_path / "feed"):
        assert publishing.try_feed_lock(tmp_path / "feed") is None
    other = publishing.try_feed_lock(tmp_path / "feed")
    assert other is not None
    other.close()


def test_feed_lock_does_not_let_the_work_go_on_when_somebody_keeps_the_lock(tmp_path):
    import pytest
    from tires_sync import publishing
    holder = publishing.try_feed_lock(tmp_path / "feed")
    try:
        with pytest.raises(publishing.FeedBusy):
            with publishing.feed_lock(tmp_path / "feed", wait=0.4):
                raise AssertionError("the block must not run")
        with publishing.feed_lock(tmp_path / "feed", wait=0.4, strict=False) as acquired:
            assert acquired is False
    finally:
        holder.close()
    with publishing.feed_lock(tmp_path / "feed", wait=0.4) as acquired:
        assert acquired is True


def test_a_lock_file_that_cannot_be_opened_still_lets_the_work_go_on(tmp_path):
    from tires_sync import publishing
    (tmp_path / "blocker").write_text("x")
    with publishing.feed_lock(tmp_path / "blocker" / "feed", wait=0.2) as acquired:           # the folder cannot be made: unlocked, as documented
        assert acquired is True


def test_panel_publish_does_not_publish_a_feed_it_failed_to_build(tmp_path):
    from tires_sync import publishing
    conn, _, _ = env(tmp_path)
    cycle.run_for_accounts(conn, goods(3), REF, TODAY, tmp_path / "feed")
    out = publishing.publish_live_accounts(conn, tmp_path / "feed", skip_ids={1})
    assert out[0]["skipped"] and not out[0]["ok"]
    assert not (tmp_path / "feed" / connections.get_account(conn, 1)["feed_token"] / "avito_feed.xml").exists()


# --- 11: behind the proxy the feed address is https ----------------------------------------------------------------------

def test_feed_address_uses_https_behind_the_proxy(tmp_path):
    from panel_helpers import login, make_app
    app = make_app(tmp_path, TRUST_PROXY=1)
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    connections.create_account(conn, "A", NOW, "owner")
    html = client.get("/connections/avito/1", headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "1.2.3.4"}).get_data(as_text=True)
    feed = re.search(r'value="(https?://[^"]*avito_feed\.xml)"', html).group(1)
    assert feed.startswith("https://")


# --- 10: the review looks at every account, with the account's own price delta and stock threshold ------------------

def test_review_counts_removals_of_a_second_account(tmp_path):
    from tires_sync import publishing, staging
    from tires_sync.panel import service
    from tires_sync.settings_store import get_draft
    conn, _, _ = env(tmp_path, guard_min=2, guard_fraction=0.0)
    connections.create_account(conn, "B", NOW, "owner")
    connections.set_live(conn, 1, False, NOW, "owner")
    connections.set_live(conn, 2, True, NOW, "owner")
    cycle.run_for_accounts(conn, goods(10), REF, TODAY, tmp_path / "feed")
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    assert store.load_published(conn, 1) == {} and len(store.load_published(conn, 2)) == 10
    for g in range(1, 9):
        staging.set_override(conn, str(g), "off", NOW)
    draft_id = staging.summary(conn)["draft_id"]
    from tires_sync.panel.data import load_merged
    from tires_sync.settings_store import active_settings
    unload = tmp_path / "unload.xml"
    unload.write_bytes(goods(10))
    parsed = load_merged(conn, active_settings(conn), unload)
    review = service.draft_review(conn, draft_id, parsed, REF, TODAY)
    assert review["guard"]["gone"] == 8 and review["guard"]["over"]


def test_review_uses_the_accounts_own_stock_threshold(tmp_path):
    from tires_sync import staging
    from tires_sync.panel import service
    from tires_sync.settings_store import active_settings
    from tires_sync.panel.data import load_merged
    conn, _, _ = env(tmp_path, guard_min=1, guard_fraction=0.0)
    connections.set_account_pricing(conn, 1, 0.0, 20, NOW, "owner")        # account threshold 20 > stock 12: nothing is live there
    unload = tmp_path / "unload.xml"
    unload.write_bytes(goods(5))
    parsed = load_merged(conn, active_settings(conn), unload)
    for g in (1, 2, 3):
        staging.set_override(conn, str(g), "off", NOW)
    review = service.draft_review(conn, staging.summary(conn)["draft_id"], parsed, REF, TODAY)
    assert review["guard"]["gone"] == 0       # nothing was going to be on this account's feed anyway


# --- minors ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["/new?days=²", "/catalog?w=²&h=²&d=²", "/catalog/table?w=٣&p=٣"])
def test_non_ascii_digits_in_filters_do_not_crash(tmp_path, url):
    from panel_helpers import login, make_app
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    assert client.get(url).status_code == 200


def test_non_ascii_digits_in_connection_forms_do_not_crash(tmp_path):
    from panel_helpers import login, make_app, post
    from cryptography.fernet import Fernet
    from tires_sync.secretbox import SecretBox
    app = make_app(tmp_path, SECRET_BOX=SecretBox(Fernet.generate_key()))
    client = app.test_client()
    tok = login(client)
    assert post(client, "/connections/webmim", tok, supplier="²", base_url="https://x.example", mode="apikey").status_code in (302, 404)
    assert post(client, "/connections/webmim/check", tok, supplier="²").status_code in (302, 404)


def test_a_failed_mutation_does_not_commit_the_stale_draft_pointer_reset(tmp_path):
    from tires_sync import staging
    from tires_sync.settings_store import create_draft
    conn, _, _ = env(tmp_path)
    draft = create_draft(conn, {"min_stock": 5}, NOW)
    conn.execute("UPDATE draft SET status = 'cancelled' WHERE id = ?", (draft,))
    store.set_state(conn, staging.KEY, str(draft))

    def boom(eff):
        raise ValueError("boom")

    with pytest.raises(ValueError):
        staging._mutate(conn, boom, NOW)
    assert store.get_state(conn, staging.KEY) == str(draft)       # rolled back together with the failed change


def test_the_menu_counter_is_dropped_when_a_cycle_or_a_settings_change_happens(tmp_path):
    from tires_sync.settings_store import confirm_draft, create_draft
    conn, _, _ = env(tmp_path)
    store.set_state(conn, "nav_pending_pairs", "5")
    store.save_run(conn, NOW, False, "x", "{}", 1)
    assert store.get_state(conn, "nav_pending_pairs") is None
    store.set_state(conn, "nav_pending_pairs", "5")
    confirm_draft(conn, create_draft(conn, {"min_stock": 5}, NOW), NOW, "owner")
    assert store.get_state(conn, "nav_pending_pairs") is None


def test_old_login_attempts_are_cleaned_up(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.add_login_failure(conn, "1.1.1.1", "2026-01-01T00:00:00")
    store.add_login_failure(conn, "1.1.1.1", "2026-10-14T10:00:00")
    assert conn.execute("SELECT COUNT(*) FROM login_attempt").fetchone()[0] == 1


def test_atomic_writes_are_synced_before_the_swap(tmp_path, monkeypatch):
    import os
    from tires_sync.feed import write_atomic
    calls = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd) or real(fd))
    write_atomic(tmp_path / "f.xml", b"<a/>")
    assert calls and (tmp_path / "f.xml").read_bytes() == b"<a/>"


def test_responses_are_size_limited(monkeypatch):
    import io
    from tires_sync import transport

    class Big:
        status = 200
        headers = {}

        def read(self, n=-1):
            return b"x" * (n if n and n > 0 else 10 ** 9)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(transport._OPENER, "open", lambda req, timeout=30: Big())
    with pytest.raises(OSError):
        transport.urllib_transport("GET", "https://x.example", {}, None)


# --- remaining minors ---------------------------------------------------------------------------------------------------

def test_draft_confirm_without_rev_is_refused(tmp_path):
    from panel_helpers import login, make_app
    from tires_sync import staging
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    staging.set_override(conn, "1", "off", NOW)
    d = staging.summary(conn)["draft_id"]
    r = client.post("/draft/%d/confirm" % d, data={"csrf": "tok"})          # no rev: not through the review screen
    assert r.status_code == 302
    assert store.get_state(conn, "settings_active") is None
    from tires_sync.settings_store import get_active
    assert get_active(conn)[0] == 1


def test_ids_of_positions_without_a_code_are_stable_across_unloads():
    from tires_sync.webmim import parse_unload
    one = commodity_xml(NNOMMODIF="", SMNFCODE="C9", TERRITORY_RN="T1")
    other = commodity_xml(NNOMMODIF="7", TERRITORY_RN="T1")
    a = parse_unload(catalog_xml(one, other), "T1")
    b = parse_unload(catalog_xml(other, one), "T1")        # the same positions in another order
    ids = lambda r: {g for g in r.offers if g.startswith("x-")}
    assert ids(a) and ids(a) == ids(b)


def test_a_rollback_to_percents_beyond_the_limit_needs_the_strong_mark(tmp_path):
    from panel_helpers import login, make_app, post
    from tires_sync.settings_store import confirm_draft, create_draft, get_active
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    base_pricing = get_active(conn)[1]["pricing"]
    key = next(iter(base_pricing))
    wide = {**base_pricing, key: {"pct_small": 12.0, "pct_large": 12.0}}
    confirm_draft(conn, create_draft(conn, {"pricing": wide, "pct_limit": 15.0}, NOW), NOW, "owner")      # v2: 12 %
    confirm_draft(conn, create_draft(conn, {"pct_limit": 5.0}, NOW), NOW, "owner", strong=True)            # v3: limit 5 %
    r = post(client, "/history/rollback/2", tok, confirm="on")
    assert get_active(conn)[0] == 3                           # refused: 12 % is above the current limit of 5 %
    post(client, "/history/rollback/2", tok, confirm="on", understand="on")
    assert get_active(conn)[0] == 4


def test_a_keep_ad_without_a_pack4_snapshot_gets_one_backfilled(tmp_path):
    from tires_sync import publishing
    conn, _, _ = env(tmp_path, pack4=True, guard_min=5)
    cycle.run_for_accounts(conn, goods(3), REF, TODAY, tmp_path / "feed")
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    conn.execute("UPDATE published_state SET snapshot4 = NULL")       # as it was before the pack4 snapshots existed
    conn.commit()
    cycle.run_for_accounts(conn, goods(3), REF, TODAY + timedelta(days=1), tmp_path / "feed")
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    assert all(store.get_snapshot4(conn, str(i), 1) for i in (1, 2, 3))


def test_catalog_shows_status_by_the_primary_accounts_own_threshold(tmp_path):
    from panel_helpers import login, make_app
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    connections.create_account(conn, "A", NOW, "owner")
    in_feed = lambda: client.get("/catalog?st=feed").get_data(as_text=True).count('class="gname"')
    assert in_feed() > 0
    connections.set_account_pricing(conn, 1, 0.0, 500, NOW, "owner")
    assert in_feed() == 0


def test_a_held_ad_is_published_from_its_snapshot_without_the_tag_the_category_no_longer_has(tmp_path):
    """Most of the positions vanish from the unload at once: the protection holds them on their saved ads. A snapshot saved before the format fix has <Quantity>."""
    from tires_sync import publishing
    conn, _, _ = env(tmp_path, guard_min=1)
    cycle.run_for_accounts(conn, goods(3), REF, TODAY, tmp_path / "feed")
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    old = store.get_snapshot(conn, "1", 1).replace("<MultiItem>", "<Quantity>за 1 шт.</Quantity><MultiItem>")
    assert "<Quantity>" in old
    conn.execute("UPDATE published_state SET snapshot = ? WHERE goods_id = '1'", (old,))
    conn.commit()
    cycle.run_for_accounts(conn, goods(1, start=3), REF, TODAY + timedelta(days=1), tmp_path / "feed")      # 1 and 2 are gone: the mass-removal protection holds them
    folder = next((tmp_path / "feed").iterdir())
    draft = (folder / "next" / "avito_feed.xml").read_text(encoding="utf-8")
    assert "<Id>1</Id>" in draft and "<Quantity>" not in draft
