from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from helpers import commodity_xml as _commodity_xml
from helpers import catalog_xml, commodity_xml
from http_fakes import ScriptedHttp
from tires_sync.webmim_client import WebmimClient, WebmimError, download_unload

BASE = "https://mimtest.svrauto.ru"
KEY = "A" * 32


def item(**kw):
    return _commodity_xml(**dict({"TERRITORY_RN": "103261489424"}, **kw))


def login_ok():
    return (200, {"result": True, "authKey": "AUTHKEY"})


def test_login_mode_gets_the_api_key_and_unloads_with_it():
    xml = catalog_xml(item())
    http = ScriptedHttp(login_ok(), (200, json.dumps('"%s"' % KEY)), (200, {"result": True}), (200, xml))
    client = WebmimClient(BASE, mode="login", login="user", password="pw", http=http)
    assert client.fetch_unload("103261489424") == xml
    method, url, headers, body = http.calls[0]
    assert (method, url) == ("POST", BASE + "/api/v3/auth/login")
    assert json.loads(body) == {"login": "user", "password": "pw"}
    assert http.calls[1][1] == BASE + "/api/v2/apikey" and http.calls[1][2]["Authorization"] == "Bearer AUTHKEY"
    assert http.calls[2][0:2] == ("POST", BASE + "/api/v3/auth/logout")
    assert http.calls[2][2]["Authorization"] == "Bearer AUTHKEY"
    unload = urlparse(http.calls[3][1])
    assert unload.path == "/api/v1/catalog/unload"
    assert parse_qs(unload.query) == {
        "access-token": [KEY], "format": ["xml"], "types": ["1"], "sterritoryid": ["103261489424"]}


def test_api_key_may_arrive_double_encoded_or_plain():
    for payload in (json.dumps('"%s"' % KEY), json.dumps(KEY), '"%s"' % KEY, KEY):
        http = ScriptedHttp(login_ok(), (200, payload), (200, {"result": True}))
        assert WebmimClient(BASE, mode="login", login="u", password="p", http=http).api_key() == KEY


def test_apikey_mode_skips_the_login():
    http = ScriptedHttp((200, catalog_xml(item())))
    client = WebmimClient(BASE, mode="apikey", api_key=KEY, http=http)
    client.fetch_unload("103261489424")
    assert len(http.calls) == 1 and "access-token=" + KEY in http.calls[0][1]


def test_check_returns_a_short_message_without_downloading():
    http = ScriptedHttp(login_ok(), (200, '"%s"' % KEY), (200, {"result": True}))
    assert WebmimClient(BASE, mode="login", login="u", password="p", http=http).check() == "ключ получен"
    assert len(http.calls) == 3 and http.calls[2][1].endswith("/api/v3/auth/logout")


@pytest.mark.parametrize("replies", [
    [(401, "no")],
    [(200, {"result": False})],
    [(200, "not json")],
    [(200, {"result": True, "authKey": "A"}), (403, "forbidden"), (200, {"result": True})],
    [(200, {"result": True, "authKey": "A"}), (200, '"has spaces in it"'), (200, {"result": True})],
    [(200, {"result": True, "authKey": "A"}), (200, '""'), (200, {"result": True})],
])
def test_bad_login_or_key_raises_webmim_error_without_echoing_secrets(replies):
    http = ScriptedHttp(*replies)
    client = WebmimClient(BASE, mode="login", login="user", password="TOPSECRET", http=http)
    with pytest.raises(WebmimError) as info:
        client.api_key()
    assert "TOPSECRET" not in str(info.value) and "user" not in str(info.value)


def test_error_text_from_the_server_is_never_copied_into_the_exception():
    http = ScriptedHttp(*[(500, "echo TOPSECRET and the key")] * 2)
    client = WebmimClient(BASE, mode="apikey", api_key="TOPSECRET", http=http)
    with pytest.raises(WebmimError) as info:
        client.fetch_unload("103261489424")
    assert "TOPSECRET" not in str(info.value) and "echo" not in str(info.value)
    assert len(http.calls) == 2                                    # the unload is a read: one repeat after a fast 5xx


def test_unload_errors_use_the_status_code_only():
    http = ScriptedHttp((401, "401: Запрос с некорректным ключом"))
    client = WebmimClient(BASE, mode="apikey", api_key=KEY, http=http)
    with pytest.raises(WebmimError) as info:
        client.fetch_unload("103261489424")
    assert "401" in str(info.value) and "ключом" not in str(info.value)


def test_constructor_and_territory_validation():
    with pytest.raises(ValueError):
        WebmimClient("http://mimtest.svrauto.ru", mode="login")
    with pytest.raises(ValueError):
        WebmimClient(BASE, mode="other")
    client = WebmimClient(BASE, mode="apikey", api_key=KEY, http=ScriptedHttp())
    with pytest.raises(WebmimError):
        client.fetch_unload("1; DROP")
    with pytest.raises(WebmimError):
        WebmimClient(BASE, mode="apikey", api_key="", http=ScriptedHttp()).api_key()


