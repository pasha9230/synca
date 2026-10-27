"""«Забыть пару» / «Это не пара» and their taking back, with the merge on, when the mark changes a published ad (the owner 2026-10-25): the
owner sees «было → стало» first (which Id stays, which comes, the risk of «Повторное размещение»), the mark is written only on the
confirmation of what was shown and only by the role «владелец»; a mark that changes no published ad and the merge off: as before.

Since the 4tochki trial (plan tochki-feed §4б) a mark never brings the 4tochki side out under a new Id: the strict entry keeps it out, the
tire has a WebMIM ad. So the screen is shown where a mark does change a published ad: the pair carries the ad of a WebMIM row that left."""
from __future__ import annotations

import re

import pytest

import test_tire_match_feed_unchanged as golden
from panel_helpers import post
from test_tire_match_feed_unchanged import UNLOAD, UNLOAD_WITHOUT_WEBMIM, prepare
from test_tire_pair_forget import page_app, published_pair, signed_in
from tires_sync import connections, store, sync, users
from tires_sync.cli import main


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    from datetime import datetime
    from tires_sync import cli
    monkeypatch.setattr(cli, "date", golden.GoldenDay)
    monkeypatch.setattr(cli, "moscow_now", lambda: datetime(2026, 10, 25, 3, 0, 0))
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


