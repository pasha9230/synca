"""Reading what the owner forwards to the notification bot: only the owner's chat, nothing else."""
from __future__ import annotations

import json

from http_fakes import ScriptedHttp
from tires_sync import cli, connections, notify, store
from tires_sync.cli import main
from tires_sync.secretbox import create_key_file, load_box

TOKEN = "123456:ABCDEFGHIJKLMNOPQRSTUV"
OWNER = "555"


def update(update_id, chat, text=None, **extra):
    message = {"message_id": update_id, "chat": {"id": int(chat)}, "from": {"id": int(chat), "first_name": "X"}, "date": 1_791_900_000 + update_id}
    if text is not None:
        message["text"] = text
    message.update(extra)
    return {"update_id": update_id, "message": message}


def test_only_messages_from_the_owners_chat_are_returned_and_the_updates_are_acknowledged():
    http = ScriptedHttp((200, {"ok": True, "result": [update(1, OWNER, "Список задач: 1. ..."), update(2, "999", "Купите крипту"),
                                                   update(3, OWNER, None, caption="Подпись", document={"file_name": "erp.pdf", "file_id": "F1"}),
                                                   update(4, OWNER, "Ещё", forward_origin={"type": "user"})]}),
                        (200, {"ok": True, "result": []}))
    got = notify.read_inbox(TOKEN, OWNER, http=http)
    assert [m["text"] for m in got["messages"]] == ["Список задач: 1. ...", "Подпись [файл: erp.pdf]", "Ещё"] and got["ignored"] == 1
    assert got["messages"][2]["forwarded"] is True and got["last_update_id"] == 4
    assert "offset=5" in http.calls[1][1]                                                                      # the next call acknowledges what was read


def test_a_bad_token_or_an_error_of_telegram_is_reported_not_raised_as_a_crash():
    import pytest
    with pytest.raises(notify.NotifyError):
        notify.read_inbox("bad", OWNER, http=ScriptedHttp())
    with pytest.raises(notify.NotifyError):
        notify.read_inbox(TOKEN, OWNER, http=ScriptedHttp((409, {"ok": False, "description": "webhook is set"})))


def test_the_command_prints_what_the_owner_sent_and_nothing_of_the_token(tmp_path, monkeypatch, capsys):
    conn = store.connect(tmp_path / "t.db")
    key_file = tmp_path / "secret.key"
    create_key_file(key_file)
    box = load_box(key_file)
    connections.set_secret(conn, box, "telegram", 0, "bot_token", TOKEN, "2026-10-13T10:00:00", "o")
    connections.set_config(conn, "telegram", 0, {"chat_id": OWNER}, "2026-10-13T10:00:00", "o")
    conn.commit()
    conn.close()
    http = ScriptedHttp((200, {"ok": True, "result": [update(7, OWNER, "Привет, вот список")]}), (200, {"ok": True, "result": []}))
    monkeypatch.setattr(cli, "_http", lambda: http)
    assert main(["telegram-inbox", "--db", str(tmp_path / "t.db"), "--key-file", str(key_file)]) == 0
    out = capsys.readouterr().out
    assert "Привет, вот список" in out and TOKEN not in out


def test_files_and_photos_are_downloaded_before_the_updates_are_acknowledged(tmp_path):
    http = ScriptedHttp(
        (200, {"ok": True, "result": [update(1, OWNER, None, caption="Инструкция", document={"file_name": "erp.pdf", "file_id": "DOC1", "file_size": 10}),
                                      update(2, OWNER, None, photo=[{"file_id": "P_small", "file_size": 5, "width": 90, "height": 90},
                                                                    {"file_id": "P_big", "file_size": 9, "width": 1280, "height": 960}])]}),
        (200, {"ok": True, "result": {"file_path": "documents/file_1.pdf"}}), (200, b"%PDF-fake"),
        (200, {"ok": True, "result": {"file_path": "photos/file_2.jpg"}}), (200, b"JPEGFAKE"),
        (200, {"ok": True, "result": []}))
    got = notify.read_inbox(TOKEN, OWNER, http=http, download_to=tmp_path)
    paths = [f["path"] for m in got["messages"] for f in m["files"]]
    assert [p.split("/")[-1] for p in paths] == ["erp.pdf", "photo_2.jpg"]
    assert open(paths[0], "rb").read() == b"%PDF-fake" and open(paths[1], "rb").read() == b"JPEGFAKE"
    assert "P_big" in http.calls[3][1] or "P_big" in str(http.calls[3])                                      # the biggest size of the photo
    assert "offset=3" in http.calls[-1][1]                                                                      # acknowledged only after the files are safe


def test_a_file_that_cannot_be_downloaded_keeps_the_updates_unacknowledged(tmp_path):
    import pytest
    http = ScriptedHttp((200, {"ok": True, "result": [update(1, OWNER, None, document={"file_name": "a.pdf", "file_id": "D", "file_size": 10})]}),
                        (500, {"ok": False}))
    with pytest.raises(notify.NotifyError):
        notify.read_inbox(TOKEN, OWNER, http=http, download_to=tmp_path)
    assert len(http.calls) == 2                                                                                  # no «offset» call: the message will come again
