from __future__ import annotations

import json
import random
import re
import uuid
import time
import urllib.parse
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from . import reliability
from .transport import Transport, _NoRedirect, urllib_transport  # noqa: F401

BASE_URL = "https://api.avito.ru"
TOKEN_URL = BASE_URL + "/token/"
UPLOAD_KINDS = ("current", "last_successful")
MIN_UPLOAD_INTERVAL = 3600
TOKEN_MARGIN = 60
MAX_PER_PAGE = 100
STOCK_INFO_CHUNK = 10        # stock-management/1/info: ads per request (the text of the method; its schema says 500)
STOCK_SET_CHUNK = 200        # stock-management/1/stocks: ads per request
STOCK_MAX = 999999
ERROR_BODY = 500             # bytes of an error answer kept in AvitoError.body

class AvitoError(Exception):
    def __init__(self, status: int, code: Optional[int], message: str, body: str = ""):
        super().__init__("Avito API error %s (code %s): %s" % (status, code, message))
        self.status = status
        self.code = code
        self.message = message
        self.body = body                    # the start of Avito's answer as it came (a body that is not its JSON leaves no other trace)


class AvitoAuthError(AvitoError):
    pass


class RateLimited(AvitoError):
    pass


class WriteDisabled(Exception):
    pass


class UploadTooSoon(Exception):
    pass


def _error_from(status: int, headers: Dict[str, str], body: bytes) -> AvitoError:
    code: Optional[int] = None
    message = "HTTP %s" % status
    try:
        data = json.loads(body.decode("utf-8"))
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            code = err.get("code")
            message = str(err.get("message") or message)
        elif isinstance(data, dict) and data.get("message"):
            message = str(data["message"])
        if code is None and isinstance(data, dict) and isinstance(data.get("code"), int):     # the calltracking answers {"code", "message"} bare
            code = data["code"]
    except (ValueError, UnicodeDecodeError):
        pass
    text = " ".join((body or b"")[:ERROR_BODY].decode("utf-8", "replace").split())
    if status == 401:
        return AvitoAuthError(status, code, message, text)
    if status == 429:
        return RateLimited(status, code, message, text)
    return AvitoError(status, code, message, text)


class _TokenDown(Exception):
    """The token request got a 5xx: inside _request it is that request's answer (repeated and counted by the breaker like it)."""

    def __init__(self, answer):
        super().__init__("token request: HTTP %s" % answer[0])
        self.answer = answer


