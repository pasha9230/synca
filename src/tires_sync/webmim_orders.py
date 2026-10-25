"""WebMIM API v2 «Управление заказами» (the supplier's official orders API): the access check, the dictionaries, the orders. Reads, except
create_order and add_item (the button «Оформить в WebMIM», webmim_create: the only writer).
Authorization: HTTP Basic with the API user's login and password gives a token for a few hours; the token goes in `Authorization: Bearer`.
Other ways in (the test pair from WebMIM, the unload's key): see OrdersClient."""
from __future__ import annotations

import base64
import json
import urllib.parse
from typing import Dict, Optional, Tuple

from . import reliability
from .transport import Transport, urllib_transport
from .webmim_client import _key_from

# The only servers the API user's login and password may be sent to (a typed address could otherwise carry them anywhere).
SERVERS = {"test": "https://mimtest.svrauto.ru", "prod": "https://webmim.svrauto.ru"}
SERVER_NAMES = {"test": "Тестовый сервер (mimtest)", "prod": "Боевой сервер (webmim)"}
DEFAULT_SERVER = "test"
WRONG_PASSWORD = "неверный логин или пароль"
OPENAPI_PATH = "/api/v3/api/open-api/file/api.v2.openapi.yaml"
DICTIONARIES = (
    ("payment_types", "/api/v2/account/payment-types"),
    ("shipment_types", "/api/v2/account/shipment-types"),
    ("payment_terms", "/api/v2/account/payment-terms"),
    ("addresses", "/api/v2/addresses"),
    ("consignees", "/api/v2/consignee"),
    ("territories", "/api/v2/territories"),
)


class OrdersError(Exception):
    """`status`: the HTTP code of the refused request (0: the login, the client's own checks)."""

    def __init__(self, message: str = "", status: int = 0):
        super().__init__(message)
        self.status = status


class OrdersRefused(OrdersError):
    """WebMIM answered a write and said no (4xx but 408): nothing was made by that request. The message is WebMIM's own, as it is."""


