"""The window of an order at a point in the colours and sizes of Ant Design (the owner 2026-11-12: «Точку тоже надо привести в порядок, мы
же хотели всё сделать в Ant Design»): the markup is the office's, so point.css gives the panel's tokens Ant's values on that page alone
(body.pt-card-page); «Строгий» keeps its own; the office never loads the sheet."""
from __future__ import annotations

import re
from pathlib import Path

from test_order_chat_window import other_user, page, window

CSS = re.sub(r"/\*.*?\*/", "", (Path(__file__).resolve().parents[1] / "src/tires_sync/panel/static/point.css").read_text(encoding="utf-8"), flags=re.S)
PAGE = "body.pt-card-page:not(.ui-new)"


def block():
    found = re.search(re.escape(PAGE) + r"\{([^}]*)\}", CSS)
    assert found
    return dict(pair.strip().split(":", 1) for pair in found.group(1).split(";") if pair.strip())


def test_the_point_s_order_page_gets_ant_design_tokens():
    tokens = block()
    assert tokens["--primary-600"] == "#1677ff" and tokens["--gray-300"] == "#d9d9d9" and tokens["--err-500"] == "#ff4d4f"
    assert tokens["--r-md"] == "6px" and tokens["--r-xl"] == "8px" and "Segoe UI" in tokens["--font"]
    assert "--ring" in tokens                                           # :root computes it from the old primary: said again
    assert not [name for name in tokens if name.startswith("--stage-")]  # the stages' colours are the stored codes


def test_every_rule_of_it_is_for_that_page_only_and_never_under_the_strict_view():
    rules = [s.strip() for s in re.findall(r"([^{}]+)\{", CSS) if "--primary-600:#1677ff" in s or "pt-card-page:not" in s]
    assert len(rules) > 10
    for selector in rules:
        for part in selector.split(","):
            assert part.strip().startswith(PAGE), part


def test_the_point_loads_the_sheet_on_the_order_page_and_the_office_does_not(tmp_path):
    app, client, tok, conn, tanya = page(tmp_path, "old")
    card = tanya["order_id"]
    alpha = conn.execute("INSERT INTO point (name, avito_account_id) VALUES ('Альфа', ?)", (tanya["account_id"],)).lastrowid
    conn.execute("UPDATE order_card SET point_id = ? WHERE id = ?", (alpha, card))
    conn.commit()
    html = window(other_user(app, conn, "gamma", "point", point_id=alpha), card)
    assert re.search(r'<body class="[^"]*\bpt-card-page\b', html) and "point.css" in html
    office = window(client, card)
    assert "pt-card-page" not in office and "point.css" not in office


# The reply field at a point: a messenger's line (the owner 2026-11-12: «можно адаптировать, убрав огромные кнопки»).

def _point(tmp_path):
    app, client, tok, conn, tanya = page(tmp_path, "old")
    alpha = conn.execute("INSERT INTO point (name, avito_account_id) VALUES ('Альфа', ?)", (tanya["account_id"],)).lastrowid
    conn.execute("UPDATE order_card SET point_id = ? WHERE id = ?", (alpha, tanya["order_id"]))
    conn.commit()
    return client, other_user(app, conn, "gamma", "point", point_id=alpha), tanya


def _form(html, chat_row):
    return re.search(r'<form method="post" action="/chats/%d/send"[^>]*>.*?</form>' % chat_row, html, re.S).group(0)


def test_the_point_s_reply_field_is_one_line_without_big_buttons_in_the_order_and_in_the_chats(tmp_path):
    client, point, tanya = _point(tmp_path)
    for url in ("/orders/%d" % tanya["order_id"], "/chats/%d" % tanya["id"]):
        form = _form(point.get(url).get_data(as_text=True), tanya["id"])
        assert 'class="chat-send pt-send"' in form and 'class="pt-send-line"' in form, url
        assert "Уйдёт сразу" not in form and "> Фото<" not in form and "btn--sm ml-auto" not in form, url          # no big buttons, no note
        assert 'class="btn btn--primary pt-send-go" title="Отправить"' in form and '<span class="sr-only">Отправить</span>' in form, url
        # the same form: the fields and the marks against a double press
        for field in ('name="csrf"', 'name="reply_token"', 'name="image_token"', 'name="text"', 'type="file" name="photo"', "data-once", "data-photo-form",
                      "data-photo-clear", "<span data-photo-note></span>"):
            assert field in form, (url, field)


def test_the_office_keeps_its_reply_field(tmp_path):
    client, point, tanya = _point(tmp_path)
    form = _form(client.get("/chats/%d" % tanya["id"]).get_data(as_text=True), tanya["id"])
    assert "pt-send" not in form and "Уйдёт сразу" in form and 'class="btn btn--primary btn--sm ml-auto">Отправить</button>' in form


def test_the_point_s_line_hides_the_empty_note_and_grows_with_the_text():
    assert ".pt-send .chat-send-note:not(:has([data-photo-note]:not(:empty))){display:none}" in CSS
    assert re.search(r"\.pt-send \.pt-send-line \.textarea\{[^}]*field-sizing:content", CSS) and re.search(r"\.pt-send \.pt-send-go\{[^}]*border-radius:50%", CSS)
