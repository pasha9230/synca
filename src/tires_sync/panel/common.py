from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import abort, current_app, flash, g, redirect, request, session

from .. import store, users


def get_conn():
    if "conn" not in g:
        g.conn = store.connect(current_app.config["DB_PATH"])
    return g.conn


def close_conn(exc=None) -> None:
    conn = g.pop("conn", None)
    if conn is not None:
        conn.close()


def now() -> datetime:
    return current_app.config["NOW"]()


def local_today():
    """«Today» for the owner: Moscow time (UTC+3, no daylight saving), not the server's UTC day."""
    return (now() + timedelta(hours=3)).date()


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_hex(16)
        session["csrf"] = token
    return token


AGENT_PREFIXES = ("/api/image-jobs", "/api/photo-copies", "/api/review-sheet",     # the photo agent: a bearer token instead of a session
                  "/api/market-snapshots")                                         # the prices of the competitors from the Mac (routes_market)


def is_agent_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in AGENT_PREFIXES)


def check_csrf() -> None:
    if is_agent_path(request.path):
        return
    if request.method not in ("GET", "HEAD", "OPTIONS") and not request.path.startswith("/hook/"):   # VK's and Avito's callbacks: their own secrets
        sent = request.form.get("csrf", "")
        expected = session.get("csrf", "")
        if not sent or not expected or not secrets.compare_digest(sent.encode("utf-8"), expected.encode("utf-8")):
            abort(400)


def safe_path(target: str) -> str:
    """A page of this panel to come back to: a local path only (never another site, never the login or draft screens)."""
    if not target.startswith("/") or target.startswith("//") or "\\" in target or any(ord(c) < 32 or ord(c) == 127 for c in target):
        return ""
    if target.split("?")[0] in ("/login", "/logout") or target.startswith(("/draft/", "/catalog/review", "/catalog/table", "/catalog/preview", "/publish")):
        return ""
    return target


def remember_page(target: str = "") -> None:
    """Remember the page the owner works on, so that confirming or cancelling brings them back to it."""
    if not target:
        from urllib.parse import urlsplit
        parts = urlsplit(request.headers.get("Referer", ""))
        if parts.netloc and parts.netloc != request.host:
            return
        target = parts.path + ("?" + parts.query if parts.query else "")
    target = safe_path(target)
    if target:
        session["return_to"] = target


def return_page(default: str = "/", keep: bool = False) -> str:
    target = safe_path(session.get("return_to", ""))
    if not keep:
        session.pop("return_to", None)
    return target or default


def epoch_login(conn, user: str):
    """Whose session counter applies: the owner of the shared password has the global one."""
    return None if user == current_app.config["AUTHOR"] else user


LOGIN_ALERT_FAILURES = 30          # more failed sign-ins than this in the window, all addresses together: the owner hears (never a lock)
LOGIN_ALERT_QUIET = LOGIN_ALERT_FAILURES // 2   # the window fell to this or lower: the series is over, the next crossing is a new alert
LOGIN_ALERT_KEY = "login_alert_at"  # the time of the alert of the series going on; "" none; "0<time>" its send failed at that time


def client_key() -> str:
    """Whose failed sign-ins count together: the IPv4 address, or the /64 network of an IPv6 one (one subscriber holds the whole /64)."""
    address = request.remote_addr or "?"
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if ip.version == 6 and ip.ipv4_mapped:
        return str(ip.ipv4_mapped)
    return str(ipaddress.ip_network("%s/64" % ip, strict=False)) if ip.version == 6 else str(ip)


def _login_since() -> str:
    return (now() - timedelta(seconds=current_app.config["LOGIN_WINDOW_SECONDS"])).isoformat(timespec="seconds")


def login_blocked(conn) -> bool:
    """Too many failed sign-ins (a password, an invitation, the current password) from this address in the window."""
    return store.count_login_failures(conn, client_key(), _login_since()) >= current_app.config["MAX_LOGIN_FAILURES"]