def client_for(xml):
    return WebmimClient(BASE, mode="apikey", api_key=KEY, http=ScriptedHttp((200, xml)))


def test_download_writes_file_and_meta_atomically(tmp_path):
    dest = tmp_path / "unload.xml"
    xml = catalog_xml(item(), item(NNOMMODIF="2"))
    meta = download_unload(client_for(xml), "103261489424", dest, "2026-10-10T12:00:00")
    assert meta == {"fetched_at": "2026-10-10T12:00:00", "items": 2}
    assert dest.read_bytes() == xml
    assert json.loads((tmp_path / "unload.xml.meta.json").read_text()) == meta
    assert not (tmp_path / "unload.xml.tmp").exists()


@pytest.mark.parametrize("bad", [b"not xml at all", catalog_xml(), b"<CATALOG></CATALOG>"])
def test_bad_download_keeps_the_previous_file_and_meta(tmp_path, bad):
    dest = tmp_path / "unload.xml"
    good = catalog_xml(item())
    download_unload(client_for(good), "103261489424", dest, "2026-10-10T10:00:00")
    with pytest.raises(WebmimError):
        download_unload(client_for(bad), "103261489424", dest, "2026-10-10T12:00:00")
    assert dest.read_bytes() == good
    assert json.loads((tmp_path / "unload.xml.meta.json").read_text())["fetched_at"] == "2026-10-10T10:00:00"


def test_shrunk_download_is_refused_but_a_small_change_is_accepted(tmp_path):
    dest = tmp_path / "unload.xml"
    ten = catalog_xml(*[item(NNOMMODIF=str(i)) for i in range(10)])
    download_unload(client_for(ten), "103261489424", dest, "2026-10-10T10:00:00")
    four = catalog_xml(*[item(NNOMMODIF=str(i)) for i in range(4)])
    with pytest.raises(WebmimError) as info:
        download_unload(client_for(four), "103261489424", dest, "2026-10-10T12:00:00")
    assert "10" in str(info.value) and "4" in str(info.value)
    assert dest.read_bytes() == ten
    seven = catalog_xml(*[item(NNOMMODIF=str(i)) for i in range(7)])
    assert download_unload(client_for(seven), "103261489424", dest, "2026-10-10T14:00:00")["items"] == 7


def test_apikey_mode_check_with_a_territory_verifies_the_key_on_the_server():
    http = ScriptedHttp((200, catalog_xml(item())))
    client = WebmimClient(BASE, mode="apikey", api_key=KEY, http=http)
    assert client.check("103261489424") == "ключ принят сервером"
    assert len(http.calls) == 1
    rejected = WebmimClient(BASE, mode="apikey", api_key=KEY, http=ScriptedHttp((401, "no")))
    with pytest.raises(WebmimError):
        rejected.check("103261489424")


def test_the_session_is_closed_even_when_the_key_request_fails_and_logout_errors_are_ignored():
    http = ScriptedHttp(login_ok(), (403, "forbidden"), (500, "boom"))
    client = WebmimClient(BASE, mode="login", login="u", password="p", http=http)
    with pytest.raises(WebmimError):
        client.api_key()
    assert http.calls[-1][1].endswith("/api/v3/auth/logout")

    flaky = ScriptedHttp(login_ok(), (200, '"%s"' % KEY))

    def transport(method, url, headers, body):
        if url.endswith("/api/v3/auth/logout"):
            raise OSError("network is down")
        return flaky(method, url, headers, body)

    assert WebmimClient(BASE, mode="login", login="u", password="p", http=transport).api_key() == KEY


def test_session_limit_is_reported_as_such_not_as_a_wrong_password():
    http = ScriptedHttp((200, {"result": False, "message": "Пользователь превысил максимальное количество подключений"}))
    client = WebmimClient(BASE, mode="login", login="user", password="TOPSECRET", http=http)
    with pytest.raises(WebmimError) as info:
        client.api_key()
    assert "лимит одновременных сессий" in str(info.value)
    assert "неверный логин" not in str(info.value) and "TOPSECRET" not in str(info.value)


def test_download_uses_a_unique_temp_file_so_parallel_runs_cannot_collide(tmp_path):
    dest = tmp_path / "unload.xml"
    stale = tmp_path / "unload.xml.tmp"
    stale.write_text("another run is writing here")
    xml = catalog_xml(item())
    download_unload(client_for(xml), "103261489424", dest, "2026-10-10T12:00:00")
    assert dest.read_bytes() == xml
    assert stale.read_text() == "another run is writing here"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["unload.xml", "unload.xml.meta.json", "unload.xml.tmp"]
