"""«Товарный чек» of an order (the owner 2026-11-13): the button «Сформировать чек» at the bottom of the card opens a window with the values
put in, every field can be corrected, «Сформировать и отправить в чат» sends the receipt to the order's Avito chat as a picture (the messenger
takes pictures only; chats.send_image with its guards and its log) and the window gives the same receipt as a PDF to download.

- the number and the day of the invoice: the first part of «Заказы WebMIM» (order_webmim.parts) that has a transfer number;
- the day «от»: today in Moscow, written with the month's name («02 ноября 2026г.»);
- the lines: the positions of the order at the client's price (sale_unit), the article is the position's code;
- the seller (ИП, ИНН, the address, who hands the goods out): «Настройки заказов › Товарный чек», never in the code (SELLER_KEY in app_state).

A sent receipt is kept with the order (order_receipt: the picture and the PDF) once per mark of the window, with a record in the journal.
Nothing here sends anything: the panel's route does it by a person's press."""
from __future__ import annotations

import io
import json
import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Dict, List, Optional

from . import orders

SELLER_KEY = "order_receipt_seller"
SELLER_FIELDS = {"seller": "Продавец (ИП и ФИО)", "inn": "ИНН", "address": "Адрес", "position": "Должность отпустившего",
                 "person": "ФИО отпустившего"}
NEEDED = ("seller", "inn")                         # without them a receipt is no receipt: the window asks to fill «Настройки заказов»
FIELD_MAX = 200
NUMBER_MAX = 40
LINES_MAX = 30
UNIT = "шт"
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")
FONTS = {False: ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/Library/Fonts/Arial Unicode.ttf",
                 "/System/Library/Fonts/Supplemental/Arial.ttf", "C:/Windows/Fonts/arial.ttf"),
         True: ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                "C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")}


class ReceiptError(orders.OrderError):
    pass


# ---- the words of a sum ---------------------------------------------------------------------------------------------------------------------

