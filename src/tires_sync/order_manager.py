"""What the point managers write to the bot: commands that move an order and free questions for the owner.

The functions take what came in and return the line to answer the point with; delivering it is the channel's job (in VK signed «(бот) », the
owner 2026-10-24: `signed`). The one message that leaves for a client from here is «можно забирать» after an unambiguous «приехал», through the
`deliver` the channel gives (chats.send_reply); its sending (`send_to_client`) also carries «срок резерва истекает» (order_reserve).
Whatever is not unambiguous (no number, no such order, several orders, another point, a word that does not fit the stage) is «спорно»: the
point is asked, the owner sees it in «Входящие» and confirms it there; the client gets nothing until then.
An order of no point is not the points' to see (the owner 2026-10-22): the point is told to ask the manager, nothing waits in «Входящие».
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import Callable, List, Optional, Tuple

from . import chats, connections, order_points as points, order_rules as rules, order_templates as templates, order_wait, orders, store
from .orders import OrderError

MAX_TEXT = 2000
MAX_COMMAND = 1000                                 # what the parser reads of a message: a command is short, a long text never makes it slow
FIRST_WORD = re.compile(r"^\W*([^\W\d_]+(?:-[^\W\d_]+)*)")
ORDER_MARK = r"(?:заказ(?:а|у|е|ом|ы)?(?![а-я])|№|#|номер)"     # «заказчик 3», «как заказывали 4» name no order (review 2026-10-25)
NUMBER_AFTER_WORD = re.compile(r"^\W*[^\W\d_]+(?:-[^\W\d_]+)*[\s,.:;!?-]*" + ORDER_MARK + r"?[\s№#:.-]*(\d{1,9})(?!\d)", re.I)
NUMBER_AFTER_PHRASE = re.compile(r"[\s,.:;!?-]*" + ORDER_MARK + r"?[\s№#:.-]*(\d{1,9})(?!\d)", re.I)
ARRIVED, ISSUED, NO_SHOW = "приехал", "выдан", "клиент не пришёл"
ARRIVE_FROM = ("placed", "moving")                 # the owner 2026-10-21 (order 423): the panel often still says «Заказ оформлен» when the tyres come
ISSUE_FROM = ("at_point",) + ARRIVE_FROM           # the owner 2026-10-21: «выдал» also when the panel never said the tyres came
ISSUE_PHRASE = "отдал клиенту"                     # the owner 2026-10-25: «отдал/отдала/отдали клиенту» is «выдал»
BUILTIN = dict([(w, (ARRIVE_FROM, "at_point")) for w in ("приехал", "приехали", "приехала", "привезли", "пришли")] +   # the words of the
               [(w, (ISSUE_FROM, "done")) for w in ("выдал", "выдали", "выдала", "забрал", "забрали", "забрала", ISSUE_PHRASE)])   # decisions
#  2026-10-19 and of the owner 2026-10-25 (the audit of «Входящие»): they work without being set up in «Команды»
NO_SHOW_PHRASE = re.compile(r"^\W*клиент[а-я]*\s+не\s+приш(?:[её]л|ла|ли)(?![а-я])", re.I)       # «не пришёл заказ» is about goods: not this
# the owner 2026-10-25: «Возврат отдал», «отдал на возврат», «отправлен(о) на возврат», «отправили на возврат» — the tyres went back to the
# supplier. No typo is guessed («лтправлен» stays a question). The client is never written to.
RETURN, RETURNED = "возврат отдал", "шины отданы на возврат"   # the command is a phrase only: a bare «возврат» is not it
RETURN_FROM = ("at_point", "oth")                 # the stages where the tyres are at the point (the lead 2026-10-26), and «Отмена / невыкуп»
                                                  # with the reason «не выкупил» (it was «Отказ/невыкуп» before the merge, the owner 2026-10-29)


def _return_from(card: dict) -> bool:
    return card["stage"]["code"] in RETURN_FROM or (card["stage"]["code"] == "cancel"
                                                    and str(card.get("cancel_reason") or "").strip().casefold().startswith(orders.NOT_BOUGHT))
RETURN_REASON = "точка: шины отданы поставщику"
RETURN_LINE = "точка: шины отданы поставщику (возврат)"
RETURN_ASK = "Точка отдала шины поставщику (возврат): заполните сумму возврата и переведите в «Возврат»."
RETURN_GAPS = "Точка отдала шины поставщику (возврат): не заполнено — %s; заполните и переведите в «Возврат»."
HERE = "на точке"                                  # the point's answer to the question of the last day of the reservation (order_reserve, 2026-10-26)
PHRASES = ((NO_SHOW_PHRASE, NO_SHOW),
           (re.compile(r"^\W*на\s+точке(?![^\W\d_])", re.I), HERE),
           (re.compile(r"^\W*отдал[аи]?\s+клиенту(?![^\W\d_])", re.I), ISSUE_PHRASE),
           (re.compile(r"^\W*(?:возврат\s+отдал[аи]?|(?:отдал[аи]?|отправлен[аоы]?|отправили)\s+на\s+возврат)(?![^\W\d_])", re.I), RETURN))
# «пришли» is also «send» («пришли фото», «пришли нам»), «приехала» is also a car or a client, «отдал клиенту» also money («отдал клиенту
# деньги», review 2026-10-26): after these only the order, the tyres, «все», «шт», «на точку»
STRICT = ("привезли", "пришли", "приехала", ISSUE_PHRASE)
STRICT_TAIL = {"шина", "шины", "колёса", "колеса", "комплект", "все", "всё", "шт", "по", "на", "точку", "точке"}
NOUN = r"(?:шин[аы]|кол[её]са|комплект)(?![^\W\d_])"   # «Шины приехали», «5245392 шина приехала!», «приехали 5259052 шины»
DISPUTE = "спорно: "
BOT_MARK = "(бот) "                                 # the owner 2026-10-24: «Подписывай сообщение от бота вк так: (бот) …» (never to a client)
MISSING = "заказа №%d нет в CRM"                   # the outcome of a number the panel does not know: the partner is told (missing_orders)
MISSING_ANSWER = "Заказа №%d нет в CRM, необходимо добавить заказ и отправить сообщение клиенту"   # the owner 2026-10-25: the point's answer
CANCELLED = "Заказ №%s отменён"                    # the owner 2026-10-25: «Если заказ отменен, надо писать в ответ точке что заказ отменен»
Deliver = Callable[[int, str, str], None]          # (chat row, text, token): chats.send_reply of the chat's account; raises when it did not go
Notify = Callable[[dict, str], str]                # (card, text) -> how it went (order_notify.deliver); never raises


def _point_of(conn, sender):
    row = conn.execute("SELECT id, name FROM point WHERE vk_peer_id != '' AND vk_peer_id = ?", (str(sender),)).fetchone()
    return {"id": row[0], "name": row[1]} if row else None


def _first_word(text) -> str:
    found = FIRST_WORD.match(rules.normalise_word(text))
    return found.group(1) if found else ""


# the owner 2026-10-25 («Привет, приехали», «5259052 приехали»): a greeting before the command is dropped, a number before it is read after it
GREETING = re.compile(r"^\W*(?:(?:привет(?:ик|ствую)?|здра(?:в)?ствуй(?:те)?|здрас(?:ь)?те|здорово|добр(?:ый|ое|ого)\s+(?:день|дня|вечер|вечера|утро|утра"
                      r"|времени\s+суток)|(?:день|вечер)\s+добрый|коллеги|ребята|ребят)(?![^\W\d_])[\s,.:;!—–-]*)+", re.I)   # a closed list, no names
NUMBER_FIRST = re.compile(r"^[\s,.:;!—–-]*(" + ORDER_MARK + r"[\s№#:.-]*\d{1,9}|\d{5,9})(?!\d)[\s,.:;!—–-]*(?=[^\W\d_])",
                          re.I)                                     # «заказали 4», «заказчик 2» are not a number in front  # «2 приехали»: a quantity
LEAD_WORD = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*")
NOUN_FIRST = re.compile(r"^[\s,.:;!—–-]*" + NOUN, re.I)          # the tyres in front of the command, or right after its number
SEPARATORS = " \t\r\n,.:;!—–-"


def _phrase(text):
    """The command of several words a text starts with: (its match, the command) or (None, '')."""
    for pattern, word in PHRASES:
        found = pattern.match(text)
        if found:
            return found, word
    return None, ""


def _drop_noun(text) -> str:
    """«Шины приехали», «шина 5245392 приехала»: the tyres in front of a built-in command (with a number between or not) are dropped."""
    noun = NOUN_FIRST.match(text)
    if not noun:
        return text
    rest = text[noun.end():].lstrip(SEPARATORS)
    first = NUMBER_FIRST.match(rest)
    probe = rest[first.end():] if first else rest
    lead = LEAD_WORD.match(probe)
    return rest if _phrase(probe)[0] or (lead and rules.normalise_word(lead.group(0)) in BUILTIN) else text


def _command_text(text) -> str:
    """The message as the parser reads it: without a greeting in front, a number in front moved after the command («5259052 приехали» is
    «приехали 5259052», «5259052 клиент не пришёл» is «клиент не пришёл 5259052»)."""
    text = str(text or "")[:MAX_COMMAND]
    greeting = GREETING.match(text)
    if greeting and text[greeting.end():].strip():
        text = text[greeting.end():]
    text = _drop_noun(text)
    first = NUMBER_FIRST.match(text)
    if first:
        rest = _drop_noun(text[first.end():])
        lead = _phrase(rest)[0] or LEAD_WORD.match(rest)
        text = "%s %s%s" % (rest[:lead.end()], first.group(1), rest[lead.end():])
    return text


def _word(text) -> str:
    """The command a message starts with: a phrase («клиент не пришёл», «отдал клиенту», «возврат отдал») or the first word."""
    text = _command_text(text)
    found, word = _phrase(text)
    return word if found else _first_word(text)


MARKED_NUMBER = re.compile(ORDER_MARK + r"[\s№#:.-]*(\d{1,9})(?!\d)", re.I)


def order_number(text):
    """The order a message names: the number after «заказ», «№», «#», or the only number right after the command word. Never a guess."""
    text = _command_text(text)
    marked = MARKED_NUMBER.search(text)
    if marked:                                                   # «заказы 123456 и 654321», «заказ 123456, 4 шт», «5000100 и заказ 654321»: none
        rest = text[:marked.start()] + " " + text[marked.end():]
        return None if re.search(r"\d", text[marked.end():]) or re.search(r"\d{5,}", rest) else int(marked.group(1))
    phrase = _phrase(text)[0]
    after = NUMBER_AFTER_PHRASE.match(text, phrase.end()) if phrase else NUMBER_AFTER_WORD.match(text)
    if after and not re.search(r"[^\W\d_]|\d", NOUN_FIRST.sub("", text[after.end():], count=1)):   # «приехали 5259052 шины»
        return int(after.group(1))
    return None


LONG_NUMBER = re.compile(r"(?<!\d)\d{5,}(?!\d)")
SPACED_NUMBER = re.compile(r"(?<!\d)\d{1,3}(?:[ \u00a0]\d{3})+(?!\d)")   # «5 259 052»: never taken as a number, so never a command
NEGATION = ("не", "нет", "нету", "ни", "неа")
WORD_PART = re.compile(r"[^\W\d_]+")
# the owner 2026-10-25: a part is not «можно забирать» — «3 из 4», «только 2», «пока две», «частично»; «приехали 4 шт», «приехали все» stay commands
COUNT = r"(?:\d+(?!\d)|(?:одн[аиоу]|дв[аеу]|тр[её]?[иеху]|четыр[её])[а-я]*(?![^\W\d_]))"   # 2, «одна», «две», «три», «четырёх»; whole words
PART = re.compile(r"(?<!\w)" + COUNT + r"(?:\s*[^\W\d_]+(?![^\W\d_])\.?){0,2}\s*из(?![^\W\d_])"   # «3 шины из 4», «три из четырёх», «3из4»
                  r"|(?<!\d)\d+\s*/\s*\d|(?<!\w)(?:только|пока|лишь)\s+" + COUNT + r"|(?<!\w)частичн", re.I)   # «3/4», «только 2»
DOUBT = ("кроме", "без", "вроде", "наверное", "ли", "другой", "другая", "другое", "другие", "другую", "другого", "других")   # the lead 2026-10-25
ORDER_WORD = re.compile(r"^(?:заказ(?:а|у|е|ом)?|номер)$")
DAMAGE = ("нехватк", "недостач", "брак", "бит", "поврежд", "царап", "порез", "грыж", "слома", "порва", "дефект", "вмятин", "перепута", "ошиб")   # anywhere in a word: «побиты», «разбиты», «перепутали»


def is_command(conn, text) -> bool:
    """A command word, and nothing that makes it doubtful (the owner 2026-10-25: never a guess): a question («приехали?»), a negation
    («приехали не все»; the «не» of «клиент не пришёл» is the command), two different commands («приехали и выдали»), an order number the
    parser did not take («приехали 5259052 шины», two numbers), a part («3 из 4», «только 2»), «приехал клиент». Those are questions for the owner."""
    word = _word(text)
    words = {c["word"] for c in rules.list_commands(conn)}
    if not word or not (word in (NO_SHOW, RETURN, HERE) or word in BUILTIN or word in words) or re.search(r"[?？]", str(text or "")):
        return False
    body = _command_text(text)
    phrase = _phrase(body)[0]
    tokens = WORD_PART.findall(rules.normalise_word(body[phrase.end():] if phrase else body))      # «не-а» is «не» and «а»
    if any(t in NEGATION or t in DOUBT or any(d in t for d in DAMAGE) for t in tokens) or PART.search(body):
        return False
    if word == HERE and any(not ORDER_WORD.match(t) for t in tokens):
        return False                                  # «на точке» and the order only: «на точке нет шин», «на точке клиент» are questions
    target = word if word in (RETURN, HERE) else BUILTIN.get(word, (None, None))[1]
    if target in ("at_point", RETURN) and any(t.startswith("клиент") for t in tokens):
        return False                                  # «приехал клиент» is about the client, not the tyres (the owner 2026-10-25);
    number = order_number(text)                       # «возврат отдал клиенту» is not the supplier's return
    if word in STRICT and any(t not in STRICT_TAIL and not (number is not None and ORDER_WORD.match(t)) for t in (tokens if phrase else tokens[1:])):
        return False                                  # «пришли фото», «приехала машина»: not about the tyres
    meanings = {NO_SHOW if word == NO_SHOW else target} if phrase else set()
    meanings |= {BUILTIN[t][1] if t in BUILTIN else t for t in tokens if t in BUILTIN or t in words}
    meanings |= {RETURN for t in tokens if t.startswith("возврат")} | {"done" for t in tokens if t in ("отдал", "отдала", "отдали")}
    if len(meanings) > 1:
        return False                                  # every number that names an order («заказ 17», «№4», 5+ digits) must be the one taken
    named = LONG_NUMBER.findall(body) + [m.group(1) for m in MARKED_NUMBER.finditer(body)]
    spaced = [n for n in SPACED_NUMBER.findall(body) if len(re.sub(r"\D", "", n)) >= 5]
    return not spaced and all(number is not None and int(n) == number for n in named)


