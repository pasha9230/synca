from __future__ import annotations

import gzip
import hashlib
import re
import sqlite3
import urllib.parse
from datetime import date

import pytest

from tires_sync import cli, connections, offsite, store
from tires_sync.cli import main

SECRET = "s3-secret-DO-NOT-SHOW-0123456789"
NOW = 1792540800.0                              # 2026-10-21T00:00:00Z


def make_db(tmp_path):
    conn = store.connect(tmp_path / "state.db")
    connections.create_account(conn, "Основной", "2026-10-10T12:00:00", "owner")
    conn.close()
    return tmp_path / "state.db"


class FakeS3:
    """An in-memory S3 bucket that checks every request is signed for exactly what is sent."""

    def __init__(self, page=1000):
        self.objects, self.calls, self.page = {}, [], page
        self.fail = {}                                                       # method -> status
        self.corrupt_get = False
        self.stale_get = None                                                 # a valid but different copy given back on read
        self.down = False

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        if self.down:
            raise OSError("сеть недоступна")
        parts = urllib.parse.urlsplit(url)
        assert parts.scheme == "https" and parts.netloc == "storage.yandexcloud.net"
        signed = {k: v for k, v in headers.items() if k != "Authorization"}
        payload = hashlib.sha256(body or b"").hexdigest()
        assert headers["x-amz-content-sha256"] == payload
        assert headers["Authorization"] == offsite.sign(method, url, signed, payload, "AKID", SECRET, "ru-central1", headers["x-amz-date"])
        assert SECRET not in url and SECRET not in "".join(headers.values())
        if method in self.fail:
            return self.fail[method], {}, b"<Error><Code>AccessDenied</Code><Message>no</Message></Error>"
        path = urllib.parse.unquote(parts.path)
        assert path.startswith("/bucket-1")
        key = path[len("/bucket-1/"):]
        if method == "PUT":
            self.objects[key] = body
            return 200, {}, b""
        if method == "DELETE":
            self.objects.pop(key, None)
            return 204, {}, b""
        if key:
            if key not in self.objects:
                return 404, {}, b"<Error><Code>NoSuchKey</Code></Error>"
            data = self.objects[key] if self.stale_get is None else self.stale_get
            if self.corrupt_get:
                data = data[:-1] + bytes([data[-1] ^ 1])
            return 200, {}, data
        query = dict(urllib.parse.parse_qsl(parts.query))
        names = sorted(k for k in self.objects if k.startswith(query.get("prefix", "")))
        start = int(query.get("continuation-token", "0"))
        chunk = names[start:start + self.page]
        more = start + self.page < len(names)
        xml = '<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>bucket-1</Name>'
        xml += "".join("<Contents><Key>%s</Key><Size>1</Size></Contents>" % n for n in chunk)
        xml += "<IsTruncated>%s</IsTruncated>" % ("true" if more else "false")
        if more:
            xml += "<NextContinuationToken>%d</NextContinuationToken>" % (start + self.page)
        return 200, {}, (xml + "</ListBucketResult>").encode()


def write_config(tmp_path, **extra):
    key = tmp_path / "backup.key"
    if not key.exists():
        offsite.new_key_file(key)
    values = {"S3_ENDPOINT": "https://storage.yandexcloud.net", "S3_BUCKET": "bucket-1", "S3_ACCESS_KEY_ID": "AKID",
              "S3_SECRET_ACCESS_KEY": SECRET, "BACKUP_KEY_FILE": str(key)}
    values.update(extra)
    path = tmp_path / "offsite.conf"
    path.write_text("# настройки\n" + "".join("%s=%s\n" % kv for kv in values.items() if kv[1] is not None), encoding="utf-8")
    return path


def cfg_for(tmp_path, **extra):
    return offsite.read_config(write_config(tmp_path, **extra))


# --- the signature: the published examples of AWS Signature V4 for S3 ---

AWS_KEY, AWS_SECRET = "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def _aws(url, extra=None):
    headers = {"Host": "examplebucket.s3.amazonaws.com", "x-amz-date": "20130524T000000Z", "x-amz-content-sha256": offsite.EMPTY_SHA}
    headers.update(extra or {})
    return offsite.sign("GET", url, headers, offsite.EMPTY_SHA, AWS_KEY, AWS_SECRET, "us-east-1", "20130524T000000Z")


def test_signature_matches_the_aws_example_of_get_object():
    auth = _aws("https://examplebucket.s3.amazonaws.com/test.txt", {"Range": "bytes=0-9"})
    assert auth == ("AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
                    "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
                    "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")


