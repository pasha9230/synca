"""Copy a supplier photo to our server for a client link: https only, public hosts only, no redirects, small, and really an image."""
from __future__ import annotations

import concurrent.futures
import http.client
import ipaddress
import socket
import ssl
import time
import urllib.parse
from typing import Optional, Tuple

MAX_PHOTO_BYTES = 2 * 1024 * 1024
TIMEOUT = 5            # one socket operation
TOTAL_SECONDS = 10     # one whole photo, however slowly the server hands it over
DNS_SECONDS = 5
CHUNK = 64 * 1024

_SIGNATURES = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
)


class PhotoError(Exception):
    pass


class _PinnedHTTPS(http.client.HTTPSConnection):
    """Connects to an address we already checked (not to whatever DNS answers a second time), but still verifies the certificate for the host name."""

    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self.pinned_ip = ip

    def connect(self):
        sock = socket.create_connection((self.pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def image_kind(data: bytes) -> Optional[str]:
    """The format by the first bytes (not by what the server claims)."""
    for signature, kind in _SIGNATURES:
        if data.startswith(signature):
            return kind
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def resolve_public(url: str):
    """(host, port, request target, ip): only https, a plain host name, and an address that is not inside our network."""
    parts = urllib.parse.urlsplit(url or "")
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise PhotoError("адрес фото не https")
    port = parts.port or 443
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(socket.getaddrinfo, parts.hostname, port, proto=socket.IPPROTO_TCP)
        addresses = [info[4][0] for info in future.result(timeout=DNS_SECONDS)]
    except (OSError, UnicodeError, concurrent.futures.TimeoutError):
        raise PhotoError("адрес фото не найден")
    finally:
        pool.shutdown(wait=False)
    if not addresses:
        raise PhotoError("адрес фото не найден")
    for raw in addresses:
        if not ipaddress.ip_address(raw.split("%")[0]).is_global:
            raise PhotoError("адрес фото ведёт во внутреннюю сеть")
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    return parts.hostname, port, target, addresses[0]


def check_url(url: str) -> str:
    resolve_public(url)
    return url


def fetch(url: str) -> Tuple[bytes, str]:
    """(bytes, extension). Raises PhotoError for anything that is not a small, real image."""
    host, port, target, ip = resolve_public(url)
    connection = _PinnedHTTPS(host, port, ip, TIMEOUT)
    try:
        connection.request("GET", target, headers={"User-Agent": "tires-sync/1", "Accept": "image/*"})
        response = connection.getresponse()
        if response.status != 200:
            raise PhotoError("фото не отдаётся (HTTP %s)" % response.status)       # a redirect is an answer too: it is never followed
        data = _read_bounded(response, time.monotonic() + TOTAL_SECONDS)
    except (OSError, http.client.HTTPException, ssl.SSLError, ValueError):
        raise PhotoError("фото не скачалось")
    finally:
        connection.close()
    return validate(data)


def _read_bounded(response, deadline: float) -> bytes:
    """Read the body in chunks: at most MAX_PHOTO_BYTES + 1 bytes and never past the deadline (a server may trickle bytes forever)."""
    chunks, size = [], 0
    while size <= MAX_PHOTO_BYTES:
        if time.monotonic() > deadline:
            raise PhotoError("фото отдаётся слишком медленно")
        chunk = response.read(min(CHUNK, MAX_PHOTO_BYTES + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def validate(data: bytes) -> Tuple[bytes, str]:
    if not data:
        raise PhotoError("пустой файл")
    if len(data) > MAX_PHOTO_BYTES:
        raise PhotoError("фото больше 2 МБ")
    kind = image_kind(data)
    if kind is None:
        raise PhotoError("это не JPEG, PNG или WebP")
    return data, kind
