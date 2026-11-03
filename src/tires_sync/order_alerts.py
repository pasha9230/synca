"""Order alerts: the colour of each record of the order journal and what every panel user has already seen.
red: an error, somebody has to act; yellow: a term is near or something needs a look; green: the order moves; grey: service records, never an alert.
A red record stays open for a user until they press «Разобрался» or until the cause is fixed (then it closes for everybody);
the other colours close when the user opens the order. Records written before this module came (order_alert_start) are seen by all.
Nothing here writes to Avito, WebMIM or a client."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

LEVELS = ("red", "yellow", "green", "grey")
ALERT_LEVELS = ("red", "yellow", "green")                       # grey records are never alerts
EVERYBODY = "*"                                                  # the ack login of «the cause is fixed»
GREEN_DAYS = 7                                                   # a green alert older than this is no longer shown (the record stays in the journal)


class AlertError(Exception):
    pass


def level_for(kind: str, text: str) -> str:
    """The colour of a record by its kind and text: for records written without a colour (all of them before this module)."""
    text = text or ""
    if kind == "refused":                                            # a refused move of the stage (orders.move_card)
        return "red"
    if kind in ("created", "stage", "restored"):
        return "green"
    if kind == "sale":
        return "yellow" if text.startswith("Продажа сторнирована") else "green"
    if kind == "chat":
        if text.startswith(("Позиция не", "Сообщение клиенту не", "Фото клиенту не")):
            return "red"
        if text.startswith(("Отправлен ответ клиенту", "Отправлено фото клиенту")):
            return "green"
        return "yellow" if text.startswith("Клиент написал после закрытия") else "grey"
    if kind == "notify":
        return "red" if text.startswith("Оповещение не отправлено") else "grey"
    if kind == "reserve":
        return "yellow"
    if kind == "rule":
        failed = text.startswith("Задача не подготовлена") or text.endswith(": ошибка") or ": ошибка (" in text
        return "red" if failed else "grey"
    if kind == "field":
        return "green" if text.startswith("Резерв: товар на точке") else "grey"
    return "grey"


def check_level(level: str) -> str:
    if level not in LEVELS:
        raise AlertError("Неизвестный цвет записи: %s." % level)
    return level


def level_of(row: dict) -> str:
    return row.get("level") or level_for(row.get("kind", ""), row.get("text", ""))


def _start(conn) -> int:
    row = conn.execute("SELECT last_id FROM order_alert_start").fetchone()
    return int(row[0]) if row else 0


def mark_seen(conn, login: str, order_id: int, now: str, up_to: Optional[int] = None) -> None:
    """The user opened the order: every record up to the last one shown (up_to; the last one when not given) is seen by them;
    red ones still wait for «Разобрался»."""
    sql = "SELECT MAX(id) FROM order_event WHERE order_id = ?" + (" AND id <= ?" if up_to is not None else "")
    last = conn.execute(sql, (order_id, up_to) if up_to is not None else (order_id,)).fetchone()[0]
    if last is None:
        return
    conn.execute("INSERT INTO order_event_seen (login, order_id, last_id, at) VALUES (?,?,?,?) "
                 "ON CONFLICT (login, order_id) DO UPDATE SET last_id = MAX(last_id, excluded.last_id), at = excluded.at",
                 (login, order_id, last, now))


def _red_event(conn, event_id: int) -> dict:
    row = conn.execute("SELECT id, order_id, kind, text, level FROM order_event WHERE id = ?", (event_id,)).fetchone()
    if not row:
        raise AlertError("Запись журнала не найдена.")
    event = {"id": row[0], "order_id": row[1], "kind": row[2], "text": row[3], "level": row[4]}
    if level_of(event) != "red":
        raise AlertError("«Разобрался» ставится только на красную запись.")
    return event


def ack(conn, event_id: int, login: str, now: str, author: Optional[str] = None) -> None:
    """«Разобрался»: the red record closes for this user only. `author`: who pressed, when not the login (the owner or the partner working
    as a point: «<name> за точку «X»», panel/common.author); the mark stays the login's."""
    if not login or login == EVERYBODY:
        raise AlertError("Не указан пользователь.")
    _red_event(conn, event_id)
    conn.execute("INSERT OR IGNORE INTO order_event_ack (event_id, login, at, author) VALUES (?,?,?,?)", (event_id, login, now, author or login))


