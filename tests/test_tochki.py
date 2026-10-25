"""4tochki connection: the form in the panel and the access check (Ping + warehouse list, reads only)."""
from __future__ import annotations

import json

from cryptography.fernet import Fernet

from http_fakes import ScriptedHttp
from panel_helpers import login, make_app, post
from tires_sync import store
from tires_sync.secretbox import SecretBox
from tires_sync.tochki_client import ENDPOINT, TochkiClient, TochkiError

import pytest

NS = "Wcf.ClientService.Client.WebAPI.TS3"


def envelope(inner):
    return ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>%s</s:Body></s:Envelope>' % inner)


PING_TRUE = (200, envelope('<PingResponse xmlns="%s"><PingResult>true</PingResult></PingResponse>' % NS))
PING_FALSE = (200, envelope('<PingResponse xmlns="%s"><PingResult>false</PingResult></PingResponse>' % NS))
WAREHOUSES = (200, envelope('<GetWarehousesResponse xmlns="%s"><warehouses><WarehouseInfo><id>7</id><name>Санкт-Петербург 2</name></WarehouseInfo></warehouses></GetWarehousesResponse>' % NS))
OTHER_WAREHOUSES = (200, envelope('<GetWarehousesResponse xmlns="%s"><warehouses><WarehouseInfo><name>Москва</name></WarehouseInfo></warehouses></GetWarehousesResponse>' % NS))


def test_check_sends_credentials_only_to_the_suppliers_address_and_finds_the_warehouse():
    http = ScriptedHttp(PING_TRUE, WAREHOUSES)
    message = TochkiClient("user<&>", "pa&ss", http=http).check()
    assert "виден" in message
    method, url, headers, body = http.calls[0]
    assert (method, url) == ("POST", ENDPOINT) and url.startswith("https://api-b2b.4tochki.ru/")
    assert headers["SOAPAction"].strip('"').endswith("/ClientService/Ping") and b"user&lt;&amp;&gt;" in body and b"pa&amp;ss" in body
    assert http.calls[1][2]["SOAPAction"].strip('"').endswith("/ClientService/GetWarehouses")


def test_missing_warehouse_is_reported_and_wrong_credentials_stop_before_the_list():
    assert "не найден" in TochkiClient("u", "p", http=ScriptedHttp(PING_TRUE, OTHER_WAREHOUSES)).check()
    http = ScriptedHttp(PING_FALSE)
    with pytest.raises(TochkiError) as err:
        TochkiClient("u", "p-SECRET", http=http).check()
    assert "не приняты" in str(err.value) and "p-SECRET" not in str(err.value) and len(http.calls) == 1


def test_empty_credentials_and_broken_answers_are_explained():
    with pytest.raises(TochkiError):
        TochkiClient("", "", http=ScriptedHttp()).ping()
    down = ScriptedHttp((500, "boom"), (500, "boom"))
    with pytest.raises(TochkiError):
        TochkiClient("u", "p", http=down).ping()
    assert len(down.calls) == 2                                    # a read: one repeat after a fast 5xx (reliability.TOCHKI)
    fault = ScriptedHttp((500, envelope("<s:Fault><faultstring>x</faultstring></s:Fault>")))
    with pytest.raises(TochkiError):
        TochkiClient("u", "p", http=fault).ping()
    assert len(fault.calls) == 1                                   # a SOAP Fault is the server's answer: not repeated
    assert "p-SECRET" not in repr(TochkiClient("u", "p-SECRET"))


def setup(tmp_path, http=None):
    box = SecretBox(Fernet.generate_key())
    extra = {"TOCHKI_HTTP": http} if http else {}
    app = make_app(tmp_path, SECRET_BOX=box, **extra)
    client = app.test_client()
    return app, client, login(client)


def test_panel_saves_login_and_encrypted_password_and_runs_the_check(tmp_path):
    find = (200, envelope('<GetFindTyreResponse xmlns="%s"><price_rest_list><TyrePriceRest><code>2329500</code></TyrePriceRest></price_rest_list></GetFindTyreResponse>' % NS))
    info = (200, envelope('<GetGoodsInfoResponse xmlns="%s"><tyreList><TyreContainer><code>2329500</code></TyreContainer></tyreList></GetGoodsInfoResponse>' % NS))
    http = ScriptedHttp(PING_TRUE, WAREHOUSES, WAREHOUSES, find, info)
    app, client, tok = setup(tmp_path, http)
    assert "4 точки" in client.get("/connections/tochki").get_data(as_text=True)
    post(client, "/connections/tochki", tok, login="api-user", password="p@ss-SECRET-4")
    html = client.get("/connections/tochki").get_data(as_text=True)
    assert "api-user" in html and "p@ss-SECRET-4" not in html and "задан" in html
    conn = store.connect(app.config["DB_PATH"])
    assert "SECRET" not in conn.execute("SELECT ciphertext FROM secret WHERE kind = 'tochki'").fetchone()[0]
    post(client, "/connections/tochki/check", tok)
    assert b"api-user" in http.calls[0][3] and b"p@ss-SECRET-4" in http.calls[0][3]
    assert "виден" in client.get("/connections/tochki").get_data(as_text=True)
    sample = client.get("/connections/tochki/sample").get_data(as_text=True)
    assert "GetFindTyre: HTTP 200" in sample and "2329500" in sample and "p@ss-SECRET-4" not in sample and "api-user" not in sample
    assert b"<a:string>2329500</a:string></code_list>" in http.calls[4][3] and b"Serialization/Arrays" in http.calls[4][3]      # a WCF array, as the WSDL says
    audit = json.dumps(store.list_audit(conn, 30), ensure_ascii=False)
    assert "SECRET" not in audit


def test_panel_page_needs_login(tmp_path):
    app, _, _ = setup(tmp_path)
    assert app.test_client().get("/connections/tochki").status_code == 302
    assert app.test_client().get("/connections/tochki/sample").status_code == 302


def test_post_without_csrf_token_is_refused(tmp_path):
    app, client, _ = setup(tmp_path)
    assert client.post("/connections/tochki/check", data={}).status_code == 400


def test_sample_hides_login_and_password_even_if_the_server_repeats_them():
    echo = (500, envelope("<s:Fault><faultstring>bad user api-user pass p@ss-SECRET</faultstring></s:Fault>"))
    text = TochkiClient("api-user", "p@ss-SECRET", http=ScriptedHttp(echo, echo)).sample()
    assert "api-user" not in text and "p@ss-SECRET" not in text and "***" in text
