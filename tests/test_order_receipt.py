"""«Товарный чек» of an order (the owner 2026-11-13): the sum in words, the values put in the window, the corrected fields, the seller of
«Настройки заказов», no chat, the rights and the send to the order's chat through a fake Avito. Every name and number here is made up."""
from __future__ import annotations

import io
import re
from decimal import Decimal

import pytest
from PIL import Image

from panel_helpers import post
from test_chats_pages import NOW, _sendable_page, setup
from test_roles import person
from tires_sync import order_receipt as receipts, orders, store, users

SELLER = {"seller": "ИП Тестов Тест Тестович", "inn": "123456789012", "address": "г. Примерск, ул. Выдуманная, д. 1",
          "position": "Менеджер", "person": "Пробный П. П."}


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


# ---- the sum in words and the receipt's figures --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("amount, words", [
    (52360, "Пятьдесят две тысячи триста шестьдесят рублей 00 копеек"),
    (0, "Ноль рублей 00 копеек"),
    (0.01, "Ноль рублей 01 копейка"),
    (1, "Один рубль 00 копеек"),
    (2.02, "Два рубля 02 копейки"),
    (5.5, "Пять рублей 50 копеек"),
    (11, "Одиннадцать рублей 00 копеек"),
    (14.11, "Четырнадцать рублей 11 копеек"),
    (21.21, "Двадцать один рубль 21 копейка"),
    (112, "Сто двенадцать рублей 00 копеек"),
    (1000, "Одна тысяча рублей 00 копеек"),
    (2001, "Две тысячи один рубль 00 копеек"),
    (5000, "Пять тысяч рублей 00 копеек"),
    (11000, "Одиннадцать тысяч рублей 00 копеек"),
    (21000, "Двадцать одна тысяча рублей 00 копеек"),
    (1000000, "Один миллион рублей 00 копеек"),
    (2003004.99, "Два миллиона три тысячи четыре рубля 99 копеек"),
    (5100000, "Пять миллионов сто тысяч рублей 00 копеек"),
    (999999999.99, "Девятьсот девяносто девять миллионов девятьсот девяносто девять тысяч девятьсот девяносто девять рублей 99 копеек"),
    (1000000000, "Один миллиард рублей 00 копеек"),
    (Decimal("10.005"), "Десять рублей 01 копейка"),                     # half up to kopecks
])
def test_the_sum_in_words_declines_rubles_kopecks_thousands_and_millions(amount, words):
    assert receipts.amount_in_words(amount) == words


def test_the_figures_of_the_receipt_are_written_its_own_way():
    assert receipts.money(13090) == "13'090.00" and receipts.money(52360) == "52'360.00" and receipts.money(1234567.5) == "1'234'567.50"
    assert receipts.money(0) == "0.00" and receipts.quantity(4) == "4.000"
    assert receipts.day_words("2026-11-02") == "02 ноября 2026г." and receipts.day_words("2026-05-31") == "31 мая 2026г."
    assert receipts.invoice_text("2026-11-01") == "01.11.2026" and receipts.invoice_text("01.11.2026") == "01.11.2026"
    for bad in (-1, "x", float("inf")):
        with pytest.raises(receipts.ReceiptError):
            receipts.amount_in_words(bad)


def test_the_title_names_the_transfer_the_invoice_and_the_day_and_leaves_out_what_is_empty():
    receipt = {"number": "26R-1480132", "invoice_day": "01.11.2026", "day": "2026-11-02"}
    assert receipts.title(receipt) == "Товарный чек № 26R-1480132 / 01.11.2026 от 02 ноября 2026г."
    assert receipts.title(dict(receipt, invoice_day="")) == "Товарный чек № 26R-1480132 от 02 ноября 2026г."
    assert receipts.title(dict(receipt, number="")) == "Товарный чек от 02 ноября 2026г."


# ---- the order and its window ---------------------------------------------------------------------------------------------------------------

def _item(conn, card_id, name, qty, sale, code=""):
    return orders.add_item(conn, card_id, goods_id="900%d" % qty, name=name, brand_key="b", season="winter", diameter=16, territory="",
                           qty=qty, sale_unit=sale, supplier_price=1000, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="o", code=code)


