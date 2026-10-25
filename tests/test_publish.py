from __future__ import annotations

import json

import pytest

from http_fakes import ScriptedHttp
from tires_sync.avito import AvitoClient, WriteDisabled
from tires_sync.publish import PublishError, ensure_feed, plan_feed, status_report, upload_now

FEED = "https://panel.example/feed/%s/avito_feed.xml" % ("a" * 32)
TOKEN = (200, {"access_token": "t", "expires_in": 3600})


def client(*replies, write=False, now=10_000.0):
    http = ScriptedHttp(TOKEN, *replies)
    return AvitoClient("cid", "secret", http=http, now=lambda: now, sleep=lambda s: None, allow_write=write), http


def profile(feeds=(), enabled=True, email="a@b.c", schedule=()):
    return (200, {"autoload_enabled": enabled, "feeds_data": list(feeds), "report_email": email, "schedule": list(schedule)})


SCHEDULE = [{"rate": 100, "time_slots": [3], "weekdays": [0]}]


def test_plan_says_what_would_change_without_writing():
    c, http = client(profile())
    plan = plan_feed(c, FEED)
    assert plan["action"] == "add" and plan["feed_url"] == FEED
    assert [call[0] for call in http.calls] == ["POST", "GET"]  # token and profile read only


def test_plan_notices_an_already_configured_feed_and_a_changed_one():
    same = {"feed_name": "tires", "feed_url": FEED}
    c, _ = client(profile([same]))
    assert plan_feed(c, FEED)["action"] == "none"
    c, _ = client(profile([{"feed_name": "tires", "feed_url": "https://old.example/f.xml"}]))
    assert plan_feed(c, FEED)["action"] == "replace"


def test_ensure_feed_keeps_email_schedule_and_other_feeds_and_never_sends_agreement():
    other = {"feed_name": "other", "feed_url": "https://x.example/o.xml"}
    c, http = client(profile([other], enabled=False, email="own@mail.ru", schedule=SCHEDULE), (200, {}), write=True)
    ensure_feed(c, FEED)
    method, url, headers, body = http.calls[-1]
    assert (method, url) == ("POST", "https://api.avito.ru/autoload/v2/profile")
    sent = json.loads(body)
    assert sent["feeds_data"] == [other, {"feed_name": "tires", "feed_url": FEED}]
    assert sent["report_email"] == "own@mail.ru" and sent["schedule"] == SCHEDULE
    assert sent["autoload_enabled"] is False and "agreement" not in sent


def test_ensure_feed_does_not_write_when_nothing_changes():
    same = {"feed_name": "tires", "feed_url": FEED}
    c, http = client(profile([same]), write=True)
    assert ensure_feed(c, FEED)["action"] == "none"
    assert [call[0] for call in http.calls] == ["POST", "GET"]


def test_ensure_feed_needs_write_permission():
    c, http = client(profile())
    with pytest.raises(WriteDisabled):
        ensure_feed(c, FEED)


def test_upload_now_starts_one_upload_and_respects_the_hourly_limit():
    c, http = client((200, {}), write=True)
    assert upload_now(c, last_upload_at=10_000.0 - 3600) is True
    assert http.calls[-1][1].endswith("/autoload/v1/upload")
    c, http = client(write=True)
    with pytest.raises(PublishError) as info:
        upload_now(c, last_upload_at=10_000.0 - 60)
    assert "раза в час" in str(info.value) and len(http.calls) == 0


def test_status_report_summarises_the_last_upload_and_lists_rejections():
    upload = {"upload_id": 5, "status": "success_warning", "started_at": "2026-10-10T12:00:00",
              "stats": {"count": 3}, "events": [{"code": 1, "description": "частично", "type": "warning"}]}
    items = {"items": [
        {"ad_id": "1", "avito_id": 11, "avito_status": "active", "messages": []},
        {"ad_id": "2", "avito_id": None, "avito_status": "rejected", "messages": [{"title": "Нет модели", "description": "Укажите модель"}]},
        {"ad_id": "3", "avito_id": 13, "avito_status": "active", "messages": []},
    ], "meta": {"page": 1, "pages": 1, "perPage": 100, "total": 3}}
    c, _ = client((404, {"error": {"code": 404, "message": "none"}}), (200, upload), (200, items))
    report = status_report(c)
    assert report["current"] is None and report["last"]["status"] == "success_warning"
    assert report["counts"] == {"active": 2, "rejected": 1}
    assert report["problems"][0]["ad_id"] == "2" and "Нет модели" in report["problems"][0]["text"]


def test_status_report_without_any_upload_is_empty_not_an_error():
    c, _ = client((404, {"error": {"code": 404, "message": "none"}}), (404, {"error": {"code": 404, "message": "none"}}))
    report = status_report(c)
    assert report == {"current": None, "last": None, "counts": {}, "problems": []}


def feed_xml(count):
    ads = "".join("<Ad><Id>%d</Id></Ad>" % i for i in range(count))
    return ('<Ads formatVersion="3" target="Avito.ru">%s</Ads>' % ads).encode()


def candidate(folder, data):
    (folder / "next").mkdir(exist_ok=True)
    (folder / "next" / "avito_feed.xml").write_bytes(data)


def test_promote_copies_the_candidate_to_the_public_feed(tmp_path):
    from tires_sync.publish import promote_feed
    candidate(tmp_path, feed_xml(3))
    assert promote_feed(tmp_path) == {"new": 3, "old": 0}
    assert (tmp_path / "avito_feed.xml").read_bytes() == feed_xml(3)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["avito_feed.xml", "next"]


def test_promote_refuses_a_feed_that_shrank_by_more_than_half_and_keeps_the_public_one(tmp_path):
    from tires_sync.publish import promote_feed
    (tmp_path / "avito_feed.xml").write_bytes(feed_xml(10))
    candidate(tmp_path, feed_xml(4))
    with pytest.raises(PublishError) as info:
        promote_feed(tmp_path)
    assert "10" in str(info.value) and "4" in str(info.value) and "--allow-shrink" in str(info.value)
    assert (tmp_path / "avito_feed.xml").read_bytes() == feed_xml(10)
    assert promote_feed(tmp_path, allow_shrink=True) == {"new": 4, "old": 10}
    candidate(tmp_path, feed_xml(3))
    assert promote_feed(tmp_path)["new"] == 3  # 4 -> 3 is a small change


@pytest.mark.parametrize("data", [None, b"", b"not xml", b"<Ads></Ads>"])
def test_promote_refuses_a_missing_empty_or_broken_candidate(tmp_path, data):
    from tires_sync.publish import promote_feed
    (tmp_path / "avito_feed.xml").write_bytes(feed_xml(2))
    if data is not None:
        candidate(tmp_path, data)
    with pytest.raises(PublishError):
        promote_feed(tmp_path)
    assert (tmp_path / "avito_feed.xml").read_bytes() == feed_xml(2)


def test_a_link_that_is_in_the_profile_under_another_name_is_not_added_again():
    from tires_sync.publish import plan_feed

    class Client:
        def get_profile(self):
            return {"feeds_data": [{"feed_name": "", "feed_url": "https://x/feed/t/avito_feed.xml"}]}

    assert plan_feed(Client(), "https://x/feed/t/avito_feed.xml")["action"] == "none"
    assert plan_feed(Client(), "https://x/feed/other/avito_feed.xml")["action"] == "add"