def test_signature_matches_the_aws_examples_with_a_query():
    assert _aws("https://examplebucket.s3.amazonaws.com/?lifecycle").endswith(
        "Signature=fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543")
    assert _aws("https://examplebucket.s3.amazonaws.com/?max-keys=2&prefix=J").endswith(
        "Signature=34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7")


# --- the key and the sealed file ---

def test_sealed_copy_opens_only_with_its_key_and_untouched(tmp_path):
    offsite.new_key_file(tmp_path / "k1")
    offsite.new_key_file(tmp_path / "k2")
    k1, k2 = offsite.load_key(tmp_path / "k1"), offsite.load_key(tmp_path / "k2")
    data = gzip.compress(b"SQLite format 3\x00" + b"x" * 1000)
    blob = offsite.seal(data, k1)
    assert data not in blob and b"SQLite" not in blob and offsite.seal(data, k1) != blob     # a fresh nonce every time
    assert offsite.open_sealed(blob, k1) == data
    with pytest.raises(offsite.OffsiteError, match="другой ключ"):
        offsite.open_sealed(blob, k2)
    tampered = bytearray(blob)
    tampered[len(offsite.MAGIC) + 20] ^= 1
    with pytest.raises(offsite.OffsiteError, match="повреждён"):
        offsite.open_sealed(bytes(tampered), k1)
    with pytest.raises(offsite.OffsiteError, match="не зашифрованная"):
        offsite.open_sealed(data, k1)


def test_new_key_is_private_and_never_overwritten(tmp_path):
    path = tmp_path / "backup.key"
    offsite.new_key_file(path)
    first = path.read_text()
    assert (path.stat().st_mode & 0o077) == 0 and len(offsite.load_key(path)) == 32
    with pytest.raises(FileExistsError):
        offsite.new_key_file(path)
    assert path.read_text() == first
    assert main(["backup-key-new", "--out", str(path)]) == 2 and path.read_text() == first


def test_a_bad_key_is_refused_without_showing_it(tmp_path):
    path = tmp_path / "backup.key"
    path.write_text("short-secret-value\n")
    with pytest.raises(offsite.OffsiteError) as err:
        offsite.load_key(path)
    assert "short-secret-value" not in str(err.value)
    with pytest.raises(offsite.OffsiteError, match="не читается"):
        offsite.load_key(tmp_path / "missing.key")


# --- the settings file ---

def test_no_settings_file_means_not_set_up(tmp_path):
    assert offsite.read_config(tmp_path / "none.conf") is None


def test_settings_are_checked(tmp_path):
    cfg = cfg_for(tmp_path, S3_PREFIX="backups/tires")
    assert cfg["prefix"] == "backups/tires/" and cfg["keep"] == 14 and cfg["region"] == "ru-central1"
    with pytest.raises(offsite.OffsiteError, match="S3_BUCKET, S3_SECRET_ACCESS_KEY"):
        cfg_for(tmp_path, S3_BUCKET="", S3_SECRET_ACCESS_KEY=None)
    with pytest.raises(offsite.OffsiteError, match="https"):
        cfg_for(tmp_path, S3_ENDPOINT="http://storage.yandexcloud.net")
    with pytest.raises(offsite.OffsiteError, match="S3_PREFIX"):
        cfg_for(tmp_path, S3_PREFIX="../x")
    with pytest.raises(offsite.OffsiteError, match="KEEP"):
        cfg_for(tmp_path, KEEP="0")
    assert cfg_for(tmp_path, KEEP='"3"')["keep"] == 3


# --- the copy itself ---

def test_copy_is_encrypted_checked_and_restorable(tmp_path):
    db, s3 = make_db(tmp_path), FakeS3()
    info = offsite.send_copy(db, cfg_for(tmp_path), date(2026, 10, 21), http=s3, clock=lambda: NOW)
    assert info["name"] == "tires-sync/state-2026-10-21.db.gz.enc" and info["tables"] > 5 and info["removed"] == 0
    stored = s3.objects["tires-sync/state-2026-10-21.db.gz.enc"]
    assert stored.startswith(offsite.MAGIC) and b"\x1f\x8b" not in stored[:40] and "Основной".encode() not in stored
    assert [c[0] for c in s3.calls] == ["PUT", "GET", "GET"]                  # sent, read back, listed
    out = tmp_path / "restored.db.gz"
    key = offsite.load_key(tmp_path / "backup.key")
    assert offsite.fetch_copy(out, key, cfg=cfg_for(tmp_path), http=s3, clock=lambda: NOW)["name"] == info["name"]
    (tmp_path / "restored.db").write_bytes(gzip.decompress(out.read_bytes()))
    assert [r[0] for r in sqlite3.connect(str(tmp_path / "restored.db")).execute("SELECT name FROM account")] == ["Основной"]
    assert (out.stat().st_mode & 0o077) == 0
    with pytest.raises(FileExistsError):
        offsite.fetch_copy(out, key, cfg=cfg_for(tmp_path), http=s3)


