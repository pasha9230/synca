"""Encrypted copy of the database outside the server (the owner's decision of 2026-10-21): once a day the consistent gzipped
copy of `backup.backup_db` is sealed with the backup key (AES-256-GCM, a key of its own, never the master key), put into an
S3-compatible storage (Yandex Object Storage), the newest `keep` copies are retained, and the copy just sent is downloaded
back, opened and checked. The storage is spoken to with plain HTTP signed by AWS Signature V4 (no S3 library)."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import http.client
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .backup import backup_db, verify_backup
from .transport import _OPENER, MAX_BODY, Transport


class OffsiteError(Exception):
    pass


REQUIRED = ("S3_ENDPOINT", "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "BACKUP_KEY_FILE")
DEFAULTS = {"S3_REGION": "ru-central1", "S3_PREFIX": "tires-sync/", "KEEP": "14"}
MAGIC = b"TSBACKUP1\n"                       # the file format; also authenticated, so a file of another kind never opens
NONCE = 12
NAME = re.compile(r"^state-\d{4}-\d{2}-\d{2}\.db\.gz\.enc$")
BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
PREFIX = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*/$")
EMPTY_SHA = hashlib.sha256(b"").hexdigest()


def read_config(path) -> Optional[dict]:
    """The settings file of the server (KEY=VALUE lines), or None when there is no file: then the copy is not set up."""
    path = Path(path)
    if not path.exists():
        return None
    raw = dict(DEFAULTS)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as err:
        raise OffsiteError("файл настроек копии не читается: %s" % type(err).__name__) from None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        raw[key.strip()] = value
    missing = [key for key in REQUIRED if not raw.get(key)]
    if missing:
        raise OffsiteError("в файле настроек копии не заполнены: %s" % ", ".join(missing))
    endpoint = raw["S3_ENDPOINT"].rstrip("/")
    parts = urllib.parse.urlsplit(endpoint)
    if parts.scheme != "https" or not parts.hostname or parts.path or parts.query or parts.username:
        raise OffsiteError("S3_ENDPOINT: нужен адрес вида https://storage.yandexcloud.net")   # keys never go over plain http
    if not BUCKET.match(raw["S3_BUCKET"]):
        raise OffsiteError("S3_BUCKET: имя бакета из строчных латинских букв, цифр, точек и дефисов")
    prefix = raw["S3_PREFIX"].strip()
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    if prefix and (not PREFIX.match(prefix) or ".." in prefix.split("/")):
        raise OffsiteError("S3_PREFIX: папка из латинских букв, цифр, точек, дефисов и «/»")
    if not re.match(r"^[a-z0-9-]{1,40}$", raw["S3_REGION"]):
        raise OffsiteError("S3_REGION: например ru-central1")
    try:
        keep = int(raw["KEEP"])
    except ValueError:
        keep = 0
    if not 1 <= keep <= 365:
        raise OffsiteError("KEEP: сколько копий хранить, от 1 до 365")
    return {"endpoint": endpoint, "bucket": raw["S3_BUCKET"], "region": raw["S3_REGION"], "prefix": prefix, "keep": keep,
            "access_key": raw["S3_ACCESS_KEY_ID"], "secret_key": raw["S3_SECRET_ACCESS_KEY"], "key_file": raw["BACKUP_KEY_FILE"]}


def new_key_file(path) -> None:
    """A new backup key: 32 random bytes, base64, in a file only its owner reads. An existing file is never overwritten."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(base64.b64encode(os.urandom(32)).decode("ascii") + "\n")


def load_key(path) -> bytes:
    try:
        text = Path(path).read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as err:
        raise OffsiteError("ключ копий не читается (%s): %s" % (path, type(err).__name__)) from None
    try:
        key = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        key = b""
    if len(key) != 32:
        raise OffsiteError("ключ копий в %s неверный: нужны 32 байта в base64 (команда backup-key-new)" % path)   # the key itself is never printed
    return key


def seal(data: bytes, key: bytes) -> bytes:
    nonce = os.urandom(NONCE)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, data, MAGIC)


