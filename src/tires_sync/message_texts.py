"""«Шаблоны сообщений» (the owner 2026-11-02: «вывести шаблоны сообщений в раздел настроек с возможностью корректировки»): every text the
system sends by itself to a client, a point or a manager, in one list.

Two kinds. «template»: a sample of order_templates (the owner's row of `order_template` by that name, the sample as the default; the order's
variables of order_templates.VARIABLES). «code»: a text the code wrote by itself until now; the owner's version is kept in `order_template` by
the same name the same way, the text here stays the default, the variables are the entry's own (the caller gives their values to `text`).
A text whose answer the code reads back (the point's «выдал N» / «на точке N») warns when the words of the answer are gone: the parser
still looks for them.
"""
from __future__ import annotations

from typing import Dict, List

from . import order_templates as templates, store
from .orders import OrderError

TO = {"client": "клиент", "point": "точка", "manager": "менеджер"}
CHANGED = "message_template_changed"              # the audit action of a change (panel/texts.py has its words)


def _sample(key: str, sample: tuple, to: str, when: str, keep=(), needs_row: bool = False) -> dict:
    """`needs_row`: the sender goes only by the owner's row of that name (order_placed: no row, no message), the sample is not its fallback."""
    name, audience, body = sample
    return {"key": key, "name": name, "audience": audience, "to": to, "when": when, "default": body, "kind": "template",
            "variables": dict(templates.VARIABLES), "keep": tuple(keep), "needs_row": needs_row}


def _code(key: str, name: str, audience: str, to: str, when: str, default: str, variables: Dict[str, str], keep=()) -> dict:
    return {"key": key, "name": name, "audience": audience, "to": to, "when": when, "default": default, "kind": "code",
            "variables": variables, "keep": tuple(keep), "needs_row": False}


PLACED_CLIENT, PLACED_MANAGER = templates.SAMPLES[0], templates.SAMPLES[1]
RESERVE_ASK = "reserve_ask"
ASK_NUMBER = "ask_number"
MOVED_NOTE = "moved_note"
UNAGREED = "unagreed"
RETURN_AGREE = "return_agree"
UNTOLD_POINT = "untold_point"
WAIT_FOUND = "wait_found"
_LABEL = {"order_label": "Номер заказа для точки (WebMIM, иначе панели)"}
TEXTS = (
    _sample("placed_client", PLACED_CLIENT, "client", "Сам, после «Оформить в WebMIM» в панели, в чат Авито заказа (и по правилу этапа «Заказ оформлен»).",
            needs_row=True),
    _sample("placed_manager", PLACED_MANAGER, "point", "Сам, после «Оформить в WebMIM» в панели, точке в ВК (и по правилу этапа «Заказ оформлен»).",
            needs_row=True),
    _sample("intercity_placed", templates.INTERCITY_PLACED_SAMPLE, "client", "Сам, после «Оформить в WebMIM» в панели, вместо «Клиенту: заказ "
            "оформлен», если в заказе отмечено «Доставка в другой город»; в чат Авито заказа."),
    _sample("intercity_shipped", templates.INTERCITY_SHIPPED_SAMPLE, "client", "Сам, когда заказ в другой город передан курьеру ТК (точка нажала "
            "«Передал курьеру ТК» или написала «отправил N», или офис перевёл заказ в «Отправлен в ТК»); в чат Авито заказа."),
    _sample("arrived", templates.ARRIVED_SAMPLE, "client", "Сам, когда точка отметила «приехал»: товар на точке."),
    _sample("reserve_client", templates.RESERVE_SAMPLE, "client", "Сам, в последний день резерва с 12:30, в чат Авито заказа."),
    _sample("oth_point", templates.OTH_POINT_SAMPLE, "point", "Когда все шины заказа взяты с ОТХ и заказ переведён сразу на «На точке, ждём клиента»."),
    _sample("oth_ask", templates.OTH_ASK_SAMPLE, "point", "На 7-й день заказа на «На ОТХ», вопросом на «Рабочем месте» точки."),
    _code(WAIT_FOUND, "Клиенту: ожидаемые шины появились в наличии", "client", "client",
          "Сам, когда на этапе «Ожидает наличие» все нужные шины есть в полном количестве: один раз за пребывание на этапе, в рабочие часы, "
          "в чат Авито заказа. Без {name} или {price} текст не уходит: менеджеру ставится задача.",
          "Здравствуйте. «{name}» появилось в наличии у нас на складе, стоимость: {price}. Желаете оформить заказ?",
          {"name": "Наименование шины", "price": "Цена за штуку"}),
    _code(RESERVE_ASK, "Точке: заказ ещё на точке? (последний день резерва)", "manager", "point",
          "В последний день резерва, в начале рабочего дня, в ВК точки. Ответ точки читается: «выдал N» или «на точке N». "
          "Точке в панели вопрос показывается на «Рабочем месте» своими словами, этот текст туда не идёт.",
          "Заказ №{order_label} ({order_summary}) ещё на точке? Ответьте «выдал {order_label}» или «на точке {order_label}».",
          dict(_LABEL, order_summary="Клиент и размеры шин"), keep=("выдал {order_label}", "на точке {order_label}")),
    _code(ASK_NUMBER, "Менеджеру: нет номера заказа WebMIM", "manager", "manager",
          "В последний день резерва, если у заказа нет номера WebMIM, один раз ответственному.",
          "Укажите номер заказа WebMIM по заказу {order_no} ({client})",
          {"order_no": "Номер заказа в панели", "client": "Клиент (имя, иначе телефон)"}),
    _code(MOVED_NOTE, "Точке и офису: заказ переведён на «На ОТХ»", "manager", "point",
          "Сам, когда клиент 12 часов не ответил на напоминание о сроке резерва; точке, владельцу и партнёру.",
          "Заказ №{order_label} переведён на «На ОТХ»: клиент не ответил на напоминание о сроке резерва", dict(_LABEL)),
    _code(UNAGREED, "Точке: возврат поставщику отменён", "manager", "point",
          "Когда владелец или партнёр нажал «Отменить согласование» возврата в заказе.",
          "Возврат по заказу №{order_label} поставщику отменён, шины остаются на точке", dict(_LABEL)),
    _code(RETURN_AGREE, "Офису: согласовать возврат (7-й день на ОТХ)", "manager", "manager",
          "Когда точка ответила «На точке» на вопрос 7-го дня на ОТХ; владельцу и партнёру, с задачей «Согласовать возврат».",
          "Заказ №{order_label} (точка «{point}») 7 дней на ОТХ, шины на точке: согласуйте возврат с поставщиком — {link}",
          dict(_LABEL, point="Точка", link="Ссылка на заказ в панели")),
    _code(UNTOLD_POINT, "Точке: клиенту не сообщили, что шины на точке", "manager", "point",
          "Раз в сутки, если «можно забирать» не ушло клиенту за 30 минут; только точке по заказу её собственного аккаунта Авито.",
          "Заказ №{order_label}: клиенту не сообщили, что шины на точке — {why}. Напишите клиенту в чате Авито.",
          dict(_LABEL, why="Причина (почему не ушло)")),
)
BY_KEY = {t["key"]: t for t in TEXTS}
NAMES = frozenset(t["name"] for t in TEXTS)
CODE_NAMES = frozenset(t["name"] for t in TEXTS if t["kind"] == "code")       # their rows are not templates of the rules