def test_only_the_newest_copies_are_kept_and_foreign_files_stay(tmp_path):
    db, s3 = make_db(tmp_path), FakeS3(page=2)                                # a short page: the list is read to its end
    s3.objects.update({"tires-sync/notes.txt": b"x", "other/state-2026-01-01.db.gz.enc": b"x", "tires-sync/state-2026-01-01.db.gz": b"x"})
    cfg = cfg_for(tmp_path, KEEP="3")
    for day in range(15, 22):
        offsite.send_copy(db, cfg, date(2026, 10, day), http=s3, clock=lambda: NOW)
    assert sorted(s3.objects) == ["other/state-2026-01-01.db.gz.enc", "tires-sync/notes.txt", "tires-sync/state-2026-01-01.db.gz",
                                  "tires-sync/state-2026-10-19.db.gz.enc", "tires-sync/state-2026-10-20.db.gz.enc",
                                  "tires-sync/state-2026-10-21.db.gz.enc"]


def test_the_copy_just_sent_is_never_removed_even_when_older_ones_look_newer(tmp_path):
    db, s3 = make_db(tmp_path), FakeS3()
    s3.objects.update({"tires-sync/state-2030-01-0%d.db.gz.enc" % d: b"x" for d in range(1, 4)})    # a wrong clock once
    offsite.send_copy(db, cfg_for(tmp_path, KEEP="2"), date(2026, 10, 21), http=s3, clock=lambda: NOW)
    assert "tires-sync/state-2026-10-21.db.gz.enc" in s3.objects


