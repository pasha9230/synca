from __future__ import annotations

import json
import os
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Optional, Tuple

from . import reliability
from .transport import Transport, urllib_transport

MODES = ("login", "apikey")


class WebmimError(Exception):
    pass


def _key_from(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace").strip()
    for _ in range(2):
        try:
            value = json.loads(text)
        except ValueError:
            break
        if isinstance(value, str):
            text = value.strip()
        else:
            break
    return text.strip('"').strip()


class WebmimClient:
    def __init__(self, base_url: str, *, mode: str, login: str = "", password: str = "",
                 api_key: str = "", http: Transport = urllib_transport):
        if not base_url.startswith("https://"):
            raise ValueError("base_url must use https")
        if mode not in MODES:
            raise ValueError("mode must be login or apikey")
        self._base = base_url.rstrip("/")
        self._mode = mode
        self._login = login
        self._password = password
        self._api_key = api_key
        self._http = http

    def __repr__(self) -> str:
        return "WebmimClient(mode=%s)" % self._mode

    def _call(self, method: str, path: str, params: Optional[dict] = None,
              headers: Optional[Dict[str, str]] = None, body: Optional[bytes] = None, safe: bool = False) -> Tuple[int, bytes]:
        """`safe`: a read, repeated after a failure (reliability.WEBMIM); the login and the logout are made once."""
        url = self._base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        status, _, raw = reliability.call(lambda: self._http(method, url, headers or {}, body), safe=safe, policy=reliability.WEBMIM,
                                          service=reliability.webmim_service(self._base), read=method == "GET" or path == "/api/v3/auth/login")
        return status, raw

    def api_key(self) -> str:
        if self._mode == "apikey":
            if not self._api_key:
                raise WebmimError("API-ключ не задан.")
            return self._api_key
        status, raw = self._call(
            "POST", "/api/v3/auth/login", headers={"Content-Type": "application/json"},
            body=json.dumps({"login": self._login, "password": self._password}).encode("utf-8"),
        )
        if status != 200:
            raise WebmimError("Вход отклонён (код %s)." % status)
        auth = ""
        limit = False
        try:
            data = json.loads(raw.decode("utf-8"))
            auth = data["authKey"] if data.get("result") else ""
            limit = "количество подключений" in str(data.get("message", ""))
        except (ValueError, KeyError, AttributeError, UnicodeError):
            auth = ""
        if not auth:
            if limit:
                raise WebmimError("Вход отклонён: превышен лимит одновременных сессий WebMIM, повторите позже.")
            raise WebmimError("Вход отклонён: неверный логин или пароль.")
        try:
            status, raw = self._call("GET", "/api/v2/apikey", headers={"Authorization": "Bearer " + auth}, safe=True)
            if status != 200:
                raise WebmimError("API-ключ недоступен (код %s)." % status)
            key = _key_from(raw)
            if not key or not key.isalnum():
                raise WebmimError("Неожиданный формат API-ключа.")
        finally:
            self._logout(auth)
        return key

    def _logout(self, auth: str) -> None:
        """Free the session: the server limits concurrent sessions per user."""
        try:
            self._call("POST", "/api/v3/auth/logout", headers={"Authorization": "Bearer " + auth})
        except OSError:
            pass

    def check(self, territory_id: Optional[str] = None) -> str:
        self.api_key()
        if self._mode == "apikey" and territory_id:
            self.fetch_unload(territory_id)
            return "ключ принят сервером"
        return "ключ получен"

    def fetch_unload(self, territory_id: str) -> bytes:
        if not (str(territory_id).isascii() and str(territory_id).isdigit()):
            raise WebmimError("Идентификатор территории должен состоять из цифр.")
        key = self.api_key()
        status, raw = self._call(
            "GET", "/api/v1/catalog/unload",
            {"access-token": key, "format": "xml", "types": "1", "sterritoryid": territory_id}, safe=True,
        )
        if status != 200:
            raise WebmimError("Выгрузка недоступна (код %s)." % status)
        return raw


def _count_items(xml_bytes: bytes, territory: str = "") -> int:
    nodes = ET.fromstring(xml_bytes).iter("COMMODITY")
    if not territory:
        return sum(1 for _ in nodes)
    return sum(1 for node in nodes if (node.findtext("TERRITORY_RN") or "").strip() == territory)


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o640)
        os.replace(tmp, str(path))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def download_unload(client: WebmimClient, territory_id: str, dest: Path, now: str, min_ratio: float = 0.5) -> dict:
    dest = Path(dest)
    body = client.fetch_unload(territory_id)
    try:
        count = _count_items(body, territory_id)
    except ET.ParseError:
        raise WebmimError("Выгрузка не является корректным XML.") from None
    if count == 0:
        raise WebmimError("В выгрузке нет товаров территории %s." % territory_id)
    previous = None
    if dest.exists():
        try:
            previous = _count_items(dest.read_bytes(), territory_id)
        except (ET.ParseError, OSError):
            previous = None
    if previous and count < previous * min_ratio:
        raise WebmimError("Выгрузка уменьшилась с %d до %d товаров, файл не заменён." % (previous, count))
    _write_atomic(dest, body)
    meta = {"fetched_at": now, "items": count}
    _write_atomic(dest.with_name(dest.name + ".meta.json"), json.dumps(meta).encode("utf-8"))
    return meta