def resolve(conn, event_id: int, now: str, author: str) -> None:
    """The cause of a red record is fixed (author: who or what fixed it): it closes for everybody."""
    _red_event(conn, event_id)
    conn.execute("INSERT OR IGNORE INTO order_event_ack (event_id, login, at, author) VALUES (?,?,?,?)", (event_id, EVERYBODY, now, author))


def open_alerts(conn, login: str, order_id: Optional[int] = None) -> List[dict]:
    """The records that are still alerts for this user, newest first: red not acked by them nor fixed, yellow and green not yet seen."""
    sql = ("SELECT e.id, e.order_id, e.at, e.author, e.kind, e.text, e.level, COALESCE(s.last_id, 0), "
           "EXISTS (SELECT 1 FROM order_event_ack a WHERE a.event_id = e.id AND a.login IN (?, ?)) "
           "FROM order_event e LEFT JOIN order_event_seen s ON s.order_id = e.order_id AND s.login = ? "
           "WHERE e.id > ? AND e.level IN ('', 'red', 'yellow', 'green')" + (" AND e.order_id = ?" if order_id is not None else "") + " ORDER BY e.id DESC")
    args = [login, EVERYBODY, login, _start(conn)] + ([order_id] if order_id is not None else [])
    found = []
    for row in conn.execute(sql, args):
        event = {"id": row[0], "order_id": row[1], "at": row[2], "author": row[3], "kind": row[4], "text": row[5], "level": row[6]}
        event["level"] = level_of(event)
        if event["level"] == "red":
            still = not row[8]
        elif event["level"] in ALERT_LEVELS:
            still = event["id"] > row[7]
        else:
            still = False
        if still:
            found.append(event)
    return found


def _staff(conn, login: str) -> bool:
    """Anybody but the manager of a point: the orders and the chats of the points' Avito accounts are no alerts for them (the owner
    2026-10-26: only the point is told of them; the panel still shows them)."""
    from . import users
    return users.get_role(conn, login)["role"] != "point"


def point_words(event: dict) -> bool:
    """«Клиент написал: …» in the order's journal (chats._note_client_wrote): the owner 2026-11-03, of an order the point works it is not the
    staff's alert. «Клиент написал после закрытия», «Клиент звонил» and the failures stay."""
    return event.get("kind") == "chat" and level_of(event) != "red" and str(event.get("text") or "").startswith(CLIENT_WROTE)


POINT_WORK_QUIET = ("owner", "partner")          # the owner 2026-11-03 «мне и Заману»: the chats' manager still hears of them (the lead, B)


def _quiet_of_point_work(conn, login: str) -> bool:
    """The owner and the partner: the client's words and «Срочно ответить» of the orders the points work are no alerts for them."""
    from . import users
    return users.get_role(conn, login)["role"] in POINT_WORK_QUIET


def _quiet(conn, login: str, staff: bool) -> dict:
    """What is no alert for this user. For the staff: every record of the orders of the points' Avito accounts and their chats (2026-10-26).
    For the owner and the partner besides: the client's words of the orders the points work now or worked when the client wrote
    (chats.POINT_WROTE_AUTHOR) and «Срочно ответить» of their chats (2026-11-03), except a client they took from the point
    (order_points.office_taken): of it they are told as usual, «Срочно ответить» of a point's chat too."""
    from . import order_points
    if not staff:
        return {"all": set(), "words": set(), "chats": set(), "on": False, "taken": set(), "taken_chats": set()}
    every, chats = order_points.account_orders(conn), order_points.account_chats(conn)
    if not _quiet_of_point_work(conn, login):
        return {"all": every, "words": set(), "chats": chats, "on": False, "taken": set(), "taken_chats": set()}
    taken, taken_chats = order_points.office_taken(conn)
    return {"all": every - taken, "words": order_points.work_orders(conn) - taken, "chats": chats - taken_chats, "on": True,
            "taken": taken, "taken_chats": taken_chats & chats}            # urgent_rows has the other chats already


def _hidden(event: dict, quiet: dict) -> bool:
    from .chats import POINT_WROTE_AUTHOR
    if event["order_id"] in quiet["all"]:
        return True
    return point_words(event) and (event["order_id"] in quiet["words"] or (
        quiet["on"] and event["author"] == POINT_WROTE_AUTHOR and event["order_id"] not in quiet["taken"]))