def _run(tmp_path, s3, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_offsite_http", lambda: s3)
    code = main(["backup-offsite", "--db", str(make_db(tmp_path)), "--config", str(write_config(tmp_path))])
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err
    return code, out


def test_cli_without_settings_does_nothing(tmp_path, capsys, monkeypatch):
    s3 = FakeS3()
    monkeypatch.setattr(cli, "_offsite_http", lambda: s3)
    assert main(["backup-offsite", "--db", str(make_db(tmp_path)), "--config", str(tmp_path / "none.conf")]) == 0
    assert "не настроена" in capsys.readouterr().out and s3.calls == []


def test_cli_success_reports_the_copy(tmp_path, capsys, monkeypatch):
    code, out = _run(tmp_path, FakeS3(), capsys, monkeypatch)
    assert code == 0 and "state-" in out.out and "проверена" in out.out


@pytest.mark.parametrize("trouble", ["put", "get", "corrupt", "down", "list"])
def test_cli_failure_is_code_1_and_nothing_old_is_removed(tmp_path, capsys, monkeypatch, trouble):
    s3 = FakeS3()
    s3.objects.update({"tires-sync/state-2026-01-0%d.db.gz.enc" % d: b"old" for d in range(1, 9)})
    s3.objects.update({"tires-sync/state-2026-02-%02d.db.gz.enc" % d: b"old" for d in range(1, 20)})
    before = dict(s3.objects)
    if trouble == "put":
        s3.fail["PUT"] = 403
    elif trouble == "get":
        s3.fail["GET"] = 500
    elif trouble == "corrupt":
        s3.corrupt_get = True
    elif trouble == "down":
        s3.down = True
    else:
        real = s3.__call__
        s3_list_broken = lambda m, u, h, b: (200, {}, b"<not xml") if (m == "GET" and "list-type" in u) else real(m, u, h, b)
        monkeypatch.setattr(cli, "_offsite_http", lambda: s3_list_broken)
        code = main(["backup-offsite", "--db", str(make_db(tmp_path)), "--config", str(write_config(tmp_path))])
        assert code == 1 and "список" in capsys.readouterr().err
        assert all(k in s3.objects for k in before)
        return
    code, out = _run(tmp_path, s3, capsys, monkeypatch)
    assert code == 1 and "копия вне сервера не сделана" in out.err
    assert all(k in s3.objects for k in before)
    if trouble == "put":
        assert "403 (AccessDenied)" in out.err


def test_cli_with_broken_settings_or_key_is_code_1(tmp_path, capsys, monkeypatch):
    s3 = FakeS3()
    monkeypatch.setattr(cli, "_offsite_http", lambda: s3)
    db = make_db(tmp_path)
    assert main(["backup-offsite", "--db", str(db), "--config", str(write_config(tmp_path, S3_BUCKET=""))]) == 1
    assert main(["backup-offsite", "--db", str(db), "--config", str(write_config(tmp_path, S3_BUCKET="bucket-1",
                                                                                   BACKUP_KEY_FILE=str(tmp_path / "nokey")))]) == 1
    assert main(["backup-offsite", "--db", str(tmp_path / "nope.db"), "--config", str(write_config(tmp_path))]) == 1
    assert s3.calls == []


def test_cli_get_restores_from_a_file_downloaded_by_hand(tmp_path, capsys, monkeypatch):
    s3 = FakeS3()
    _run(tmp_path, s3, capsys, monkeypatch)
    by_hand = tmp_path / "state-2026-10-21.db.gz.enc"
    by_hand.write_bytes(next(iter(s3.objects.values())))
    out = tmp_path / "state.db.gz"
    assert main(["backup-offsite-get", "--in", str(by_hand), "--key-file", str(tmp_path / "backup.key"), "--out", str(out),
                 "--config", str(tmp_path / "none.conf")]) == 0
    assert gzip.decompress(out.read_bytes()).startswith(b"SQLite format 3")
    assert main(["backup-offsite-get", "--in", str(by_hand), "--key-file", str(tmp_path / "backup.key"), "--out", str(out)]) == 2
    other = tmp_path / "other.key"
    offsite.new_key_file(other)
    assert main(["backup-offsite-get", "--in", str(by_hand), "--key-file", str(other), "--out", str(tmp_path / "x.gz")]) == 2
    assert not (tmp_path / "x.gz").exists()


def test_cli_get_takes_the_newest_or_the_named_copy_from_the_storage(tmp_path, capsys, monkeypatch):
    s3 = FakeS3()
    monkeypatch.setattr(cli, "_offsite_http", lambda: s3)
    db, conf = make_db(tmp_path), write_config(tmp_path)
    cfg = offsite.read_config(conf)
    for day in (19, 20):
        offsite.send_copy(db, cfg, date(2026, 10, day), http=s3, clock=lambda: NOW)
    assert main(["backup-offsite-get", "--config", str(conf), "--out", str(tmp_path / "a.gz")]) == 0
    assert "state-2026-10-20" in capsys.readouterr().out
    assert main(["backup-offsite-get", "--config", str(conf), "--name", "state-2026-10-19.db.gz.enc", "--out", str(tmp_path / "b.gz")]) == 0
    assert "state-2026-10-19" in capsys.readouterr().out
    assert main(["backup-offsite-get", "--config", str(tmp_path / "none.conf"), "--out", str(tmp_path / "c.gz")]) == 2


def test_object_names_in_the_address_are_encoded(tmp_path):
    s3 = FakeS3()
    storage = offsite.Storage(cfg_for(tmp_path), http=s3, clock=lambda: NOW)
    storage.put("tires-sync/a b+c.txt", b"x")
    assert re.search(r"/bucket-1/tires-sync/a%20b%2Bc\.txt$", s3.calls[-1][1])
    assert s3.objects == {"tires-sync/a b+c.txt": b"x"}


def test_a_different_copy_read_back_is_a_failure_and_nothing_is_removed(tmp_path):
    db, s3 = make_db(tmp_path), FakeS3()
    cfg = cfg_for(tmp_path, KEEP="1")
    offsite.send_copy(db, cfg, date(2026, 10, 20), http=s3, clock=lambda: NOW)
    conn = store.connect(db)
    connections.create_account(conn, "Второй", "2026-10-21T12:00:00", "owner")      # the base changed since yesterday
    conn.close()
    s3.stale_get = s3.objects["tires-sync/state-2026-10-20.db.gz.enc"]             # the storage gives yesterday's copy back
    with pytest.raises(offsite.OffsiteError, match="не совпадает"):
        offsite.send_copy(db, cfg, date(2026, 10, 21), http=s3, clock=lambda: NOW)
    assert "tires-sync/state-2026-10-20.db.gz.enc" in s3.objects


def test_the_storage_is_spoken_to_with_a_long_timeout(monkeypatch):
    seen = []

    class Answer:
        status, headers = 200, {}

        def read(self, limit):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(offsite, "_OPENER", type("O", (), {"open": lambda self, request, timeout: seen.append(timeout) or Answer()})())
    assert offsite.storage_transport("PUT", "https://storage.yandexcloud.net/b/k", {}, b"x")[0] == 200
    assert seen == [offsite.STORAGE_TIMEOUT] and offsite.STORAGE_TIMEOUT >= 600
    assert cli._offsite_http() is offsite.storage_transport