class AvitoClient:
    """Read-only by default: anything that changes Avito state needs allow_write=True."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        http: Transport = urllib_transport,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
        allow_write: bool = False,
        base_url: str = BASE_URL,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._now = now
        self._sleep = sleep
        self._rand = rand
        self.allow_write = allow_write
        if not base_url.startswith("https://"):
            raise ValueError("base_url must use https")
        self._base = base_url.rstrip("/")
        self._token: Optional[str] = None
        self._token_until = 0.0

    def __repr__(self) -> str:
        return "AvitoClient(allow_write=%s)" % self.allow_write

    def _fetch_token(self) -> str:
        body = urllib.parse.urlencode(
            {"grant_type": "client_credentials", "client_id": self._client_id, "client_secret": self._client_secret}
        ).encode()
        status, headers, raw = self._http(
            "POST", self._base + "/token/", {"Content-Type": "application/x-www-form-urlencoded"}, body
        )
        if status != 200:
            err = _error_from(status, headers, raw)
            if status in (400, 401, 403):
                raise AvitoAuthError(status, err.code, "token request rejected")
            if status >= 500:
                raise _TokenDown((status, headers, raw))
            raise err
        try:
            data = json.loads(raw.decode("utf-8"))
            token = data["access_token"]
            expires = float(data.get("expires_in", 0))
        except (ValueError, KeyError, TypeError, AttributeError):
            raise AvitoAuthError(status, None, "invalid token response")
        self._token = token
        self._token_until = self._now() + expires - min(TOKEN_MARGIN, expires / 2)
        return token

    def _bearer(self) -> str:
        if self._token is None or self._now() >= self._token_until:
            self._fetch_token()
        return self._token  # type: ignore[return-value]

    def _request(self, method: str, path: str, params: Optional[dict] = None, payload: Optional[dict] = None, retry: bool = True,
                 form: Optional[tuple] = None, binary: bool = False, safe: bool = False):
        """`form` is (content type, bytes) for a multipart upload; otherwise the payload goes as JSON. `binary`: the answer is a file (the bytes).
        Repeated (reliability.AVITO) only with `retry` and when a GET or marked `safe` (a POST that only reads); `retry=False`: one attempt, no wait."""
        url = self._base + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        body = json.dumps(payload).encode("utf-8") if payload is not None else (form[1] if form else None)

        def attempt():
            try:
                bearer = self._bearer()
            except _TokenDown as down:
                return down.answer
            headers = {"Authorization": "Bearer " + bearer, "Accept": "*/*" if binary else "application/json"}
            if body is not None:
                headers["Content-Type"] = form[0] if form else "application/json"
            return self._http(method, url, headers, body)

        for refreshed in (False, True):
            status, resp_headers, raw = reliability.call(attempt, safe=retry and (method == "GET" or safe), read=method == "GET" or safe,
                                                         policy=reliability.AVITO, service="avito", sleep=self._sleep, rand=self._rand)
            if status == 401 and not refreshed:
                self._token = None
                continue
            break
        if binary and 200 <= status < 300:
            kind = str(resp_headers.get("content-type", "")).lower()
            if "json" in kind or raw.lstrip()[:1] == b"{":                 # an error inside a 200 is never taken for the file
                err = _error_from(status, resp_headers, raw)
                raise AvitoError(status, err.code, err.message)
            return raw
        if 200 <= status < 300:
            if not raw.strip():
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise AvitoError(status, None, "invalid response")
        raise _error_from(status, resp_headers, raw)

    def _require_write(self) -> None:
        if not self.allow_write:
            raise WriteDisabled("this client is read-only; create it with allow_write=True to change Avito state")

    def get_self(self, retry: bool = True) -> dict:
        """The cabinet the keys belong to: its Avito user id, name and main phone (a plain read)."""
        data = self._request("GET", "/core/v1/accounts/self", retry=retry)
        phones = data.get("phones")
        return {"id": data.get("id"), "name": data.get("name"), "phone": str(data.get("phone") or ""),
                "phones": [str(p) for p in phones] if isinstance(phones, list) else []}

    def get_profile(self, retry: bool = True) -> dict:
        return self._request("GET", "/autoload/v2/profile", retry=retry)

    def set_profile(
        self,
        *,
        feed_url: str,
        feed_name: str = "tires",
        autoload_enabled: bool = True,
        report_email: str,
        schedule: List[dict],
        other_feeds: Optional[List[dict]] = None,
    ) -> dict:
        self._require_write()
        payload = {
            "autoload_enabled": autoload_enabled,
            "feeds_data": list(other_feeds or []) + [{"feed_name": feed_name, "feed_url": feed_url}],
            "report_email": report_email,
            "schedule": schedule,
        }
        return self._request("POST", "/autoload/v2/profile", payload=payload, safe=True)   # the whole profile replaced: again gives the same

    def start_upload(self, last_upload_at: Optional[float] = None) -> dict:
        self._require_write()
        if last_upload_at is not None and self._now() - last_upload_at < MIN_UPLOAD_INTERVAL:
            raise UploadTooSoon("an upload was started less than an hour ago")
        return self._request("POST", "/autoload/v1/upload", retry=False)

    def _optional(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        try:
            return self._request("GET", path, params)
        except AvitoError as err:
            if err.status == 404:
                return None
            raise

    def current_upload(self) -> Optional[dict]:
        return self._optional("/autoload/v4/uploads/current")

    def last_successful_upload(self) -> Optional[dict]:
        return self._optional("/autoload/v4/uploads/last_successful")

    def uploads(self, page: int = 1, per_page: int = 25, date_from: Optional[str] = None, date_to: Optional[str] = None) -> dict:
        return self._request(
            "GET", "/autoload/v4/uploads",
            {"perPage": per_page, "page": page, "dateFrom": date_from, "dateTo": date_to},
        )

    def upload_items(
        self, kind: str = "last_successful", per_page: int = MAX_PER_PAGE, sections: Optional[str] = None, query: Optional[str] = None
    ) -> Iterator[dict]:
        if kind not in UPLOAD_KINDS:
            raise ValueError("kind must be one of %s" % (UPLOAD_KINDS,))
        if not 1 <= per_page <= MAX_PER_PAGE:
            raise ValueError("per_page must be between 1 and %d" % MAX_PER_PAGE)
        page = 1
        while True:
            data = self._request(
                "GET", "/autoload/v4/uploads/%s/items" % kind,
                {"perPage": per_page, "page": page, "sections": sections, "query": query},
            )
            for item in data.get("items", []):
                yield item
            meta = data.get("meta") or {}
            if page >= int(meta.get("pages", page)):
                return
            page += 1

    def items(self, status: str = "active", per_page: int = MAX_PER_PAGE, retry: bool = True) -> Iterator[dict]:
        """Our ads on Avito (a plain read): id, title, price, status."""
        page = 1
        while True:
            data = self._request("GET", "/core/v1/items", {"per_page": per_page, "page": page, "status": status}, retry=retry)
            resources = data.get("resources", [])
            for item in resources:
                yield item
            if len(resources) < per_page:
                return
            page += 1

    def chats(self, user_id, limit: int = 50, offset: int = 0, retry: bool = True) -> dict:
        """The account's chats, newest activity first (a plain read)."""
        return self._request("GET", "/messenger/v2/accounts/%s/chats" % user_id, {"limit": limit, "offset": offset}, retry=retry)

    def send_message(self, user_id, chat_id: str, text: str) -> dict:
        """A text message into a chat (a write, so only a client made with allow_write; never retried: a retry could send it twice)."""
        self._require_write()
        return self._request("POST", "/messenger/v1/accounts/%s/chats/%s/messages" % (user_id, urllib.parse.quote(str(chat_id), safe="")),
                             payload={"message": {"text": text}, "type": "text"}, retry=False)

    def upload_image(self, user_id, filename: str, data: bytes) -> str:
        """Put a picture into the messenger of the cabinet; returns the id Avito gives it (a write: never retried)."""
        self._require_write()
        boundary = "----tiressync" + uuid.uuid4().hex
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(filename or "photo.jpg"))[:80] or "photo.jpg"
        body = b"".join([("--%s\r\nContent-Disposition: form-data; name=\"uploadfile[]\"; filename=\"%s\"\r\nContent-Type: image/jpeg\r\n\r\n" % (boundary, safe)).encode("ascii"),
                         data, ("\r\n--%s--\r\n" % boundary).encode("ascii")])
        answer = self._request("POST", "/messenger/v1/accounts/%s/uploadImages" % user_id, retry=False, form=("multipart/form-data; boundary=" + boundary, body))
        if not isinstance(answer, dict) or len(answer) != 1 or not str(next(iter(answer))).strip():
            raise AvitoError(200, None, "invalid response")
        return str(next(iter(answer)))

    def send_image(self, user_id, chat_id: str, image_id: str) -> dict:
        """A picture already uploaded, into a chat (a write; never retried: a retry could send it twice)."""
        self._require_write()
        return self._request("POST", "/messenger/v1/accounts/%s/chats/%s/messages/image" % (user_id, urllib.parse.quote(str(chat_id), safe="")),
                             payload={"image_id": str(image_id)}, retry=False)

    def chat_messages(self, user_id, chat_id: str, limit: int = 100, offset: int = 0, retry: bool = True) -> List[dict]:
        """Messages of one chat (a plain read). Avito answers 402 for chats its messenger API subscription does not cover."""
        data = self._request("GET", "/messenger/v3/accounts/%s/chats/%s/messages/" % (user_id, urllib.parse.quote(str(chat_id), safe="")),
                             {"limit": limit, "offset": offset}, retry=retry)
        return data.get("messages", []) if isinstance(data, dict) else list(data or [])

    def item_stats(self, user_id: int, avito_ids: List[int], date_from: str, date_to: str) -> List[dict]:
        """Views, contacts and favourites per ad and day (a read, although Avito wants a POST for it)."""
        data = self._request("POST", "/stats/v1/accounts/%s/items" % user_id, safe=True, payload={
            "itemIds": list(avito_ids), "dateFrom": date_from, "dateTo": date_to,
            "fields": ["uniqViews", "uniqContacts", "uniqFavorites"], "periodGrouping": "day"})
        return (data.get("result") or {}).get("items", [])

    def stocks_info(self, avito_ids: List[int], retry: bool = True) -> List[dict]:
        """What Avito shows as the stock of these ads, at most STOCK_INFO_CHUNK (a read, although Avito wants a POST): item_id, quantity
        (less Avito's own reservations), is_multiple, is_unlimited, is_out_of_stock. One unknown id fails the whole request (400)."""
        if not 1 <= len(avito_ids) <= STOCK_INFO_CHUNK:
            raise ValueError("stocks_info takes 1..%d ads" % STOCK_INFO_CHUNK)
        data = self._request("POST", "/stock-management/1/info", safe=True, retry=retry,
                             payload={"item_ids": [int(i) for i in avito_ids], "strong_consistency": True})
        return list(data.get("stocks") or []) if isinstance(data, dict) else []

    def set_stocks(self, stocks: List[Tuple[int, int]]) -> List[dict]:
        """Set the number in stock of these ads, (item_id, quantity) pairs, at most STOCK_SET_CHUNK. A write: made once whatever the answer
        (never retried). Avito answers per ad: item_id, success, errors."""
        self._require_write()
        if not 1 <= len(stocks) <= STOCK_SET_CHUNK:
            raise ValueError("set_stocks takes 1..%d ads" % STOCK_SET_CHUNK)
        if any(not 0 <= int(q) <= STOCK_MAX for _, q in stocks):
            raise ValueError("a quantity is outside 0..%d" % STOCK_MAX)
        data = self._request("PUT", "/stock-management/1/stocks", retry=False,
                             payload={"stocks": [{"item_id": int(i), "quantity": int(q)} for i, q in stocks]})
        if not isinstance(data, dict) or not isinstance(data.get("stocks"), list):
            raise AvitoError(200, None, "invalid response")
        return data["stocks"]

    def avito_ids_for(self, ad_ids: List[str]) -> Dict[str, Optional[int]]:
        data = self._request("GET", "/autoload/v2/items/avito_ids", {"query": ",".join(str(i) for i in ad_ids)})
        return {item["ad_id"]: item.get("avito_id") for item in data.get("items", [])}

    def ad_ids_for(self, avito_ids: List[int], retry: bool = True) -> Dict[int, Optional[str]]:
        data = self._request("GET", "/autoload/v2/items/ad_ids", {"query": ",".join(str(i) for i in avito_ids)}, retry=retry)
        return {item["avito_id"]: item.get("ad_id") for item in data.get("items", [])}

    def calls(self, date_from: str, date_to: str, limit: int = MAX_PER_PAGE, offset: int = 0, retry: bool = True) -> List[dict]:
        """The calls through the account's substitute numbers (calltracking) between two RFC 3339 moments, a page of at most 100 (a read,
        although Avito wants a POST). Avito may answer 200 with an error inside: that is an AvitoError too."""
        data = self._request("POST", "/calltracking/v1/getCalls/", safe=True, payload={"dateTimeFrom": date_from, "dateTimeTo": date_to,
                                                                            "limit": limit, "offset": offset}, retry=retry)
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict) and err.get("code"):
            raise AvitoError(200, err.get("code"), str(err.get("message") or "calltracking error"))
        return list(data.get("calls") or []) if isinstance(data, dict) else []

    def call_record(self, call_id: int, retry: bool = True) -> bytes:
        """The recording of one call (audio/mpeg bytes). Not ready yet: AvitoError 425 or code 1005; no access to the records: 403 code 1002."""
        return self._request("GET", "/calltracking/v1/getRecordByCallId/", {"callId": int(call_id)}, retry=retry, binary=True)