def open_sealed(blob: bytes, key: bytes) -> bytes:
    if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + NONCE + 16:
        raise OffsiteError("это не зашифрованная копия базы Tires Sync")
    nonce = blob[len(MAGIC):len(MAGIC) + NONCE]
    try:
        return AESGCM(key).decrypt(nonce, blob[len(MAGIC) + NONCE:], MAGIC)
    except InvalidTag:
        raise OffsiteError("копия не расшифровывается: другой ключ или файл повреждён") from None


def _quote(text: str, safe: str = "-_.~") -> str:
    return urllib.parse.quote(text, safe=safe)


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()


def sign(method: str, url: str, headers: Dict[str, str], payload_sha: str, access_key: str, secret_key: str,
         region: str, amz_date: str, service: str = "s3") -> str:
    """The Authorization header of AWS Signature V4. The path of `url` is already encoded (S3 does not encode it twice);
    every header in `headers` is signed."""
    parts = urllib.parse.urlsplit(url)
    query = sorted((_quote(k), _quote(v)) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    lowered = {k.strip().lower(): " ".join(str(v).split()) for k, v in headers.items()}
    names = sorted(lowered)
    canonical = "\n".join([method, parts.path or "/", "&".join("%s=%s" % pair for pair in query),
                           "".join("%s:%s\n" % (name, lowered[name]) for name in names), ";".join(names), payload_sha])
    scope = "%s/%s/%s/aws4_request" % (amz_date[:8], region, service)
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode("utf-8"), amz_date[:8]), region), service), "aws4_request")
    signature = hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s" % (access_key, scope, ";".join(names), signature)


STORAGE_TIMEOUT = 900


def storage_transport(method: str, url, headers, body):
    """The usual transport (no redirects, the same answers and failures) with a long timeout: the timeout of a socket covers
    the whole sending of the body, so the 30 seconds of `urllib_transport` would cap the size of the copy."""
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with _OPENER.open(request, timeout=STORAGE_TIMEOUT) as response:
            answer = response.read(MAX_BODY + 1)
            if len(answer) > MAX_BODY:
                raise OSError("ответ хранилища слишком большой")
            return response.status, {k.lower(): v for k, v in response.headers.items()}, answer
    except urllib.error.HTTPError as err:
        try:
            return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read(MAX_BODY)
        except http.client.HTTPException as cut:
            raise OSError("обрыв ответа хранилища (%s)" % type(cut).__name__) from None
    except http.client.HTTPException as cut:
        raise OSError("обрыв ответа хранилища (%s)" % type(cut).__name__) from None


def _error_code(body: bytes) -> str:
    """The short code of an S3 error (AccessDenied, NoSuchBucket…): safe to show, the rest of the answer is not shown."""
    match = re.search(rb"<Code>([A-Za-z]{1,64})</Code>", body or b"")
    return match.group(1).decode("ascii") if match else "без кода"


class Storage:
    def __init__(self, cfg: dict, http: Transport = storage_transport, clock: Callable[[], float] = time.time):
        self.cfg, self.http, self.clock = cfg, http, clock
        self.host = urllib.parse.urlsplit(cfg["endpoint"]).netloc

    def _call(self, method: str, key: str = "", query: Optional[List[tuple]] = None, body: bytes = b"",
              ok=(200,)) -> bytes:
        path = "/" + self.cfg["bucket"] + ("/" + _quote(key, safe="/-_.~") if key else "")
        url = self.cfg["endpoint"] + path + ("?" + "&".join("%s=%s" % (_quote(k), _quote(v)) for k, v in query) if query else "")
        payload_sha = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA
        amz_date = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.clock()))
        headers = {"Host": self.host, "x-amz-date": amz_date, "x-amz-content-sha256": payload_sha}
        if method == "PUT":
            headers["Content-Type"] = "application/octet-stream"
        headers["Authorization"] = sign(method, url, headers, payload_sha, self.cfg["access_key"], self.cfg["secret_key"],
                                        self.cfg["region"], amz_date)
        status, _, answer = self.http(method, url, headers, body if method == "PUT" else None)
        if status not in ok:
            raise OffsiteError("хранилище ответило %d (%s) на %s %s" % (status, _error_code(answer), method, key or "список"))
        return answer

    def put(self, key: str, data: bytes) -> None:
        self._call("PUT", key, body=data)

    def get(self, key: str) -> bytes:
        return self._call("GET", key)

    def delete(self, key: str) -> None:
        self._call("DELETE", key, ok=(200, 204))

    def list(self, prefix: str) -> List[str]:
        keys: List[str] = []
        token = ""
        for _ in range(100):                                                 # 1000 names a page; a copy a day never needs more
            query = [("list-type", "2"), ("prefix", prefix)] + ([("continuation-token", token)] if token else [])
            try:
                root = ET.fromstring(self._call("GET", query=query))
            except ET.ParseError:
                raise OffsiteError("хранилище вернуло непонятный список файлов") from None
            for element in root.iter():
                if element.tag.rsplit("}", 1)[-1] == "Key" and element.text:
                    keys.append(element.text)
            truncated = next((e.text for e in root.iter() if e.tag.rsplit("}", 1)[-1] == "IsTruncated"), "false")
            token = next((e.text or "" for e in root.iter() if e.tag.rsplit("}", 1)[-1] == "NextContinuationToken"), "")
            if truncated != "true" or not token:
                return keys
        raise OffsiteError("список файлов в хранилище не кончается")