def owner_people(conn) -> List[str]:
    """Both owners: the owner's login of the alerts and every active user of the role «owner» (Заман's). They hear of an order nobody answers
    for (the owner 2026-11-03: «не должно быть», but never lost)."""
    from . import order_notify, users
    return sorted({order_notify.get_owner_login(conn)} | {u["login"] for u in users.list_users(conn) if u["active"] and u["role"] == "owner"})


def people_kinds(conn) -> Dict[str, str]:
    """{login: "staff" | "point"} of everybody who may answer for an order: the owner's login of the alerts and every active panel user but a
    client and a viewer (who may not act on an order). Anybody else (the robot «чаты Авито», a removed or switched-off user) answers for
    nothing."""
    from . import order_notify, users
    found = {order_notify.get_owner_login(conn): "staff"}
    for user in users.list_users(conn):
        if user["active"] and user["role"] not in ("client", "viewer"):
            found[user["login"]] = "point" if user["role"] == "point" else "staff"
    return found


def responsibles(conn, kinds: Optional[Dict[str, str]] = None, order_id: Optional[int] = None) -> Dict[int, str]:
    """{order: who answers for it}: its «Ответственный», else whoever made it (orders.get_card: the maker answers until another is named);
    '' when that is nobody of people_kinds. All the orders, or the one `order_id`."""
    from .orders import ACTING_MARK
    kinds = people_kinds(conn) if kinds is None else kinds
    found = {}
    for card_id, named, made_by in conn.execute("SELECT id, responsible, created_by FROM order_card" + (" WHERE id = ?" if order_id is not None else ""),
                                                (order_id,) if order_id is not None else ()):
        who = named or ("" if ACTING_MARK in (made_by or "") else made_by or "")
        found[card_id] = who if who in kinds else ""
    return found


def told_people(conn, order_id, usual) -> List[str]:
    """Who of the staff is told of an order in Telegram (the owner 2026-11-03: only the one who answers for it): that person; both owners when
    nobody answers for it; `usual` (the list as before) when a point user does: a point never gets Telegram."""
    kinds = people_kinds(conn)
    who = responsibles(conn, kinds, order_id).get(order_id, "") if order_id else ""
    if not who:
        return owner_people(conn)
    return [who] if kinds[who] == "staff" else list(usual)


def _mine(conn, login: str, staff: bool, quiet: dict) -> Optional[dict]:
    """What of the orders and the chats without an order is this user's (the owner 2026-11-03: the news of an order go only to its responsible,
    of an order nobody answers for to both owners; the chats without an order to «Ответственный за заказы этого аккаунта», as before when the
    account has none). Besides: a client the owner or the partner took from a point (_quiet's «taken») while a point (or nobody) answers for
    the order is theirs as before (the owner's same-day rule); the red failures of an order a point user answers for are both owners' too
    (a point seldom can fix them); a chat without an order a person answers for (its own «Ответственный») is that person's.
    None: no such rule for them (a point user, the owner or the partner working as a point: their rows as before)."""
    if not staff:
        return None
    from . import connections
    kinds = people_kinds(conn)
    owner = login in owner_people(conn)
    orders_of, reds = set(), set()
    for card_id, who in responsibles(conn, kinds).items():
        if who == login or (not who and owner) or (card_id in quiet["taken"] and kinds.get(who) != "staff"):
            orders_of.add(card_id)
        elif owner and kinds.get(who) == "point":
            reds.add(card_id)
    others = {account_id for account_id in [r[0] for r in conn.execute("SELECT id FROM account")]
              if connections.account_responsible(conn, account_id) not in ("", login)}
    chat_people = {chat_row: who for chat_row, who in conn.execute("SELECT id, responsible FROM avito_chat WHERE responsible != ''")
                   if kinds.get(who) == "staff"}
    return {"login": login, "orders": orders_of, "reds": reds, "other_accounts": others, "chat_people": chat_people}


def _not_mine(event: dict, mine: Optional[dict]) -> bool:
    return mine is not None and event["order_id"] not in mine["orders"] and not (event["level"] == "red" and event["order_id"] in mine["reds"])


def _not_theirs(row: dict, mine: Optional[dict]) -> bool:
    """A chat's row (chat_rows, urgent_rows) that is not this user's: of an order they do not answer for, of a chat without an order another
    person answers for, or else whose account has another «Ответственный за заказы этого аккаунта»."""
    if mine is None:
        return False
    if row.get("of_order"):
        return row["of_order"] not in mine["orders"]
    who = mine["chat_people"].get(row.get("chat_row"))
    if who:
        return who != mine["login"]
    return row.get("account_id") in mine["other_accounts"]