_ONES = {False: ("", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"),
         True: ("", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять")}
_TEENS = ("десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать",
          "девятнадцать")
_TENS = ("", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто")
_HUNDREDS = ("", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот")
_GROUPS = ((("рубль", "рубля", "рублей"), False), (("тысяча", "тысячи", "тысяч"), True), (("миллион", "миллиона", "миллионов"), False),
           (("миллиард", "миллиарда", "миллиардов"), False))


def plural(n: int, forms) -> str:
    """«1 рубль, 2 рубля, 5 рублей, 11 рублей, 21 рубль»."""
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return forms[2]
    if n % 10 == 1:
        return forms[0]
    return forms[1] if 2 <= n % 10 <= 4 else forms[2]


def _triplet(n: int, female: bool) -> List[str]:
    words = [_HUNDREDS[n // 100]]
    rest = n % 100
    if 10 <= rest <= 19:
        words.append(_TEENS[rest - 10])
    else:
        words += [_TENS[rest // 10], _ONES[female][rest % 10]]
    return [w for w in words if w]


def cents(amount) -> Decimal:
    """The sum rounded to kopecks (half up): what the receipt shows and writes in words."""
    try:
        value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ReceiptError("Сумма: нужно число.")
    if not value.is_finite() or value < 0 or value >= Decimal(10) ** 12:
        raise ReceiptError("Сумма вне допустимых пределов.")
    return value


def amount_in_words(amount) -> str:
    """«Пятьдесят две тысячи триста шестьдесят рублей 00 копеек»: the rubles in words, the kopecks in figures."""
    value = cents(amount)
    rubles, kop = int(value), int((value - int(value)) * 100)
    words: List[str] = []
    if rubles == 0:
        words = ["ноль", "рублей"]
    else:
        rest, groups = rubles, []
        while rest:
            groups.append(rest % 1000)
            rest //= 1000
        for index in range(len(groups) - 1, -1, -1):
            part, (forms, female) = groups[index], _GROUPS[index]
            if part or index == 0:
                words += _triplet(part, female)
                words.append(plural(part, forms))
    text = " ".join(words)
    return "%s%s %02d %s" % (text[0].upper(), text[1:], kop, plural(kop, ("копейка", "копейки", "копеек")))


def money(amount) -> str:
    """«13'090.00»: the receipt's own way, an apostrophe between the thousands."""
    return format(cents(amount), ",.2f").replace(",", "'")


def quantity(qty) -> str:
    return "%.3f" % float(qty)


def day_words(day: str) -> str:
    """«2026-11-02» → «02 ноября 2026г.»."""
    when = date.fromisoformat(str(day)[:10])
    return "%02d %s %dг." % (when.day, MONTHS[when.month - 1], when.year)


def invoice_text(day: str) -> str:
    """«Дата счета» of WebMIM as the receipt writes it: «2026-11-01» → «01.11.2026»; a typed text stays as it is."""
    text = " ".join(str(day or "").split())
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text[:10]).strftime("%d.%m.%Y")
        except ValueError:
            return text
    return text


def title(receipt: dict) -> str:
    """«Товарный чек № 26R-1480132 / 01.11.2026 от 02 ноября 2026г.» (no number, no invoice day: those parts are left out)."""
    head = "Товарный чек"
    if receipt["number"]:
        head += " № %s" % receipt["number"]
        if receipt["invoice_day"]:
            head += " / %s" % receipt["invoice_day"]
    return "%s от %s" % (head, day_words(receipt["day"]))


# ---- the seller: «Настройки заказов › Товарный чек» -------------------------------------------------------------------------------------------

def seller(conn) -> Dict[str, str]:
    from . import store
    try:
        kept = json.loads(store.get_state(conn, SELLER_KEY) or "{}")
    except ValueError:
        kept = {}
    return {k: str(kept.get(k) or "") for k in SELLER_FIELDS}


def _field(value, label: str, limit: int = FIELD_MAX) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        raise ReceiptError("%s: не длиннее %d знаков." % (label, limit))
    return text


def _inn(value) -> str:
    text = re.sub(r"\s+", "", str(value or ""))
    if text and not re.fullmatch(r"\d{10}|\d{12}", text):
        raise ReceiptError("ИНН: 10 или 12 цифр.")
    return text


def set_seller(conn, form, now: str, author: str) -> List[str]:
    """Keeps the seller's fields (with who and when); returns the names of the changed ones."""
    from . import store
    new = {k: (_inn(form.get(k)) if k == "inn" else _field(form.get(k), label)) for k, label in SELLER_FIELDS.items()}
    old = seller(conn)
    changed = [SELLER_FIELDS[k] for k in SELLER_FIELDS if new[k] != old[k]]
    if changed:
        store.set_state(conn, SELLER_KEY, json.dumps(dict(new, updated_at=now, updated_by=author), ensure_ascii=False))
    return changed


def missing(receipt: dict) -> List[str]:
    """The names of the seller's fields the receipt needs and does not have."""
    return [SELLER_FIELDS[k] for k in NEEDED if not receipt.get(k)]


# ---- the values of the window ---------------------------------------------------------------------------------------------------------------

def article(item: dict) -> str:
    code = item.get("code") or (item.get("goods_id") if item.get("goods_id") != orders.POINT_GOODS else "")
    return " ".join(str(code or "").split())


def defaults(conn, card: dict, today: str) -> dict:
    """What the window shows first: the number and the day of the invoice of the first part of «Заказы WebMIM» that has a transfer, today,
    the positions at the client's price, the seller of the settings."""
    from . import order_webmim
    number, invoice = "", ""
    try:
        parts = order_webmim.parts(conn, card)
    except Exception:                                       # the numbers are a help: a broken part never takes the window down
        parts = []
    first = next((p for p in parts if p.get("transfer")), None)
    if first:
        number, invoice = str(first["transfer"]), invoice_text(first.get("day"))
    lines = [{"code": article(i), "name": i["name"], "qty": int(i["qty"]), "unit": UNIT, "price": cents(i["sale_unit"])}
             for i in card["items"] if int(i["qty"]) >= 1]
    return dict(seller(conn), number=number, invoice_day=invoice, day=str(today)[:10], lines=lines, several=len(parts) > 1)


def _amount(value, label: str) -> Decimal:
    text = str(value if value is not None else "").replace("'", "").replace(" ", "").replace("\xa0", "").replace(",", ".")
    if not text:
        raise ReceiptError("%s: нужно число." % label)
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ReceiptError("%s: нужно число." % label)
    if not number.is_finite():
        raise ReceiptError("%s: нужно число." % label)
    return number


def from_form(form) -> dict:
    """The receipt as the window sent it (every field corrected by hand). ReceiptError: what is wrong, in words."""
    receipt = {k: (_inn(form.get(k)) if k == "inn" else _field(form.get(k), label)) for k, label in SELLER_FIELDS.items()}
    receipt["number"] = _field(form.get("number"), "Номер чека", NUMBER_MAX)
    receipt["invoice_day"] = _field(form.get("invoice_day"), "Дата счёта", 20)
    day = str(form.get("day") or "").strip()
    try:
        date.fromisoformat(day)
    except ValueError:
        raise ReceiptError("Дата чека: выберите день.")
    receipt["day"] = day
    getlist = form.getlist if hasattr(form, "getlist") else (lambda k: form.get(k) or [])
    names, codes, qtys, units, prices = (getlist(k) for k in ("line_name", "line_code", "line_qty", "line_unit", "line_price"))
    lines = []
    for at, name in enumerate(names):
        name = _field(name, "Наименование", FIELD_MAX)
        if not name:
            continue
        row = at + 1
        qty = _amount(qtys[at] if at < len(qtys) else "", "Количество в строке %d" % row)
        if qty <= 0 or qty > 10000 or qty != qty.quantize(Decimal("0.001")):
            raise ReceiptError("Количество в строке %d: больше нуля, до трёх знаков после точки." % row)
        price = _amount(prices[at] if at < len(prices) else "", "Цена в строке %d" % row)
        if price < 0 or price >= Decimal(10) ** 9:
            raise ReceiptError("Цена в строке %d вне допустимых пределов." % row)
        lines.append({"code": _field(codes[at] if at < len(codes) else "", "Артикул", 60), "name": name, "qty": qty,
                      "unit": _field(units[at] if at < len(units) else "", "Ед. изм.", 10) or UNIT, "price": cents(price)})
    if not lines:
        raise ReceiptError("В чеке нет ни одной строки: впишите наименование товара.")
    if len(lines) > LINES_MAX:
        raise ReceiptError("В чеке больше %d строк." % LINES_MAX)
    receipt["lines"] = lines
    return receipt


def total(receipt: dict) -> Decimal:
    return cents(sum((cents(line["price"] * Decimal(str(line["qty"]))) for line in receipt["lines"]), Decimal(0)))


# ---- the picture and the PDF ------------------------------------------------------------------------------------------------------------------

WIDTH, MARGIN = 1600, 70
COLUMNS = (("№", 60, "c"), ("Артикул", 200, "l"), ("Наименование товара", 0, "l"), ("Количество", 190, "r"), ("Ед. изм.", 120, "c"),
           ("Цена", 190, "r"), ("Сумма", 210, "r"))


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    for path in FONTS[bold]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _wrap(draw, text: str, font, width: int) -> List[str]:
    lines, line = [], ""
    for word in str(text).split():
        while draw.textlength(word, font=font) > width and len(word) > 1:      # a word longer than the column: cut it
            cut = len(word)
            while cut > 1 and draw.textlength(word[:cut], font=font) > width:
                cut -= 1
            if line:
                lines.append(line)
                line = ""
            lines.append(word[:cut])
            word = word[cut:]
        probe = (line + " " + word).strip()
        if line and draw.textlength(probe, font=font) > width:
            lines.append(line)
            line = word
        else:
            line = probe
    if line:
        lines.append(line)
    return lines or [""]


def image(receipt: dict):
    """The receipt as a PIL picture, white, WIDTH wide, as high as it needs."""
    from PIL import Image, ImageDraw
    canvas = Image.new("RGB", (WIDTH, 6000), "white")
    draw = ImageDraw.Draw(canvas)
    ink, inner = (0, 0, 0), WIDTH - 2 * MARGIN
    small, normal, bold, big = _font(26), _font(28), _font(28, True), _font(40, True)
    y = MARGIN
    head = ", ".join(p for p in (receipt["seller"], "ИНН %s" % receipt["inn"] if receipt["inn"] else "") if p)
    for line in _wrap(draw, head, normal, inner):
        draw.text((MARGIN, y), line, font=normal, fill=ink)
        y += 34
    for line in _wrap(draw, receipt["address"], small, inner) if receipt["address"] else []:
        draw.text((MARGIN, y), line, font=small, fill=ink)
        y += 32
    y += 30
    for line in _wrap(draw, title(receipt), big, inner):
        draw.text((MARGIN, y), line, font=big, fill=ink)
        y += 48
    y += 24
    widths = [w for _, w, _ in COLUMNS]
    widths[2] = inner - sum(widths)
    xs = [MARGIN]
    for w in widths:
        xs.append(xs[-1] + w)
    pad, cell = 10, _font(26)
    head_font = _font(24, True)

    def row(values, font, aligns=None):
        nonlocal y
        wrapped = [_wrap(draw, v, font, widths[i] - 2 * pad) for i, v in enumerate(values)]
        height = max(len(w) for w in wrapped) * 34 + 2 * pad
        for i, lines in enumerate(wrapped):
            align = (aligns or [c[2] for c in COLUMNS])[i]
            for n, text in enumerate(lines):
                tw = draw.textlength(text, font=font)
                x = xs[i] + pad if align == "l" else (xs[i + 1] - pad - tw if align == "r" else xs[i] + (widths[i] - tw) / 2)
                draw.text((x, y + pad + n * 34), text, font=font, fill=ink)
            draw.rectangle((xs[i], y, xs[i + 1], y + height), outline=ink, width=2)
        y += height

    row([c[0] for c in COLUMNS], head_font, ["c"] * len(COLUMNS))
    for n, line in enumerate(receipt["lines"], 1):
        row([str(n), line["code"], line["name"], quantity(line["qty"]), line["unit"], money(line["price"]),
             money(line["price"] * Decimal(str(line["qty"])))], cell)
    whole = total(receipt)
    label, value = "Итого:", money(whole)
    draw.text((xs[-2] - pad - draw.textlength(label, font=bold), y + 12), label, font=bold, fill=ink)
    draw.text((xs[-1] - pad - draw.textlength(value, font=bold), y + 12), value, font=bold, fill=ink)
    y += 70
    said = "Всего наименований %d, на сумму %s руб." % (len(receipt["lines"]), money(whole))
    for line in _wrap(draw, said, normal, inner):
        draw.text((MARGIN, y), line, font=normal, fill=ink)
        y += 36
    for line in _wrap(draw, amount_in_words(whole), bold, inner):
        draw.text((MARGIN, y), line, font=bold, fill=ink)
        y += 36
    y += 70
    x, under = MARGIN, _font(18)
    for shown, note in (("Отпустил", None), ("", "(подпись)"), (receipt["position"], "(должность)"), (receipt["person"], "(Ф.И.О.)")):
        if note is None:
            draw.text((x, y), shown, font=normal, fill=ink)
            x += draw.textlength(shown, font=normal) + 20
            continue
        width = max(220, int(draw.textlength(shown, font=normal)) + 30)
        width = min(width, WIDTH - MARGIN - x)
        draw.text((x + (width - draw.textlength(shown, font=normal)) / 2, y), shown, font=normal, fill=ink)
        draw.line((x, y + 34, x + width, y + 34), fill=ink, width=2)
        draw.text((x + (width - draw.textlength(note, font=under)) / 2, y + 40), note, font=under, fill=ink)
        x += width + 30
    y += 90
    return canvas.crop((0, 0, WIDTH, y + MARGIN // 2))


def jpeg(receipt: dict) -> bytes:
    out = io.BytesIO()
    image(receipt).save(out, "JPEG", quality=92)
    return out.getvalue()


def pdf(receipt: dict) -> bytes:
    """One page as wide as A4 (the picture at about 193 dpi)."""
    out = io.BytesIO()
    image(receipt).save(out, "PDF", resolution=WIDTH / 8.27)
    return out.getvalue()


# ---- the kept receipts ------------------------------------------------------------------------------------------------------------------------

def keep(conn, card_id: int, token: str, receipt: dict, chat_row: Optional[int], now: str, author: str) -> bool:
    """The sent receipt with the order: the picture and the PDF, and the record «Товарный чек № … отправлен в чат». Once per mark of the
    window: a press again after it was sent records nothing twice. True: recorded now."""
    head = title(receipt)
    done = conn.execute(
        "INSERT OR IGNORE INTO order_receipt (order_id, token, title, total, chat_row, image, pdf, made_at, made_by) VALUES (?,?,?,?,?,?,?,?,?)",
        (int(card_id), str(token)[:64], head, float(total(receipt)), chat_row, jpeg(receipt), pdf(receipt), now, author))
    if done.rowcount != 1:
        return False
    from . import chat_deals
    orders.add_event(conn, card_id, now, author, "receipt", "%s отправлен в чат %s, сумма %s руб." % (
        head, chat_deals.ref(chat_row) if chat_row else "", money(total(receipt))))
    conn.commit()
    return True


def kept(conn, card_id: int) -> List[dict]:
    return [{"id": r[0], "title": r[1], "total": r[2], "made_at": r[3], "made_by": r[4]} for r in conn.execute(
        "SELECT id, title, total, made_at, made_by FROM order_receipt WHERE order_id = ? ORDER BY id DESC", (int(card_id),))]


def kept_file(conn, card_id: int, receipt_id: int, kind: str) -> bytes:
    column = {"pdf": "pdf", "jpg": "image"}.get(kind)
    row = conn.execute("SELECT %s FROM order_receipt WHERE id = ? AND order_id = ?" % column, (int(receipt_id), int(card_id))).fetchone() \
        if column else None
    if row is None:
        raise ReceiptError("Такого чека нет.")
    return bytes(row[0])