class OrdersClient:
    """Gets in one of two ways (`method` names the one that worked, for the check's message):
    the Basic token of /api/v2/token; when Basic gets 401, the same login and password at /api/v3/auth/login (the way the unload logs in) — its authKey
    goes as Bearer, and when v2 refuses it, the account's key of /api/v2/apikey goes as `access-token`. Logs in at most once per client (two attempts: Basic and the login); the login's session is closed by `close()` (`with`)."""

    def __init__(self, base_url: str, login: str, password: str, http: Transport = urllib_transport):
        if not base_url.startswith("https://"):
            raise ValueError("base_url must use https")
        self._base = base_url.rstrip("/")
        self._login = login
        self._password = password
        self._http = http
        self._token: Optional[str] = None          # goes as Bearer
        self._key: Optional[str] = None            # goes as access-token
        self._session: Optional[str] = None        # the login's authKey, to log out
        self._tried = False
        self._failed = ""
        self._fallback_used = False
        self.method = ""

    def __repr__(self) -> str:
        return "OrdersClient(login=%s)" % ("set" if self._login else "empty")

    def __enter__(self) -> "OrdersClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _call(self, path: str, headers: Dict[str, str], params: Optional[dict] = None, method: str = "GET",
              body: Optional[bytes] = None, safe: bool = False) -> Tuple[int, Dict[str, str], bytes]:
        """`safe`: a read of the data, repeated after a failure (reliability.WEBMIM); the token, the login, the logout and the writes go once."""
        url = self._base + path + (("?" + urllib.parse.urlencode(params)) if params else "")
        return reliability.call(lambda: self._http(method, url, dict(headers, Accept="application/json"), body), safe=safe,
                                policy=reliability.WEBMIM, service=reliability.webmim_service(self._base),
                                read=method == "GET" or path == "/api/v3/auth/login")     # the pause holds the login (no session made), never a write or the logout

    def _authorized(self, path: str, params: Optional[dict] = None) -> Tuple[int, bytes]:
        status, _, raw = self._authorized_full(path, params)
        return status, raw

    def _authorized_full(self, path: str, params: Optional[dict] = None, method: str = "GET",
                         body: Optional[bytes] = None, safe: bool = False) -> Tuple[int, Dict[str, str], bytes]:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if self._key is not None:
            return self._call(path, headers, dict(params or {}, **{"access-token": self._key}), method, body, safe)
        return self._call(path, dict(headers, Authorization="Bearer " + str(self._token)), params, method, body, safe)

    def token(self) -> str:
        if self._token is not None or self._key is not None:
            return self._token or self._key or ""
        if self._failed:
            raise OrdersError(self._failed)
        if self._tried:
            raise OrdersError("Вход не повторяется.")
        if not self._login or not self._password:
            raise OrdersError("Логин или пароль API-пользователя не заданы.")
        self._tried = True
        basic = base64.b64encode(("%s:%s" % (self._login, self._password)).encode("utf-8")).decode("ascii")
        status, _, raw = self._call("/api/v2/token", {"Authorization": "Basic " + basic})
        if status == 200:
            try:
                token = json.loads(raw.decode("utf-8")).get("accessToken") or ""
            except (ValueError, AttributeError, UnicodeError):
                token = ""
            if not token:
                self._failed = "Сервер не вернул токен."
                raise OrdersError(self._failed)
            self._token, self.method = token, "Basic-токен"
            return token
        if status != 401:
            self._failed = "Токен не выдан (код %s)." % status
            raise OrdersError(self._failed)
        auth, reason = self._login_v3()
        if not auth:
            lead = "Логин или пароль не приняты" if reason == WRONG_PASSWORD else "Войти не удалось"
            self._failed = "%s (Basic-токен: 401; вход логином: %s)." % (lead, reason)
            raise OrdersError(self._failed)
        self._session = self._token = auth
        self.method = "вход логином"
        return auth

    def _login_v3(self) -> Tuple[str, str]:
        """(authKey, "") or ("", why not) from /api/v3/auth/login."""
        status, _, raw = self._call("/api/v3/auth/login", {"Content-Type": "application/json"}, method="POST",
                                    body=json.dumps({"login": self._login, "password": self._password}).encode("utf-8"))
        if status != 200:
            return "", "код %s" % status
        try:
            data = json.loads(raw.decode("utf-8"))
            auth = str(data.get("authKey") or "") if data.get("result") else ""
            limit = "количество подключений" in str(data.get("message", ""))
        except (ValueError, AttributeError, UnicodeError):
            return "", "ответ не разобран"
        if auth:
            return auth, ""
        return "", "превышен лимит одновременных сессий, повторите позже" if limit else WRONG_PASSWORD

    def _fallback(self) -> bool:
        """Once per client, after a 401: the login's authKey → the account's key as access-token."""
        if self._fallback_used:
            return False
        self._fallback_used = True
        if not self._session:
            return False
        status, _, raw = self._call("/api/v2/apikey", {"Authorization": "Bearer " + self._session})
        key = _key_from(raw) if status == 200 else ""
        if not key or not key.isalnum():
            return False                                   # the session stays: the other dictionaries may still open with it
        self.close()
        self._token, self._key = None, key
        self.method = "вход логином, ключ API"
        return True

    def close(self) -> None:
        """Free the login's session: the server limits concurrent sessions per user. Errors of the logout itself are ignored."""
        auth, self._session = self._session, None
        if not auth:
            return
        if self._token == auth:
            self._token = None
            self._failed = self._failed or "Сессия закрыта."
        try:
            self._call("/api/v3/auth/logout", {"Authorization": "Bearer " + auth}, method="POST")
        except OSError:
            pass

    def get(self, path: str, params: Optional[dict] = None):
        """A GET with the token; returns the decoded JSON."""
        return self.get_with_headers(path, params)[0]

    def get_with_headers(self, path: str, params: Optional[dict] = None) -> Tuple[object, Dict[str, str]]:
        """A GET with the token: (the decoded JSON, the answer's headers, lower-case; the lists give their pages there)."""
        self.token()
        status, headers, raw = self._authorized_full(path, params, safe=True)
        if status == 401 and self._fallback():
            status, headers, raw = self._authorized_full(path, params, safe=True)
        if status in (401, 403):
            raise OrdersError("%s: вход есть (%s), к заказам нет прав (код %s)." % (path, self.method, status), status)
        if status != 200:
            raise OrdersError("%s: код %s." % (path, status), status)
        try:
            return json.loads(raw.decode("utf-8")), {str(k).lower(): v for k, v in (headers or {}).items()}
        except (ValueError, UnicodeError):
            raise OrdersError("%s: ответ не разобран." % path, status)

    def orders_page(self, page: int, per_page: int = 50) -> Tuple[list, int]:
        """One page of GET /api/v2/orders (ordered by the creation date, the direction is not described): (orders, pages in all; 0: not said)."""
        data, headers = self.get_with_headers("/api/v2/orders", {"page": int(page), "per-page": int(per_page)})
        return (data if isinstance(data, list) else []), _int_header(headers, "x-pagination-page-count")

    def items(self, order_id, per_page: int = 100, max_pages: int = 5) -> list:
        """The positions of a WebMIM order as WebMIM gives them (goodsId, quantity, price, sum, goodsName), every page up to `max_pages`."""
        text = _order_id(order_id)
        rows: list = []
        for page in range(1, max_pages + 1):
            data, headers = self.get_with_headers("/api/v2/orders/%s/items" % text, {"page": page, "per-page": int(per_page)})
            rows.extend(row for row in (data if isinstance(data, list) else []) if isinstance(row, dict))
            if page >= _int_header(headers, "x-pagination-page-count") or not data:
                break
        return rows

    def post(self, path: str, payload: dict):
        """A POST with the token; returns the decoded JSON of 200/201. OrdersRefused: WebMIM said no, nothing made (safe to ask again).
        OrdersError (5xx, 408, an answer not parsed) and OSError (the network): it may have been made — never repeated blindly."""
        self.token()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, _, raw = self._authorized_full(path, method="POST", body=body)
        if status == 401 and self._fallback():               # 401: not let in, nothing made
            status, _, raw = self._authorized_full(path, method="POST", body=body)
        if status in (401, 403):
            raise OrdersRefused("%s: вход есть (%s), к заказам нет прав (код %s)." % (path, self.method, status), status)
        if 400 <= status < 500 and status != 408:
            raise OrdersRefused(_refusal(raw) or "%s: код %s." % (path, status), status)
        if status not in (200, 201):
            raise OrdersError("%s: код %s." % (path, status), status)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise OrdersError("%s: ответ не разобран." % path, status)

    def create_order(self, payload: dict) -> dict:
        """POST /api/v2/orders → the made order (OrderCreated: id, number, statusCode, statusDescription). Without an id in the answer: OrdersError
        (it may be made)."""
        data = self.post("/api/v2/orders", payload)
        order_id = str(data.get("id", "")).strip() if isinstance(data, dict) else ""
        if not order_id.isascii() or not order_id.isdigit() or len(order_id) > 20:
            raise OrdersError("/api/v2/orders: в ответе нет номера заказа.")
        return data

    def add_item(self, order_id, goods_id: str, territory_id: str, quantity: int, stock_type: int = 0) -> dict:
        """POST /api/v2/orders/{id}/items. The same position again adds to its quantity in WebMIM (no new row): read the order's items first."""
        data = self.post("/api/v2/orders/%s/items" % _order_id(order_id),
                         {"goodsId": str(goods_id), "territoryId": str(territory_id), "stockType": int(stock_type), "quantity": int(quantity)})
        return data if isinstance(data, dict) else {}

    def version(self) -> str:
        data = self.get("/api/v2/version")
        return str(data.get("version", "?")) if isinstance(data, dict) else "?"

    def dictionaries(self) -> Dict[str, object]:
        out: Dict[str, object] = {}
        for name, path in DICTIONARIES:
            try:
                out[name] = self.get(path)
            except OrdersError as err:
                out[name] = {"error": str(err)}
        return out

    def order_items(self, order_id) -> Dict[str, float]:
        """The price of one of each position of a WebMIM order (goodsId → price), by the order's id in WebMIM (the «ID заказа WebMIM» of the card,
        not its number). GET /api/v2/orders/{order_id}/items, docs/integrations/webmim-orders-api-v2.md. One page of up to 100 positions."""
        text = str(order_id or "").strip()
        if not text.isascii() or not text.isdigit() or len(text) > 20:
            raise OrdersError("ID заказа WebMIM: только цифры.")
        data = self.get("/api/v2/orders/%s/items" % text, {"limit": 100})
        out: Dict[str, float] = {}
        for row in data if isinstance(data, list) else []:
            price = row.get("price") if isinstance(row, dict) else None
            if isinstance(price, (int, float)) and not isinstance(price, bool) and price > 0 and str(row.get("goodsId", "")).strip():
                out[str(row["goodsId"]).strip()] = float(price)
        return out

    def openapi(self) -> Optional[bytes]:
        """The full description of the API, when the token opens it; None when the supplier keeps it behind the site's own login."""
        self.token()
        status, raw = self._authorized(OPENAPI_PATH)
        return raw if status == 200 and b"openapi" in raw[:300].lower() else None