def copy_name(cfg: dict, day: date) -> str:
    return cfg["prefix"] + "state-%s.db.gz.enc" % day.isoformat()


def _check_opened(gz_bytes: bytes, workdir: str) -> int:
    """The decrypted copy as a database: integrity check passes. Returns the number of its tables."""
    path = Path(workdir) / "check.db.gz"
    path.write_bytes(gz_bytes)
    try:
        return verify_backup(path)["tables"]
    finally:
        path.unlink()


def send_copy(db_path, cfg: dict, today: date, http: Transport = storage_transport, clock: Callable[[], float] = time.time) -> dict:
    """Snapshot, seal, upload, read back and check, then drop the copies beyond `keep`. Old copies are dropped only after
    the new one is proven readable, and never the one just sent."""
    key = load_key(cfg["key_file"])                                          # a missing key stops the run before any work
    storage = Storage(cfg, http, clock)
    with tempfile.TemporaryDirectory() as workdir:
        packed = backup_db(db_path, workdir, today, keep=1)                  # consistent, gzipped and already checked
        data = packed.read_bytes()
        name = copy_name(cfg, today)
        storage.put(name, seal(data, key))
        back = open_sealed(storage.get(name), key)
        if back != data:
            raise OffsiteError("копия, скачанная из хранилища, не совпадает с отправленной")
        tables = _check_opened(back, workdir)
    ours = sorted(k for k in storage.list(cfg["prefix"]) if k.startswith(cfg["prefix"]) and NAME.match(k[len(cfg["prefix"]):]))
    removed = [k for k in ours[:-cfg["keep"]] if k != name]
    for old in removed:
        storage.delete(old)
    return {"name": name, "size": len(data), "tables": tables, "removed": len(removed), "kept": len(ours) - len(removed)}


def fetch_copy(out, key: bytes, cfg: Optional[dict] = None, name: str = "", in_file: str = "",
               http: Transport = storage_transport, clock: Callable[[], float] = time.time) -> dict:
    """For a restore: the newest copy (or the named one) from the storage, or a file downloaded by hand, decrypted,
    checked and written as the usual `state-<date>.db.gz`. An existing file is never overwritten."""
    if in_file:
        try:
            blob = Path(in_file).read_bytes()
        except OSError as err:
            raise OffsiteError("файл копии не читается: %s" % type(err).__name__) from None
        name = Path(in_file).name
    else:
        if cfg is None:
            raise OffsiteError("нужен файл настроек хранилища или --in с файлом копии")
        storage = Storage(cfg, http, clock)
        if name:
            name = name if name.startswith(cfg["prefix"]) else cfg["prefix"] + name
        else:
            ours = sorted(k for k in storage.list(cfg["prefix"]) if k.startswith(cfg["prefix"]) and NAME.match(k[len(cfg["prefix"]):]))
            if not ours:
                raise OffsiteError("в хранилище нет копий")
            name = ours[-1]
        blob = storage.get(name)
    data = open_sealed(blob, key)
    with tempfile.TemporaryDirectory() as workdir:
        tables = _check_opened(data, workdir)
    fd = os.open(str(out), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return {"name": name, "tables": tables, "size": len(data)}