def counts(conn, login: str) -> Dict[str, int]:
    """How many open alerts of every colour this user has (as page_rows: of the orders they answer for, without the orders of the points'
    accounts nor the client's words of the orders the points work for the owner and the partner)."""
    staff = _staff(conn, login)
    quiet = _quiet(conn, login, staff)
    mine = _mine(conn, login, staff, quiet)
    result = {level: 0 for level in ALERT_LEVELS}
    for event in open_alerts(conn, login):
        if not _hidden(event, quiet) and not _not_mine(event, mine):
            result[event["level"]] += 1
    return result


def page_rows(conn, login: str, now: Optional[str] = None, staff: Optional[bool] = None, own_prefix: str = "") -> List[dict]:
    """The rows of «Уведомления», of the bell and of its counter, and the dots by the order numbers: one source for all of them. Newest first:
    every alert on its own, except that the non-red chat records of one order (client messages) are one row until the order is opened; such a row
    has the most important colour of its records. The user's own records are not alerts for them, except red ones.
    `now` (the panel's local time): a green alert older than GREEN_DAYS days is not shown either; nothing is deleted.
    The chats without an order whose client waits (chat_rows) are rows too; with `now` also «Срочно ответить» (urgent_rows, in place of the
    chat's «Клиент написал»; a due task is no row here since 2026-10-31 — it is «Задачи»'s); for the partner the orders a point named that are not
    in the panel (missing_rows). For the staff (_staff) the orders and the chats of the points' Avito accounts are no alerts; for the owner and
    the partner (_quiet_of_point_work) nor are the client's words (point_words) and «Срочно ответить» of an order the point works now
    (order_points.work_orders, the owner 2026-11-03), unless they took the client from the point (_quiet).
    Of the staff, only the one who answers for the order gets its rows, both owners those of an order nobody answers for, and the chats
    without an order go to «Ответственный за заказы этого аккаунта» when the account has one (_mine, the owner 2026-11-03).
    `staff` False: the owner or the partner working as a point (panel/common.current_user) sees the point's; `own_prefix`: the start of their
    author «<name> за точку «» — what they pressed as a point is their own record too."""
    border = (datetime.fromisoformat(now) - timedelta(days=GREEN_DAYS)).isoformat(timespec="seconds") if now else ""
    staff = _staff(conn, login) if staff is None else staff
    quiet = _quiet(conn, login, staff)
    mine = _mine(conn, login, staff, quiet)
    rows, folded = [], {}
    for event in open_alerts(conn, login):
        if _hidden(event, quiet) or _not_mine(event, mine):
            continue
        if (event["author"] == login or (own_prefix and str(event["author"] or "").startswith(own_prefix))) \
                and event["level"] != "red":                                 # one's own failure still waits for «Разобрался»
            continue
        if border and event["level"] == "green" and (event["at"] or "") < border:
            continue
        event["more"] = 0
        if event["kind"] == "chat" and event["level"] != "red":
            have = folded.get(event["order_id"])
            if have is not None:
                have["more"] += 1
                if ALERT_LEVELS.index(event["level"]) < ALERT_LEVELS.index(have["level"]):
                    have["level"] = event["level"]
                continue
            folded[event["order_id"]] = event
        rows.append(event)
    urgent = [row for row in (urgent_rows(conn, now, also=quiet["taken_chats"]) if now else [])
              if (row["chat_row"] in quiet["taken_chats"] or (row["of_order"] not in quiet["words"] and row["of_order"] not in quiet["all"]))
              and not _not_theirs(row, mine)]
    late = {row["chat_row"] for row in urgent}
    late_orders = {row["of_order"] for row in urgent if row["of_order"]}
    rows = [row for row in rows if not (row["order_id"] in late_orders and row is folded.get(row["order_id"]) and row["level"] == "yellow")]
    skip = late | quiet["chats"]
    rows += [row for row in chat_rows(conn) if row["chat_row"] not in skip and not _not_theirs(row, mine)] + urgent   # one row per chat: «Срочно ответить»
                                                                                          # takes the place of «Клиент написал», of a chat
                                                                                          # with an order too
    rows += missing_rows(conn, login)
    for row in rows:
        shown = stub_text(row.get("text"))
        if shown:
            row["text"] = shown
    rows.sort(key=lambda r: r["at"] or "", reverse=True)                    # stable: the records of one moment keep their order
    return rows


