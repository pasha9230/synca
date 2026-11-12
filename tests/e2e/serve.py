"""The panel on a temporary base for the screen audit: every outside client is a fake that refuses and is counted, and the process cannot open
a connection to anything but this machine (a guard on socket.connect and getaddrinfo). Nothing reaches Avito, WebMIM, Telegram, VK, the
Google sheet, Точки or a mail server.

    python tests/e2e/serve.py [dir] [port]      # seeds <dir> (default: a temporary directory) and serves http://127.0.0.1:<port>/"""
from __future__ import annotations

import ipaddress
import socket
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent.parent / "src"), str(HERE.parent), str(HERE)]

import seed  # noqa: E402

OUTSIDE = []          # (client, args) of every attempt of the panel to call outside: the audit reports them


class NoNetwork(OSError):
    pass


def _local(host) -> bool:
    if host in ("localhost", "", None):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def guard_network() -> None:
    """socket.connect / getaddrinfo to anything but loopback raise NoNetwork (and are counted)."""
    real_connect, real_getaddrinfo = socket.socket.connect, socket.getaddrinfo

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else None
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _local(host):
            OUTSIDE.append(("socket", str(address)))
            raise NoNetwork("e2e: no network (%s)" % (address,))
        return real_connect(self, address)

    def getaddrinfo(host, *args, **kwargs):
        if not _local(host if not isinstance(host, bytes) else host.decode()):
            OUTSIDE.append(("dns", str(host)))
            raise NoNetwork("e2e: no network (%s)" % host)
        return real_getaddrinfo(host, *args, **kwargs)
    socket.socket.connect, socket.getaddrinfo = connect, getaddrinfo


def fake(name):
    def call(*args, **kwargs):
        OUTSIDE.append((name, repr(args[:2])[:200]))
        raise NoNetwork("e2e: %s is a fake" % name)
    return call


def photo(url, *args, **kwargs):
    """The seeded photos (https://example.test/…) are a grey square made here; any other photo is an outside call."""
    if not str(url).startswith("https://example.test/"):
        return fake("photo_fetch")(url)
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 200, 200)).save(buf, "JPEG")
    return buf.getvalue(), "jpg"


def config(root: Path) -> dict:
    from werkzeug.security import generate_password_hash
    cfg = {
        "SECRET_KEY": "e2e-not-a-secret",
        "PASSWORD_HASH": generate_password_hash(seed.OWNER_PASSWORD, method="pbkdf2:sha256:1000"),
        "DB_PATH": str(root / "panel.db"), "UNLOAD_PATH": str(root / "unload.xml"), "REFERENCE_PATH": str(root / "reference.txt"),
        "FEED_ROOT": str(root / "feeds"), "PHOTOS_ROOT": str(root / "photos"), "IMG_ROOT": str(root / "img"), "SHARE_ROOT": str(root / "share"),
        "WHATSNEW_DIR": str(root / "whatsnew"),
        "POPEN": fake("popen"),
        # the work a press hands to a thread is dropped: nothing of it may run here
        "VK_RUN": lambda work: OUTSIDE.append(("vk_run", "")), "SHEET_RUN": lambda work: OUTSIDE.append(("sheet_run", "")),
        "AVITO_HOOK_RUN": lambda work: OUTSIDE.append(("avito_hook_run", "")),
    }
    for key in ("AVITO_HTTP", "WEBMIM_HTTP", "TG_HTTP", "TELEGRAM_HTTP", "VK_HTTP", "SHEET_HTTP", "TOCHKI_HTTP"):
        cfg[key] = fake(key.lower())
    cfg["PHOTO_FETCH"] = photo
    return cfg


def start(root: Path = None, port: int = 0):
    """Seed, build the app and serve it in a daemon thread. Returns (base_url, server, seeded)."""
    import logging
    from werkzeug.serving import make_server
    logging.getLogger("werkzeug").setLevel(logging.WARNING)      # one line per request drowns what matters
    guard_network()
    root = Path(root or tempfile.mkdtemp(prefix="e2e-panel-"))
    seeded = seed.build(root)
    from tires_sync.panel import create_app
    app = create_app(config(root))
    server = make_server("127.0.0.1", port, app, threaded=True)
    threading.Thread(target=server.serve_forever, name="e2e-panel", daemon=True).start()
    return "http://127.0.0.1:%d" % server.server_port, server, seeded


if __name__ == "__main__":
    url, server, seeded = start(Path(sys.argv[1]) if len(sys.argv) > 1 else None, int(sys.argv[2]) if len(sys.argv) > 2 else 8765)
    print("panel: %s  owner / %s, others: %s / %s" % (url, seed.OWNER_PASSWORD, ", ".join(l for l in seed.PEOPLE if l != "owner"), seed.PASSWORD))
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()