def find_orders(conn, number) -> List[int]:
    """The orders a number names: the WebMIM number the points know, or the panel's own number of an order that has none yet.
    All of them: the caller never picks one."""
    found = sorted({r[0] for r in conn.execute("SELECT id, webmim_number FROM order_card WHERE webmim_number != ''")
                    if re.sub(r"[\s№#]", "", r[1]).isdigit() and int(re.sub(r"[\s№#]", "", r[1])) == number})
    if number not in found and conn.execute("SELECT 1 FROM order_card WHERE id = ? AND webmim_number = ''", (number,)).fetchone():
        found.append(number)                                     # an order with a WebMIM number is known to the points by that one only
    from .order_merge import moved_into
    return sorted({moved_into(conn, i) or i for i in found})     # a source of a merge: its merged order (order_merge, этап 2)


COMMUNITY_LINE = re.compile(r"^\W*(?:\(бот\)\s*)?заказ\s*№?\s*(\d{1,9})(?!\d)", re.I)     # the bot signs its lines «(бот) » (signed)


def line_number(text):
    """The order a line of the community starts with: the bot's «(бот) Заказ №…», the owner's «Заказ 5000100 оформлен…», an alert «Заказ №17: …»."""
    found = COMMUNITY_LINE.match(str(text or ""))
    return int(found.group(1)) if found else None


def find_named(conn, number) -> List[int]:
    """The orders a number the panel itself wrote names (a line of the community, the owner's confirmation): the WebMIM number, else the
    panel's own number of any order."""
    found = find_orders(conn, number)
    if len(found) != 1 and conn.execute("SELECT 1 FROM order_card WHERE id = ?", (number,)).fetchone():
        found = [number]
    return found


def label(card: dict) -> str:
    """The number the point knows the order by: WebMIM's, else the panel's."""
    return str(card.get("webmim_number") or card["id"])


def handle_command(conn, message_id, sender, text, reply_order_no, now: str, deliver: Optional[Deliver] = None, notify: Optional[Notify] = None) -> str:
    """Act on the command a manager writes. Returns the answer for the manager ('' for a stranger)."""
    claimed = conn.execute("INSERT OR IGNORE INTO order_command_log (message_id, at, sender, word, order_id, outcome) VALUES (?,?,?,?,NULL,'обрабатывается')",
                           (str(message_id), now, str(sender), _word(text)))
    conn.commit()
    if claimed.rowcount != 1:
        return "Это сообщение уже обработано."
    state = {"order_id": None, "message_id": str(message_id)}

    def finish(outcome, answer):
        conn.rollback()
        conn.execute("UPDATE order_command_log SET outcome = ?, order_id = ? WHERE message_id = ?", (outcome, state["order_id"], str(message_id)))
        conn.commit()
        return answer

    def dispute(point, why, answer):
        """Not unambiguous: the owner confirms it in «Входящие»; the point is asked."""
        finish(DISPUTE + why, answer)
        conn.execute("INSERT OR IGNORE INTO manager_message (message_id, at, point_id, sender, text, status) VALUES (?,?,?,?,?,'new')",
                     (str(message_id), now, point["id"], str(sender), str(text or "").strip()[:MAX_TEXT]))
        conn.commit()
        return answer

    try:
        return _command(conn, finish, dispute, state, sender, text, reply_order_no, now, deliver, notify)
    except Exception:                                              # whatever happens, the message must not stay «обрабатывается»
        logging.getLogger("tires_sync.orders").exception("the command of message %s failed", message_id)
        return finish("ошибка при выполнении команды", "Не получилось выполнить команду. Напишите владельцу.")


def handle_many(conn, message_id, sender, text, numbers: List[int], now: str, deliver: Optional[Deliver] = None, notify: Optional[Notify] = None) -> str:
    """The word of a point with several lines of the bot forwarded (30.10, Янино: «Приехали» + two «Заказ … оформлен»): one command per
    order, as if the point answered each line (the same checks; «можно забирать» once per arrival; a number unknown or of another point waits
    in «Входящие», the others go on). The journal has a line per order («<message>#<number>») and the message's own («пакет: …»), so a repeat
    of the event, vk_catchup and the sweep see it handled. The answer lists every order."""
    numbers, over = numbers[:MAX_BATCH], numbers[MAX_BATCH:]
    claimed = conn.execute("INSERT OR IGNORE INTO order_command_log (message_id, at, sender, word, order_id, outcome) VALUES (?,?,?,?,NULL,'обрабатывается')",
                           (str(message_id), now, str(sender), _batch_word(_word(text), numbers)))      # a cut batch: note_stuck names its orders
    conn.commit()
    if claimed.rowcount != 1:
        return "Это сообщение уже обработано."
    answers, outcomes = [], []
    for number in numbers:
        part = "%s#%d" % (message_id, number)
        answers.append(handle_command(conn, part, sender, text, number, now, deliver, notify))
        row = conn.execute("SELECT outcome FROM order_command_log WHERE message_id = ?", (part,)).fetchone()
        outcomes.append("№%d: %s" % (number, row[0] if row else "не записано"))
    answers = _numbered(answers, numbers)
    if over:                                                            # never dropped without a word: the point writes them again
        left = ", ".join("№%d" % n for n in over)
        answers.append("Больше %d заказов в одном сообщении: не разобраны: %s. Напишите их отдельным сообщением." % (MAX_BATCH, left))
        outcomes.append("не разобраны (больше %d): %s" % (MAX_BATCH, left))
    conn.execute("UPDATE order_command_log SET outcome = ? WHERE message_id = ?",
                 (("пакет: %d %s; " % (len(numbers), _orders_word(len(numbers))) + "; ".join(outcomes))[:1000], str(message_id)))
    conn.commit()
    return "\n".join(a for a in answers if a)


MAX_BATCH = 10                                     # the orders of one message acted on; the rest are named in the answer


def _batch_word(word: str, numbers: List[int]) -> str:
    """The word of a batch with its orders («приехали (пакет: №5284060, №5287827)»): note_stuck names them when the work is cut."""
    return "%s (пакет: %s)" % (word, ", ".join("№%d" % n for n in numbers))


def _numbered(answers: List[str], numbers: List[int]) -> List[str]:
    """An answer of a batch that does not say its order («Не получилось…») gets «№…: » before it."""
    return [a if not a or str(n) in a else "№%d: %s" % (n, a) for a, n in zip(answers, numbers)]


def _orders_word(n: int) -> str:
    return "заказ" if n % 10 == 1 and n % 100 != 11 else "заказа" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "заказов"


def _command(conn, finish, dispute, state, sender, text, reply_order_no, now: str, deliver, notify) -> str:
    point = _point_of(conn, sender)
    if point is None:
        return finish("отправитель не привязан ни к одной точке", "")
    word = _word(text)
    written = order_number(text)
    if written is not None and written < 100 and not reply_order_no and not MARKED_NUMBER.search(_command_text(text)):
        written = None                                 # «приехали 2»: a quantity, not an order (the lead 2026-10-25); «№5», «заказ 17» are orders
    if reply_order_no and written and find_named(conn, reply_order_no) != find_orders(conn, written):
        return dispute(point, "в ответе заказ №%d, в тексте №%d" % (reply_order_no, written),
                       "Вы ответили на сообщение о заказе №%d, а написали №%d: какой из них? Сообщение передано владельцу." % (reply_order_no, written))
    number = written or reply_order_no
    if number is None:
        if BUILTIN.get(word, ((), None))[1] == "done":                           # the owner 2026-10-26: «выдал» without a number
            return _ask_which(conn, dispute, state, point, sender, word, now)
        return dispute(point, "нет номера заказа", "О каком заказе речь? Напишите номер заказа сразу после слова, например: «%s 123456», "
                                                    "или ответьте на сообщение о заказе." % (word or "приехал"))
    found = find_orders(conn, number) if written else find_named(conn, number)
    own = conn.execute("SELECT id, webmim_number FROM order_card WHERE id = ? AND point_id = ? AND webmim_number != ''",
                       (number, point["id"])).fetchone() if written and not found else None
    if own:                                            # the panel's number of the point's own order, known by its WebMIM one (the owner 2026-10-23)
        state["order_id"] = own[0]
        return finish("внутренний номер заказа: подсказан номер WebMIM", "У этого заказа номер WebMIM %s — напишите его: «%s %s»."
                      % (own[1], word or "приехал", own[1]))
    if not found:
        return dispute(point, MISSING % number, MISSING_ANSWER % number)
    if len(found) > 1:
        return dispute(point, "под №%d несколько заказов (%s)" % (number, ", ".join(map(str, found))),
                       "Под №%d несколько заказов: владелец уточнит." % number)
    card = orders.get_card(conn, found[0])
    state["order_id"] = card["id"]
    if card["point_id"] is None:                       # the owner 2026-10-22: an order of no point is not the points' to see, nor another point's
        answer = finish("заказ без точки: не выполнено", "Заказ №%s пока не назначен вашей точке — уточните у менеджера." % label(card))
        try:
            orders.add_event(conn, card["id"], now, "менеджер %s" % point["name"], "point",
                             "Точка «%s» написала «%s», но заказ не назначен ни одной точке: команда не выполнена" % (point["name"], word))
            conn.commit()
        except sqlite3.Error:                                              # a busy base must not turn the plain answer into an error
            conn.rollback()
            logging.getLogger("tires_sync.orders").warning("the refused command about order %s could not be noted in its journal", card["id"])
        return answer
    if card["point_id"] != point["id"]:
        return dispute(point, "заказ другой точки", "Заказ №%s не на вашей точке; сообщение передано владельцу." % label(card))
    code = card["stage"]["code"]
    if word == HERE:                                   # the client hears «срок резерва истекает» only when the bot asked (order_reserve.here)
        from . import order_reserve
        if code != "at_point":
            return dispute(point, "«%s», а заказ на этапе «%s»" % (HERE, card["stage"]["name"]),
                           CANCELLED % label(card) if code == "cancel" else
                           "Заказ №%s сейчас на этапе «%s», ничего не изменено; сообщение передано владельцу." % (label(card), card["stage"]["name"]))
        if not order_reserve.was_asked(conn, card):    # «на точке» of its own (maybe «приехал»): the owner sees it, nothing else happens
            return dispute(point, "«%s» без вопроса бота" % HERE,
                           "Про заказ №%s бот не спрашивал, ничего не изменено; сообщение передано владельцу. Если шины приехали, напишите «приехал %s»."
                           % (label(card), label(card)))
        return finish(*order_reserve.here(conn, card, "менеджер %s" % point["name"], now, deliver))
    if word == NO_SHOW:
        if code != "at_point":
            return dispute(point, "«%s», а заказ на этапе «%s»" % (NO_SHOW, card["stage"]["name"]),
                           CANCELLED % label(card) if code == "cancel" else
                           "Заказ №%s сейчас на этапе «%s»; сообщение передано владельцу." % (label(card), card["stage"]["name"]))
        return finish(*no_show(conn, card, "менеджер %s" % point["name"], now, notify))
    if word == RETURN:
        if code == "return":
            return finish("уже отмечено", "Заказ №%s уже отмечен: %s." % (label(card), RETURNED))
        if not _return_from(card):
            return dispute(point, "«%s», а заказ на этапе «%s»" % (RETURN, card["stage"]["name"]),
                           CANCELLED % label(card) if code == "cancel" else
                           "Заказ №%s сейчас на этапе «%s»; сообщение передано владельцу." % (label(card), card["stage"]["name"]))
        return finish(*returned(conn, card, "менеджер %s" % point["name"], now))
    command = rules.find_command(conn, card["stage_id"], word)                   # a word the owner set up for this stage comes first
    if command is None and word in BUILTIN:
        sources, target = BUILTIN[word]
        if code == target:                                                       # moved by hand: the client still hears it once
            sent, note = tell_client_arrived(conn, card["id"], deliver, now, "менеджер %s" % point["name"]) if target == "at_point" else (False, "")
            return finish("уже отмечено" + ("; клиенту %s" % ("отправлено" if sent else "не отправлено") if note else ""),
                          ("Заказ №%s уже отмечен: %s. %s" % (label(card), ARRIVED if target == "at_point" else ISSUED, note)).strip())
        if code in sources:
            command = {"to_stage_id": _stage_id(conn, target)}
        elif code == "new" and target == "at_point":                            # not placed yet: nothing can have come to the point
            return dispute(point, "«%s», а заказ ещё не оформлен" % word,
                           "Заказ №%s ещё не оформлен в WebMIM; сообщение передано владельцу." % label(card))
    if command is None or command["to_stage_id"] is None:
        return dispute(point, "слово «%s» не подходит к этапу «%s»" % (word, card["stage"]["name"]),
                       CANCELLED % label(card) if code == "cancel" else
                       "Для заказа №%s на этапе «%s» такая команда не нужна; сообщение передано владельцу." % (label(card), card["stage"]["name"]))
    return finish(*apply(conn, card, command["to_stage_id"], "менеджер %s" % point["name"], now, deliver, merged_reason(conn, command)))


# ---- «выдал» without a number and not a reply (the owner 2026-10-26): the point's one order is asked about, several are listed; nothing
# moves without the point's «да» to the question or a number. The line of «Входящие» stays as before; the «да» closes it.

ISSUE_ASK_KEY = "vk_issue_ask_%s"                 # app_state: the question «Это заказ №…?» to this sender (json)
ISSUE_ASK_FOR = timedelta(minutes=30)
ISSUE_LISTED = 10
SIZE = re.compile(r"(\d{3})\s*/\s*(\d{2})\s*(?:Z?R|/)\s*(\d{2}(?:[.,]5)?)", re.I)


def short(card: dict) -> str:
    """The sizes of the order's tyres («205/55 R16») for a question to the point; else the name of the first position; '' without positions."""
    items = card.get("items") or []
    sizes: List[str] = []
    for item in items:
        found = SIZE.search(item.get("name") or "")
        size = "%s/%s R%s" % found.groups() if found else ""
        if size and size not in sizes:
            sizes.append(size)
    return ", ".join(sizes) if sizes else (str(items[0].get("name") or "")[:40] if items else "")


def _issuable(conn, point_id) -> List[dict]:
    """The point's orders on the stages «выдал» moves from."""
    return [orders.get_card(conn, r[0]) for r in conn.execute(
        "SELECT c.id FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE c.point_id = ? AND s.code IN (%s) ORDER BY c.id"
        % ",".join("?" * len(ISSUE_FROM)), (point_id,) + ISSUE_FROM)]


def _ask_which(conn, dispute, state, point, sender, word, now: str) -> str:
    why = "нет номера заказа"
    found = _issuable(conn, point["id"])
    if len(found) == 1:
        card = found[0]
        answer = dispute(point, why, "Это заказ №%s (%s)? Ответьте «да» или «нет»." % (label(card), short(card) or "без позиций"))
        store.set_state(conn, ISSUE_ASK_KEY % sender, json.dumps({"card": card["id"], "entered_at": card["entered_at"] or "", "at": now,
                                                                  "message_id": state["message_id"], "word": word}, ensure_ascii=False))
        return answer
    if found:
        listed = ", ".join("№%s — %s" % (label(c), short(c) or "без позиций") for c in found[:ISSUE_LISTED])
        more = " и ещё %d" % (len(found) - ISSUE_LISTED) if len(found) > ISSUE_LISTED else ""
        return dispute(point, why, "Какой заказ выдан? %s%s; напишите «выдал <номер>»." % (listed, more))
    return dispute(point, why, "О каком заказе речь? Напишите номер заказа сразу после слова, например: «%s 123456», "
                               "или ответьте на сообщение о заказе." % word)


def _yes_no(text) -> str:
    said = re.sub(r"[\s.!)]+$", "", str(text or "").strip()).casefold()
    return said if said in ("да", "нет") else ""