def counts(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    out = {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in ("tire_pair_forget", "tire_not_pair", "audit")}
    conn.close()
    return out


def digest(html):
    return re.search(r'name="seen" value="([0-9a-f]+)"', html).group(1)


def test_not_a_pair_of_a_merged_published_ad_shows_was_and_will_and_writes_only_on_confirmation(tmp_path):
    carried_by_the_pair(tmp_path)               # 100 | s2-TCH1: the WebMIM ad, Id 100 (№1) and 100_a2 (№2), carried by the pair
    owner = signed_in(page_app(tmp_path), "owner")
    before = counts(tmp_path)
    r = post(owner, "/pairs/not-pair", "tok", a="100", b="s2-TCH1")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Это не пара: 100 | s2-TCH1" in html and "ничего не записано" in html
    assert re.search(r'<td class="t-code">100</td>\s*<td>s2-TCH1[^<]*</td><td><span class="badge badge--warn">уйдёт из фида', html)
    assert '<td class="t-code">100_a2</td>' in html and "новый Id" not in html and "s2-TCH1_a2" not in html     # no 4tochki Id
    assert "№1 «Основной»" in html and "№2 «Партнёр-2»" in html
    assert counts(tmp_path) == before                                       # the screen wrote nothing
    r = post(owner, "/pairs/not-pair", "tok", a="100", b="s2-TCH1", seen="0" * 16)
    assert r.status_code == 200 and "картина изменилась" in r.get_data(as_text=True) and counts(tmp_path) == before
    r = post(owner, "/pairs/not-pair", "tok", a="100", b="s2-TCH1", seen=digest(html))
    assert r.status_code == 302 and r.headers["Location"].endswith("/pairs")
    conn = store.connect(tmp_path / "t.db")
    assert list(store.not_tire_pairs(conn)) == [("100", "s2-TCH1")]
    conn.close()
    assert counts(tmp_path)["audit"] == before["audit"] + 1


def test_taking_the_mark_back_merges_again_and_shows_the_ad_that_comes_back(tmp_path):
    carried_by_the_pair(tmp_path)
    owner = signed_in(page_app(tmp_path), "owner")
    html = post(owner, "/pairs/not-pair", "tok", a="100", b="s2-TCH1").get_data(as_text=True)
    assert post(owner, "/pairs/not-pair", "tok", a="100", b="s2-TCH1", seen=digest(html)).status_code == 302
    before = counts(tmp_path)
    r = post(owner, "/pairs/not-pair", "tok", a="100", b="s2-TCH1", undo="1")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "Это пара (снять запрет): 100 | s2-TCH1" in html and counts(tmp_path) == before
    # the merge carries Id 100 again (the screen names the Id that comes into the feed «новый Id»: it is the WebMIM ad's own)
    assert re.search(r'<td class="t-code">100</td>\s*<td class="muted">объявления нет</td><td><span class="badge badge--warn">новый Id', html)
    assert "s2-TCH1</td>" not in html and 'name="undo" value="1"' in html
    assert post(owner, "/pairs/not-pair", "tok", a="100", b="s2-TCH1", undo="1", seen=digest(html)).status_code == 302
    conn = store.connect(tmp_path / "t.db")
    assert store.not_tire_pairs(conn) == {}
    conn.close()


def carried_by_the_pair(tmp_path):
    """The WebMIM row left after cycle 1 was published, before the next cycle: the memory of the pair is what keeps the ad on Id 100 (after
    that cycle the Id is bound to the 4tochki position, sync.bind_ids, and forgetting the pair changes nothing published)."""
    published_pair(tmp_path)
    (tmp_path / "unload.xml").write_bytes(UNLOAD_WITHOUT_WEBMIM)


def test_after_the_next_cycle_the_bound_id_stays_and_forgetting_is_written_at_once(tmp_path):
    args = published_pair(tmp_path)
    (tmp_path / "unload.xml").write_bytes(UNLOAD_WITHOUT_WEBMIM)
    assert main(["run-cycle"] + args) == 0
    conn = store.connect(tmp_path / "t.db")
    for account in connections.list_accounts(conn):
        store.commit_pending(conn, account["id"], (tmp_path / "feed" / account["feed_token"] / "next" / "avito_feed.xml").read_bytes())
    conn.close()
    r = post(signed_in(page_app(tmp_path), "owner"), "/pairs/forget", "tok", a="100", b="s2-TCH1")
    assert r.status_code == 302 and counts(tmp_path)["tire_pair_forget"] == 1


def test_forgetting_a_pair_that_carries_the_ad_shows_the_new_id_and_its_risk(tmp_path):
    carried_by_the_pair(tmp_path)
    owner = signed_in(page_app(tmp_path), "owner")
    before = counts(tmp_path)
    r = post(owner, "/pairs/forget", "tok", a="100", b="s2-TCH1")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "Забыть пару: 100 | s2-TCH1" in html and counts(tmp_path) == before
    assert re.search(r'<td class="t-code">100</td>\s*<td>s2-TCH1[^<]*</td><td><span class="badge badge--warn">уйдёт из фида', html)
    assert "новый Id" not in html and '<td class="t-code">s2-TCH1</td>' not in html      # the strict entry: no Id of its own
    assert post(owner, "/pairs/forget", "tok", a="100", b="s2-TCH1", seen=digest(html)).status_code == 302
    assert counts(tmp_path)["tire_pair_forget"] == before["tire_pair_forget"] + 1


def test_a_mark_that_changes_no_published_ad_is_written_at_once(tmp_path):
    published_pair(tmp_path)                    # both sides in the unload: the pair found now merges them, the memory is not needed
    owner = signed_in(page_app(tmp_path), "owner")
    r = post(owner, "/pairs/forget", "tok", a="100", b="s2-TCH1")
    assert r.status_code == 302 and counts(tmp_path)["tire_pair_forget"] == 1


def test_with_the_merge_off_every_mark_is_written_at_once_and_builds_no_plan(tmp_path, monkeypatch):
    prepare(tmp_path)
    monkeypatch.setattr(sync, "plan_decisions", lambda *a, **k: pytest.fail("the merge off: no preview"))
    owner = signed_in(page_app(tmp_path), "owner")
    assert post(owner, "/pairs/not-pair", "tok", a="100", b="s2-TCH1").status_code == 302
    assert counts(tmp_path)["tire_not_pair"] == 1


def test_only_the_owner_role_breaks_a_live_pair_even_with_the_right(tmp_path, monkeypatch):
    carried_by_the_pair(tmp_path)
    partner = dict(users.MATRIX["partner"], sections=dict(users.MATRIX["partner"]["sections"], catalog=users.EDIT),
                   rights=users.MATRIX["partner"]["rights"] | {"publish"})
    monkeypatch.setitem(users.MATRIX, "partner", partner)                  # a role given the right to change the feed is still not the owner
    client = signed_in(page_app(tmp_path), "partner")
    before = counts(tmp_path)
    r = post(client, "/pairs/not-pair", "tok", a="100", b="s2-TCH1")
    assert r.status_code == 302 and counts(tmp_path) == before
    assert "только владелец" in client.get("/pairs").get_data(as_text=True)
    (tmp_path / "unload.xml").write_bytes(UNLOAD)                          # both sides here again: forgetting changes no published ad
    assert post(client, "/pairs/forget", "tok", a="100", b="s2-TCH1").status_code == 302     # the right is enough
    assert counts(tmp_path)["tire_pair_forget"] == 1


def test_a_commit_inside_the_plan_cannot_leave_the_trial_mark_in_the_base(tmp_path, monkeypatch):
    """Review 1: the trial mark lives in a temporary copy of the table of this connection only: a commit inside the plan, or a failure after
    one, leaves the base as it was."""
    carried_by_the_pair(tmp_path)
    real = sync.plan_decisions
    calls = []

    def committing(conn, *a, **k):
        conn.commit()
        calls.append(1)
        if len(calls) == 4:                                                # the second account of the plan with the mark: fails
            raise sync.SupplierDataError("broken")
        return real(conn, *a, **k)
    monkeypatch.setattr(sync, "plan_decisions", committing)
    owner = signed_in(page_app(tmp_path), "owner")
    r = post(owner, "/pairs/not-pair", "tok", a="100", b="s2-TCH1")
    assert r.status_code == 302 and len(calls) == 4
    conn = store.connect(tmp_path / "t.db")
    assert store.not_tire_pairs(conn) == {} and conn.execute("SELECT COUNT(*) FROM tire_not_pair").fetchone()[0] == 0
    conn.close()
    calls.clear()
    monkeypatch.setattr(sync, "plan_decisions", lambda conn, *a, **k: (conn.commit(), real(conn, *a, **k))[1])
    html = post(owner, "/pairs/not-pair", "tok", a="100", b="s2-TCH1").get_data(as_text=True)
    assert "уйдёт из фида" in html and counts(tmp_path)["tire_not_pair"] == 0
    assert "картина изменилась" not in owner.get("/pairs").get_data(as_text=True)


def test_the_preview_does_not_hold_the_base_for_writing(tmp_path, monkeypatch):
    """Review 1: the cycle may write while the owner looks at «было → стало»: the trial mark takes no lock of the base."""
    carried_by_the_pair(tmp_path)
    real = sync.plan_decisions
    wrote = []

    def with_a_writer(conn, *a, **k):
        other = store.open_db(tmp_path / "t.db", timeout=0.2)
        other.execute("INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                      ("lane_probe", str(len(wrote))))
        other.commit()
        other.close()
        wrote.append(1)
        return real(conn, *a, **k)
    monkeypatch.setattr(sync, "plan_decisions", with_a_writer)
    r = post(signed_in(page_app(tmp_path), "owner"), "/pairs/not-pair", "tok", a="100", b="s2-TCH1")
    assert r.status_code == 200 and len(wrote) == 4 and counts(tmp_path)["tire_not_pair"] == 0


def test_the_ids_the_screen_shows_are_the_ids_the_cycle_writes(tmp_path):
    """Review 1: «стало» of the screen against the feed of a real cycle after the confirmation."""
    args = published_pair(tmp_path)
    (tmp_path / "unload.xml").write_bytes(UNLOAD_WITHOUT_WEBMIM)            # carried by the pair
    owner = signed_in(page_app(tmp_path), "owner")
    html = post(owner, "/pairs/not-pair", "tok", a="100", b="s2-TCH1").get_data(as_text=True)
    shown = set(re.findall(r'<td class="t-code">([^<]+)</td>', html))
    assert post(owner, "/pairs/not-pair", "tok", a="100", b="s2-TCH1", seen=digest(html)).status_code == 302
    assert main(["run-cycle"] + args) == 0
    conn = store.connect(tmp_path / "t.db")
    built = set()
    for account in connections.list_accounts(conn):
        built |= set(re.findall(r"<Id>([^<]+)</Id>", (tmp_path / "feed" / account["feed_token"] / "next" / "avito_feed.xml").read_text(encoding="utf-8")))
    conn.close()
    assert shown == {"100", "100_a2"} and not shown & built                 # the screen said they leave: they are not in the feeds
    assert not [i for i in built if "TCH1" in i]


def test_a_mark_the_route_refuses_anyway_builds_no_screen(tmp_path, monkeypatch):
    """Review 1: codes of one supplier, one code twice, no code: tire_match refuses them, the screen is not shown first."""
    published_pair(tmp_path)
    owner = signed_in(page_app(tmp_path), "owner")
    monkeypatch.setattr(sync, "plan_decisions", lambda *a, **k: pytest.fail("a refused mark: no preview"))
    for a, b in (("100", "101"), ("100", "100"), ("", "s2-TCH1")):
        assert post(owner, "/pairs/not-pair", "tok", a=a, b=b).status_code == 302, (a, b)
    assert counts(tmp_path)["tire_not_pair"] == 0


def test_the_unload_that_cannot_be_read_stops_a_mark_with_the_merge_on(tmp_path):
    published_pair(tmp_path)
    (tmp_path / "unload.xml").write_bytes(b"<broken")
    owner = signed_in(page_app(tmp_path), "owner")
    r = post(owner, "/pairs/not-pair", "tok", a="100", b="s2-TCH1")
    assert r.status_code == 302 and counts(tmp_path)["tire_not_pair"] == 0
    (tmp_path / "unload.xml").write_bytes(UNLOAD)