def login_failed(conn) -> None:
    """Count a failed sign-in for this address. A success never wipes the count: one's own login would otherwise reset the guessing of
    another's password. Many failures from everywhere together (a botnet) tell the owner instead of locking anybody out."""
    store.add_login_failure(conn, client_key(), now_iso())
    try:
        total = conn.execute("SELECT COUNT(*) FROM login_attempt WHERE at >= ?", (_login_since(),)).fetchone()[0]
        if total > LOGIN_ALERT_FAILURES:
            _tell_owner_of_guessing(conn)
        elif total <= LOGIN_ALERT_QUIET:                    # the series ended: its sent mark goes (a failed send keeps its retry time)
            conn.execute("UPDATE app_state SET value = '' WHERE key = ? AND value >= '1'", (LOGIN_ALERT_KEY,))
            conn.commit()
    except Exception:                                       # noqa: BLE001 - the failure is counted; the sign-in page answers whatever the alert does
        logging.getLogger("tires_sync.panel").exception("the alert about failed sign-ins was not sent")


LOGIN_ALERT_RETRY = timedelta(minutes=5)       # Telegram did not take the alert: the next failed sign-in tries again this soon, not in an hour


def _tell_owner_of_guessing(conn) -> None:
    """Once a series (the owner 2026-10-27: every series, no longer once an hour), by its own mark: never through cli._alert, whose pause
    would let a guesser silence the alerts of the services. The mark is taken in one statement, so of two workers only one sends; it stays
    while the series goes on (login_failed clears it). A refused send leaves "0<time>": the next try in 5 minutes."""
    at = now()
    if not store.claim_state(conn, LOGIN_ALERT_KEY, at.isoformat(timespec="seconds"), "0" + (at - LOGIN_ALERT_RETRY).isoformat(timespec="seconds")):
        return
    text = ("Tires Sync: больше %d неудачных входов в панель за %d мин. Похоже на подбор пароля: адреса в журнале действий."
            % (LOGIN_ALERT_FAILURES, current_app.config["LOGIN_WINDOW_SECONDS"] // 60))
    from .. import notify
    from ..transport import urllib_transport
    log = logging.getLogger("tires_sync.panel")
    try:
        send = notify.notifier_for(conn, current_app.config.get("SECRET_BOX"), http=current_app.config.get("TG_HTTP") or urllib_transport)
        if send is not None:
            send(text)
    except Exception:                                       # noqa: BLE001 - the sign-in page answers whatever Telegram does
        log.exception("the alert about failed sign-ins was not sent")
        try:
            store.set_state(conn, LOGIN_ALERT_KEY, "0" + at.isoformat(timespec="seconds"))
        except Exception:                                   # noqa: BLE001 - a busy base: the mark stays, the next try with the next series
            log.exception("the mark of the alert about failed sign-ins was not moved back")
    store.write_audit(conn, now_iso(), "system", "alert", {"text": text})      # after the send: a busy base never costs the message


def shared_mark() -> str:
    """The mark of the shared owner password in its sessions: a new PANEL_PASSWORD_HASH in panel.env ends them all."""
    key = current_app.config["SECRET_KEY"]
    return hmac.new(key if isinstance(key, bytes) else str(key).encode("utf-8"), str(current_app.config["PASSWORD_HASH"]).encode("utf-8"),
                    hashlib.sha256).hexdigest()[:16]


def session_valid() -> bool:
    user = session.get("user")
    if not user:
        return False
    conn = get_conn()
    login = epoch_login(conn, user)
    if login is not None and (not users.exists(conn, login) or not users.is_active(conn, login)):      # the user was removed or switched off
        return False
    if login is None and not secrets.compare_digest(str(session.get("pw", "")), shared_mark()):        # the shared password was changed
        return False
    return session.get("epoch") == store.get_session_epoch(conn, login)


def author() -> str:
    """Who is acting, for the journal and the settings versions."""
    return session.get("user") or current_app.config["AUTHOR"]


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        valid = session_valid()
        if not valid:
            if session.get("user"):
                session.clear()
            return redirect("/login")
        return view(*args, **kwargs)

    return wrapper


def review_or_back(conn, back: str):
    """After a settings change: back to the same page, the change waits in the working draft with the others (the owner 2026-10-29: changes
    pile up, the bar of the page says how many and applies them at once; the page /draft/N is still there as «Подробнее»)."""
    from .. import staging
    from .service import plural
    staged = staging.summary(conn)
    if staged["draft_id"] is not None and not staged["stale"]:
        remember_page(back)                                # «Подробнее» (/draft/N) and its «Вернуться к правке» lead back here
        flash("В черновике, не применено: %d %s. Нажмите «Применить», когда закончите." % (
            staged["count"], plural(staged["count"], "правка", "правки", "правок")), "ok")
    elif staged["draft_id"] is None:
        flash("Изменений нет: настройки уже такие.")
    return redirect(back)



# ---- rights: one check for every page -----------------------------------------------------------------------------------------------
# Every endpoint of the panel is named here once: its section (the level is «смотреть» for GET and «менять» for POST) and, for the dangerous
# actions, the right it needs on top. An endpoint missing from these tables is refused (403) and fails tests/test_roles.py.

OPEN = frozenset({
    "static", "login", "logout", "health", "invite_page",
    "account_photo", "stored_photo",                       # the photos Avito reads (a token in the address)
    "share_public", "share_photo",                         # the client's page of a link (its own token)
    "vk_hook",                                             # VK's callbacks (their own secret)
    "avito_hook",                                          # Avito's messenger webhook (a secret in the address)
    "agent_jobs", "agent_fail", "agent_ref", "agent_result", "agent_copy_upload", "agent_copies_todo", "agent_review_sheet",   # bearer token
    "agent_work", "agent_run_done",
    "market_intake",                                       # the prices of the competitors (routes_market): its own bearer token
})
SECTION_OF = {}
for _section, _endpoints in {
    "home": ("index", "analytics_page"),
    "orders": ("orders_page", "order_card_page", "order_merge_page", "order_merge", "order_unmerge", "order_oth_agreed", "order_oth_unagree", "order_oth_take", "order_oth_untake", "order_oth_at_point", "order_delete", "order_item_delete", "order_item_update", "order_item_add", "order_move",
               "order_note", "order_sale", "order_sale_reverse", "order_save", "order_task_run", "order_task_skip", "order_task_send",
               "order_messages_send",
               "order_want_delete", "order_want_update", "order_want_add", "orders_export_file", "orders_inbox_page", "orders_inbox_answer",
               "orders_inbox_confirm", "order_item_find", "order_new_page", "order_new", "orders_bulk_page", "orders_bulk_check",
               "orders_bulk_run", "orders_bulk_undo_page", "orders_bulk_undo_run", "orders_cleanup_page", "orders_cleanup_run",
               "deleted_page", "deleted_restore", "orders_webmim_page", "order_webmim_link", "order_webmim_create",
               "order_tochki_placed", "order_delivery_tomorrow",           # «Заказ поставщику» (order_supplier)
               "catalog_wait"),                                    # «В лист ожидания» of the catalog makes an order: the right of a new order
    "chats": ("chats_page", "chat_assign", "chat_items_add", "chat_create_deal", "chat_delete", "chat_link", "chat_send", "chat_send_image", "chat_stage", "chat_status",
              "chat_unlink", "chat_wait", "chat_handoff", "chats_merge", "chats_refresh", "share_create", "share_list", "share_price", "share_revoke",
              "share_created", "share_new"),
    "alerts": ("alerts_page", "alerts_ack", "alerts_feed", "alerts_go", "alerts_take", "alerts_accept_all"),
    "calls": ("calls_page", "call_record", "call_create_order", "call_handoff"),
    "clients": ("clients_page", "client_page", "client_save", "client_merge", "client_split_order", "client_split_chat",
                "clients_duplicates", "client_tag", "client_unmerge", "client_skip"),
    "catalog": ("feeds_page", "new_positions", "new_positions_seen", "mappings", "accept_confident", "confirm_brand", "export_mappings", "import_mappings",
                "confirm_model", "save_old_model_name", "catalog", "catalog_cancel", "catalog_item_page", "catalog_market_switch", "catalog_market_token", "catalog_own_price", "catalog_own_price_bulk", "catalog_preview", "catalog_stage", "catalog_table", "brands", "oth_page", "oth_return",
                "pairs_page", "pairs_forget", "pairs_not_pair",    # «Пары поставщиков»: who sees the catalog sees it; «Забыть пару», «Это не пара» need «менять»
                "feed_update_page", "feed_update_confirm", "feed_update_fetch",
                "history_ads", "history_catalog", "photos_feed", "photos_frame_action", "photos_generate", "photos_priority", "photos_queue_switch", "photos_ref", "photos_review", "photos_switch"),
    "analytics": ("economics_page", "economics_brand", "economics_params", "economics_sales", "economics_sale_add", "economics_sales_export",
                  "economics_sale_new", "economics_sale_reverse", "economics_terms", "economics_apply_pricing", "economics_terms_delete",
                  "economics_terms_import", "economics_terms_save", "economics_terms_update", "economics_funnel", "orders_report",
                  "orders_report_export"),
    "setup": ("draft", "draft_cancel", "draft_confirm", "catalog_review", "publish_result", "summary_toggle",      # a draft shows the markups: the owner's
              "settings_page", "settings_brands_page", "settings_brand_card", "settings_draft", "settings_pending", "settings_exclusions_page", "settings_pricing",
              "history", "history_rollback", "connections_page", "connections_add_account", "connections_account_brand_pricing",
              "connections_account_check", "account_contact_name", "account_responsible", "account_exclude_types", "connections_account_keys",
              "connections_account_live", "account_modes", "account_phone", "account_phone_source", "connections_account_photo_base",
              "connections_account_own_photos", "connections_account_photo_style", "connections_account_pricing", "connections_account_stock", "connections_account_title", "connections_avito_page",
              "connections_account_page", "connections_add_supplier", "connections_excluded_brand", "connections_excluded_brands_bulk",
              "connections_excluded_size", "connections_telegram_page", "connections_telegram", "connections_telegram_test",
              "connections_telegram_user_chat", "connections_tochki_page", "connections_tochki", "connections_tochki_check",
              "connections_tochki_sample", "connections_webmim_page", "connections_webmim", "connections_webmim_check",
              "connections_webmim_orders", "connections_webmim_orders_check", "description_page", "description_save", "addresses_page",
              "addresses_save", "point_rules", "connections_points_page", "connections_points_code", "connections_points_delete",
              "connections_points_save", "connections_vk_page", "connections_vk", "connections_openai_page", "connections_openai",
              "connections_openai_clear", "connections_sheet_page", "connections_sheet", "connections_sheet_check",
              "photos_agent_token", "orders_settings_page", "orders_duty",
              "orders_command_delete", "orders_command_add", "orders_default_responsible", "orders_rule_delete", "orders_rule_toggle",
              "orders_rule_add", "orders_stage_delete", "orders_stage_shift", "orders_stage_update", "orders_stage_add",
              "orders_templates_page", "orders_template_delete", "orders_template_samples", "orders_template_save",
              "orders_bot_answer_delete", "orders_bot_answer_toggle", "orders_bot_answer_add", "orders_report_rates"),
    "users": ("users_page", "users_invite", "users_role", "users_active", "users_invite_revoke",
              "users_shop_responsible",                 # «Ответственный за заказы клиентов» (shop_cart.RESPONSIBLE_KEY)
              "users_clients_page", "users_clients_role_save", "users_client_page", "users_client_save",     # the conditions of the clients
              "users_client_preview",                   # «Каталог глазами клиента»
              "points_history", "history_points"),     # «История → Точки» (and its old address): the owner and the partner only (users: nobody else looks)
    "self": ("account_password_page", "account_password", "orders_list_columns",     # «Колонки» of the list of orders: one's own setting
             "orders_hint_fold",                        # «Что делать сейчас» folded: one's own setting
             "search", "search_json",                   # the search of the top bar: every role; search.scope_for narrows what it finds
             "tour_done", "tour_again"),                # «Обзор» (panel/tour.py): one's own mark «пройден», every role
    "mypoint": ("mypoint_page", "mypoint_action", "mypoint_feed", "mypoint_handoff_cancel"),
    "shop": ("shop_page", "shop_photo",         # «Каталог для клиента»: the client's only (its photos through our server)
             "shop_cart_add", "shop_cart_page", "shop_cart_set", "shop_checkout", "shop_orders_page", "shop_order_page"),   # its cart, its orders
}.items():
    for _endpoint in _endpoints:
        SECTION_OF[_endpoint] = _section
# the dangerous actions: the right is needed on top of «менять» in the section (POST only)
NEEDS = {"catalog_own_price": "money", "catalog_own_price_bulk": "money", "catalog_market_token": "money", "catalog_market_switch": "money", "order_delete": "delete", "chat_delete": "delete", "deleted_restore": "delete", "economics_terms_delete": "delete",
         "orders_bulk_check": "bulk", "orders_bulk_run": "bulk", "orders_bulk_undo_run": "bulk", "orders_cleanup_run": "bulk", "chats_merge": "bulk",
         "draft_confirm": "publish", "feed_update_confirm": "publish", "pairs_forget": "publish", "pairs_not_pair": "publish", "feed_update_fetch": "publish", "history_rollback": "publish",
         "connections_account_live": "publish", "connections_account_stock": "publish", "economics_apply_pricing": "publish",
         "order_sale_reverse": "delete", "order_webmim_create": "supplier", "order_tochki_placed": "supplier",
         "order_oth_agreed": "supplier",                  # «Согласовано с поставщиком»: the owner and the partner (the owner 2026-10-29)
         "order_oth_unagree": "supplier",                 # «Отменить согласование»: the same people (the lead 2026-10-30)
         "chat_send": "send", "chat_send_image": "send", "order_task_send": "send", "order_messages_send": "send", "orders_inbox_answer": "send", "share_new": "send",
         "share_create": "send"}
NEEDS.update({e: "connections" for e, s in SECTION_OF.items() if s == "setup" and e not in NEEDS
              and e.startswith(("connections_", "account_", "addresses_", "point_rules", "photos_agent", "description_"))})
# the manager of a point: the orders of the point only (the board, the file and the cards of the point)
POINT_PAGES = frozenset({"orders_page", "order_card_page", "orders_export_file", "order_move", "order_note", "orders_list_columns", "orders_hint_fold", "account_password_page",
                         "account_password", "tour_done", "tour_again", "mypoint_page", "mypoint_action", "mypoint_feed", "mypoint_handoff_cancel",
                         "search", "search_json",   # the search: the point's own (search.scope_for)
                         "chats_page", "chats_refresh", "alerts_page", "alerts_feed", "alerts_ack", "alerts_accept_all",       # the chats of the point's account, its alerts
                         "chat_send", "chat_send_image", "chat_status", "chat_assign", "chat_create_deal", "chat_link", "chat_unlink",
                         "chat_stage", "chat_handoff", "alerts_go",   # positions of an order (items, «клиент ждёт»): not the point's (the owner did not say)
                         "calls_page", "call_record", "call_create_order", "call_handoff"})           # the calls of the point's account (own_call)
# the client (a buyer): the own catalog and the own password, nothing else — not the search of the top bar either (its section is «self»)
CLIENT_PAGES = frozenset({"shop_page", "shop_photo", "account_password_page", "account_password", "tour_done", "tour_again",
                          "shop_cart_add", "shop_cart_page", "shop_cart_set", "shop_checkout", "shop_orders_page", "shop_order_page"})
# «CRM» is not the point's (the owner 2026-10-29: hidden; before, its own part of a client, 2026-10-26)
# a POST that only reads (an open «Чаты» page asks for fresh messages): «смотреть» is enough for the manager of a point
VIEW_POSTS = frozenset({"chats_refresh"})


def current_user() -> dict:
    """{"login", "role", "point_id", "display_name"} of the person signed in (the shared password is the owner)."""
    if "user_info" not in g:
        login = session.get("user") or ""
        if login and login == current_app.config["AUTHOR"]:
            info = {"role": "owner", "point_id": None, "display_name": ""}
        else:
            info = users.get_role(get_conn(), login) if login else {"role": "", "point_id": None, "display_name": ""}
        g.user_info = {"login": login, "role": info["role"], "point_id": info["point_id"], "display_name": info["display_name"]}
    return g.user_info


def can(section: str, need: str = "view") -> bool:
    return users.level(current_user()["role"], section) >= (users.EDIT if need == "edit" else users.VIEW)


def has_right(right: str) -> bool:
    return users.has_right(current_user()["role"], right)


def can_post(endpoint: str) -> bool:
    """For the templates: the person may send this form (the same tables as check_access for a POST; the server still checks)."""
    role = current_user()["role"]
    section = SECTION_OF.get(endpoint)
    if endpoint in OPEN:
        return True
    if section is None or users.level(role, section) < (users.VIEW if role == "point" and endpoint in VIEW_POSTS else users.EDIT):
        return False
    if role == "point" and endpoint not in POINT_PAGES:
        return False
    if role == "client" and endpoint not in CLIENT_PAGES:
        return False
    if section == "analytics" and not users.has_right(role, "money"):
        return False
    return endpoint not in NEEDS or users.has_right(role, NEEDS[endpoint])


def own_point():
    """The point a manager of a point is limited to; None for everybody else."""
    user = current_user()
    return user["point_id"] if user["role"] == "point" else None


def _card_point(card_id):
    row = get_conn().execute("SELECT point_id FROM order_card WHERE id = ?", (card_id,)).fetchone()
    return row[0] if row else None


def point_account():
    """The Avito account whose chats the manager of a point sees (None: no chat); for everybody else None too: use own_point() first."""
    point = own_point()
    if point is None:
        return None
    row = get_conn().execute("SELECT avito_account_id FROM point WHERE id = ?", (point,)).fetchone()
    return row[0] if row else None


def own_chat(chat_row) -> bool:
    """The chat is of the account of the manager's point."""
    account = point_account()
    row = get_conn().execute("SELECT account_id FROM avito_chat WHERE id = ?", (chat_row,)).fetchone()
    return account is not None and row is not None and row[0] == account


def chat_of_other_point(chat_row) -> bool:
    """The chat's order is of another point: the manager may look at the chat, never act on that order through it. An order without a point
    is not another point's (a deal made by the owner from the chat of the point's account)."""
    row = get_conn().execute("SELECT c.point_id FROM avito_chat a JOIN order_card c ON c.id = a.order_id WHERE a.id = ?", (chat_row,)).fetchone()
    return row is not None and row[0] is not None and row[0] != own_point()


def own_call(call_row) -> bool:
    """The call is of the account of the manager's point."""
    account = point_account()
    row = get_conn().execute("SELECT account_id FROM avito_call WHERE id = ?", (call_row,)).fetchone()
    return account is not None and row is not None and row[0] == account


def own_client(client_id) -> bool:
    """The manager's point has a part of the client: an order of the point, a chat or a call of the point's account (clients.point_part)."""
    from .. import clients
    return clients.point_part(get_conn(), client_id, own_point(), point_account()) is not None


def own_event(event_id) -> bool:
    """The record is of an order of the manager's point."""
    row = get_conn().execute("SELECT c.point_id FROM order_event e JOIN order_card c ON c.id = e.order_id WHERE e.id = ?", (event_id,)).fetchone()
    return row is not None and row[0] == own_point()


def check_access():
    """before_request: the right of the page, after the CSRF check. Signed out: to the login page; no right: 403."""
    endpoint = request.endpoint
    if endpoint is None or endpoint in OPEN:              # the client's pages under /s/ are in OPEN by name: no prefix opens a page
        return None
    if not session_valid():
        if session.get("user"):
            session.clear()
        return redirect("/login")
    user = current_user()
    section = SECTION_OF.get(endpoint)
    if section is None:                                   # a page nobody gave a right to: closed
        abort(403)
    role = user["role"]
    if role == "client":                                  # before the level of the section: «self» would open the search
        if endpoint == "index":
            return redirect("/shop")
        if endpoint not in CLIENT_PAGES:
            abort(403)
    if role == "point":
        if not user["point_id"]:                          # a manager of a point without a point sees nothing
            abort(403)
        if endpoint == "index":
            return redirect("/my-point")                  # «Рабочее место» is the point's start page (the board of its orders stays open)
        args = request.view_args or {}
        if endpoint not in POINT_PAGES or ("card_id" in args and _card_point(args["card_id"]) != user["point_id"]) \
                or ("chat_row" in args and not own_chat(args["chat_row"])) or ("event_id" in args and not own_event(args["event_id"])) \
                or ("call_row" in args and not own_call(args["call_row"])) \
                or ("client_id" in args and not own_client(args["client_id"])) \
                or (request.method == "POST" and "chat_row" in args and chat_of_other_point(args["chat_row"])):
            abort(403)
    if users.level(role, section) < (users.EDIT if request.method == "POST" and not (role == "point" and endpoint in VIEW_POSTS) else users.VIEW):
        abort(403)
    if section == "analytics" and not users.has_right(role, "money"):
        abort(403)
    right = NEEDS.get(endpoint)
    if right and request.method == "POST" and not users.has_right(role, right):
        abort(403)
    return None