def issue_answer(conn, message_id, sender, text, now: str, deliver: Optional[Deliver] = None) -> Optional[str]:
    """«да» or «нет» of a point to the bot's «Это заказ №…?»; None when the message is not that (not exactly the word, no question to this
    sender). «да» issues the order only within ISSUE_ASK_FOR, when it is still the point's and on the same stage since the question, and only
    as the point's next message: any other new message of the point ends the question."""
    said = _yes_no(text)
    if said and conn.execute("SELECT 1 FROM order_command_log WHERE message_id = ?", (str(message_id),)).fetchone():
        return ""                                                       # a repeat of a «да» or «нет» handled already
    point = _point_of(conn, sender)
    raw = store.get_state(conn, ISSUE_ASK_KEY % sender) if point else ""
    if not raw:
        return None
    if not said:
        if not is_handled(conn, message_id):                            # a repeat of an earlier message keeps the question
            conn.execute("UPDATE app_state SET value = '' WHERE key = ? AND value = ?", (ISSUE_ASK_KEY % sender, raw))
            conn.commit()
        return None
    try:
        asked = json.loads(raw)
        late = datetime.fromisoformat(now[:19]) - datetime.fromisoformat(str(asked["at"])[:19]) > ISSUE_ASK_FOR
        card_id = int(asked["card"])
    except (ValueError, KeyError, TypeError):
        return None
    claimed = conn.execute("INSERT OR IGNORE INTO order_command_log (message_id, at, sender, word, order_id, outcome) VALUES (?,?,?,?,?,'обрабатывается')",
                           (str(message_id), now, str(sender), said, card_id))
    conn.commit()
    if claimed.rowcount != 1:
        return ""                                                       # a repeat of the same message
    taken = conn.execute("UPDATE app_state SET value = '' WHERE key = ? AND value = ?", (ISSUE_ASK_KEY % sender, raw)).rowcount == 1
    conn.commit()

    def done(outcome, answer):
        conn.rollback()
        conn.execute("UPDATE order_command_log SET outcome = ? WHERE message_id = ?", (outcome, str(message_id)))
        conn.commit()
        return answer
    try:
        return _issue_yes_no(conn, done, said, asked, card_id, taken, late, point, now, deliver)
    except Exception:                                                   # whatever happens, the message must not stay «обрабатывается»
        logging.getLogger("tires_sync.orders").exception("the «%s» of message %s failed", said, message_id)
        return done("ошибка при выполнении команды", "Не получилось выполнить команду. Напишите владельцу.")


def _issue_yes_no(conn, done, said, asked, card_id, taken, late, point, now: str, deliver) -> str:
    try:
        card = orders.get_card(conn, card_id)
    except OrderError:
        card = None
    number = label(card) if card else str(card_id)
    original = str(asked.get("message_id") or "")
    if not taken:
        return done("вопрос уже разобран другим сообщением", "Ответ уже получен, ничего не изменено.")
    if late:
        return done("«%s» позже %d минут: ничего не изменено" % (said, ISSUE_ASK_FOR.seconds // 60),
                    "Вопрос про заказ №%s устарел, ничего не изменено. Если выдали его, напишите «выдал %s»." % (number, number))
    if said == "нет":
        return done("«нет»: не этот заказ, ничего не изменено", "Понятно, ничего не изменено. Напишите «выдал <номер>» или ответьте на сообщение о заказе.")
    if card is None or card["point_id"] != point["id"] or card["stage"]["code"] not in ISSUE_FROM or (card["entered_at"] or "") != asked.get("entered_at"):
        return done("«да», но заказ изменился после вопроса: ничего не изменено",
                    "Заказ №%s уже изменился, ничего не сделано. Если выдали его, напишите «выдал %s»." % (number, number))
    command = rules.find_command(conn, card["stage_id"], asked.get("word") or "")
    if command is not None and command["to_stage_id"] is None:          # as _command: the owner's word that moves nothing on this stage
        return done("«да», но слово «%s» не подходит к этапу «%s»: ничего не изменено" % (asked.get("word"), card["stage"]["name"]),
                    "Для заказа №%s такая команда не нужна, ничего не изменено." % number)
    target = command["to_stage_id"] if command is not None else _stage_id(conn, "done")
    dispute = conn.execute("SELECT outcome FROM order_command_log WHERE message_id = ?", (original,)).fetchone()
    claimed = conn.execute("UPDATE order_command_log SET outcome = 'подтверждается' WHERE message_id = ? AND outcome LIKE ?",
                           (original, DISPUTE + "%")).rowcount == 1          # as confirm(): the owner may have dealt with the «выдал» already
    conn.commit()
    if not claimed:
        return done("«да», но исходное сообщение уже разобрано: ничего не изменено",
                    "Сообщение уже разобрал владелец, ничего не изменено. Если выдали заказ №%s, напишите «выдал %s»." % (number, number))
    try:
        outcome, reply = apply(conn, card, target, "менеджер %s" % point["name"], now, deliver)
    except Exception:
        conn.rollback()
        outcome, reply = "", ""
    if not outcome or outcome.startswith("не "):                        # nothing moved: the line waits for the owner as before
        conn.execute("UPDATE order_command_log SET outcome = ? WHERE message_id = ?", (dispute[0], original))
        conn.commit()
        return done("«да»: %s" % (outcome or "ошибка при выполнении команды"), reply or "Не получилось выполнить команду. Напишите владельцу.")
    conn.execute("UPDATE manager_message SET status = 'answered', answer = ?, answered_at = ? WHERE message_id = ? AND status = 'new'",
                 (reply, now, original))                                 # the line of «Входящие» of the «выдал» without a number is closed
    conn.execute("UPDATE order_command_log SET outcome = ?, order_id = ? WHERE message_id = ?",
                 ("подтверждено точкой («да»): %s" % outcome, card["id"], original))
    conn.commit()
    return done("«да»: %s" % outcome, reply)


def _stage_id(conn, code) -> Optional[int]:
    return next((s["id"] for s in orders.list_stages(conn) if s["code"] == code), None)


def merged_reason(conn, command: Optional[dict]) -> str:
    """The reason a word of the point gives on its move into «Отмена / невыкуп»: «не выкупил» for a word that led to «Отказ/невыкуп» before
    the merge (it lets «возврат отдал» through), '' (apply: «команда точки») for any other."""
    return orders.NOT_BOUGHT + " (команда точки)" if command and command.get("id") and orders.merged_command(conn, command["id"]) else ""


def _not_bought_oth(conn, card: dict, stage_id: int, reason: str) -> Optional[int]:
    """«На ОТХ» for the point's «не выкупил» (`reason` of merged_reason) into «Отмена / невыкуп» of an order whose tyres may be at the point
    (the stages «выдал» is taken from) and that holds no tyres of OTH (the owner 2026-10-30: the tyres stay at the point, as after «Клиент не
    пришёл» → «Да, на ОТХ»); None: the move stays as asked (an order with tyres of OTH is refused by move_card, as before)."""
    if not reason.startswith(orders.NOT_BOUGHT) or card["stage"]["code"] not in ISSUE_FROM or orders.get_stage(conn, stage_id)["code"] != "cancel":
        return None
    from . import oth_sale
    if oth_sale.holds(conn, card["id"]):
        return None
    return _stage_id(conn, "oth")


def apply(conn, card: dict, stage_id: int, who: str, now: str, deliver: Optional[Deliver], reason: str = "") -> Tuple[str, str]:
    """Move the order; on the arrival at the point the client is told at once. (outcome for the log, answer for the point)."""
    try:
        changes = None
        to_oth = _not_bought_oth(conn, card, stage_id, reason)
        stage_id = to_oth or stage_id
        if (to_oth or orders.get_stage(conn, stage_id)["code"] == "cancel") and not str(card.get("cancel_reason") or "").strip():
            changes = {"cancel_reason": reason or "команда точки"}      # a word of the point into «Отмена / невыкуп» (the reason «не выкупил» only
        moved = orders.move_card(conn, card["id"], stage_id, now, who, changes)   # for a word that led to «Отказ/невыкуп» before the merge)
    except OrderError as err:
        return "не выполнено: %s" % err, "Не получилось: %s" % err
    stage = orders.get_stage(conn, stage_id)
    if not moved:
        return "не перенесён: заказ уже на другом этапе", "Заказ №%s уже переместили, проверьте его в панели." % label(card)
    if stage["code"] == "at_point":
        sent, note = tell_client_arrived(conn, card["id"], deliver, now, who)
        return ("заказ перенесён на «%s»; клиенту %s" % (stage["name"], "отправлено" if sent else "не отправлено"),
                "Заказ №%s — отмечено: %s (теперь на этапе «%s»). %s" % (label(card), ARRIVED, stage["name"], note))
    if stage["code"] == "done":
        if card["stage"]["code"] in ARRIVE_FROM:                         # issued straight from «Заказ оформлен» / «Перемещение на точку»
            orders.add_event(conn, card["id"], now, who, "point", "Заказ выдан, минуя «Товар на точке» (был на этапе «%s»)" % card["stage"]["name"])
            conn.commit()
        return "заказ перенесён на «%s»" % stage["name"], "Заказ №%s — отмечено: %s (теперь на этапе «%s»)." % (label(card), ISSUED, stage["name"])
    if to_oth:
        return "заказ перенесён на «%s»" % stage["name"], "Заказ №%s на «%s»: шины остаются на точке нашим товаром." % (label(card), stage["name"])
    return "заказ перенесён на «%s»" % stage["name"], "Готово: заказ №%s на этапе «%s»." % (label(card), stage["name"])


def returned(conn, card: dict, who: str, now: str) -> Tuple[str, str]:
    """«Возврат отдал»: the tyres went back to the supplier (the owner 2026-10-25). The type and the reason of the return are filled when empty,
    the amount never (the lead 2026-10-26): without it the stage stays and the responsible gets a yellow record to finish it. The client is
    never written to. (outcome for the log, answer for the point)."""
    stage = orders.get_stage(conn, _stage_id(conn, "return"))
    changes = {k: v for k, v in (("return_kind", "supplier"), ("return_reason", RETURN_REASON)) if not card[k]}
    gaps = orders.missing_for(dict(card, **changes), stage)
    answer = "Заказ №%s: отмечено — %s" % (label(card), RETURNED)
    if gaps:                                       # the type and the reason are kept, so that the amount is all the manager fills (review 2026-10-26)
        if conn.execute("SELECT 1 FROM order_event WHERE order_id = ? AND kind = 'point' AND text = ? AND at >= ? LIMIT 1",
                        (card["id"], RETURN_LINE, card["entered_at"] or "")).fetchone():
            return "уже отмечено: ждёт суммы возврата", "Заказ №%s уже отмечен: %s." % (label(card), RETURNED)
        if changes:
            orders.update_card(conn, card["id"], changes, now, who, commit=False)
        orders.add_event(conn, card["id"], now, who, "point", RETURN_LINE)
        orders.add_event(conn, card["id"], now, who, "point", RETURN_ASK if gaps == ["сумма возврата"] else RETURN_GAPS % ", ".join(gaps),
                         level="yellow")
        conn.commit()
        return "возврат: этап не изменён, не заполнено: %s (жёлтая запись)" % ", ".join(gaps), answer
    try:
        moved = orders.move_card(conn, card["id"], stage["id"], now, who, changes=changes)
    except OrderError as err:
        return "не выполнено: %s" % err, "Не получилось: %s" % err
    if not moved:
        return "не перенесён: заказ уже на другом этапе", "Заказ №%s уже переместили, проверьте его в панели." % label(card)
    orders.add_event(conn, card["id"], now, who, "point", RETURN_LINE)
    conn.commit()
    return "заказ перенесён на «%s»" % stage["name"], answer


def no_show(conn, card: dict, who: str, now: str, notify: Optional[Notify]) -> Tuple[str, str]:
    """«Клиент не пришёл»: the stage stays; a red record (it burns until somebody presses «Разобрался») and an alert to whoever answers for the order.
    The client is not written to."""
    until = card["reserve_until"]
    point = (card.get("point") or {}).get("name") or "точка не указана"
    text = "Клиент не пришёл за заказом (точка «%s»%s). Свяжитесь с клиентом: продлить резерв (новая дата в заказе) или отказ." % (
        point, ", резерв до %s" % date.fromisoformat(until).strftime("%d.%m.%Y") if until else "")
    orders.add_event(conn, card["id"], now, who, "point", text, level="red")
    conn.commit()
    if notify is not None and points.of_account(conn, card["id"]) is not None:    # the point itself reports it: the staff call the client,
        card = staff_card(conn, card)                                              # not the point again (the owner 2026-10-26)
    told = notify(card, "Заказ №%d: %s" % (card["id"], text)) if notify is not None else "оповещение не отправлено: канал не настроен"
    return "клиент не пришёл: красная запись, %s" % told, "Заказ №%s — отмечено: %s. С клиентом свяжутся." % (label(card), NO_SHOW)


ARRIVED_TEMPLATE = templates.ARRIVED_SAMPLE[0]


def template_body(conn, sample: tuple) -> str:
    """The text of a message that goes by itself: the owner's client template of the sample's name if there is one, else the sample (also in
    place of a retired sample kept word for word: the owner 2026-10-26, «убрать про предоплату»)."""
    row = conn.execute("SELECT body FROM order_template WHERE name = ? AND audience = 'client' ORDER BY id LIMIT 1", (sample[0],)).fetchone()
    return row[0] if row and row[0] not in templates.RETIRED.get(sample[0], ()) else sample[2]


def _arrived_body(conn) -> str:
    """The text of «Клиенту: можно забирать»: the owner's template of that name if there is one, else the sample."""
    return template_body(conn, templates.ARRIVED_SAMPLE)


ARRIVAL = "Поступили шины по заказу №%s (точка «%s»). Клиенту «можно забирать»: %s."


def note_arrival(conn, card_id, who: str, now: str, sent: bool) -> None:
    """The goods came to the point (the point's «приехал» or its button, the owner's confirmation; the owner 2026-10-23, п. 18): a yellow record
    for the bell, so that the responsible manager can write or call the client by hand; once per stay at the point (a repeated «приехал», or
    one after a move by hand, adds nothing more). Never raises: the move is made."""
    try:
        card = orders.get_card(conn, card_id)
        if conn.execute("SELECT 1 FROM order_event WHERE order_id = ? AND kind = 'point' AND text LIKE 'Поступили шины по заказу%' AND at >= ? LIMIT 1",
                        (card_id, card["entered_at"] or "")).fetchone():
            return
        point = (card.get("point") or {}).get("name") or "точка не указана"
        orders.add_event(conn, card["id"], now, who, "point", ARRIVAL % (label(card), point, "отправлено" if sent else "не отправлено"), level="yellow")
        conn.commit()
    except Exception:
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the arrival of card %s could not be noted for the bell", card_id)


def _client_chat(conn, card_id) -> Tuple[Optional[int], str]:
    """The newest Avito chat of the order whose account may write, or why there is none (as order_engine.send_target)."""
    chats = [(r[0], bool(r[1] and r[2])) for r in conn.execute(
        "SELECT c.id, a.chats_enabled, a.chats_send FROM avito_chat c LEFT JOIN account a ON a.id = c.account_id WHERE c.order_id = ? "
        "ORDER BY c.last_at DESC, c.id DESC", (card_id,))]
    if not chats:
        return None, "у заказа нет чата Авито"
    may = [c for c, ok in chats if ok]
    return (may[0], "") if may else (None, "у аккаунта чата выключена отправка ответов")


def _mark(conn, token):
    """The state of a send mark (chat_send_log) or None; a busy base answers None."""
    try:
        return conn.execute("SELECT status FROM chat_send_log WHERE token = ?", (token,)).fetchone() if token else None
    except Exception:
        conn.rollback()
        return None


def send_to_client(conn, card: dict, body: str, token: str, deliver: Optional[Deliver], now: str) -> Tuple[str, str, str]:
    """A message that goes to the client of the order by itself, once per `token` (the send mark). Never raises.
    Returns (status, why, the text to send by hand): "sent"; "unknown" (it may have reached the client: never a second one, nor one by hand);
    "failed" (it did not go: why; the text only when nothing is lacking in it)."""
    copy = ""
    try:
        earlier = conn.execute("SELECT status, at FROM chat_send_log WHERE token = ?", (token,)).fetchone()
        if earlier and earlier[0] == "sent":                        # told already (maybe to a chat that is no longer the newest)
            return "sent", "", ""
        if earlier and (earlier[0] == "unknown" or earlier[0] == "sending" and not chats.sending_is_stale(earlier[1], now)):
            raise chats.ChatSendUnknown("an earlier send of this message is not finished")
        text, missing = templates.render(conn, card, body, now)
        copy = "" if missing else text                            # the text to send by hand; none while values are lacking
        chat, why = _client_chat(conn, card["id"])
        if earlier and earlier[0] == "sending" and (missing or chat is None or deliver is None):
            raise chats.ChatSendUnknown("an interrupted send of this message can be settled only by sending")    # never «send it by hand»
        if missing:
            why = "не хватает: %s" % ", ".join(missing)
        elif chat is not None and deliver is None:
            why = "ключи Авито недоступны"
        elif chat is not None:
            deliver(chat, text, token)
            return "sent", "", ""
        return "failed", why, copy
    except chats.ChatSendUnknown as err:                          # it may have reached the client: a second one by hand would be a double
        return "unknown", (str(err) if isinstance(err, (chats.ChatSendRetry, chats.ChatSendLost)) else "неизвестно, ушло ли"), ""    # a 402 / a lost one: its own words
    except Exception as err:                                      # only the message did not go
        mark = _mark(conn, token)
        if mark and mark[0] == "sent":                            # the same message went out from another thread meanwhile
            return "sent", "", ""
        if mark and mark[0] == "retry":                           # a 402 the sync sends again by itself (L5): never a second one by hand
            return "unknown", "Авито отказал (402), система повторит сама.", ""
        if mark and mark[0] == "lost":                            # not found in the chat: sent again by «Отправить снова» only
            return "unknown", "не дошло: отправьте его снова кнопкой «Отправить снова» в чате.", ""
        if mark and mark[0] in ("sending", "unknown"):            # another thread is sending it, or nobody knows: never a second one by hand
            return "unknown", "отправляется из другого окна или неизвестно, ушло ли", ""
        return "failed", "сбой отправки: %s" % (str(err)[:200] or err.__class__.__name__), copy


def tell_client_arrived(conn, card_id, deliver: Optional[Deliver], now: str, who: str, body: Optional[str] = None) -> Tuple[bool, str]:
    """The point says the goods came: «можно забирать» to the client (_tell_arrived) and «Поступили шины» for the bell (note_arrival), once per
    stay at the point each. Every «приехал» of a point comes here (the command, the button of «Рабочее место», the owner's confirmation), and
    «Шины уже на точке» of an order whose tyres all come from «На ОТХ» (its `body`: the template without «№{webmim_number}», it has none)."""
    sent, note = _tell_arrived(conn, card_id, deliver, now, who, body)
    note_arrival(conn, card_id, who, now, sent)
    return sent, note


def _tell_arrived(conn, card_id, deliver: Optional[Deliver], now: str, who: str, body: Optional[str] = None) -> Tuple[bool, str]:
    """«Можно забирать» to the client of the order, once per arrival (the send token is the order and the moment it reached the point).
    Never raises: when it does not go, a red record in the journal and a line in «Входящие» tell the owner to send it by hand. The line's id
    begins with the send token: the sync closes it once it delivered the message (chats._inbox_done, tails5)."""
    token = ""
    try:
        card = orders.get_card(conn, card_id)
        token = arrived_token(card, now)
        body = body or _oth_arrived_body(conn, card)
    except Exception as err:
        status, why, copy = "failed", "сбой отправки: %s" % (str(err)[:200] or err.__class__.__name__), ""
    else:
        task = arrived_task(conn, card_id, token)
        if task is not None and task["status"] == "done":          # this arrival's message went already (by hand or by itself): never again
            return True, "Клиенту сообщение «можно забирать» по этому приезду уже отправлено."
        if task is not None and task["status"] in ("running", "skipped"):
            return False, "Сообщение «можно забирать» по этому приезду %s: само не отправляется." % (
                "уже отправляется" if task["status"] == "running" else "пропущено вручную")
        if task is not None and _answered_by_hand(conn, token):
            _arrived_close(conn, task, now, who, "владелец ответил во «Входящих»")
            return True, "Сообщение «можно забирать» по этому приезду владелец уже разобрал во «Входящих»."
        status, why, copy = send_to_client(conn, card, body or _arrived_body(conn), token, deliver, now)    # body: «Шины уже на точке» (oth_sale)
        if status == "sent" and task is not None:
            _arrived_close(conn, task, now, who)
        elif status == "failed":
            _arrived_wait(conn, card, token, copy, why, now, own_text=body is not None)
    if status == "sent":
        return True, "Клиенту отправлено сообщение «можно забирать»."
    by_hand = "Проверьте чат Авито заказа: если сообщение там есть, повторно не отправляйте." if status == "unknown" else "Отправьте вручную."
    mark = _mark(conn, token) if status == "unknown" else None
    if mark and mark[0] in ("retry", "lost"):                     # a 402 the sync sends again, a lost one: their own words only (L3)
        by_hand = ""
    try:
        orders.add_event(conn, card_id, now, who, "chat", "Сообщение клиенту не отправлено («%s»): %s" % (ARRIVED_TEMPLATE, why), level="red")
        point = conn.execute("SELECT point_id, webmim_number FROM order_card WHERE id = ?", (card_id,)).fetchone()
        conn.execute("INSERT OR IGNORE INTO manager_message (message_id, at, point_id, sender, text, status) SELECT ?,?,?,?,?,'new' "
                     + chats.NOT_SENT_MEANWHILE,                  # another «приехал» delivered it meanwhile: no line (tails-answers 2)
                     ("%s-%s" % (token or "arrived-%d" % card_id, re.sub(r"\D", "", now)), now, point[0] if point else None, "система",
                      "Заказ №%s: клиенту не отправлено «можно забирать» — %s" % ((point[1] if point and point[1] else card_id),
                                                                                   "%s. %s" % (why, by_hand) if by_hand else why)
                      + (COPY_MARK + copy if copy else ""), token))
        conn.commit()
    except Exception:
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the failed «можно забирать» of card %s could not be noted", card_id)
    return False, "Клиенту сообщение не отправлено: владелец разберётся."


def arrived_by_move(conn, before: dict, deliver: Optional[Deliver], now: str, who: str) -> str:
    """A person moved the order to «Товар на точке» (the board, the card, the chat): «можно забирать» goes as after the point's «приехал» (the
    owner 2026-10-30: «впредь надо делать это автоматически»; the default of the lead of that morning, «a move by hand sends nothing», is
    over) — once per arrival, the same checks. Not from «На ОТХ» without a new client (the old client came back to its own tyres: the point
    tells it). A merge never comes here. Returns the line for the person ('' when nothing was due)."""
    try:
        card = orders.get_card(conn, before["id"])
    except OrderError:
        return ""
    if card["stage"]["code"] != "at_point":
        return ""
    new_client = (card["client_phone"], card["client_name"]) != (before["client_phone"], before["client_name"])
    if before["stage"]["code"] not in ARRIVE_FROM and not (before["stage"]["code"] == "oth" and new_client):
        return ""                                                       # as the point's «приехал» only (review 2026-10-30): never from
                                                                        # «Отмена», «Выдан», «Новый», nor back from «На ОТХ» to its own client
    if not new_client and conn.execute("SELECT 1 FROM chat_send_log WHERE token LIKE ? AND status = 'sent' LIMIT 1",
                                       ("arrived-%d-%%" % card["id"],)).fetchone():
        return ""                                                       # told at an earlier arrival: a move back and forth tells nobody again
    if conn.execute("SELECT 1 FROM order_task WHERE order_id = ? AND kind = 'message' AND title = ? AND status IN ('pending', 'error') "
                    "AND created_at >= ? LIMIT 1", (card["id"], ARRIVED_TEMPLATE, card["entered_at"] or "")).fetchone():
        return "«%s» ждёт в «Сообщениях по заказу» (его приготовило правило этапа)." % ARRIVED_TEMPLATE      # never a second one beside it
    sent, note = tell_client_arrived(conn, card["id"], deliver, now, who)
    return note


def arrived_token(card: dict, now: str) -> str:
    """The send mark of «можно забирать» of one arrival: the order and the moment it reached the point."""
    return "arrived-%d-%s" % (card["id"], re.sub(r"\D", "", card["entered_at"] or now))


def arrived_task(conn, card_id, token: str) -> Optional[dict]:
    """The message task «можно забирать» of this arrival (made when it did not go by itself), or None."""
    from . import order_engine as engine
    row = conn.execute("SELECT id FROM order_task WHERE order_id = ? AND rule_id = ? AND entered_at = ?", (card_id, engine.ARRIVED_RULE, token)).fetchone()
    return engine.get_task(conn, row[0]) if row else None


def _oth_arrived_body(conn, card: dict) -> Optional[str]:
    """«Можно забирать» of an order whose tyres all come from «На ОТХ» (oth_sale) and that has no WebMIM number (it was never ordered):
    the template without «№{webmim_number}», as the reminder of the reservation writes it (order_reserve._client_body); None for any other
    order (its template as it is)."""
    from . import order_merge, order_reserve, oth_sale
    if card.get("webmim_number") or not (oth_sale.taken_ids(conn, card["id"]) or order_merge.placed_ids(conn, card["id"])):
        return None                                                  # a merged order of placed sources has no number of its own either
    if [i for i in oth_sale.supplier_items(conn, card) if int(i["qty"]) >= 1]:
        return None
    return order_reserve.NUMBER.sub("", _arrived_body(conn))


def _arrived_wait(conn, card: dict, token: str, copy: str, why: str, now: str, own_text: bool = False) -> None:
    """«Можно забирать» did not go (no chat, no keys, a value lacking): it waits as a message of the order (the owner 2026-10-29: the text in the
    card, «Скопировать», «Отправлено вручную»); a chat bound later sends it once (arrived_after_link). Never raises."""
    from . import order_engine as engine
    try:
        template = conn.execute("SELECT id FROM order_template WHERE name = ? AND audience = 'client' ORDER BY id LIMIT 1", (ARRIVED_TEMPLATE,)).fetchone()
        conn.execute("INSERT OR IGNORE INTO order_task (order_id, rule_id, entered_at, kind, arg, stage_id, status, title, body, result, created_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (card["id"], engine.ARRIVED_RULE, token, "message", str(template[0]) if template and not own_text else "", card["stage_id"],
                      "pending" if copy else "error", ARRIVED_TEMPLATE, copy, "" if copy else "Не отправлено само: %s." % why, now))
        conn.commit()
    except Exception:
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the waiting «можно забирать» of card %s could not be kept", card["id"])