CLIENT_WROTE = "Клиент написал: "
CLIENT_CALLED = "Клиент звонил"
NO_TEXT = CLIENT_WROTE + "сообщение без текста (голосовое, файл, кнопка…), видно только в Авито"


def stub_text(text: str) -> Optional[str]:
    """«Клиент написал: Сообщение не поддерживается…» (Avito's stub instead of a voice, a file…): the label shown in its place, None for
    any other text (хвост З19). The stored record is not changed."""
    from .chats import UNSUPPORTED
    text = str(text or "")
    return NO_TEXT if text.startswith(CLIENT_WROTE) and text[len(CLIENT_WROTE):].lstrip().startswith(UNSUPPORTED) else None


def chat_rows(conn) -> List[dict]:
    """A chat without an order whose client waits for an answer: one row «Клиент написал» per chat until it is read or answered (its unread
    count is zero again) or marked «Не требует ответа» / closed; a call through Avito that we did not answer with a message is the row
    «Клиент звонил». The chats that were there before their account was first read are the past. Two queries: they run for the counter of
    the menu on every page."""
    from . import chats, store
    rows, baselines = [], {}
    for chat_row, account_id, peer, unread, text, at in conn.execute(
            "SELECT c.id, c.account_id, c.peer_name, c.unread, m.text, m.at FROM avito_chat c JOIN account a ON a.id = c.account_id "
            "JOIN avito_message m ON m.id = (SELECT id FROM avito_message WHERE chat_row = c.id AND direction = 'in' AND kind = 'text' ORDER BY ts DESC, id DESC LIMIT 1) "
            "WHERE (c.order_id IS NULL OR c.order_id = 0) AND c.unread > 0 AND c.status = 'open' AND a.chats_enabled = 1").fetchall():
        if account_id not in baselines:
            baselines[account_id] = store.get_state(conn, "chats_baseline_%d" % account_id) or ""
        if (at or "") < baselines[account_id]:
            continue
        text = " ".join((text or "").split())
        rows.append({"id": "chat-%d" % chat_row, "order_id": None, "chat_row": chat_row, "account_id": account_id, "client": peer or "", "at": at, "author": "",
                     "kind": "chat", "level": "yellow", "more": max(0, unread - 1),
                     "text": CLIENT_WROTE + text[:80] + ("…" if len(text) > 80 else "")})
    seen = {row["chat_row"] for row in rows}
    for chat_row, account_id, peer, text, at in conn.execute(                    # a call of the client through Avito that nobody answered with a message
            "SELECT c.id, c.account_id, c.peer_name, m.text, m.at FROM avito_chat c JOIN account a ON a.id = c.account_id "
            "JOIN avito_message m ON m.id = (SELECT id FROM avito_message WHERE chat_row = c.id AND direction = 'in' AND kind = 'call' ORDER BY ts DESC, id DESC LIMIT 1) "
            "WHERE (c.order_id IS NULL OR c.order_id = 0) AND c.status = 'open' AND a.chats_enabled = 1 "
            "AND m.ts > " + chats.OUR_LAST_REPLY % "c.id").fetchall():                  # Avito's auto-answer is no answer to the call
        if chat_row in seen:                                                     # one row per chat: «Клиент написал» is already there
            continue
        if account_id not in baselines:
            baselines[account_id] = store.get_state(conn, "chats_baseline_%d" % account_id) or ""
        if (at or "") < baselines[account_id]:
            continue
        rows.append({"id": "chat-%d" % chat_row, "order_id": None, "chat_row": chat_row, "account_id": account_id, "client": peer or "", "at": at, "author": "",
                     "kind": "chat", "level": "yellow", "more": 0,
                     "text": CLIENT_CALLED + (text or "").replace("Звонок через Авито", "", 1)})
    return rows


URGENT = "Срочно ответить: "
URGENT_MINUTES = 60                         # the owner 2026-10-23: as soon as possible, at most an hour
URGENT_DAYS = 7                             # a chat silent for longer is no longer «срочно» (the inquiry is «отказ» by then)
URGENT_START_KEY = "urgent_start"           # app_state: a client who waited from before this moment is the past (set by the look of every minute)


