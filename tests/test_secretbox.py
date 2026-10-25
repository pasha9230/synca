from __future__ import annotations

import os
import stat

import pytest
from cryptography.fernet import Fernet

from tires_sync.secretbox import SecretBox, SecretKeyError, create_key_file, load_box, mask


def test_roundtrip_and_ciphertext_hides_plaintext():
    box = SecretBox(Fernet.generate_key())
    token = box.encrypt("super-secret-123")
    assert "super-secret" not in token
    assert box.decrypt(token) == "super-secret-123"


def test_wrong_key_and_garbage_raise_secret_key_error():
    a, b = SecretBox(Fernet.generate_key()), SecretBox(Fernet.generate_key())
    token = a.encrypt("x")
    with pytest.raises(SecretKeyError):
        b.decrypt(token)
    with pytest.raises(SecretKeyError):
        a.decrypt("not-a-token")


def test_invalid_key_is_rejected_without_leaking_it():
    with pytest.raises(SecretKeyError) as info:
        SecretBox(b"short-key-value")
    assert "short-key-value" not in str(info.value)


def test_create_key_file_is_private_and_never_overwrites(tmp_path):
    path = tmp_path / "secret.key"
    create_key_file(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    first = path.read_bytes()
    with pytest.raises(FileExistsError):
        create_key_file(path)
    assert path.read_bytes() == first
    assert load_box(path) is not None


def test_load_box_returns_none_for_missing_empty_or_corrupt(tmp_path):
    assert load_box(None) is None
    assert load_box("") is None
    assert load_box(tmp_path / "none.key") is None
    bad = tmp_path / "bad.key"
    bad.write_bytes(b"garbage")
    assert load_box(bad) is None
    empty = tmp_path / "empty.key"
    empty.write_bytes(b"")
    assert load_box(empty) is None


def test_mask_only_for_identifiers_never_for_passwords():
    assert mask("client_id", "abcdefghijkl") == "••••ijkl"
    assert mask("api_key", "abcdefghijkl") == "••••ijkl"
    assert mask("client_secret", "abcdefghijkl") == ""
    assert mask("password", "abcdefghijkl") == ""
    assert mask("client_id", "abc") == ""