def _arrived_close(conn, task: dict, now: str, who: str, how: str = "отправлено само") -> None:
    """The arrival's message went by itself (a second «приехал» with the chat there now) or a person dealt with it in «Входящие»: its waiting
    task is done, never sent again."""
    conn.execute("UPDATE order_task SET status = 'done', result = ?, done_at = ? WHERE id = ? AND status IN ('pending', 'error')",
                 (how, now, task["id"]))
    conn.commit()


def _answered_by_hand(conn, token: str) -> bool:
    """A person answered the line of «Входящие» about this arrival's message (the system's «Итог: …» answers are not a person's)."""
    return conn.execute("SELECT 1 FROM manager_message WHERE (message_id = ? OR message_id LIKE ?) AND status = 'answered' AND answer NOT LIKE 'Итог:%'",
                        (token, token + "-%")).fetchone() is not None


def arrived_after_link(conn, card_id, deliver: Optional[Deliver], now: str, who: str) -> str:
    """A chat was bound to the order while it stands on «Товар на точке» and its «можно забирать» of this arrival waits: it is sent once, by itself
    (the owner 2026-10-29). The task is taken first (order_engine.send_task: pending → running), so a second binding, a person's press or the
    sync never send it again; done by hand, skipped or sent already — nothing. Returns a line for the person ('' when nothing waited). Never raises."""
    from . import order_engine as engine
    try:
        card = orders.get_card(conn, card_id)
        if card["stage"]["code"] != "at_point":
            return ""
        token = arrived_token(card, now)
        task = arrived_task(conn, card_id, token)
        if task is None or task["status"] not in ("pending", "error"):
            return ""
        if _answered_by_hand(conn, token):                         # the owner answered its line of «Входящие» (wrote to the client by hand)
            _arrived_close(conn, task, now, who, "владелец ответил во «Входящих»")
            return ""
        if deliver is None:
            return "«Можно забирать» ждёт в «Сообщениях по заказу»: ключи Авито недоступны."
        chat_row = engine.send_task(conn, task["id"], task["body"], deliver, now, who)
        try:
            chats._inbox_done(conn, token, now, "отправлено само после привязки чата")    # the line «отправьте вручную» of «Входящие» is answered
        except Exception:
            conn.rollback()
        return "Клиенту отправлено «можно забирать» в чат Ч-%d (сообщение ждало привязки чата)." % chat_row
    except chats.ChatSendUnknown:                                  # it may have reached the client (a 402 the sync repeats, a lost answer)
        conn.rollback()
        return "«Можно забирать»: неизвестно, ушло ли. Проверьте чат Авито заказа, повторно не отправляйте."
    except (OrderError, chats.ChatError) as err:
        conn.rollback()
        orders.add_event(conn, card_id, now, who, "chat", "Сообщение клиенту не отправлено («%s»): %s" % (ARRIVED_TEMPLATE, str(err)[:300]), level="red")
        conn.commit()
        return "Клиенту «можно забирать» не отправлено: %s Оно ждёт в «Сообщениях по заказу»." % str(err)[:300]
    except Exception:
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("«можно забирать» after binding a chat to card %s", card_id)
        return "«Можно забирать» не отправлено: сбой панели. Оно ждёт в «Сообщениях по заказу»."


def signed(text: str) -> str:
    """A message of the bot to a point in VK, signed «(бот) » once (an auto answer the owner wrote may carry it already)."""
    return text if text.lstrip().startswith(BOT_MARK.strip()) else BOT_MARK + text


MISSING_ALERT = "Точка «%s» сообщила о заказе №%s («%s»), его нет в CRM — добавьте"


def missing_orders(conn, message_id: Optional[str] = None) -> List[dict]:
    """The commands of the points about a number the panel does not know, while their line in «Входящие» waits (the owner 2026-10-24: the partner
    has probably not added the order; they are told). Newest first; `message_id`: that one only. {"id": line of «Входящие», "at", "text"}."""
    sql = ("SELECT m.id, m.at, COALESCE(p.name, ''), l.word, l.outcome FROM manager_message m JOIN order_command_log l ON l.message_id = m.message_id "
           "LEFT JOIN point p ON p.id = m.point_id WHERE m.status = 'new' AND l.outcome LIKE ?" + (" AND m.message_id = ?" if message_id else "")
           + " ORDER BY m.id DESC")
    args = [DISPUTE + "заказа №% нет в CRM%"] + ([str(message_id)] if message_id else [])
    found = []
    for item_id, at, point, word, outcome in conn.execute(sql, args):
        number = re.search(r"№(\d+)", outcome)
        found.append({"id": item_id, "at": at, "text": MISSING_ALERT % (point or "?", number.group(1) if number else "?", word or "сообщение")})
    return found