def _order_id(order_id) -> str:
    text = str(order_id or "").strip()
    if not text.isascii() or not text.isdigit() or len(text) > 20:
        raise OrdersError("ID заказа WebMIM: только цифры.")
    return text


def _refusal(raw: bytes) -> str:
    """The text of WebMIM's 4xx (FieldError {field, message|description}, a list of them, or Error {message}), up to 300 characters."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, AttributeError):
        return ""
    rows = data if isinstance(data, list) else [data]
    texts = []
    for row in rows:
        if isinstance(row, dict):
            text = str(row.get("message") or row.get("description") or "").strip()
            field = str(row.get("field") or "").strip()
            if text:
                texts.append("%s: %s" % (field, text) if field and field not in ("gatewayMessage", "checkMessage") else text)
    return " ".join(" ".join(texts).split())[:300]


def _int_header(headers: Dict[str, str], name: str) -> int:
    try:
        return max(0, int(str(headers.get(name, "")).strip()))
    except ValueError:
        return 0


def summarize(dictionaries: Dict[str, object]) -> str:
    """One short line for the connection status: what the API user can use."""
    def names(value):
        if isinstance(value, dict) and "error" not in value:
            return ", ".join(str(v) for v in value.values())[:60]
        return ""

    def count(value):
        return "ошибка" if isinstance(value, dict) and "error" in value else str(len(value) if hasattr(value, "__len__") else 0)

    parts = []
    shipment = dictionaries.get("shipment_types")
    parts.append("отгрузка: %s" % (names(shipment) or "нет доступа"))
    parts.append("оплата: %s" % (names(dictionaries.get("payment_types")) or "нет доступа"))
    parts.append("адресов %s, получателей %s, территорий %s" % (
        count(dictionaries.get("addresses")), count(dictionaries.get("consignees")), count(dictionaries.get("territories"))))
    return "; ".join(parts)
