from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from tires_sync.avito import (
    AvitoAuthError,
    AvitoClient,
    AvitoError,
    RateLimited,
    UploadTooSoon,
    WriteDisabled,
)


class Fake:
    """Scripted transport: replies are consumed in order, calls are recorded."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        status, payload, extra = self.replies.pop(0)
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return status, {k.lower(): v for k, v in extra.items()}, data


def token(expires=3600, value="tok"):
    return 200, {"access_token": value, "expires_in": expires, "token_type": "Bearer"}, {}


def make(fake, **kw):
    clock = {"t": 1000.0}
    sleeps = []
    client = AvitoClient("cid", "secret", http=fake, now=lambda: clock["t"], sleep=sleeps.append, **kw)
    return client, clock, sleeps


def test_token_is_requested_with_client_credentials_and_cached():
    fake = Fake(token(), (200, {"autoload_enabled": True, "feeds_data": [], "report_email": "", "schedule": []}, {}),
                (200, {"autoload_enabled": True, "feeds_data": [], "report_email": "", "schedule": []}, {}))
    client, _, _ = make(fake)
    client.get_profile()
    client.get_profile()
    assert len(fake.calls) == 3
    method, url, headers, body = fake.calls[0]
    assert (method, url) == ("POST", "https://api.avito.ru/token/")
    assert parse_qs(body.decode()) == {"grant_type": ["client_credentials"], "client_id": ["cid"], "client_secret": ["secret"]}
    assert fake.calls[1][2]["Authorization"] == "Bearer tok"
    assert fake.calls[2][2]["Authorization"] == "Bearer tok"


def test_token_is_reused_while_valid_and_refreshed_near_expiry():
    ok = (200, {"autoload_enabled": False, "feeds_data": [], "report_email": "", "schedule": []}, {})
    fake = Fake(token(expires=100, value="a"), ok, ok, token(expires=100, value="b"), ok)
    client, clock, _ = make(fake)
    client.get_profile()
    clock["t"] += 40   # margin is min(60, 100/2) = 50 s, so the token is still valid
    client.get_profile()
    clock["t"] += 20   # 60 s in: inside the margin, must refresh
    client.get_profile()
    assert [c[2]["Authorization"] for c in fake.calls if c[0] == "GET"] == ["Bearer a", "Bearer a", "Bearer b"]
    assert sum(1 for c in fake.calls if c[1].endswith("/token/")) == 2


def test_short_lived_token_is_not_refetched_on_every_call():
    ok = (200, {"autoload_enabled": False, "feeds_data": [], "report_email": "", "schedule": []}, {})
    fake = Fake(token(expires=30), ok, ok, ok, ok, ok)
    client, clock, _ = make(fake)
    for _ in range(5):
        client.get_profile()
        clock["t"] += 2
    assert sum(1 for c in fake.calls if c[1].endswith("/token/")) == 1


def test_unauthorized_refreshes_the_token_once_then_fails():
    fake = Fake(token(value="a"), (401, {"error": {"code": 401, "message": "bad token"}}, {}),
                token(value="b"), (401, {"error": {"code": 401, "message": "bad token"}}, {}))
    client, _, _ = make(fake)
    with pytest.raises(AvitoAuthError):
        client.get_profile()
    assert sum(1 for c in fake.calls if c[1].endswith("/token/")) == 2


def test_rate_limit_waits_and_retries_then_gives_up():
    limited = (429, {"error": {"code": 429, "message": "slow down"}}, {"Retry-After": "7"})
    fake = Fake(token(), limited, limited, limited, limited)
    client, _, sleeps = make(fake)
    with pytest.raises(RateLimited):
        client.get_profile()
    assert sleeps == [7, 7, 7]


def test_server_errors_are_retried_with_backoff():
    ok = (200, {"autoload_enabled": True, "feeds_data": [], "report_email": "", "schedule": []}, {})
    fake = Fake(token(), (503, {"error": {"code": 503, "message": "down"}}, {}), ok)
    client, _, sleeps = make(fake)
    assert client.get_profile()["autoload_enabled"] is True
    assert len(sleeps) == 1 and 1 <= sleeps[0] <= 1.5            # a growing pause with a random part (reliability.AVITO), never shorter


def test_error_exposes_code_and_message_but_never_the_secret():
    fake = Fake(token(), (400, {"error": {"code": 42, "message": "wrong field"}}, {}))
    client, _, _ = make(fake)
    with pytest.raises(AvitoError) as info:
        client.get_profile()
    assert info.value.status == 400 and info.value.code == 42 and "wrong field" in str(info.value)
    assert "secret" not in str(info.value) and "tok" not in repr(info.value).replace("token", "")


def test_writes_are_disabled_by_default_and_make_no_http_call():
    fake = Fake()
    client, _, _ = make(fake)
    with pytest.raises(WriteDisabled):
        client.start_upload()
    with pytest.raises(WriteDisabled):
        client.set_profile(feed_url="https://x/feed.xml", report_email="", schedule=[])
    assert fake.calls == []


def test_start_upload_posts_when_writes_enabled_and_respects_the_hourly_limit():
    fake = Fake(token(), (200, {}, {}))
    client, _, _ = make(fake, allow_write=True)
    client.start_upload(last_upload_at=1000.0 - 3600)
    assert fake.calls[-1][0:2] == ("POST", "https://api.avito.ru/autoload/v1/upload")
    with pytest.raises(UploadTooSoon):
        client.start_upload(last_upload_at=1000.0 - 3599)
    assert len(fake.calls) == 2


def test_set_profile_sends_feed_schedule_and_flags():
    fake = Fake(token(), (200, {}, {}))
    client, _, _ = make(fake, allow_write=True)
    client.set_profile(feed_url="https://panel.example/feed/avito_feed.xml", feed_name="tires", autoload_enabled=True,
                       report_email="a@b.c", schedule=[{"rate": 100, "time_slots": [3], "weekdays": [0, 1]}])
    method, url, headers, body = fake.calls[-1]
    assert (method, url) == ("POST", "https://api.avito.ru/autoload/v2/profile")
    sent = json.loads(body)
    assert sent["feeds_data"] == [{"feed_name": "tires", "feed_url": "https://panel.example/feed/avito_feed.xml"}]
    assert sent["autoload_enabled"] is True and sent["report_email"] == "a@b.c"
    assert sent["schedule"] == [{"rate": 100, "time_slots": [3], "weekdays": [0, 1]}]
    assert "agreement" not in sent  # the owner accepts the terms in the Avito cabinet


def test_upload_items_pages_through_all_pages_with_filters():
    page1 = {"items": [{"ad_id": "1", "avito_id": 10, "avito_status": "active"}], "meta": {"page": 1, "pages": 2, "perPage": 1, "total": 2}}
    page2 = {"items": [{"ad_id": "2", "avito_id": None, "avito_status": "rejected"}], "meta": {"page": 2, "pages": 2, "perPage": 1, "total": 2}}
    fake = Fake(token(), (200, page1, {}), (200, page2, {}))
    client, _, _ = make(fake)
    items = list(client.upload_items("last_successful", per_page=1, sections="s1,s2", query="1,2"))
    assert [i["ad_id"] for i in items] == ["1", "2"]
    first = urlparse(fake.calls[1][1])
    assert first.path == "/autoload/v4/uploads/last_successful/items"
    assert parse_qs(first.query) == {"perPage": ["1"], "page": ["1"], "sections": ["s1,s2"], "query": ["1,2"]}
    assert parse_qs(urlparse(fake.calls[2][1]).query)["page"] == ["2"]


def test_upload_items_rejects_unknown_upload_kind():
    client, _, _ = make(Fake())
    with pytest.raises(ValueError):
        list(client.upload_items("nope"))


def test_current_and_last_successful_return_none_on_404():
    fake = Fake(token(), (404, {"error": {"code": 404, "message": "no upload"}}, {}),
                (200, {"upload_id": 5, "status": "success", "events": [], "feed_urls": [], "stats": {}, "source": "Url", "started_at": "x"}, {}))
    client, _, _ = make(fake)
    assert client.current_upload() is None
    assert client.last_successful_upload()["status"] == "success"


def test_ad_and_avito_id_mappings_join_ids_and_return_dicts():
    fake = Fake(token(), (200, {"items": [{"ad_id": "A", "avito_id": 7}]}, {}), (200, {"items": [{"ad_id": "A", "avito_id": 7}]}, {}))
    client, _, _ = make(fake)
    assert client.avito_ids_for(["A", "B"]) == {"A": 7}
    assert urlparse(fake.calls[1][1]).path == "/autoload/v2/items/avito_ids"
    assert parse_qs(urlparse(fake.calls[1][1]).query) == {"query": ["A,B"]}
    assert client.ad_ids_for([7]) == {7: "A"}
    assert urlparse(fake.calls[2][1]).path == "/autoload/v2/items/ad_ids"
    assert parse_qs(urlparse(fake.calls[2][1]).query) == {"query": ["7"]}


def test_upload_items_default_page_size_respects_the_api_maximum():
    fake = Fake(token(), (200, {"items": [], "meta": {"page": 1, "pages": 1, "perPage": 100, "total": 0}}, {}))
    client, _, _ = make(fake)
    assert list(client.upload_items()) == []
    assert parse_qs(urlparse(fake.calls[1][1]).query)["perPage"] == ["100"]
    with pytest.raises(ValueError):
        list(client.upload_items(per_page=101))
    with pytest.raises(ValueError):
        list(client.upload_items(per_page=0))


@pytest.mark.parametrize("status,extra", [(503, {}), (502, {}), (429, {"Retry-After": "3"})])
def test_start_upload_is_never_retried(status, extra):
    fake = Fake(token(), (status, {"error": {"code": status, "message": "x"}}, extra))
    client, _, sleeps = make(fake, allow_write=True)
    with pytest.raises(AvitoError):
        client.start_upload(last_upload_at=-10000.0)
    assert len(fake.calls) == 2 and sleeps == []


def test_set_profile_requires_email_and_schedule_so_it_cannot_wipe_them_by_default():
    client, _, _ = make(Fake(), allow_write=True)
    with pytest.raises(TypeError):
        client.set_profile(feed_url="https://x/feed.xml")


@pytest.mark.parametrize("header,expected", [("nan", 5.0), ("inf", 5.0), ("-5", 0.0), ("999999", 300.0), ("abc", 5.0), (None, 5.0), ("2.5", 2.5)])
def test_retry_after_is_sanitised(header, expected):
    extra = {"Retry-After": header} if header is not None else {}
    limited = (429, {"error": {"code": 429, "message": "slow"}}, extra)
    fake = Fake(token(), limited, (200, {}, {}))
    client, _, sleeps = make(fake)
    client.get_profile()
    assert sleeps == [expected]


def test_non_json_success_body_becomes_an_avito_error():
    fake = Fake(token(), (200, b"<html>bad gateway</html>", {}))
    client, _, _ = make(fake)
    with pytest.raises(AvitoError) as info:
        client.get_profile()
    assert info.value.message == "invalid response"


def test_empty_success_body_is_an_empty_dict():
    fake = Fake(token(), (200, b"", {}))
    client, _, _ = make(fake, allow_write=True)
    assert client.start_upload() == {}


def test_bad_token_responses_are_auth_errors_and_server_errors_are_not():
    client, _, _ = make(Fake((200, b"not json", {})))
    with pytest.raises(AvitoAuthError):
        client.get_profile()
    client, _, _ = make(Fake((200, {"nope": 1}, {})))
    with pytest.raises(AvitoAuthError):
        client.get_profile()
    client, _, _ = make(Fake((401, {"error": {"code": 1, "message": "bad secret"}}, {})))
    with pytest.raises(AvitoAuthError) as info:
        client.get_profile()
    assert "secret" not in str(info.value)
    down = (503, {"error": {"code": 9, "message": "down"}}, {})
    fake = Fake(down, down, down)
    client, _, _ = make(fake)
    with pytest.raises(AvitoError) as info:
        client.get_profile()
    assert not isinstance(info.value, AvitoAuthError) and len(fake.calls) == 3     # a token 5xx of a read is repeated like the read (reliability)


def test_server_error_retries_are_bounded():
    down = (503, {"error": {"code": 503, "message": "down"}}, {})
    fake = Fake(token(), down, down, down)
    client, _, sleeps = make(fake)
    with pytest.raises(AvitoError):
        client.get_profile()
    assert len(sleeps) == 2 and 1 <= sleeps[0] <= 1.5 and 2 <= sleeps[1] <= 3 and len(fake.calls) == 4    # the same two repeats as before


def test_unauthorized_then_success_uses_the_new_token():
    ok = (200, {"autoload_enabled": True, "feeds_data": [], "report_email": "", "schedule": []}, {})
    fake = Fake(token(value="a"), (401, {"error": {"code": 401, "message": "expired"}}, {}), token(value="b"), ok)
    client, _, _ = make(fake)
    assert client.get_profile()["autoload_enabled"] is True
    assert fake.calls[-1][2]["Authorization"] == "Bearer b"


def test_start_upload_without_history_is_allowed():
    fake = Fake(token(), (200, {}, {}))
    client, _, _ = make(fake, allow_write=True)
    client.start_upload()
    assert fake.calls[-1][1].endswith("/autoload/v1/upload")


def test_ad_ids_for_tolerates_missing_ad_id():
    fake = Fake(token(), (200, {"items": [{"avito_id": 5}]}, {}))
    client, _, _ = make(fake)
    assert client.ad_ids_for([5]) == {5: None}


def test_repr_and_error_never_contain_credentials():
    client, _, _ = make(Fake(), allow_write=False)
    assert "secret" not in repr(client) and "cid" not in repr(client)


def test_http_base_url_is_rejected_and_redirects_are_not_followed():
    from tires_sync.avito import _NoRedirect

    with pytest.raises(ValueError):
        AvitoClient("a", "b", base_url="http://api.avito.ru")
    assert _NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example/") is None


def test_get_profile_without_retries_fails_fast_on_rate_limit_and_server_errors():
    limited = (429, {"error": {"code": 429, "message": "slow"}}, {"Retry-After": "7"})
    fake = Fake(token(), limited)
    client, _, sleeps = make(fake)
    with pytest.raises(RateLimited):
        client.get_profile(retry=False)
    assert sleeps == [] and len(fake.calls) == 2
    fake = Fake(token(), (503, {"error": {"code": 503, "message": "down"}}, {}))
    client, _, sleeps = make(fake)
    with pytest.raises(AvitoError):
        client.get_profile(retry=False)
    assert sleeps == [] and len(fake.calls) == 2


def test_set_profile_keeps_other_feeds_of_the_profile():
    fake = Fake(token(), (200, {}, {}))
    client, _, _ = make(fake, allow_write=True)
    other = {"feed_name": "other", "feed_url": "https://x.example/o.xml"}
    client.set_profile(feed_url="https://x.example/f.xml", report_email="", schedule=[], other_feeds=[other])
    sent = json.loads(fake.calls[-1][3])
    assert sent["feeds_data"] == [other, {"feed_name": "tires", "feed_url": "https://x.example/f.xml"}]


def test_get_self_returns_the_phone_of_the_cabinet_too():
    fake = Fake(token(), (200, {"id": 1, "name": "n", "phone": "79000000016", "phones": ["79000000016", "78120000000"]}, {}))
    client, _, _ = make(fake)
    assert client.get_self(retry=False) == {"id": 1, "name": "n", "phone": "79000000016", "phones": ["79000000016", "78120000000"]}


def test_get_self_returns_the_cabinet_id_and_name_with_a_plain_read():
    fake = Fake(token(), (200, {"id": 80000001, "name": "Шины СПб", "email": "x@y.z"}, {}))
    client, _, _ = make(fake)
    assert client.get_self(retry=False) == {"id": 80000001, "name": "Шины СПб", "phone": "", "phones": []}
    assert fake.calls[1][0] == "GET" and fake.calls[1][1].endswith("/core/v1/accounts/self")