def list_bot_answers(conn) -> List[dict]:
    return [dict(zip(("id", "keywords", "answer", "enabled"), r)) for r in conn.execute("SELECT id, keywords, answer, enabled FROM bot_answer ORDER BY id")]


def add_bot_answer(conn, keywords, answer) -> int:
    keywords = ", ".join(k for k in (rules.normalise_word(x) for x in str(keywords or "").split(",")) if k)
    answer = str(answer or "").strip()
    if not keywords or len(keywords) > 200:
        raise OrderError("Ключевые слова: от 1 до 200 знаков через запятую.")
    if not answer or len(answer) > MAX_TEXT:
        raise OrderError("Ответ: от 1 до %d знаков." % MAX_TEXT)
    cur = conn.execute("INSERT INTO bot_answer (keywords, answer, enabled) VALUES (?,?,0)", (keywords, answer))
    conn.commit()
    return cur.lastrowid


def set_bot_answer_enabled(conn, answer_id, enabled: bool) -> None:
    conn.execute("UPDATE bot_answer SET enabled = ? WHERE id = ?", (1 if enabled else 0, answer_id))
    conn.commit()


def delete_bot_answer(conn, answer_id) -> None:
    conn.execute("DELETE FROM bot_answer WHERE id = ?", (answer_id,))
    conn.commit()


def _auto_answer(conn, text) -> str:
    low = " %s " % " ".join(re.findall(r"[\w-]+", str(text).lower()))          # whole words only: «да» is not found in «когда»
    for item in list_bot_answers(conn):
        if item["enabled"] and any(k.strip() and " %s " % " ".join(re.findall(r"[\w-]+", k.lower())) in low for k in item["keywords"].split(",")):
            return item["answer"]
    return ""


def handle_message(conn, message_id, sender, text, now: str, typed: Optional[str] = None) -> str:
    """A free question from a manager. It lands in the inbox; the answer text comes back if an enabled auto answer matched, else ''.
    `typed`: what the manager wrote, when `text` carries our own words too (the auto answer looks only at the manager's)."""
    text = str(text or "").strip()[:MAX_TEXT]
    if not text or conn.execute("SELECT 1 FROM manager_message WHERE message_id = ?", (str(message_id),)).fetchone():
        return ""
    point = _point_of(conn, sender)
    reply = _auto_answer(conn, text if typed is None else typed) if point else ""
    status = "ignored" if point is None else ("answered" if reply else "new")
    conn.execute("INSERT OR IGNORE INTO manager_message (message_id, at, point_id, sender, text, status, answer, answered_at) VALUES (?,?,?,?,?,?,?,?)",
                 (str(message_id), now, point["id"] if point else None, str(sender), text if point else "", status, reply, now if reply else None))
    conn.commit()
    return reply


INBOX_FIELDS = ("id", "message_id", "at", "point", "sender", "text", "status", "answer", "answered_at", "outcome", "word", "order_id")


COPY_MARK = "\n\nТекст клиенту:\n"                                # what follows it in a line of «Входящие» is the text to copy (no new column)


def _inbox_row(row) -> dict:
    item = dict(zip(INBOX_FIELDS, row))
    item["text"], _, item["client_text"] = (item["text"] or "").partition(COPY_MARK)
    item["dispute"] = item["outcome"][len(DISPUTE):] if (item["outcome"] or "").startswith(DISPUTE) else ""
    return item


INBOX_SQL = ("SELECT m.id, m.message_id, m.at, COALESCE(p.name, ''), m.sender, m.text, m.status, m.answer, m.answered_at, COALESCE(l.outcome, ''), "
             "COALESCE(l.word, ''), l.order_id FROM manager_message m LEFT JOIN point p ON p.id = m.point_id "
             "LEFT JOIN order_command_log l ON l.message_id = m.message_id WHERE m.status != 'ignored'")


def list_inbox(conn, only_new: bool = False) -> List[dict]:
    """Questions of the points, their commands the owner has to confirm («dispute»: why) and the messages to the client that did not go."""
    return [_inbox_row(r) for r in conn.execute(INBOX_SQL + (" AND m.status = 'new'" if only_new else "") + " ORDER BY m.id DESC")]


def get_message(conn, item_id) -> dict:
    row = conn.execute(INBOX_SQL + " AND m.id = ?", (item_id,)).fetchone()
    if row is None:
        raise OrderError("Такого сообщения нет.")
    return _inbox_row(row)


def new_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM manager_message WHERE status = 'new'").fetchone()[0]


def answer(conn, message_id, text, now: str) -> None:
    """The owner's answer to a question (sending it is up to the channel; here it is only recorded)."""
    text = str(text or "").strip()
    if not text or len(text) > MAX_TEXT:
        raise OrderError("Ответ: от 1 до %d знаков." % MAX_TEXT)
    cur = conn.execute("UPDATE manager_message SET status = 'answered', answer = ?, answered_at = ? WHERE id = ? AND status != 'ignored'", (text, now, message_id))
    if cur.rowcount != 1:
        raise OrderError("Такого вопроса нет.")
    conn.commit()


def confirm(conn, item_id, order_no, now: str, author: str, deliver: Optional[Deliver] = None, notify: Optional[Notify] = None) -> str:
    """The owner confirms a disputed command of «Входящие» for the order they name (WebMIM's number or the panel's): the same as an
    unambiguous command, with its checks (the point's own order, a stage the word fits), the client message of «приехал» included.
    Returns the answer for the point."""
    item = get_message(conn, item_id)
    if not item["dispute"]:
        raise OrderError("Это сообщение не ждёт подтверждения.")
    raw = str(order_no or "").strip().lstrip("№#").strip()
    if not (raw.isascii() and raw.isdigit() and len(raw) <= 9):
        raise OrderError("Укажите номер заказа цифрами.")
    found = find_named(conn, int(raw))
    if len(found) != 1:
        raise OrderError("Заказа №%s нет." % raw if not found else "Под №%s несколько заказов: укажите номер заказа в панели." % raw)
    card = orders.get_card(conn, found[0])
    point = conn.execute("SELECT point_id FROM manager_message WHERE id = ?", (item_id,)).fetchone()[0]
    if card["point_id"] is None:
        raise OrderError("Заказ №%s не назначен ни одной точке: поставьте точку в заказе или переведите его вручную." % label(card))
    if card["point_id"] != point:
        raise OrderError("Заказ №%s не этой точки: исправьте точку в заказе или переведите его вручную." % label(card))
    word, who, code = item["word"], "%s (подтверждение из «Входящих»)" % author, card["stage"]["code"]
    sources, target = BUILTIN.get(word, ((), None))
    command = None if word in (NO_SHOW, RETURN) or target else rules.find_command(conn, card["stage_id"], word)
    fits = (code == "at_point") if word == NO_SHOW else _return_from(card) if word == RETURN else \
        ((code in sources or code == target) if target else command is not None)
    if not fits:
        raise OrderError("Команда «%s» не подходит к заказу №%s на этапе «%s»: переведите заказ вручную и ответьте точке."
                         % (word, label(card), card["stage"]["name"]))
    claimed = conn.execute("UPDATE order_command_log SET outcome = 'подтверждается' WHERE message_id = ? AND outcome LIKE ?",
                           (item["message_id"], DISPUTE + "%"))
    conn.commit()
    if claimed.rowcount != 1:
        raise OrderError("Это сообщение уже разобрано.")
    try:
        if word == NO_SHOW:
            outcome, reply = no_show(conn, card, who, now, notify)
        elif word == RETURN:
            outcome, reply = returned(conn, card, who, now)
            if outcome.startswith("не "):
                raise OrderError(reply)
            reply = reply.replace(": отмечено", ": отмечено (подтвердил владелец)", 1)
        elif target and code == target:                                 # moved by hand already: only what the command adds
            sent, note = tell_client_arrived(conn, card["id"], deliver, now, who) if target == "at_point" else (False, "")
            outcome = "уже на «%s»%s" % (card["stage"]["name"], "; клиенту %s" % ("отправлено" if sent else "не отправлено") if note else "")
            reply = ("Заказ №%s — отмечено: %s. %s" % (label(card), ARRIVED if target == "at_point" else ISSUED, note)).strip()
        else:
            outcome, reply = apply(conn, card, _stage_id(conn, target) if target else command["to_stage_id"], who, now, deliver,
                                   merged_reason(conn, None if target else command))
            if outcome.startswith("не "):
                raise OrderError(reply)
    except Exception:
        conn.rollback()
        conn.execute("UPDATE order_command_log SET outcome = ? WHERE message_id = ?", (DISPUTE + item["dispute"], item["message_id"]))
        conn.commit()
        raise
    reply = reply.replace("— отмечено", "— отмечено (подтвердил владелец)", 1)
    conn.execute("UPDATE manager_message SET status = 'answered', answer = ?, answered_at = ? WHERE id = ?", (reply, now, item_id))
    conn.execute("UPDATE order_command_log SET outcome = ?, order_id = ? WHERE message_id = ?", ("подтверждено (%s): %s" % (author, outcome), card["id"], item["message_id"]))
    conn.commit()
    return reply


def note_stuck(conn, now: str, before: str) -> int:
    """Commands of the points that began before `before` and never ended (the work after VK's «ok» was cut): a line in «Входящие» each,
    once. Returns how many."""
    made = 0
    for message_id, at, word, order_id, sender, outcome in conn.execute(
            "SELECT message_id, at, word, order_id, sender, outcome FROM order_command_log WHERE outcome = 'обрабатывается' AND at < ?",
            (before,)).fetchall():                      # a confirmation runs in the owner's own request: if it is cut, they see it there
        point = _point_of(conn, sender) or _handoff_point_of(conn, message_id) or _panel_point_of(conn, sender)
        conn.execute("UPDATE order_command_log SET outcome = 'прервано' WHERE message_id = ? AND outcome = ?", (message_id, outcome))
        made += conn.execute("INSERT OR IGNORE INTO manager_message (message_id, at, point_id, sender, text, status) VALUES (?,?,?,?,?,'new')", (
            "stuck-" + message_id, now, point["id"] if point else None, "система",
            "%s «%s»%s от %s не завершена: обработка прервалась (например, перезапуск панели). Проверьте этап заказа и сообщение клиенту." % (
                "Команда" if outcome == "обрабатывается" else "Подтверждение команды", word, " по заказу №%s" % order_id if order_id else "",
                str(at)[:16].replace("T", " ")))).rowcount
    conn.commit()
    return made


PANEL_SENDER = "панель: "                          # the sender of a handoff from «Рабочее место» without VK: «панель: <логин>»


def _handoff_point_of(conn, message_id):
    """The point of the handoff a command already wrote (the owner may hand over for a point they picked), or None."""
    row = conn.execute("SELECT p.id, p.name FROM order_handoff h JOIN point p ON p.id = h.point_id WHERE h.message_id = ?", (str(message_id),)).fetchone()
    return {"id": row[0], "name": row[1]} if row else None


def _panel_point_of(conn, sender):
    """The point of the panel login a command came from (a handoff from «Рабочее место» of a point without VK), or None."""
    if not str(sender).startswith(PANEL_SENDER):
        return None
    row = conn.execute("SELECT p.id, p.name FROM panel_user_role r JOIN point p ON p.id = r.point_id WHERE r.login = ?",
                       (str(sender)[len(PANEL_SENDER):],)).fetchone()
    return {"id": row[0], "name": row[1]} if row else None


def is_handled(conn, message_id) -> bool:
    """The bot has a trace of this message: a command, a question, a binding."""
    return any(conn.execute("SELECT 1 FROM %s WHERE message_id = ?" % table, (str(message_id),)).fetchone()
               for table in ("order_command_log", "manager_message"))


def note_lost(conn, message_id, sender, text, at: str, now: str) -> bool:
    """A message of a point that was never handled (the work after VK's «ok» was cut): a line in «Входящие»."""
    point = _point_of(conn, sender)
    made = conn.execute("INSERT OR IGNORE INTO manager_message (message_id, at, point_id, sender, text, status) VALUES (?,?,?,?,?,'new')", (
        str(message_id), now, point["id"] if point else None, str(sender),
        ("Сообщение от %s не обработано (обработка прервалась, например перезапуск панели), выполните вручную: %s"
         % (str(at)[:16].replace("T", " "), str(text or "").strip()))[:MAX_TEXT])).rowcount == 1
    conn.commit()
    return made


BIND_CODE = re.compile(r"^[%s]{8}$" % points.CODE_ALPHABET)


def bind(conn, code: str, sender) -> str:
    """A manager sends the code the owner made in «Подключения → Точки»: their VK becomes the point's. Returns the answer for them."""
    try:
        point = points.bind_vk(conn, code, sender)
    except points.PointError as err:
        return str(err)
    return "ВК привязан к точке «%s». Команды: «приехал 123456», «выдал 123456», «клиент не пришёл 123456»." % point["name"]


def dispatch(conn, message_id, sender, text, reply_order_no, now: str, deliver: Optional[Deliver] = None, notify: Optional[Notify] = None,
             voice: str = "", transcript: str = "", voice_link: str = "", reply_nos=()) -> str:
    """The entry point for a channel: a binding code binds the sender to its point, a command word acts on an order, anything else is a question
    for the owner. A voice without VK's transcript is a question too (the owner 2026-10-21: never an order); `voice_link` is its record."""
    code = str(text or "").strip().upper()
    if BIND_CODE.match(code) and _point_of(conn, sender) is None:
        answer = bind(conn, code, sender)
        conn.execute("INSERT OR IGNORE INTO order_command_log (message_id, at, sender, word, order_id, outcome) VALUES (?,?,?,'код привязки',NULL,?)",
                     (str(message_id), now, str(sender), answer[:200]))
        conn.commit()
        return answer
    if is_button(text) and _point_of(conn, sender) is not None:          # «Передать под заказ» of the bot's keyboard: the next message is the client
        return ask_client(conn, message_id, sender, now)
    answered = issue_answer(conn, message_id, sender, text, now, deliver)  # «да»/«нет» to «Это заказ №…?»
    if answered is not None:
        return answered
    handed = parse_handoff(text) or (parse_handoff(transcript) if voice == "done" else None)     # a transcript: read for a handoff only, never a command
    if not handed and waiting(conn, sender, now):                        # after the button: «+7… что нужно» without the word «клиент»
        handed = parse_handoff("клиент " + str(text or "").strip()) or (parse_handoff("клиент " + transcript) if voice == "done" else None)
    if handed and store.get_state(conn, WAIT_KEY % sender):
        store.set_state(conn, WAIT_KEY % sender, "")
    first = not is_handled(conn, message_id)                             # a repeat of the event writes and answers nothing new
    if handed and _point_of(conn, sender) is not None:                   # «клиент <телефон> <что нужно>», typed or in VK's transcript
        answer = handoff(conn, message_id, sender, handed, voice if voice == "done" else "", now, notify)     # a voice in work does not wipe what was typed
        if voice == "pending" and first:
            _note_voice(conn, message_id, voice_link, now)
        return answer
    if voice == "pending":                                                # a voice without a transcript: «Входящие», with the record and what was typed
        reply = handle_message(conn, message_id, sender, voice_line(text, voice_link), now, typed=str(text or "").strip())
        if not reply and first and _point_of(conn, sender) is not None and voice_answer_on(conn):
            reply = VOICE_ANSWER                                          # on (the owner 2026-10-21): the point is asked to type the client
        return reply
    if voice == "done" and not str(text or "").strip():                 # any other voice is a question for the owner, with its transcript
        return handle_message(conn, message_id, sender, "(голосовое) %s" % transcript, now)
    if len(reply_nos) > 1 and is_command(conn, text):                  # the word with several of the bot's lines forwarded
        written = order_number(text)
        if not written:
            return handle_many(conn, message_id, sender, text, list(reply_nos), now, deliver, notify)
        reply_order_no = written if written in reply_nos else reply_nos[0]       # a number of its own: that order; one not forwarded:
                                                                                  # «в ответе №…, в тексте №…» waits for the owner
    if is_command(conn, text) and (reply_order_no or order_number(text) or len(_command_text(text).split()) <= 3):    # a long text without a number is a question
        return handle_command(conn, message_id, sender, text, reply_order_no, now, deliver, notify)
    reply = handle_message(conn, message_id, sender, text, now)
    return reply or (hint(conn, sender, now) if first else "")          # silence leaves the point without the keyboard: the hint brings it