def taken(name) -> bool:
    """The name is one of the texts here: a template of the rules may not take it (it would become the system's text)."""
    return str(name or "").strip() in NAMES


def entry(key: str) -> dict:
    found = BY_KEY.get(str(key or ""))
    if found is None:
        raise OrderError("Такого шаблона нет.")
    return found


def _row(conn, item: dict):
    return conn.execute("SELECT id, body FROM order_template WHERE name = ? AND audience = ? ORDER BY id LIMIT 1",
                        (item["name"], item["audience"])).fetchone()


def body(conn, key: str) -> str:
    """The text as it goes now: the owner's version if there is one (not a retired sample kept word for word), else the default."""
    item = entry(key)
    row = _row(conn, item)
    return row[1] if row and row[1] not in templates.RETIRED.get(item["name"], ()) else item["default"]


def text(conn, key: str, **values) -> str:
    """A «code» text filled with the values the caller gives (a variable without a value becomes empty)."""
    return templates.PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), "")), body(conn, key))


def listing(conn) -> List[dict]:
    """Every entry with the text it has now and whether the owner changed it."""
    shown = []
    for item in TEXTS:
        now = body(conn, item["key"])
        off = item["needs_row"] and _row(conn, item) is None             # the sender has no row of that name: nothing goes
        shown.append(dict(item, body=now, changed=now != item["default"], to_name=TO[item["to"]], off=off))
    return shown


def check(item: dict, value) -> tuple:
    """(the cleaned text, warnings). Refused (OrderError): empty, too long, a variable the text cannot have."""
    value = str(value or "").replace("\r\n", "\n").strip()
    if not value or len(value) > templates.MAX_BODY:
        raise OrderError("Текст шаблона: от 1 до %d знаков." % templates.MAX_BODY)
    unknown = sorted({m for m in templates.PLACEHOLDER.findall(value) if m not in item["variables"]})
    if unknown:
        raise OrderError("Неизвестная подстановка: %s. В этом шаблоне можно: %s." % (
            ", ".join("{%s}" % u for u in unknown), ", ".join("{%s}" % v for v in item["variables"])))
    lost = [w for w in item["keep"] if w.lower() not in value.lower()]
    warnings = ["В тексте нет «%s»: ответ точки читается по этим словам, без них точка может ответить не так." % "», «".join(lost)] if lost else []
    return value, warnings


def save(conn, key: str, value, who: str, now: str) -> List[str]:
    """The owner's text of the entry (its row of order_template is made when there is none). Returns the warnings; the journal of actions
    gets who, which and before → after."""
    item = entry(key)
    value, warnings = check(item, value)
    was, row = body(conn, key), _row(conn, item)
    if value == was and (row is not None or not item["needs_row"]):      # «Сохранить» of a missing row makes it: the message goes again
        return warnings
    if row is None:
        conn.execute("INSERT INTO order_template (name, audience, body) VALUES (?,?,?)", (item["name"], item["audience"], value))
    else:
        conn.execute("UPDATE order_template SET body = ? WHERE id = ?", (value, row[0]))
    audit(conn, who, now, {"name": item["name"], "from": was, "to": value, "added": row is None and item["needs_row"]})
    return warnings


def reset(conn, key: str, who: str, now: str) -> bool:
    """«Вернуть исходный текст»: the default again (the row stays, a rule may use it). False when it already was."""
    item = entry(key)
    was, row = body(conn, key), _row(conn, item)
    if row is None or row[1] == item["default"]:
        return False
    conn.execute("UPDATE order_template SET body = ? WHERE id = ?", (item["default"], row[0]))
    audit(conn, who, now, {"name": item["name"], "from": was, "to": item["default"], "reset": True})
    return True


def audit(conn, who: str, now: str, detail: dict) -> None:
    """A change of a text in the journal of actions: {"name", "from", "to"} (+ "reset" / "deleted"); commits."""
    store.write_audit(conn, now, who, CHANGED, detail)