def _order(conn, card_id, transfer="26R-1480132"):
    _item(conn, card_id, "Шина Пробная 205/55 R16", 4, 13090, code="A-100")
    conn.execute("UPDATE order_card SET webmim_number = 'W-1', transfer_number = ? WHERE id = ?", (transfer, card_id))
    conn.execute("INSERT INTO webmim_order (server, id, number, create_date, seen_at) VALUES ('prod', 'w1', 'W-1', '2026-11-01T10:00:00', ?)", (NOW,))
    conn.commit()


def _seller(conn):
    receipts.set_seller(conn, SELLER, NOW, "o")


def test_the_window_puts_in_the_transfer_the_invoice_today_the_positions_at_the_clients_price_and_the_seller(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    card_id = orders.create_card(conn, {"client_name": "Клиент Пробный"}, NOW, "o")
    _order(conn, card_id)
    _seller(conn)
    values = receipts.defaults(conn, orders.get_card(conn, card_id), "2026-11-02")
    assert (values["number"], values["invoice_day"], values["day"]) == ("26R-1480132", "01.11.2026", "2026-11-02")
    assert values["lines"] == [{"code": "A-100", "name": "Шина Пробная 205/55 R16", "qty": 4, "unit": "шт", "price": Decimal("13090.00")}]
    assert values["seller"] == SELLER["seller"] and values["inn"] == SELLER["inn"] and not receipts.missing(values)
    page = client.get("/orders/%d" % card_id).get_data(as_text=True)
    assert "Сформировать чек" in page and 'id="order-receipt"' in page and 'value="26R-1480132"' in page and 'value="01.11.2026"' in page
    assert 'value="2026-10-13"' in page                                    # today in Moscow (the app's clock: 2026-10-13 09:00 UTC)
    assert "52&#39;360.00" in page or "52'360.00" in page
    assert "data-receipt-nochat" in page and re.search(r"data-receipt-send[^>]*disabled", page)      # no chat: no send, the PDF is there
    assert "Скачать PDF" in page and "data-receipt-missing" not in page


def test_the_receipt_needed_mark_lights_the_button(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    card_id = orders.create_card(conn, {"client_name": "Клиент Пробный"}, NOW, "o")
    page = client.get("/orders/%d" % card_id).get_data(as_text=True)
    assert re.search(r'class="btn btn--secondary"[^>]*data-open-modal="order-receipt"', page)
    conn.execute("UPDATE order_card SET receipt_needed = 1 WHERE id = ?", (card_id,))
    conn.commit()
    page = client.get("/orders/%d" % card_id).get_data(as_text=True)
    assert re.search(r'class="btn btn--primary"[^>]*data-open-modal="order-receipt"', page) and "Сформировать чек · нужен" in page


def test_without_the_seller_the_window_asks_for_the_settings_and_no_file_is_made(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    card_id = orders.create_card(conn, {"client_name": "Клиент Пробный"}, NOW, "o")
    _order(conn, card_id)
    page = client.get("/orders/%d" % card_id).get_data(as_text=True)
    assert "data-receipt-missing" in page and "/orders/settings/receipt" in page and "Продавец (ИП и ФИО), ИНН" in page
    form = _form(client, card_id)
    response = client.post("/orders/%d/receipt/file" % card_id, data=dict(form, kind="pdf"))
    assert response.status_code == 302 and "receipt=1" in response.headers["Location"]
    page = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Не заполнено: Продавец (ИП и ФИО), ИНН" in page and 'class="modal-backdrop is-open" id="order-receipt"' in page


def _form(client, card_id) -> dict:
    """The window's fields as the page gives them (lists for the lines)."""
    page = client.get("/orders/%d" % card_id).get_data(as_text=True)
    window = page[page.index('id="order-receipt"'):]
    window = window[:window.index("</form>")]
    form: dict = {}
    for name, value in re.findall(r'<input[^>]*name="([a-z_]+)"[^>]*value="([^"]*)"', window):
        form.setdefault(name, []).append(value.replace("&#39;", "'").replace("&#34;", '"').replace("&amp;", "&"))
    return {k: v if k.startswith("line_") else v[0] for k, v in form.items()}


def test_every_field_can_be_corrected_and_the_pdf_and_the_picture_follow_the_window(tmp_path, monkeypatch):
    app, client, tok, conn, account = setup(tmp_path)
    card_id = orders.create_card(conn, {"client_name": "Клиент Пробный"}, NOW, "o")
    _order(conn, card_id)
    _seller(conn)
    form = _form(client, card_id)
    assert form["csrf"] == tok and form["line_name"][-1] == ""                  # an empty line to add one
    seen = []
    real = receipts.image
    monkeypatch.setattr(receipts, "image", lambda r: seen.append(r) or real(r))
    form.update(number="99X-1", invoice_day="05.11.2026", day="2026-11-06", seller="ИП Другой Д. Д.", position="Продавец", person="Другой Д. Д.")
    form["line_name"] = ["Шина Исправленная", "Колпак пробный"]
    form["line_code"] = ["B-1", ""]
    form["line_qty"] = ["2", "1,5"]
    form["line_unit"] = ["шт", "компл"]
    form["line_price"] = ["1'000,50", "100"]
    response = client.post("/orders/%d/receipt/file" % card_id, data=dict(form, kind="pdf"))
    assert response.status_code == 200 and response.mimetype == "application/pdf" and response.data[:5] == b"%PDF-"
    receipt = seen[-1]
    assert receipts.title(receipt) == "Товарный чек № 99X-1 / 05.11.2026 от 06 ноября 2026г."
    assert receipt["seller"] == "ИП Другой Д. Д." and receipt["inn"] == SELLER["inn"]
    assert [(l["code"], l["name"], l["qty"], l["unit"], l["price"]) for l in receipt["lines"]] == [
        ("B-1", "Шина Исправленная", Decimal("2"), "шт", Decimal("1000.50")), ("", "Колпак пробный", Decimal("1.5"), "компл", Decimal("100.00"))]
    assert receipts.total(receipt) == Decimal("2151.00")
    picture = client.post("/orders/%d/receipt/file" % card_id, data=dict(form, kind="jpg"))
    assert picture.mimetype == "image/jpeg" and Image.open(io.BytesIO(picture.data)).size[0] == receipts.WIDTH
    assert receipts.seller(conn)["seller"] == SELLER["seller"]                   # the window corrects one receipt, never the settings
    assert not conn.execute("SELECT COUNT(*) FROM order_receipt").fetchone()[0]    # a file is not a sent receipt
    for bad, said in (({"line_qty": ["0", "1"]}, "Количество в строке 1"), ({"line_price": ["абв", "1"]}, "Цена в строке 1"),
                      ({"line_name": ["", ""]}, "нет ни одной строки"), ({"inn": "12345"}, "ИНН: 10 или 12 цифр"), ({"day": ""}, "Дата чека")):
        back = client.post("/orders/%d/receipt/file" % card_id, data=dict(form, kind="pdf", **bad))
        assert back.status_code == 302
        assert said in client.get(back.headers["Location"]).get_data(as_text=True)


def test_the_seller_is_kept_in_the_order_settings_by_who_sees_the_money(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    page = client.get("/orders/settings/receipt").get_data(as_text=True)
    assert "Реквизиты продавца" in page and 'href="/orders/settings/receipt"' in page
    post(client, "/orders/settings/receipt", tok, **SELLER)
    assert receipts.seller(conn) == SELLER
    post(client, "/orders/settings/receipt", tok, **dict(SELLER, inn="12"))
    assert receipts.seller(conn)["inn"] == SELLER["inn"]
    assert "ИНН: 10 или 12 цифр" in client.get("/orders/settings/receipt").get_data(as_text=True)
    partner, _ = person(app, "partner")
    post(partner, "/orders/settings/receipt", "tok", **dict(SELLER, position="Старший продавец"))
    assert receipts.seller(conn)["position"] == "Старший продавец"


def test_who_does_not_see_the_money_has_no_receipt(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    card_id = orders.create_card(conn, {"client_name": "Клиент Пробный"}, NOW, "o")
    _order(conn, card_id)
    _seller(conn)
    form = _form(client, card_id)
    for role in ("chats", "viewer"):
        other, _ = person(app, role)
        page = other.get("/orders/%d" % card_id).get_data(as_text=True)
        assert "Сформировать чек" not in page and 'id="order-receipt"' not in page
        assert other.get("/orders/settings/receipt").status_code == 403
        assert "Товарный чек</a>" not in other.get("/orders/settings").get_data(as_text=True)
        for url in ("/orders/%d/receipt/file" % card_id, "/orders/%d/receipt/send" % card_id, "/orders/settings/receipt"):
            assert other.post(url, data=dict(form, kind="pdf")).status_code == 403
    assert receipts.seller(conn) == SELLER


# ---- the send -----------------------------------------------------------------------------------------------------------------------------

def test_the_receipt_goes_to_the_orders_chat_as_a_picture_once_and_is_kept_with_the_order(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path, (200, {"img-5": {"1600x900": "https://img.example/5.jpg"}}),
                                                   (200, {"id": "m-receipt", "created": 1_791_900_500}))
    card_id = tanya["order_id"]
    _order(conn, card_id)
    _seller(conn)
    page = client.get("/orders/%d" % card_id).get_data(as_text=True)
    assert "data-receipt-nochat" not in page and not re.search(r"data-receipt-send[^>]*disabled", page) and "Ч-%d" % tanya["id"] in page
    form = _form(client, card_id)
    assert re.fullmatch(r"[0-9a-f]{24}", form["receipt_token"])
    assert client.post("/orders/%d/receipt/send" % card_id, data=form).status_code == 302
    assert client.post("/orders/%d/receipt/send" % card_id, data=form).status_code == 302                  # a double press
    page = client.get("/orders/%d" % card_id).get_data(as_text=True)
    assert "отправлен клиенту в чат Ч-%d" % tanya["id"] in page and "data-receipt-kept" in page
    http = app.config["AVITO_HTTP"]
    uploads = [c for c in http.calls if c[1].endswith("/uploadImages")]
    sends = [c for c in http.calls if c[1].endswith("/messages/image")]
    assert len(uploads) == 1 and len(sends) == 1
    body = uploads[0][3]
    assert Image.open(io.BytesIO(body[body.index(b"\xff\xd8"):])).format == "JPEG"
    assert conn.execute("SELECT status, photo FROM chat_send_log WHERE token = ?", (form["receipt_token"],)).fetchone() == ("sent", 1)
    kept = receipts.kept(conn, card_id)
    assert len(kept) == 1 and kept[0]["title"] == "Товарный чек № 26R-1480132 / 01.11.2026 от 13 октября 2026г." and kept[0]["total"] == 52360
    texts = [e["text"] for e in orders.list_events(conn, card_id) if e["kind"] == "receipt"]
    assert texts == ["Товарный чек № 26R-1480132 / 01.11.2026 от 13 октября 2026г. отправлен в чат Ч-%d, сумма 52'360.00 руб." % tanya["id"]]
    pdf = client.get("/orders/%d/receipts/%d.pdf" % (card_id, kept[0]["id"]))
    assert pdf.status_code == 200 and pdf.data[:5] == b"%PDF-"
    assert client.get("/orders/%d/receipts/%d.jpg" % (card_id, kept[0]["id"])).mimetype == "image/jpeg"
    assert client.get("/orders/%d/receipts/%d.pdf" % (card_id + 1, kept[0]["id"])).status_code == 404       # another order's address
    page = client.get("/orders/%d" % card_id).get_data(as_text=True)


def test_a_refused_send_keeps_nothing_and_the_window_comes_back_with_the_typed_values(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path, (400, {"error": {"message": "bad image"}}))
    card_id = tanya["order_id"]
    _order(conn, card_id)
    _seller(conn)
    form = dict(_form(client, card_id), number="ПРАВКА-7")
    response = client.post("/orders/%d/receipt/send" % card_id, data=form)
    assert response.status_code == 302 and "receipt=1" in response.headers["Location"]
    page = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Авито не принял фото" in page and 'value="ПРАВКА-7"' in page
    assert not receipts.kept(conn, card_id) and not [e for e in orders.list_events(conn, card_id) if e["kind"] == "receipt"]


def test_without_a_chat_that_may_answer_nothing_is_sent(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    from http_fakes import ScriptedHttp
    app.config["AVITO_HTTP"] = http = ScriptedHttp()
    tanya = next(c for c in store_chats(conn) if c["peer_name"] == "Татьяна")
    card_id = tanya["order_id"]                                             # the chat is there, its account may not answer
    _order(conn, card_id)
    _seller(conn)
    page = client.get("/orders/%d" % card_id).get_data(as_text=True)
    assert "data-receipt-nochat" in page
    response = client.post("/orders/%d/receipt/send" % card_id, data=dict(_form(client, card_id), receipt_token="a" * 24))
    assert response.status_code == 302 and not http.calls
    assert "нет чата Авито" in client.get(response.headers["Location"]).get_data(as_text=True)
    assert not receipts.kept(conn, card_id)


def store_chats(conn):
    from tires_sync import chats
    return chats.list_chats(conn)


def test_the_send_needs_the_right_to_send_to_the_client(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path)
    card_id = tanya["order_id"]
    _order(conn, card_id)
    _seller(conn)
    users.MATRIX["partner"]["rights"].discard("send")
    try:
        partner, _ = person(app, "partner")
        page = partner.get("/orders/%d" % card_id).get_data(as_text=True)
        assert "Сформировать чек" in page and re.search(r"data-receipt-send[^>]*disabled", page)
        assert partner.post("/orders/%d/receipt/send" % card_id, data=dict(_form(client, card_id))).status_code == 403
    finally:
        users.MATRIX["partner"]["rights"].add("send")
    assert not [c for c in app.config["AVITO_HTTP"].calls if "messenger" in c[1]]


# ---- review 13.11 -------------------------------------------------------------------------------------------------------------------------

def test_a_send_that_goes_later_keeps_the_mark_so_a_press_again_sends_no_second_receipt(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path, (200, {"img-5": {"1600x900": "https://img.example/5.jpg"}}),
                                                   (402, {"error": {"message": "payment required"}}))
    card_id = tanya["order_id"]
    _order(conn, card_id)
    _seller(conn)
    form = _form(client, card_id)
    response = client.post("/orders/%d/receipt/send" % card_id, data=form)
    assert response.status_code == 302 and "receipt=1" in response.headers["Location"]
    again = _form(client, card_id)                                                  # the window as it comes back (the draft read once)
    assert again["receipt_token"] == form["receipt_token"]                          # the window keeps the mark of the press
    client.post("/orders/%d/receipt/send" % card_id, data=again)
    sends = [c for c in app.config["AVITO_HTTP"].calls if c[1].endswith("/messages/image")]
    assert len(sends) == 1


def test_a_refused_send_keeps_its_mark(tmp_path):
    """A refused upload: the window comes back with the same mark; a press again goes through chats.send_image's retake of a failed
    mark (it first looks whether a message of ours is there), never a second mark."""
    app, client, tok, conn, tanya = _sendable_page(tmp_path, (400, {"error": {"message": "bad image"}}))
    card_id = tanya["order_id"]
    _order(conn, card_id)
    _seller(conn)
    form = _form(client, card_id)
    assert client.post("/orders/%d/receipt/send" % card_id, data=form).status_code == 302
    assert _form(client, card_id)["receipt_token"] == form["receipt_token"]
    assert conn.execute("SELECT status FROM chat_send_log WHERE token = ?", (form["receipt_token"],)).fetchone()[0] == "failed"

def test_a_double_press_while_the_first_is_sending_keeps_the_mark(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path)
    card_id = tanya["order_id"]
    _order(conn, card_id)
    _seller(conn)
    form = _form(client, card_id)
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, photo, orig_at) VALUES (?,?,?,?, 'sending', 1, ?)",
                 (form["receipt_token"], tanya["id"], NOW, "owner", NOW))            # the first press is still on its way
    conn.commit()
    client.post("/orders/%d/receipt/send" % card_id, data=form)
    assert _form(client, card_id)["receipt_token"] == form["receipt_token"]
    assert not [c for c in app.config["AVITO_HTTP"].calls if c[1].endswith("/messages/image")]
    page = client.get("/orders/%d" % card_id).get_data(as_text=True)
    assert 'action="/orders/%d/receipt/send" data-once' % card_id not in page    # the form also downloads the PDF: the server guards the send

def test_no_font_with_russian_letters_makes_no_receipt(monkeypatch):
    monkeypatch.setattr(receipts, "FONTS", {False: ("/nonexistent/a.ttf",), True: ("/nonexistent/b.ttf",)})
    with pytest.raises(receipts.ReceiptError):
        receipts._font(28)


def test_a_receipt_too_long_for_the_sheet_is_refused_not_cut():
    from werkzeug.datastructures import MultiDict
    n = receipts.LINES_MAX
    form = MultiDict([(k, "Тестов Т.Т.") for k in receipts.SELLER_FIELDS if k != "inn"] + [("inn", "123456789012"), ("number", "26R-1"),
                     ("invoice_day", "01.11.2026"), ("day", "2026-11-13")]
                     + [("line_name", "Ш" * receipts.FIELD_MAX)] * n + [("line_code", "К" * 60)] * n + [("line_qty", "1")] * n
                     + [("line_unit", "шт")] * n + [("line_price", "100")] * n)
    receipt = receipts.from_form(form)                                             # a valid window
    with pytest.raises(receipts.ReceiptError, match="не помещается"):
        receipts.image(receipt)