# ---- the keyboard of the bot: «Передать под заказ» in one press, then one message «+7… что нужно» (the owner 2026-10-23, п. 20) ----------
# VK shows the keyboard only when the community has «Возможности ботов» on; until then a message with it is refused (912) and the channel
# sends the answer without it (vk_callback): «клиент <телефон> <что нужно>» works either way.

BUTTON = "Передать под заказ"
KEYBOARD = json.dumps({"one_time": False, "buttons": [[{"action": {"type": "text", "label": BUTTON, "payload": json.dumps({"command": "under_order"})},
                                                       "color": "primary"}]]}, ensure_ascii=False)
WAIT_KEY = "vk_under_order_%s"                    # app_state: the moment the point pressed the button
WAIT_FOR = timedelta(minutes=30)
BUTTON_ANSWER = ("Напишите одним сообщением телефон клиента и что нужно, например: +7 900 111-22-33 зима шипы 205/55 R16 Nokian. "
                 "Можно и как раньше: клиент <телефон> <что нужно>.")


HINT = ("Команды: приехал <номер>, выдал <номер>, клиент <телефон> что нужно — или кнопка «%s».\n"
        "Лучше всего — ответом на сообщение о заказе или с номером заказа." % BUTTON)
HINT_KEY = "vk_hint_%s"                           # app_state: the last hint to the point
HINT_EVERY = timedelta(minutes=30)


def hint(conn, sender, now: str) -> str:
    """The answer to a plain message of a point (the owner 2026-10-23: «Кнопка не появилась» after «привет»): the commands, and with them the
    keyboard (vk_callback sends a point's answer with it); once in HINT_EVERY per point, '' to a stranger. The pause is taken in one statement
    (only over the mark that was read): of two messages handled at once, one gets the hint."""
    point = _point_of(conn, sender)
    if point is None:
        return ""
    key = HINT_KEY % point["id"]
    last = store.get_state(conn, key)
    try:
        if last and datetime.fromisoformat(now[:19]) - datetime.fromisoformat(last[:19]) < HINT_EVERY:
            return ""
    except ValueError:
        pass
    taken = conn.execute("INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value WHERE value = ?",
                         (key, now, last or "")).rowcount == 1
    conn.commit()
    return HINT if taken else ""


def hint_failed(conn, sender, at: str) -> None:
    """The hint taken at `at` did not reach the point (VK refused it, no key of the community): its pause is given back, so the point's next
    plain message brings it again. A later hint (another mark) is left as it is."""
    point = _point_of(conn, sender)
    if point is not None:
        conn.execute("UPDATE app_state SET value = '' WHERE key = ? AND value = ?", (HINT_KEY % point["id"], at))
        conn.commit()


def is_button(text) -> bool:
    return " ".join(str(text or "").split()).casefold() == BUTTON.casefold()


def ask_client(conn, message_id, sender, now: str) -> str:
    """The button was pressed: the point is asked for the client, and its next message in WAIT_FOR is read as one."""
    claimed = conn.execute("INSERT OR IGNORE INTO order_command_log (message_id, at, sender, word, order_id, outcome) VALUES (?,?,?,'под заказ',NULL,'ждём клиента')",
                           (str(message_id), now, str(sender))).rowcount == 1
    conn.commit()
    if not claimed:
        return ""
    store.set_state(conn, WAIT_KEY % sender, now)
    return BUTTON_ANSWER


def waiting(conn, sender, now: str) -> bool:
    """The point pressed the button less than WAIT_FOR ago and has not handed a client since."""
    pressed = store.get_state(conn, WAIT_KEY % sender) or ""
    try:
        return bool(pressed) and datetime.fromisoformat(now[:19]) - datetime.fromisoformat(pressed[:19]) <= WAIT_FOR
    except ValueError:
        return False


# ---- clients handed over by the points («клиент <телефон> <что нужно>») ---------------------------------------------------------------
# The owner's decisions 2026-10-21: the client goes to the person on duty that day (the owner / the partner, changed by hand for a day); 15 minutes of
# working time to take it, then a red record. The point gets in VK only the confirmation of the handoff (the owner 2026-10-23, it replaces the
# statuses of 2026-10-21); what became of its client it sees on «Рабочее место».
# Nothing here is sent to a client: only to the point that handed the client over and to the person on duty.

HANDOFF = re.compile(r"^\W*клиент(?:ка)?[\s:,.\-\u2013\u2014]+((\+7|8|7)?[\s()-]*\d{3}[\s()-]*\d{3}[\s-]*\d{2}[\s-]*\d{2})(?!\d)(.*)$",
                     re.I | re.S)                     # «клиент» or «клиентка», a dash or a colon, a whole phone, then what the client needs
TYPO_START = ("7", "88", "89", "0", "1", "2")         # ten such digits are «8»/«7» and a mobile or a city code with a digit missing, not a number
DUTY_KEY, DUTY_TODAY_KEY, HOURS_KEY = "handoff_duty", "handoff_duty_today", "handoff_hours"
DEFAULT_HOURS = (10, 20)                           # Moscow time
TAKE_MINUTES = 15
VOICE_NOTE = "голосовое сообщение, нужна расшифровка"
VOICE_NO_TRANSCRIPT = "(голосовое, расшифровки нет)"
VOICE_LINK = re.compile(r"^https://[A-Za-z0-9.-]+\.(?:userapi\.com|vk\.com|vk\.me|vkuserapi\.com)/[^\s<>\"']{1,500}$")
VOICE_ANSWER = "Голосовое получено, расшифровки нет — напишите текстом: клиент <телефон> <что нужно>"
VOICE_ANSWER_KEY = "orders.voice_answer"                                 # app_state: true = the point is asked to type (the owner decides)
VOICE_ANSWER_DEFAULT = True                                              # the owner 2026-10-21: «Ответ точке на голосовое без расшифровки — включи»


def voice_answer_on(conn) -> bool:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (VOICE_ANSWER_KEY,)).fetchone()
    try:
        return bool(json.loads(row[0])) if row else VOICE_ANSWER_DEFAULT
    except ValueError:
        return VOICE_ANSWER_DEFAULT
def voice_line(text, link) -> str:
    """The line of «Входящие» for a voice without a transcript: the mark, the record (first: a long text never cuts it off), what was typed."""
    link = str(link or "").strip()
    return " ".join(w for w in (VOICE_NO_TRANSCRIPT, link if VOICE_LINK.match(link) else "", str(text or "").strip()) if w)


def _note_voice(conn, message_id, link, now: str) -> None:
    """A handoff typed with a voice still in work: the record goes to the order's journal (what the point said by voice is not lost)."""
    try:                                                                  # the order is saved already: a failure here never keeps the point's answer
        row = conn.execute("SELECT order_id FROM order_command_log WHERE message_id = ? AND order_id IS NOT NULL", (str(message_id),)).fetchone()
        if row:
            record = str(link or "").strip()
            orders.add_event(conn, row[0], now, "система", "handoff", "К сообщению точки приложено голосовое, расшифровки нет%s" % (
                ": " + record if VOICE_LINK.match(record) else "."))
            conn.commit()
    except Exception:
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the voice of message %s could not be noted in its order", message_id)


WEEKDAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")
NOT_SENT = "не отправляется: точке в ВК только подтверждение передачи"       # order_handoff_status: the status is marked, never sent


def parse_handoff(text) -> Optional[dict]:
    """{"phone": "+7…", "need": "…"} for «клиент <телефон> <что нужно>», else None (a wrong phone is no handoff: «клиент не пришёл 123456»).
    Ten digits without «8»/«+7» that look like a mobile phone with a digit missing give {"phone": "", "typo": the digits}: never taken silently."""
    if NO_SHOW_PHRASE.match(str(text or "")):
        return None
    found = HANDOFF.match(str(text or "").strip())
    if not found:
        return None
    need = re.sub(r"\s+", " ", found.group(3)).strip(" ,.:;-\u2013\u2014")[:500]
    digits = re.sub(r"\D", "", found.group(1))
    if found.group(2) is None and digits.startswith(TYPO_START):
        return {"phone": "", "typo": digits, "need": need}
    phone = connections.normalize_phone(found.group(1))
    if not phone:
        return None
    return {"phone": phone, "need": need}


def duty_settings(conn) -> dict:
    def load(key, default):
        row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        try:
            return json.loads(row[0]) if row else default
        except ValueError:
            return default
    week = load(DUTY_KEY, {})
    today = load(DUTY_TODAY_KEY, {})
    hours = load(HOURS_KEY, list(DEFAULT_HOURS))
    if not (isinstance(hours, list) and len(hours) == 2 and all(isinstance(h, int) for h in hours) and 0 <= hours[0] < hours[1] <= 24):
        hours = list(DEFAULT_HOURS)
    return {"week": {int(k): str(v) for k, v in week.items() if str(k).isdigit()} if isinstance(week, dict) else {},
            "today": today if isinstance(today, dict) else {}, "hours": hours}


def set_duty(conn, week: dict, hours: Tuple[int, int], today_login: str, today: str) -> None:
    """The person on duty for every day of the week (0 = Monday), the working hours, and a change by hand for `today` only."""
    clean = {}
    for day, login in week.items():
        if int(day) not in range(7):
            raise OrderError("День недели: от понедельника до воскресенья.")
        if str(login or "").strip():
            clean[str(int(day))] = str(login).strip()[:60]
    start, end = int(hours[0]), int(hours[1])
    if not 0 <= start < end <= 24:
        raise OrderError("Рабочие часы: начало раньше конца, от 0 до 24.")
    for key, value in ((DUTY_KEY, clean), (HOURS_KEY, [start, end]),
                       (DUTY_TODAY_KEY, {"date": today, "login": str(today_login).strip()[:60]} if str(today_login or "").strip() else {})):
        conn.execute("INSERT OR REPLACE INTO app_state VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))
    conn.commit()


def on_duty(conn, now: str) -> str:
    """Who gets a client of a point at `now` (Moscow): the change by hand for that day, else the day of the week, else the usual responsible."""
    settings = duty_settings(conn)
    day = now[:10]
    if settings["today"].get("date") == day and settings["today"].get("login"):
        return settings["today"]["login"]
    return settings["week"].get(date.fromisoformat(day).weekday(), "") or orders.default_responsible(conn)


def take_deadline(created: str, hours, minutes: int = TAKE_MINUTES) -> datetime:
    """`minutes` of working time after `created` (Moscow): the clock runs only between the working hours, every day."""
    start, end = hours
    at, left = datetime.fromisoformat(created[:19]), timedelta(minutes=minutes)
    while True:
        opens = at.replace(hour=start, minute=0, second=0, microsecond=0)
        closes = at.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=end)
        if at < opens:
            at = opens
        if at >= closes:
            at = opens + timedelta(days=1)
            continue
        if at + left <= closes:
            return at + left
        left -= closes - at
        at = opens + timedelta(days=1)


def handoff(conn, message_id, sender, handed: Optional[dict], voice: str, now: str, notify: Optional[Notify] = None, source: str = "vk",
            point: Optional[dict] = None) -> str:
    """A client handed over by a point: an order on «Передано с точки» for the person on duty, who is told in the order and in Telegram.
    The message is claimed first: a repeat of the event makes nothing. `handed` may carry what the client wants (size, season, brand,
    comment: handoff_fields); `source`: "vk" or "page" («Рабочее место»). `point`: the page's own point (it needs no VK), else the sender's."""
    point = {"id": point["id"], "name": point["name"]} if point else _point_of(conn, sender)
    claimed = conn.execute("INSERT OR IGNORE INTO order_command_log (message_id, at, sender, word, order_id, outcome) VALUES (?,?,?,'клиент',NULL,'обрабатывается')",
                           (str(message_id), now, str(sender)))
    conn.commit()
    if claimed.rowcount != 1:
        return "Это сообщение уже обработано."
    phone, typo = (handed or {}).get("phone", ""), (handed or {}).get("typo", "")
    fields = handoff_fields(handed or {})
    if not (handed or {}).get("comment") and (handed or {}).get("need"):          # the VK command: what follows the phone is the comment
        fields["comment"] = re.sub(r"\s+", " ", str(handed["need"])).strip()[:500]
    if voice == "pending":
        fields = dict(fields, comment=VOICE_NOTE)
    elif voice == "done":
        fields = dict(fields, comment=(fields["comment"] + " (из голосового)").strip())
    try:
        card_id, text = _open_handoff(conn, point, str(message_id), str(sender), None, phone, fields, {"kind": source}, bool(voice), "менеджер %s" % point["name"], now)
        if typo:
            orders.add_event(conn, card_id, now, "система", "handoff", "Проверьте телефон: точка написала «%s» — похоже, не хватает цифры; "
                                                                      "телефон в заказ не записан. Если точка пришлёт клиента заново, этот заказ можно закрыть." % typo, level="yellow")
        conn.execute("UPDATE order_command_log SET outcome = 'передан клиент', order_id = ? WHERE message_id = ?", (card_id, str(message_id)))
        conn.commit()
        orders._fire_rules(conn, card_id, now, ())                # the rules of «Передано с точки», as on entering it: they prepare, never send
    except orders.OpenOrderExists as busy:                        # the owner 2026-10-27: one open order per phone and point, the point is told which
        conn.rollback()
        number = label(orders.get_card(conn, busy.order_id))        # the number the point knows; order_id stays empty: nothing of it is this message's
        conn.execute("UPDATE order_command_log SET outcome = ? WHERE message_id = ?", ("открытый заказ №%s" % number, str(message_id)))
        conn.commit()
        return "У клиента уже есть открытый заказ №%s — откройте его." % number
    except Exception:
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the client of message %s could not be handed over", message_id)
        conn.execute("UPDATE order_command_log SET outcome = 'ошибка при передаче клиента' WHERE message_id = ?", (str(message_id),))
        conn.commit()
        return "Не получилось принять клиента. Напишите владельцу."
    _add_want(conn, card_id, fields if fields["size"] else dict(fields, size=fields["comment"]), now, "менеджер %s" % point["name"])   # VK: the size in the text
    _tell_staff(conn, orders.get_card(conn, card_id), "Новый клиент с точки «%s», заказ №%d.\n%s" % (point["name"], card_id, text), notify)
    if voice == "pending":
        return "Принято голосовое, заказ №%d. Расшифруем и возьмём в работу; телефон клиента лучше написать текстом." % card_id
    if typo:
        return ("Принято, заказ №%d, но проверьте номер: «%s» — похоже, не хватает цифры. Напишите заново: клиент <телефон> <что нужно>."
                % (card_id, typo))
    return "Клиент передан: %s, заказ №%d." % (connections.format_phone(phone), card_id)


