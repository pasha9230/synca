"""A cut answer of a server is a network trouble (OSError), not a crash of the whole run (the nights of 2026-10-19: IncompleteRead killed tires-chats)."""
from __future__ import annotations

import http.client
import io
import urllib.error

import pytest

from tires_sync import transport


class Boom:
    def __init__(self, error):
        self.error = error

    def open(self, request, timeout=None):
        raise self.error


class CutBody:
    status = 200
    headers = {}

    def read(self, n=-1):
        raise http.client.IncompleteRead(b"par")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class CutOpener:
    def open(self, request, timeout=None):
        return CutBody()


@pytest.mark.parametrize("error", [http.client.IncompleteRead(b""), http.client.BadStatusLine("x"), http.client.RemoteDisconnected("gone")])
def test_a_cut_or_broken_answer_becomes_an_oserror(monkeypatch, error):
    monkeypatch.setattr(transport, "_OPENER", Boom(error))
    with pytest.raises(OSError, match=type(error).__name__):                      # the kind is in the message for the journal
        transport.urllib_transport("GET", "https://x.example/", {}, None)


def test_a_body_cut_in_the_middle_becomes_an_oserror(monkeypatch):
    monkeypatch.setattr(transport, "_OPENER", CutOpener())
    with pytest.raises(OSError):
        transport.urllib_transport("GET", "https://x.example/", {}, None)


def test_a_body_cut_inside_an_http_error_becomes_an_oserror_and_a_normal_error_still_answers(monkeypatch):
    class Cut(io.BytesIO):
        def read(self, n=-1):
            raise http.client.IncompleteRead(b"")
    monkeypatch.setattr(transport, "_OPENER", Boom(urllib.error.HTTPError("https://x", 502, "bad", {}, Cut())))
    with pytest.raises(OSError):
        transport.urllib_transport("GET", "https://x.example/", {}, None)
    monkeypatch.setattr(transport, "_OPENER", Boom(urllib.error.HTTPError("https://x", 503, "busy", {}, io.BytesIO(b"later"))))
    assert transport.urllib_transport("GET", "https://x.example/", {}, None)[0:3:2] == (503, b"later")