def urgent_rows(conn, now: str, also=()) -> List[dict]:
    """«Срочно ответить»: an open chat of our accounts (not of a point; `also`: the chats of a point that are, the clients the owner or the
    partner took, order_points.office_taken) whose client waits for an answer longer than an hour of working time
    (the hours of the handoffs) since their first message after our last one. Avito's auto-answer (an outgoing «system» message, on for
    the owner's accounts since 2026-10-13) is not an answer. Goes by itself when a person answers or the chat is closed or marked
    «Не требует ответа», and never of an order on a closing stage (won, lost, closed; the owner 2026-11-03). Nothing is stored: it is read
    from the chat every time."""
    from . import order_manager, store
    start = store.get_state(conn, URGENT_START_KEY)
    if not start:
        return []
    hours = order_manager.duty_settings(conn)["hours"]
    moment = datetime.fromisoformat(now[:19])
    since = max(start, (moment - timedelta(days=URGENT_DAYS)).isoformat(timespec="seconds"))
    rows = []
    # one statement for the counter of every page; the chat's own last_dir is not a filter: Avito's auto-answer (out, «system») comes last
    for chat_row, account_id, peer, order_id, first_at, first_text in conn.execute(
            "SELECT c.id, c.account_id, c.peer_name, c.order_id, w.at, w.text FROM avito_chat c JOIN account a ON a.id = c.account_id "
            "JOIN avito_message w ON w.id = (SELECT id FROM avito_message WHERE chat_row = c.id AND direction = 'in' "
            "AND kind NOT IN ('call', 'system', 'deleted') AND ts > COALESCE((SELECT MAX(ts) FROM avito_message WHERE chat_row = c.id "
            "AND direction = 'out' AND kind NOT IN ('system', 'deleted')), 0) ORDER BY ts, id LIMIT 1) "
            "WHERE c.status = 'open' AND c.last_at >= ? AND a.chats_enabled = 1 "
            "AND NOT EXISTS (SELECT 1 FROM order_card o JOIN order_stage s ON s.id = o.stage_id WHERE o.id = c.order_id "
            "AND s.kind IN ('won', 'lost', 'closed')) "                   # a closed order: never «срочно» (the owner 2026-11-03)
            "AND (NOT EXISTS (SELECT 1 FROM point p WHERE p.avito_account_id = c.account_id) OR c.id IN (%s))"
            % (",".join(str(int(r)) for r in also) or "0"), (since,)).fetchall():
        first = (first_at, first_text)
        if (first[0] or "") < start:
            continue
        due = order_manager.take_deadline(first[0], hours, URGENT_MINUTES)
        if due > moment:
            continue
        text = " ".join((first[1] or "").split())
        rows.append({"id": "urgent-%d" % chat_row, "order_id": None, "chat_row": chat_row, "account_id": account_id, "client": peer or "", "at": due.isoformat(timespec="seconds"),
                     "author": "", "kind": "chat", "level": "red", "more": 0, "urgent": True, "of_order": order_id if order_id and order_id > 0 else None,
                     "title": "Чат" + (" · заказ №%d" % order_id if order_id and order_id > 0 else ""),
                     "text": URGENT + "ждёт с %s: %s" % (first[0][11:16], text[:80] + ("…" if len(text) > 80 else ""))})
    return rows


def missing_rows(conn, login: str) -> List[dict]:
    """«Точка X сообщила о заказе №N (…), его нет в CRM — добавьте» (the owner 2026-10-24) for the people order_notify.missing_order_people
    names: one row per line of «Входящие» while it waits there."""
    from . import order_manager, order_notify
    if login not in order_notify.missing_order_people(conn):
        return []
    return [{"id": "inbox-%d" % item["id"], "order_id": None, "chat_row": None, "client": "", "at": item["at"], "author": "", "kind": "inbox",
             "level": "yellow", "more": 0, "title": "Входящие", "href": "/orders/inbox", "text": item["text"]} for item in order_manager.missing_orders(conn)]


def order_levels(conn, login: str, now: Optional[str] = None) -> Dict[int, str]:
    """{order: the most important colour among its rows of «Уведомления»} (page_rows: the same rows as the dots by the order numbers)."""
    result: Dict[int, str] = {}
    for row in page_rows(conn, login, now):
        order_id = row["order_id"] if row["order_id"] is not None else row.get("of_order")    # «Срочно ответить» of the order's chat
        if order_id is None:
            continue
        have = result.get(order_id)
        if have is None or ALERT_LEVELS.index(row["level"]) < ALERT_LEVELS.index(have):
            result[order_id] = row["level"]
    return result