# ---- «Передать под заказ» (the owner 2026-10-23, п. 6, 7, 20–24): the same handoff from a chat of the point's account, a call (the calls
# lane gives its id), «Рабочее место» or VK, with what the client wants. The person on duty sees «Клиент хочет заказать шины…» in «Уведомления»;
# «взяли в работу» is a mark of the handoff (opened the alert or the order, answered in the chat, called, or by hand), the stage stays.

ASK = "Клиент хочет заказать шины, нужно проверить и предложить."      # the words of the alert (the owner 2026-10-23, п. 20)
HANDOFF_TAG = "передан точкой"
SEASONS = {"winter_studded": ("winter", "yes", "зима, шипы"), "winter_soft": ("winter", "no", "зима, без шипов"), "summer": ("summer", "", "лето")}
SOURCE_NAMES = {"vk": "команда ВК", "page": "«Рабочее место»", "chat": "чат Авито", "call": "звонок"}
EARLY_STAGES = ("new", "talk", "wait")                 # an order of the point's chat still this early is handed over itself, not doubled
WANT_QTY = 4


def handoff_fields(form: dict) -> dict:
    """What the window, the page or VK gave about the tires, cleaned; nothing here is required. `season`: a key of SEASONS or ''."""
    season = str(form.get("season") or "").strip()
    return {"size": " ".join(str(form.get("size") or "").split())[:40], "season": season if season in SEASONS else "",
            "brand": " ".join(str(form.get("brand") or "").split())[:60],
            "comment": re.sub(r"\s+", " ", str(form.get("comment") or "")).strip()[:500]}


def need_text(fields: dict) -> str:
    """One line of what the client wants: «205/55 R16, зима, шипы, Nokian. комментарий»."""
    head = ", ".join(p for p in (fields.get("size", ""), SEASONS[fields["season"]][2] if fields.get("season") else "", fields.get("brand", "")) if p)
    tail = fields.get("comment", "")
    return ("%s. %s" % (head, tail) if head and tail else head or tail)[:500]


def _open_handoff(conn, point: dict, message_id: str, sender: str, card: Optional[dict], phone: str, fields: dict, source: dict, voice: bool,
                  author: str, now: str, client_name: str = "") -> Tuple[int, str]:
    """The order of the handed-over client, not saved yet (the caller commits and fires the rules): a new one on «Передано с точки», or
    `card` (an early order of the point's chat) moved there for the person on duty, its point kept. Returns (order, the line of the alert)."""
    who = on_duty(conn, now)
    stage = orders.ensure_from_point_stage(conn)
    need = need_text(fields)
    if card is None:
        card_id = orders.create_card(conn, {"client_phone": phone, "client_name": client_name, "comment": need, "source": "pvz",
                                            "point_id": str(point["id"]), "responsible": who, "tags": HANDOFF_TAG},
                                     now, author, require_contact=False, commit=False, stage=stage)
    else:
        card_id = card["id"]
        tags = card["tags"] + ([HANDOFF_TAG] if HANDOFF_TAG not in card["tags"] else [])
        changes = {"tags": ",".join(tags[-orders.MAX_TAGS:])}
        if who:                                                   # nobody on duty: the order keeps who answers for it
            changes["responsible"] = who
        busy = orders.open_order_conflict(conn, phone, point["id"], besides=card_id) if phone and not card["client_phone"] else None
        if busy is not None:                                      # one open order per phone and point: the phone stays out, the person on duty sees why
            orders.add_event(conn, card_id, now, author, "handoff", "Точка передала телефон %s, но по нему уже есть открытый заказ №%s: телефон не записан, "
                                                                   "проверьте, не тот же ли это клиент." % (connections.format_phone(phone), label(orders.get_card(conn, busy["id"]))),
                             level="yellow")
        elif phone and not card["client_phone"]:
            changes["client_phone"] = phone
        if need:
            changes["comment"] = ("%s\n%s" % (card["comment"], need)).strip()[:orders.MAX_COMMENT] if card["comment"] else need
        orders.update_card(conn, card_id, changes, now, author, commit=False)
        if conn.execute("UPDATE order_card SET stage_id = ?, entered_at = ?, updated_at = ? WHERE id = ? AND stage_id = ? AND sale_ids = '[]'",
                        (stage["id"], now, now, card_id, card["stage_id"])).rowcount != 1:     # moved or sold meanwhile: nothing is handed over
            raise OrderError("Заказ по чату только что изменили: обновите страницу.")
        orders.add_event(conn, card_id, now, author, "stage", "Этап: «%s» → «%s» (точка передала клиента под заказ)" % (card["stage"]["name"], stage["name"]))
    conn.execute("INSERT INTO order_handoff (order_id, message_id, point_id, sender, phone, need, voice, responsible, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (card_id, message_id, point["id"], sender, phone, need, int(voice), who, now))
    conn.execute("INSERT INTO order_handoff_source (order_id, kind, chat_row, call_id, size, season, brand, comment) VALUES (?,?,?,?,?,?,?,?)",
                 (card_id, source.get("kind", ""), source.get("chat_row"), str(source.get("call_id") or ""), fields.get("size", ""),
                  fields.get("season", ""), fields.get("brand", ""), fields.get("comment", "")))
    where = SOURCE_NAMES.get(source.get("kind", ""), "")
    text = "%s Точка «%s»%s: %s, %s. Ответственный: %s. Взять в работу за %d минут." % (
        ASK, point["name"], " (%s)" % where if where else "", connections.format_phone(phone) or ("телефона нет, есть чат" if source.get("chat_row") else "телефона нет"),
        need or "что нужно — не написано", who or "не назначен", TAKE_MINUTES)
    orders.add_event(conn, card_id, now, author, "handoff", text, level="yellow")
    return card_id, text


def _add_want(conn, card_id: int, fields: dict, now: str, author: str) -> None:
    """A size the point typed becomes a line of «Что нужно клиенту» (4 tires, the season, the brand). A size that is not read stays in the
    comment only; a failure here never undoes the handoff. The wait list looks only at «Ожидает наличие»: nothing goes anywhere from here."""
    found = order_wait.SIZE_IN_TITLE.search(fields.get("size", ""))
    if not found:
        return
    season, studded, _ = SEASONS.get(fields.get("season", ""), ("", "", ""))
    size = tuple(int(g) for g in found.groups())
    if any((w["width"], w["profile"], w["radius"]) == size for w in order_wait.list_wants(conn, card_id)):
        return                                                    # the early order has this line already
    try:
        order_wait.add_want(conn, card_id, {"brand": fields.get("brand", ""), "width": found.group(1), "profile": found.group(2), "radius": found.group(3),
                                            "season": season, "studded": studded, "qty": str(WANT_QTY)}, now, author)
    except Exception:
        conn.rollback()
        logging.getLogger("tires_sync.orders").warning("the size of handed-over order %s could not become a line", card_id, exc_info=True)


def hand_over(conn, point_id: int, source: dict, phone: str, fields: dict, token: str, who: str, now: str, notify: Optional[Notify] = None) -> Tuple[int, str]:
    """«Передать под заказ» from the panel: a chat of the point's account (source {"kind": "chat", "chat_row": …}) or a call ({"kind": "call",
    "call_id": …, "chat_row": optional}). The phone or the chat is needed, the rest is not. The press is claimed by its token: twice makes one
    order. A chat with an early open order of this point hands that order over (no second card); another order of the chat stays as it is,
    a chat without an order is bound to the new one. Returns (order, the answer for the manager); raises OrderError with what is wrong."""
    point = points.get_point(conn, point_id)
    typed = " ".join(str(phone or "").split())
    phone = connections.normalize_phone(typed) if typed else ""
    if typed and not phone:
        raise OrderError("Телефон не распознан: нужен номер из 11 цифр, например +7 900 111-22-33.")
    chat_row = source.get("chat_row")
    chat = chats.get_chat(conn, chat_row) if chat_row else None
    if chat_row and chat is None:
        raise OrderError("Такого чата нет.")
    call_row = source.get("call_id") if source.get("kind") == "call" else None
    if call_row is not None:
        call = conn.execute("SELECT account_id FROM avito_call WHERE id = ?", (call_row,)).fetchone()
        if call is None:
            raise OrderError("Такого звонка нет.")
        if call[0] != point.get("avito_account_id"):
            raise OrderError("Это звонок не на аккаунт вашей точки: передать клиента нельзя.")
    if not phone and chat is None:
        raise OrderError("Укажите телефон клиента.")
    if not re.fullmatch(r"[0-9a-f]{24}", str(token or "")):
        raise OrderError("Форма устарела: обновите страницу.")
    if chat is not None and chat["account_id"] != point.get("avito_account_id"):
        raise OrderError("Это чат не аккаунта вашей точки: передать из него клиента нельзя.")
    if chat is not None and chat["order_id"] and chat["order_id"] > 0:
        owner = conn.execute("SELECT point_id FROM order_card WHERE id = ?", (chat["order_id"],)).fetchone()
        if owner and owner[0] is not None and owner[0] != point["id"]:
            raise OrderError("Заказ этого чата — другой точки: передать клиента нельзя.")
    message_id = "%s-%s" % (source.get("kind", "panel"), token)
    claimed = conn.execute("INSERT OR IGNORE INTO order_command_log (message_id, at, sender, word, order_id, outcome) VALUES (?,?,?,'под заказ',NULL,'обрабатывается')",
                           (message_id, now, "панель: %s" % who))
    conn.commit()
    if claimed.rowcount != 1:
        made = conn.execute("SELECT order_id FROM order_handoff WHERE message_id = ?", (message_id,)).fetchone()
        raise OrderError("Этот клиент уже передан, заказ №%d." % made[0] if made else "Прошлая попытка не удалась: обновите страницу и передайте снова.")
    card = None
    busy = open_handoff_of_chat(conn, chat_row) if chat is not None else None
    if busy is None and call_row is not None:
        busy = open_handoff_of_call(conn, call_row)
    if busy is not None:                                          # the client is in work already (another tab, a second press); a closed one is new
        conn.execute("UPDATE order_command_log SET outcome = 'уже передан', order_id = ? WHERE message_id = ?", (busy, message_id))
        conn.commit()
        raise OrderError("Клиент по этому %s уже передан, заказ №%d." % ("звонку" if chat is None else "чату", busy))
    if chat is not None and chat["order_id"] and chat["order_id"] > 0:
        current = orders.get_card(conn, chat["order_id"])
        if (current["point_id"] == point["id"] and current["stage"]["kind"] == "open" and current["stage"]["code"] in EARLY_STAGES
                and not current["sale_ids"]):
            card = current
    author = "менеджер %s (%s)" % (point["name"], who)
    from . import chat_deals
    try:
        card_id, text = _open_handoff(conn, point, message_id, point.get("vk_peer_id") or "", card, phone, fields,
                                      {"kind": source.get("kind", ""), "chat_row": chat_row, "call_id": source.get("call_id")}, False, author, now,
                                      client_name=chat_deals.client_name_for(conn, chat) if chat is not None else "")
        if card is None:                                          # asked again inside the write (хвост З10b): two tabs at once make one order
            other = open_handoff_of_chat(conn, chat_row, besides=card_id) if chat is not None else None
            if other is None and call_row is not None:
                other = open_handoff_of_call(conn, call_row, besides=card_id)
            if other is not None:
                raise OrderError("Клиент по этому %s уже передан, заказ №%d." % ("звонку" if chat is None else "чату", other))
        conn.execute("UPDATE order_command_log SET outcome = 'передан клиент', order_id = ? WHERE message_id = ?", (card_id, message_id))
        conn.commit()
    except OrderError as err:                                     # the order of the chat changed meanwhile: said as it is
        conn.rollback()
        if isinstance(err, orders.OpenOrderExists):               # one open order per phone and point: named by the number the point knows
            err = OrderError("У клиента уже есть открытый заказ №%s — откройте его." % label(orders.get_card(conn, err.order_id)))
        conn.execute("UPDATE order_command_log SET outcome = ? WHERE message_id = ?", (("не передан: %s" % err)[:200], message_id))
        conn.commit()
        raise err
    except Exception:
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the client of %s could not be handed over", message_id)
        conn.execute("UPDATE order_command_log SET outcome = 'ошибка при передаче клиента' WHERE message_id = ?", (message_id,))
        conn.commit()
        raise OrderError("Не получилось передать клиента. Напишите владельцу.")
    if chat is not None and card is None and not (chat["order_id"] and chat["order_id"] > 0):
        try:                                                      # the chat goes with the client; one bound meanwhile elsewhere stays there
            from . import chat_deals
            chat_deals.bind_chat(conn, card_id, chat_row, "handoff", now, author)
        except Exception:
            conn.rollback()
            logging.getLogger("tires_sync.orders").exception("the chat %s could not be bound to handed-over order %s", chat_row, card_id)
    orders._fire_rules(conn, card_id, now, ())
    _add_want(conn, card_id, fields, now, author)
    _tell_staff(conn, orders.get_card(conn, card_id), "Новый клиент с точки «%s», заказ №%d.\n%s" % (point["name"], card_id, text), notify)
    return card_id, ("Клиент передан, заказ №%d: %s. Что с ним стало — на «Рабочем месте»." % (
        card_id, "этот заказ теперь у дежурного менеджера" if card is not None else "клиента возьмёт дежурный менеджер"))


def open_handoff_of_chat(conn, chat_row, besides: Optional[int] = None) -> Optional[int]:
    """The open order of a client handed over from this chat (or bound to it), or None: then the chat may be handed over (again).
    `besides`: an order not to count (the one being made)."""
    row = conn.execute("SELECT h.order_id FROM order_handoff h JOIN order_card c ON c.id = h.order_id JOIN order_stage g ON g.id = c.stage_id "
                       "LEFT JOIN order_handoff_source s ON s.order_id = h.order_id "
                       "WHERE (s.chat_row = ? OR h.order_id = (SELECT order_id FROM avito_chat WHERE id = ?)) AND g.kind = 'open' AND h.order_id != ? "
                       "ORDER BY h.order_id DESC LIMIT 1", (chat_row, chat_row, besides if besides is not None else -1)).fetchone()
    return row[0] if row else None


def open_handoff_of_call(conn, call_row, besides: Optional[int] = None) -> Optional[int]:
    """The open order of a client handed over from this call (avito_call row), or None. `besides`: an order not to count."""
    row = conn.execute("SELECT h.order_id FROM order_handoff h JOIN order_handoff_source s ON s.order_id = h.order_id JOIN order_card c ON c.id = h.order_id "
                       "JOIN order_stage g ON g.id = c.stage_id WHERE s.kind = 'call' AND s.call_id = ? AND g.kind = 'open' AND h.order_id != ? "
                       "ORDER BY h.order_id DESC LIMIT 1", (str(call_row), besides if besides is not None else -1)).fetchone()
    return row[0] if row else None


def handed_calls(conn, call_rows) -> dict:
    """{call row: {"order": the latest order of the client handed over from it, "open": a handed order of it is still open}} — «передан
    точкой» by the calls (the list, the client's card); a call whose handed order is closed may be handed over again."""
    rows = [str(r) for r in call_rows]
    if not rows:
        return {}
    found = conn.execute("SELECT s.call_id, MAX(s.order_id), MAX(g.kind = 'open') FROM order_handoff_source s JOIN order_card c ON c.id = s.order_id "
                         "JOIN order_stage g ON g.id = c.stage_id WHERE s.kind = 'call' AND s.call_id IN (%s) GROUP BY s.call_id"
                         % ",".join("?" * len(rows)), rows).fetchall()
    return {int(r[0]): {"order": r[1], "open": bool(r[2])} for r in found}


def chat_phone(conn, chat_row) -> str:
    """The client's phone for the window: the one of the chat's order, else the first one the client wrote in the chat."""
    from . import chat_deals
    chat = chats.get_chat(conn, chat_row)
    if chat is None:
        return ""
    if chat["order_id"] and chat["order_id"] > 0:
        row = conn.execute("SELECT client_phone FROM order_card WHERE id = ?", (chat["order_id"],)).fetchone()
        if row and row[0]:
            return connections.normalize_phone(row[0]) or row[0]
    own = chat_deals.own_phones(conn)
    return next((p for p in (chat_deals.extract_phone(t, own) for t in chat_deals._incoming_texts(conn, chat_row)) if p), "")


def take(conn, order_id, how: str, who: str, now: str) -> bool:
    """«Взяли в работу» for a handed-over client (the lead 2026-10-23: a mark of the handoff and a record, no move of the stage): once, only
    while the order is still on «Передано с точки» (a later stage is «взяли» by itself). The caller makes sure `who` is not the point.
    True when it is taken now; the point sees it on «Рабочее место»."""
    if not conn.execute("SELECT 1 FROM order_handoff WHERE order_id = ?", (order_id,)).fetchone():
        return False
    row = conn.execute("SELECT g.code FROM order_card c JOIN order_stage g ON g.id = c.stage_id WHERE c.id = ?", (order_id,)).fetchone()
    if row is None or row[0] != orders.FROM_POINT[0]:
        return False
    conn.execute("INSERT OR IGNORE INTO order_handoff_source (order_id, kind) VALUES (?, '')", (order_id,))      # a handoff made before the source was kept
    if conn.execute("UPDATE order_handoff_source SET taken_at = ?, taken_by = ?, taken_how = ? WHERE order_id = ? AND taken_at IS NULL",
                    (now, str(who)[:60], str(how)[:60], order_id)).rowcount != 1:
        conn.commit()
        return False
    orders.add_event(conn, order_id, now, who, "handoff", "Клиент взят в работу: %s (%s)." % (how, who), level="grey")
    conn.commit()
    return True


def take_chat(conn, chat_row, how: str, who: str, now: str) -> List[int]:
    """«Ответил в чате»: the handed-over orders of this chat (the one it was handed from, the one it is bound to) are taken."""
    ids = {r[0] for r in conn.execute("SELECT order_id FROM order_handoff_source WHERE chat_row = ?", (chat_row,))}
    row = conn.execute("SELECT order_id FROM avito_chat WHERE id = ?", (chat_row,)).fetchone()
    if row and row[0] and row[0] > 0:
        ids.add(row[0])
    return [i for i in sorted(ids) if take(conn, i, how, who, now)]


def handoff_target(conn, order_id) -> Optional[int]:
    """The chat a handed-over client came from (the alert leads there), or None: then the order (its card has the phone and the comment)."""
    row = conn.execute("SELECT chat_row FROM order_handoff_source WHERE order_id = ?", (order_id,)).fetchone()
    return row[0] if row and row[0] else None


def handoff_client(conn, order_id) -> Optional[int]:
    """The client (CRM card: the calls with their recordings, the chats) of a client handed over from a call, or None."""
    row = conn.execute("SELECT l.client_id FROM order_handoff_source s JOIN client_call l ON l.call_row = CAST(s.call_id AS INTEGER) "
                       "WHERE s.order_id = ? AND s.kind = 'call'", (order_id,)).fetchone()
    return row[0] if row else None


def take_call(conn, call_row, how: str, who: str, now: str) -> List[int]:
    """«Прослушал звонок»: the handed-over orders of this call are taken."""
    ids = [r[0] for r in conn.execute("SELECT order_id FROM order_handoff_source WHERE kind = 'call' AND call_id = ?", (str(call_row),))]
    return [i for i in sorted(ids) if take(conn, i, how, who, now)]


CALLED = "звонки Авито"


def _called_back(conn, order_id: int, phone: str, created: str) -> bool:
    """The client talked with us after the handoff: an answered call from the client's number to an account of no point (the owner's, the partner's)
    — «позвонил» (the lead 2026-10-23). A call to any point (the one that handed it over or another) is not «взяли»."""
    if not phone:
        return False
    for buyer, at in conn.execute("SELECT k.buyer_phone, k.call_at FROM avito_call k WHERE k.answered = 1 AND k.call_at > ? AND k.buyer_phone LIKE ? "
                                  "AND NOT EXISTS (SELECT 1 FROM point p WHERE p.avito_account_id = k.account_id) ORDER BY k.call_at",
                                  (created, "%" + phone[-10:])):
        if connections.normalize_phone(buyer) == phone:
            return take(conn, order_id, "позвонил (звонок %s)" % str(at).replace("T", " ")[:16], CALLED, at)
    return False


POINT_STATUS = {"": "передан, ждёт", "taken": "взяли в работу", "placed": "оформлен заказ", "done": "выдан", "refused": "отказ",
                "cancelled": "отменён точкой"}
CANCELLED_BY = "отменено точкой"                   # the mark in order_handoff_source.taken_by: the handoff was cancelled, nobody may take it
CANCEL_REASON = "Точка отменила передачу клиента"
CANCELLED_MARK = "отменила передачу клиента"       # in the yellow record of the cancel (routes_alerts keeps it off the point's bell)


def cancellable(conn, order_id, point_id) -> bool:
    """The point may cancel its handoff (the owner 2026-10-29) while the manager has not taken it: the order was made by the handoff of this
    point (an early order of a chat handed over is not: it was there before), it stands on «Передано с точки» and nobody took it."""
    row = conn.execute("SELECT h.created_at, c.created_at, g.code, s.taken_at FROM order_handoff h JOIN order_card c ON c.id = h.order_id "
                       "JOIN order_stage g ON g.id = c.stage_id LEFT JOIN order_handoff_source s ON s.order_id = h.order_id "
                       "WHERE h.order_id = ? AND h.point_id = ?", (order_id, point_id)).fetchone()
    return row is not None and row[0] == row[1] and row[2] == orders.FROM_POINT[0] and row[3] is None


def cancel_handoff(conn, order_id, point: dict, who: str, now: str, notify: Optional[Notify] = None) -> str:
    """«Отменить передачу»: the order of the handoff goes to «Отмена» and the person who would take it is told, in the panel (a yellow record
    of the order) and through the channel of the handoff itself (_tell_staff). The mark that nobody may take it any more, the record and the
    move are one transaction: a manager taking the client at the same moment either takes it first (then it is not cancelled) or takes nothing.
    Returns the answer for the point; raises OrderError when it cannot be cancelled. The client hears nothing."""
    if not cancellable(conn, order_id, point["id"]):
        raise OrderError("Передачу уже не отменить: клиента взяли в работу, заказ перенесли или он не из этой передачи.")
    cancel = next((s for s in orders.list_stages(conn) if s["code"] == "cancel"), None)
    if cancel is None:
        raise OrderError("В воронке нет этапа «Отмена»: напишите владельцу.")
    conn.execute("INSERT OR IGNORE INTO order_handoff_source (order_id, kind) VALUES (?, '')", (order_id,))      # a handoff made before the source was kept
    if conn.execute("UPDATE order_handoff_source SET taken_at = ?, taken_by = ?, taken_how = 'отмена' WHERE order_id = ? AND taken_at IS NULL "
                    "AND EXISTS (SELECT 1 FROM order_card c JOIN order_stage g ON g.id = c.stage_id WHERE c.id = ? AND g.code = ?)",
                    (now, CANCELLED_BY, order_id, order_id, orders.FROM_POINT[0])).rowcount != 1:     # taken or moved a moment ago
        conn.rollback()
        raise OrderError("Клиента только что взяли в работу или перенесли: передачу уже не отменить.")
    author = "менеджер %s (%s)" % (point["name"], who)
    text = "Точка «%s» %s (%s): заказ №%d переведён в «Отмена», брать в работу не нужно." % (point["name"], CANCELLED_MARK, who, order_id)
    orders.add_event(conn, order_id, now, author, "handoff", text, level="yellow")          # saved with the move (move_card commits it all)
    conn.execute("INSERT OR IGNORE INTO order_handoff_status (order_id, status, at, outcome) VALUES (?, 'cancelled', ?, '')", (order_id, now))
    try:
        moved = orders.move_card(conn, order_id, cancel["id"], now, author, changes={"cancel_reason": CANCEL_REASON}, fire_rules=False)
    except OrderError:
        conn.rollback()
        raise
    if not moved:                                                 # moved meanwhile: move_card rolled the mark and the record back (it commits
        # instead when the order already stands on the target stage: the UPDATE above makes sure it is on «Передано с точки», never «Отмена»)
        conn.rollback()
        raise OrderError("Заказ только что перенесли: передачу уже не отменить.")
    _tell_staff(conn, orders.get_card(conn, order_id), "Заказ №%d: %s" % (order_id, text), notify)
    return "Передача отменена: заказ №%d в «Отмена», менеджеру сообщено." % order_id


def point_handoffs(conn, point_id, since: str, limit: int = 50) -> List[dict]:
    """The clients this point handed over since `since`, newest first, with what became of them (the point's «Рабочее место»)."""
    placed = next((s for s in orders.list_stages(conn) if s["code"] == "placed"), None)
    found = []
    for order_id, phone, need, created, kind, taken, taken_by in conn.execute(
            "SELECT h.order_id, h.phone, h.need, h.created_at, COALESCE(s.kind, ''), s.taken_at, COALESCE(s.taken_by, '') FROM order_handoff h "
            "JOIN order_card c ON c.id = h.order_id LEFT JOIN order_handoff_source s ON s.order_id = h.order_id "
            "WHERE h.point_id = ? AND h.created_at >= ? ORDER BY h.created_at DESC, h.order_id DESC LIMIT ?", (point_id, since, limit)).fetchall():
        card, into = _handed_card(conn, order_id)
        status = _status_of(card, placed, bool(taken))
        if taken_by == CANCELLED_BY and card["stage"]["code"] == "cancel":
            status = "cancelled"
        found.append({"id": order_id, "label": label(orders.get_card(conn, order_id)) + (" → №%s" % label(card) if into else ""), "phone": phone, "client": card["client_name"], "need": need, "at": created,
                      "source": SOURCE_NAMES.get(kind, ""), "status": status, "status_name": POINT_STATUS[status],
                      "cancellable": not status and cancellable(conn, order_id, point_id)})
    return found


def staff_card(conn, card: dict) -> dict:
    """The card for an alert that is for the staff and never for the point: the person who answers for it; when nobody in the panel does,
    the owner. order_notify would write to the point's VK (or its «Рабочее место») for an order of the point's account (to_staff skips that)
    or for a card whose responsible is not in the panel, and to the point's own user who answers for it (a deal the point made)."""
    from . import order_notify, users
    card = dict(card, to_staff=True)
    who = card.get("responsible") or ""
    role = users.get_role(conn, who) if who and users.exists(conn, who) else None
    if (role and role["role"] == "point") or order_notify.route(conn, card)["channel"] in ("vk", "panel"):
        card["responsible"] = order_notify.get_owner_login(conn)
    return card


def _tell_staff(conn, card: dict, text: str, notify: Optional[Notify]) -> None:
    """An alert about a handed-over client for the staff (staff_card)."""
    if notify is None:
        return
    notify(staff_card(conn, card), text)                          # never raises


def _handed_card(conn, order_id) -> Tuple[dict, bool]:
    """The order a handed client's fate is read from: the merged one when its tyres moved there (order_merge, этап 2), else its own."""
    from .order_merge import moved_into
    into = moved_into(conn, order_id)
    return orders.get_card(conn, into or order_id), bool(into)


def _status_of(card: dict, placed: Optional[dict], taken: bool = False) -> str:
    """What became of a handed-over client: '' (waits), taken, placed, done, refused. `taken`: the mark of take() while the stage stays."""
    stage = card["stage"]
    if stage["code"] == orders.FROM_POINT[0]:
        return "taken" if taken else ""
    if stage["kind"] == "lost":
        return "refused"
    if stage["kind"] == "won":
        return "done"
    if stage["code"] in orders.PLACED_CODES or (placed is not None and stage["kind"] == "open" and stage["position"] >= placed["position"]):
        return "placed"
    return "taken"


def handoff_tick(conn, now: str, *, notify: Optional[Notify] = None) -> dict:
    """The look at the handed-over clients (Moscow `now`): a red record when nobody took one in 15 working minutes. Nothing goes to the point
    (the owner 2026-10-23: only the confirmation of the handoff); what became of the client is marked in order_handoff_status, so that a
    finished one is not looked at again. Never raises for one order: the others go on."""
    made = {"overdue": 0}
    hours = duty_settings(conn)["hours"]
    placed = next((s for s in orders.list_stages(conn) if s["code"] == "placed"), None)
    rows = conn.execute("SELECT h.order_id, h.phone, h.created_at, h.overdue_at, s.taken_at FROM order_handoff h JOIN order_card c ON c.id = h.order_id "
                        "JOIN order_stage g ON g.id = c.stage_id LEFT JOIN order_handoff_source s ON s.order_id = h.order_id "
                        "WHERE NOT EXISTS (SELECT 1 FROM order_handoff_status s WHERE s.order_id = h.order_id AND (s.status = 'done' "
                        "OR (s.status = 'refused' AND g.kind = 'lost'))) "                    # a refused order brought back is looked at again
                        "ORDER BY h.order_id").fetchall()                       # a deleted order is gone from order_card; a finished one is not looked at
    for order_id, phone, created, overdue, taken in rows:
        try:
            card = _handed_card(conn, order_id)[0]                # a source of a merge: the merged order's fate, never «отказ»
            if not taken and card["stage"]["code"] == orders.FROM_POINT[0] and _called_back(conn, order_id, phone, created):
                taken = True                                      # the client talked with the owner or the partner: taken before the 15 minutes count
            status = _status_of(card, placed, bool(taken))
            if not status and overdue is None and take_deadline(created, hours) <= datetime.fromisoformat(now[:19]):
                if conn.execute("UPDATE order_handoff SET overdue_at = ? WHERE order_id = ? AND overdue_at IS NULL", (now, order_id)).rowcount == 1:
                    text = "Клиента с точки не взяли в работу за %d минут рабочего времени (ответственный: %s)." % (TAKE_MINUTES, card["responsible"] or "не назначен")
                    orders.add_event(conn, order_id, now, "система", "handoff", text, level="red")
                    conn.commit()
                    made["overdue"] += 1
                    _tell_staff(conn, card, "Заказ №%d: %s" % (order_id, text), notify)
            if status and not conn.execute("SELECT 1 FROM order_handoff_status WHERE order_id = ? AND status = ?", (order_id, status)).fetchone():
                conn.execute("INSERT OR IGNORE INTO order_handoff_status (order_id, status, at, outcome) VALUES (?,?,?,?)", (order_id, status, now, NOT_SENT))
                conn.commit()                                     # a mark already made writes nothing (the look runs every minute)
        except Exception:
            conn.rollback()
            logging.getLogger("tires_sync.orders").exception("the handed-over client of order %s could not be looked at", order_id)
    return made
