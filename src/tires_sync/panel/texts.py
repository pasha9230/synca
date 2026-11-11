"""Plain-Russian wording for things the system stores as codes."""
from __future__ import annotations

RUN_TEXT = {
    "publish": "к публикации", "update": "обновить цену", "keep": "без изменений", "remove": "снять",
    "hold": "на удержании", "skip": "пропущено", "defer": "ждут очереди",
}
DRAFT_STATUS = {"open": "Открыт", "confirmed": "Применён", "cancelled": "Отменён"}


def run_text(key: str) -> str:
    return RUN_TEXT.get(key, key)


def draft_status(status: str) -> str:
    return DRAFT_STATUS.get(status, status)


AUTHOR_NAMES = {"system": "Система", "cli": "Сервер", "server": "Сервер"}


def author_label(author: str) -> str:
    return AUTHOR_NAMES.get(author, author)


ORDER_RATES = {"point_fee": ("Выплата точке за заказ", "₽"), "owner_share": ("Доля собственника", "%"), "handoff_fee": ("За переданного клиента", "₽")}
INQUIRY_EVENTS = {"message": "сообщение", "call": "звонок", "handoff": "передача заказа"}


def _order_rate_text(d: dict) -> str:
    label, unit = ORDER_RATES.get(d.get("name"), (d.get("name", "?"), ""))

    def shown(value):
        if not isinstance(value, (int, float)):
            return "—"
        return ("%.2f" % (value * 100 if unit == "%" else value)).rstrip("0").rstrip(".")      # the share is a fraction, shown in per cent
    return "Ставка «%s» с %s: %s → %s %s" % (label, d.get("valid_from", "?"), shown(d.get("from")), shown(d.get("to")), unit)


def audit_text(entry: dict) -> str:
    action, d = entry["action"], entry.get("detail") or {}
    if not isinstance(d, dict):                                          # a record is a dict; anything else shows its fields as «?»
        d = {}
    if action == "settings_confirm":
        return "Применены настройки: черновик №%s, версия %s" % (d.get("draft", "?"), d.get("to", "?"))
    if action == "settings_cancel":
        return "Отменён черновик №%s" % d.get("draft", "?")
    if action == "own_price_confirmed":                                # «было → стало» of the own prices a «Применить» brought in
        lines = [str(x) for x in d.get("lines") or []]
        more = len(lines) - 5 + int(d.get("more") or 0)
        return "Свои цены, черновик №%s: %s%s" % (d.get("draft", "?"), "; ".join(lines[:5]) or "—", "; ещё %d" % more if more > 0 else "")
    if action == "settings_rollback":
        return "Откат настроек к версии %s" % d.get("to_copy_of", "?")
    if action == "account_created":
        return "Добавлен аккаунт Авито"
    if action == "account_live":
        return "Публикация аккаунта %s %s" % (d.get("account_id", "?"), "включена" if d.get("live") else "выключена")
    if action == "account_addresses":
        return "Изменены Авито адреса аккаунта %s" % d.get("account_id", "?")
    if action == "point_rule":
        what = "адрес %s" % d["address_id"] if d.get("address_id") else "правило снято"
        return "Правило адреса для %s · %s: %s" % (d.get("brand", "?"), d.get("season", "?"), what)
    if action == "mapping_confirm":
        return "Сопоставлена модель %s → %s" % (d.get("model_raw", "?"), d.get("avito_model", "?"))
    if action == "brand_mapping_confirm":
        return "Сопоставлен бренд %s → %s" % (d.get("brand_key", "?"), d.get("avito_brand", "?"))
    if action == "settings_init":
        return "Созданы начальные настройки"
    if action == "account_pricing":
        return "Изменены цены и остатки аккаунта %s" % d.get("account_id", "?")
    if action == "account_own_photos":
        return "Свои фото шин в аккаунте %s %s" % (d.get("account_id", "?"), "включены" if d.get("on") else "выключены")
    if action == "secret_set":
        return "Сохранён секрет: %s" % d.get("name", "?")
    if action == "sale_added":
        return "Записана продажа: %s шт. (%s)" % (d.get("pieces", "?"), d.get("day", "?"))
    if action == "sale_reversed":
        return "Сторнирована продажа №%s" % d.get("id", "?")
    if action == "term_saved":
        return "Условия поставщика: %s · %s · %s, с %s" % (d.get("brand_key", "?"), _pick(TERM_SEASONS, d.get("season")),
                                                           _pick(TERM_SIZES, d.get("size_class")), d.get("valid_from", "?"))
    if action == "term_deleted":
        return "Удалены условия поставщика: %s · %s, с %s" % (d.get("brand_key", "?"), d.get("season", "?"), d.get("valid_from", "?"))
    if action == "terms_imported":
        return "Загружена таблица условий поставщика на %s: строк %s, ошибок %s" % (d.get("valid_from", "?"), d.get("rows", "?"), d.get("errors", 0))
    if action == "economics_params":
        return "Расходы Авито: комиссия %s %%, реклама %s %%" % (d.get("fee_pct", "?"), d.get("promo_pct", "?"))
    if action == "chat_assign":
        return "Чат Ч-%s: ответственный %s" % (d.get("chat", "?"), ("— " + d["to"]) if d.get("to") else "снят")
    if action == "chat_assign_default":
        return "Чат Ч-%s: ответственный по умолчанию — %s (ответственный аккаунта №%s)" % (d.get("chat", "?"), d.get("to", "?"), d.get("account", "?"))
    if action == "oth_detach":
        return "Заказ №%s на ОТХ: клиент №%s отвязан" % (d.get("order", "?"), d.get("client", "?"))
    if action == "oth_split":
        return "Заказ №%s: часть шин выделена в заказ №%s (%s)" % (d.get("order", "?"), d.get("new", "?"), "на возврат" if d.get("stage") == "return_given" else "остаток на ОТХ")
    if action == "oth_storage_split":
        return "Заказ №%s отменён (отказ клиента): шины на ОТХ, запись хранения №%s" % (d.get("order", "?"), d.get("record", "?"))
    if action == "oth_storage_take":
        return "ОТХ: заказ №%s взял запись хранения №%s целиком (%s шт.)" % (d.get("order", "?"), d.get("record", "?"), d.get("pieces", "?"))
    if action == "oth_storage_reopen":
        return "ОТХ: запись хранения №%s снова на ОТХ (отмена была ошибкой)" % d.get("record", "?")
    if action == "oth_storage_back":
        return "ОТХ: заказ №%s возвращён клиенту, запись хранения №%s закрыта" % (d.get("order", "?"), d.get("record", "?"))
    if action == "callback_extend":
        return "Задача №%s продлена до %s" % (d.get("task", "?"), str(d.get("to") or "?").replace("T", " "))
    if action == "oth_client_swap":
        return "Заказ №%s с ОТХ: клиент сменён (было: клиент №%s, %s)%s" % (
            d.get("order", "?"), d.get("old_client") or "—", d.get("old_name") or "без имени",
            " — клиент из заказа №%s" % d["from_order"] if d.get("from_order") else "")
    if action == "oth_reattach":
        return "Заказ №%s ушёл с ОТХ: клиент №%s снова привязан" % (d.get("order", "?"), d.get("client", "?"))
    if action == "oth_return":
        return "ОТХ: заказ №%s, позиция возвращена поставщику (%s шт.)" % (d.get("order", "?"), d.get("qty", "?"))
    if action == "oth_return_undo":
        return "ОТХ: заказ №%s, отметка «возвращено поставщику» снята%s" % (d.get("order", "?"), " (%s шт.)" % d["qty"] if d.get("qty") else "")
    if action == "oth_return_agreed":
        return "ОТХ: заказ №%s, возврат согласован с поставщиком на %s" % (d.get("order", "?"), d.get("date", "?"))
    if action == "oth_return_unagreed":
        return "ОТХ: заказ №%s, согласование возврата с поставщиком отменено" % d.get("order", "?")
    if action == "oth_act_signed":
        return "ОТХ: заказ №%s, точка подписала акт МХ-3 и отдала шины поставщику" % d.get("order", "?")
    if action == "price_guard_accept":
        return "Цены поставщика: принята новая цена (%d поз.)" % len(d.get("goods") or [])
    if action == "price_guard_keep":
        return "Цены поставщика: оставлена прежняя цена (%d поз.)" % len(d.get("goods") or [])
    if action == "order_merge":
        return "Заказы %s объединены в заказ №%s" % (", ".join("№%s" % i for i in d.get("orders") or []), d.get("into", "?"))
    if action == "order_unmerge":
        return "Заказ №%s разъединён: заказы %s вернулись" % (d.get("into", "?"), ", ".join("№%s" % i for i in d.get("orders") or []) or "—")
    if action == "oth_new_client":
        return "ОТХ: шины заказа №%s переданы новому клиенту, заказ №%s" % (d.get("from_order", "?"), d.get("order", "?"))
    if action == "oth_take":
        if d.get("manual"):
            return "ОТХ: заказ №%s взял %s шт. из своего остатка" % (d.get("order", "?"), d.get("qty", "?"))
        return "ОТХ: заказ №%s взял %s шт. с ОТХ заказа №%s" % (d.get("order", "?"), d.get("qty", "?"), d.get("from_order", "?"))
    if action == "oth_untake":
        if d.get("manual"):
            return "ОТХ: заказ №%s вернул %s шт. в свой остаток" % (d.get("order", "?"), d.get("qty", "?"))
        return "ОТХ: заказ №%s вернул %s шт. на ОТХ заказа №%s" % (d.get("order", "?"), d.get("qty", "?"), d.get("from_order", "?"))
    if action == "oth_numbers" and d.get("typed"):
        return "ОТХ: заказ №%s, вместо скопированного записано: %s" % (d.get("order", "?"), ", ".join(
            "%s = %s" % ({"webmim_number": "номер", "transfer_number": "перемещение"}.get(k, k), v or "пусто") for k, v in (d.get("changes") or {}).items()))
    if action == "oth_numbers":
        return "ОТХ: заказ №%s, номера WebMIM и перемещения по заказу №%s: %s" % (d.get("order", "?"), d.get("from_order") or "—", ", ".join(
            "%s = %s" % ({"webmim_number": "номер", "transfer_number": "перемещение"}.get(k, k), v or "пусто") for k, v in (d.get("changes") or {}).items()))
    if action == "oth_manual":
        return "Свой остаток (ОТХ) %s, точка №%s: %s → %s шт." % (d.get("goods_id", "?"), d.get("point", "?"), d.get("before", "?"), d.get("after", "?"))
    if action == "oth_manual_no_id":
        return "Свой остаток (ОТХ) %s: в аккаунте №%s позиция раньше не публиковалась — новый Id не выдаём, %s шт. не ушли на Авито" % (
            d.get("goods_id", "?"), d.get("account", "?"), d.get("qty", "?"))
    if action == "oth_manual_no_price":
        return "Свой остаток (ОТХ) %s: аккаунт №%s без своей цены — %s шт. не ушли на Авито" % (d.get("goods_id", "?"), d.get("account", "?"), d.get("qty", "?"))
    if action == "point_action":
        from .. import point_log
        return "Точка %s: %s%s" % ("«%s»" % d["name"] if d.get("name") else "№%s" % d.get("point", "?"), point_log.NAMES.get(d.get("kind", ""), d.get("kind") or "действие"),
                                    ", заказ №%s" % d["order"] if d.get("order") else "")
    if action == "share_labels":
        to = d.get("to") if isinstance(d.get("to"), dict) else {}
        return "Подписи долей в отчёте по заказам: «%s» и «%s»" % (to.get("owner") or "по умолчанию", to.get("partner") or "по умолчанию")
    if action == "order_rate":
        return _order_rate_text(d)
    if action == "inquiry":
        from ..inquiry import STATUSES
        return "Обращение клиента №%s: %s → %s%s" % (d.get("client", "?"), STATUSES.get(d.get("from"), d.get("from") or "—"),
                                                    STATUSES.get(d.get("to"), d.get("to") or "?"), (" (%s)" % d["why"]) if d.get("why") else "")
    if action == "inquiry_failed":
        return "Обращение не обработано: %s, попыток %s (%s)" % (INQUIRY_EVENTS.get(d.get("event"), d.get("event", "?")), d.get("tries", "?"),
                                                                 d.get("why") or "без причины")
    if action in ("alert", "notice"):
        return d.get("text", "Сбой").replace("Tires Sync: ", "", 1).replace("\n", " ")
    if action == "staff_note":                                     # critical.note: an order's alert on the person's own bell
        return "Оповещение в колокольчик (%s): %s" % (", ".join(d.get("to") or []), (d.get("text") or "")[:200])
    if action == "critical":                                       # critical.py: the bell of a critical news, the text is in «alert»/«notice»
        return "Критичное в колокольчик (%s)" % ", ".join(d.get("to") or []) if d.get("to") else "Критичное в колокольчик"
    if action == "service_pause":
        return (d.get("text") or "Служба приостановлена").replace("Tires Sync: ", "", 1).replace("\n", " ")
    words = _MORE_TEXTS.get(action)
    return words(d) if words else action


def _task_kind(kind) -> str:
    """«Задачи» (tasks.KINDS, 2026-10-31): an entry before them has no kind — it was «перезвонить»."""
    from ..tasks import KINDS
    return KINDS.get(kind or "callback", _q(kind))


def _q(value) -> str:
    """A field of a record as it is shown: «?» when it is not there."""
    return "?" if value in (None, "") else str(value)


def _count(value) -> str:
    """How many: a number as it is, a list or a dict by its length (a record keeps the codes, the journal names how many)."""
    if isinstance(value, bool):
        return "?"
    if isinstance(value, int):
        return "%d" % value
    if isinstance(value, float):
        return "%g" % value
    return str(len(value)) if isinstance(value, (list, dict, tuple)) else "?"


def _rub(value) -> str:
    return ("%.2f" % value).rstrip("0").rstrip(".") + " ₽" if isinstance(value, (int, float)) and not isinstance(value, bool) else "—"


def _pick(names: dict, key, default: str = "?") -> str:
    """names[key], the default for a missing key or for one that is no key at all (a list in an odd record)."""
    try:
        return names.get(key, default)
    except TypeError:
        return default


def _stopped(d: dict) -> str:
    return ", прервано (база занята)" if d.get("stopped") else ""


def _acc(d: dict) -> str:
    return "аккаунт №%s" % _q(d.get("account_id", d.get("account")))


def _ui_group(key) -> str:
    from .routes_design import UI_GROUPS
    return _pick(UI_GROUPS, key, _q(key))


def _ui_state(key) -> str:
    from .routes_design import UI_STATES
    return _pick(UI_STATES, key, _q(key))


def _role(key) -> str:
    from ..users import ROLES
    return _pick(ROLES, key, _q(key))


def _on(value, on: str, off: str) -> str:
    return on if value else off


TERM_SEASONS = {"winter": "зима", "summer": "лето", "all": "всесезонные"}
TERM_SIZES = {"small": "до R17", "large": "выше R17", "all": "все радиусы"}
STOCK_MODES = {"off": "выключено", "list": "по списку", "all": "все позиции"}


def _stock_text(what: str, d: dict) -> str:
    return "%s, %s: отправлено %s, ошибок %s, неизвестно %s, не отправлено %s" % (
        what, _acc(d), _count(d.get("sent")), _count(d.get("failed")), _count(d.get("unknown")), _count(d.get("not_sent")))


def _feed_failed(d: dict) -> str:
    lines = d.get("lines") if isinstance(d.get("lines"), list) else []
    return "Ручное обновление фидов не удалось" + (": %s" % str(lines[0])[:200] if lines else "")


def _split_text(d: dict) -> str:
    what = "заказ №%s" % d["order"] if d.get("order") else ("чат Ч-%s" % d["chat"] if d.get("chat") else "часть данных")
    return "Из клиента №%s выделен клиент №%s (%s)" % (_q(d.get("from")), _q(d.get("to")), what)


def _client_terms_text(d: dict) -> str:
    """«Условия клиента ivan: до R17 10 → 12 %, бренды +2 −1»; the role's conditions are «роли «Клиент»». Only %s and brand keys."""
    pct = lambda v: "—" if v is None else "%g" % v if isinstance(v, (int, float)) else _q(v)
    was, now = (x if isinstance(x, dict) else {} for x in (d.get("from"), d.get("to")))
    parts = ["%s %s → %s %%" % (label, pct(was.get(k)), pct(now.get(k))) for k, label in (("small", "до R17"), ("large", "с R18"))
             if was.get(k) != now.get(k)]
    if was.get("mode") != now.get("mode"):
        parts.append("бренды %s" % ("свой список" if now.get("mode") == "own" else "как у роли"))
    added, removed, pcts = (x if isinstance(x, (list, dict)) else [] for x in (d.get("brands_added"), d.get("brands_removed"), d.get("brand_pct")))
    if added or removed:
        parts.append("бренды +%d −%d" % (len(added), len(removed)))
    if pcts:
        parts.append("свой %% у брендов: %d" % len(pcts))
    who = "роли «Клиент» (общий %)" if d.get("subject") == "*" else "клиента %s" % _q(d.get("subject"))
    return "Условия %s: %s" % (who, ", ".join(parts) or "без изменений")


def _client_pct_text(d: dict) -> str:
    """«Свой % клиента ivan: GISLAVED (до R17 как у нас, от R18 как у нас) → (до R17 +2 п. к нашему, …)» (the owner 2026-10-31)."""
    changes = d.get("changes") if isinstance(d.get("changes"), dict) else {}
    parts = ["%s: %s → %s" % (_q(str(k).upper()), _q(v[0]), _q(v[1])) for k, v in sorted(changes.items())
             if isinstance(v, (list, tuple)) and len(v) == 2]
    shown = "; ".join(parts[:10]) + (" и ещё %d" % (len(parts) - 10) if len(parts) > 10 else "")
    return "Свой %% клиента %s: %s" % (_q(d.get("subject")), shown or "без изменений")


def _wh_text(value) -> str:
    return ", ".join("Склад %d" % n for n in (1, 2) if isinstance(value, (list, tuple)) and n in value) or "—"


def _client_followed_text(d: dict) -> str:
    """«Наш % изменён — % клиентов следом (клиентов 3): buyer, GISLAVED (лето) до R17: 12 % → 15 %; …»."""
    lines = [str(x) for x in d.get("lines") or [] if isinstance(x, str)]
    more = d.get("more") if isinstance(d.get("more"), int) else 0
    return "Наш %% изменён — %% клиентов следом (клиентов %s): %s%s" % (_q(d.get("clients")), "; ".join(lines[:10]) or "—",
                                                                     " и ещё %d" % (len(lines) - 10 + more) if len(lines) > 10 or more else "")


def _template_text(d: dict) -> str:
    """«Шаблон «…»: «было» → «стало»», each text cut to 300 signs (the owner's texts, never a client's)."""
    def cut(value):
        value = str(value or "")
        return "«%s»" % (value[:300] + "…" if len(value) > 300 else value) if value else "—"
    head = ("Шаблон «%s» удалён" if d.get("deleted") else "Шаблону «%s» возвращён исходный текст" if d.get("reset")
            else "Шаблон «%s» добавлен" if d.get("added") else "Шаблон «%s» изменён") % _q(d.get("name"))
    if d.get("was_name"):
        head += " (было имя «%s»)" % _q(d["was_name"])
    if d.get("audience"):
        head += ", адресат: %s" % _q(d["audience"])
    return "%s: %s → %s" % (head, cut(d.get("from")), cut(d.get("to")))


# The journal of actions (owner's word 2026-10-25, run «match2»): every action the code writes has its text. No secret, no address of a
# subscription, no text of a client, no login typed at a failed sign-in (it may be a password typed in the wrong field) is shown.
_MORE_TEXTS = {
    "login": lambda d: "Вход в панель с адреса %s" % _q(d.get("ip")),
    "login_failed": lambda d: "Неудачная попытка входа с адреса %s" % _q(d.get("ip")),
    "owner_login": lambda d: "Логин владельца для уведомлений: %s → %s" % (_q(d.get("from")), _q(d.get("to"))),
    "user_added": lambda d: "Добавлен пользователь %s (%s)" % (_q(d.get("login")), _role(d.get("role"))),
    "user_removed": lambda d: "Удалён пользователь %s" % _q(d.get("login")),
    "user_role": lambda d: "Роль пользователя %s: %s → %s" % (_q(d.get("login")), _role(d.get("from")), _role(d.get("to"))),
    "user_on": lambda d: "Пользователь %s включён" % _q(d.get("login")),
    "user_off": lambda d: "Пользователь %s выключен" % _q(d.get("login")),
    "user_password": lambda d: ("Пользователь %s сменил свой пароль" if d.get("self") else "Пароль пользователя %s изменён") % _q(d.get("login")),
    "invite_created": lambda d: "Создано приглашение: %s, действует до %s" % (_role(d.get("role")), _q(d.get("expires"))),
    "invite_revoked": lambda d: "Приглашение №%s отозвано" % _q(d.get("id")),
    "login_reset_created": lambda d: "Сброс входа выдан для %s, действует до %s" % (_q(d.get("login")), _q(d.get("expires"))),
    "login_reset_revoked": lambda d: "Сброс входа №%s для %s отозван" % (_q(d.get("id")), _q(d.get("login"))),
    "login_reset_used": lambda d: "Вход сброшен: %s → %s" % (_q(d.get("from")), _q(d.get("to"))),
    "shop_responsible": lambda d: ("Ответственный за заказы клиентов: %s" % d["login"]) if d.get("login")
    else "Ответственный за заказы клиентов не выбран: по умолчанию",
    "alerts_clear_all": lambda d: "Уведомления почищены у всех: красных закрыто %s, прочитанными отмечено %s (людей %s)" % (d.get("red", 0), d.get("seen", 0), d.get("people", 0)),
    "alerts_resolve_done": lambda d: "Закрыты красные записи задач, выполненных до автозакрытия: %d (заказы: %s)" % (
        int(d.get("closed") or 0), ", ".join("№%s" % o for o in d.get("orders") or ()) or "—"),
    "orders_fill_responsible": lambda d: "Заказам без ответственного поставлен ответственный: %d (без ответственного осталось %d)" % (
        len(d.get("set") or {}), len(d.get("left") or [])),
    "shop_order_cancel": lambda d: "Клиент %s отменил свой заказ №%s из каталога (этап был %s)" % (_q(d.get("login")), d.get("order", "?"), _q(d.get("stage") or "—")),
    "stage_renamed": lambda d: "Этап переименован: «%s» → «%s»" % (d.get("from", "?"), d.get("to", "?")),
    "stage_names_templates": lambda d: "Шаблоны сообщений называют этапы по-старому (тексты не изменены, поправьте вручную): %s" % "; ".join(
        "№%s «%s» — %s" % (t.get("id", "?"), t.get("name", ""), ", ".join("«%s»" % n for n in t.get("stages") or ())) for t in d.get("templates") or ()),
    "return_flow_migrated": lambda d: ("Схема возврата 2026-11-03: закрыто в «Вернули поставщику» %s, ждут суммы %s, на «На возврат» %s, пропущено %s%s"
                                       % (d.get("closed", 0), d.get("waiting_amount", 0), d.get("to_return_given", 0), d.get("skipped", 0),
                                          ", этап переименован" if d.get("renamed") else "")),
    "oth_item_to_return": lambda d: "На возврат: позиция заказа №%s, %s шт." % (d.get("order", "?"), d.get("qty", "?")),
    "oth_back_on_sale": lambda d: ("Шины снова в продаже: заказ №%s, %s шт. (не возвращаем)" % (d.get("order", "?"), d.get("qty", "?")) if d.get("qty")
                                   else "Шины снова в продаже: заказ №%s снова на ОТХ (не возвращаем)" % d.get("order", "?")),
    "oth_sold_early": lambda d: "Продано до согласования возврата: заказ №%s, метка «На возврат» снята с %s шт." % (
        d.get("order", "?"), sum(int(v) for v in (d.get("items") or {}).values()) or "?"),
    "oth_issued_early": lambda d: "Заказ выдан, возврат не нужен: заказ №%s, метка «На возврат» снята с %s шт." % (
        d.get("order", "?"), sum(int(v) for v in (d.get("items") or {}).values()) or "?"),
    "oth_marks_back": lambda d: "Метка «На возврат» возвращена: заказ №%s, %s шт." % (
        d.get("order", "?"), sum(int(v) for v in (d.get("items") or {}).values()) or "?"),
    "oth_return_amount": lambda d: "Возврат поставщику: заказ №%s, сумма возврата %s ₽ — закупка шин" % (d.get("order", "?"), d.get("amount", "?")),
    "stage_off_board": lambda d: "Этап «%s» убран с доски: его заказы — в «Списке», в поиске и в фильтре «Этап»" % (d.get("name") or d.get("stage", "?")),
    "client_own_name": lambda d: "Клиент %s сменил своё имя: %s → %s" % (_q(d.get("login")), _q(d.get("from") or "—"), _q(d.get("to") or "—")),
    "user_sheet_label": lambda d: "Подпись в таблице заказов у «%s» изменена" % _q(d.get("login")),
    "main_display_name": lambda d: ("Имя основного входа: %s" % d["name"]) if d.get("name") else "Имя основного входа убрано: показывается логин",
    "client_terms_changed": lambda d: _client_terms_text(d),
    "ui_new": lambda d: "Новый вид, %s: %s → %s" % (_ui_group(d.get("group")), _ui_state(d.get("from")), _ui_state(d.get("to"))),
    "as_point_start": lambda d: "Начал работать как точка «%s»" % _q(d.get("name") or d.get("point")),
    "as_point_stop": lambda d: "Закончил работать как точка %s%s" % (
        "«%s»" % d["name"] if d.get("name") else "№%s" % _q(d.get("point")),
        {"exit": "", "password": " (сменил пароль)", "logout": " (вышел из панели)", "signed_out": " (вход закончился)", "login_reset": " (сброс входа)",
         "invalid": " (режим больше не действует)"}.get(d.get("why"), "")),
    "message_template_changed": lambda d: _template_text(d),             # «Шаблоны сообщений» (message_texts, 2026-11-02)
    "client_pct_changed": lambda d: _client_pct_text(d),
    "client_pct_followed": lambda d: _client_followed_text(d),
    "client_warehouses_changed": lambda d: "Склады клиента %s: %s → %s" % (_q(d.get("subject")), _wh_text(d.get("was")), _wh_text(d.get("now"))),
    "shop_tochki_stale": lambda d: "Каталог клиента: «Склад 2» скрыт, снимок 4 Точек старше 6 часов (загружен %s)" % _q(d.get("fetched_at")),
    "supplier_created": lambda d: "Добавлен поставщик «%s»" % _q(d.get("name")),
    "account_chats_send": lambda d: "Отправка сообщений из панели, %s: %s" % (_acc(d), _on(d.get("enabled"), "включена", "выключена")),
    "account_modes": lambda d: "Режимы, %s: чаты %s, фид %s" % (_acc(d), _on(d.get("chats"), "вкл.", "выкл."), _on(d.get("feed"), "вкл.", "выкл.")),
    "description_template_set": lambda d: "Шаблон описания, %s: %s" % (
        _acc(d), "по умолчанию" if d.get("default") else "свой (%s знаков)" % _count(d.get("length"))),
    "connection_config": lambda d: "Изменены настройки подключения «%s»" % _q(d.get("kind")),
    "market_token": lambda d: "Выпущен новый токен парсера цен (прежний больше не действует)",
    "market_switch": lambda d: "Приём снимков цен конкурентов " + ("остановлен" if d.get("stopped") else "включён"),
    "secret_cleared": lambda d: "Удалён секрет: %s" % _q(d.get("name")),
    "secrets_cleared": lambda d: "Удалены секреты подключения «%s»" % _q(d.get("kind")),
    "daily_summary": lambda d: "Ежедневная сводка %s" % _on(d.get("on"), "включена", "выключена"),
    "term_updated": lambda d: "Изменены условия поставщика: %s · %s · %s, с %s" % (
        _q(d.get("brand_key")), _pick(TERM_SEASONS, d.get("season")), _pick(TERM_SIZES, d.get("size_class")), _q(d.get("valid_from"))),
    "old_model_name": lambda d: "Старое название модели %s %s: «%s» → «%s»" % (
        _q(d.get("avito_brand")), _q(d.get("avito_model")), d.get("before") or "—", d.get("after") or "—"),
    "id_scheme": lambda d: "Схема Id, %s: %s" % (_acc(d), _pick({"alias": "псевдонимы", "code": "коды позиций"}, d.get("scheme"))),
    "mirror_adopt": lambda d: "%s: перенесены прежние Id (%s) и фото (%s)" % (_acc(d).capitalize(), _count(d.get("ids")), _count(d.get("images"))),
    "ids_bound": lambda d: "%s: позиции привязаны к Id из кабинета (%s)" % (_acc(d).capitalize(), _count(d.get("ids"))),
    "ids_rebound": lambda d: "%s: Id перепривязаны (%s, из них общих %s)" % (_acc(d).capitalize(), _count(d.get("count")), _count(d.get("shared") or [])),
    "mirror_reissue": lambda d: "%s: новые Id для удалённых объявлений (%s)" % (_acc(d).capitalize(), _count(d.get("ids"))),
    "mirror_reissue_own": lambda d: "%s: позиции переизданы под новыми Id (%s)" % (_acc(d).capitalize(), _count(d.get("ids"))),
    "mirror_unreissue_own": lambda d: "%s: переиздание отменено, прежние Id (%s)" % (_acc(d).capitalize(), _count(d.get("codes"))),
    "avito_stock_config": lambda d: "Остатки на Авито, %s: %s, позиций в списке %s, предел %s" % (
        _acc(d), _pick(STOCK_MODES, d.get("mode")), _count(d.get("codes")), d.get("cap") if d.get("cap") not in (None, "") else "нет"),
    "avito_stock_push": lambda d: _stock_text("Остатки отправлены на Авито", d),
    "avito_stock_restore": lambda d: _stock_text("Остатки на Авито возвращены", d),
    "avito_price_config": lambda d: "Цены сразу через API, %s: %s" % (_acc(d), _pick({"off": "выключено", "dry": "проверка", "on": "включено"}, d.get("mode"))),
    "avito_price_queued": lambda d: "Цены на Авито, %s: в очереди %s%s" % (_acc(d), _count(d.get("count")), " (проверка, не отправляются)" if d.get("mode") == "dry" else ""),
    "avito_price_push": lambda d: "Цены отправлены на Авито, %s: отправлено %s, ошибок %s, не отправлено %s, пропущено %s" % (
        _acc(d), _count(d.get("sent")), _count(d.get("failed")), _count(d.get("not_sent")), _count(d.get("skipped"))),
    "feed_now_started": lambda d: "Запущен сбор фида по кнопке «Собрать фид сейчас»",
    "feed_now_refused": lambda d: "Сбор фида по кнопке не запущен: %s" % _q(d.get("why")),
    "avito_upload_started": lambda d: "Запущена загрузка на Авито, %s" % _acc(d),
    "avito_upload_refused": lambda d: "Загрузка на Авито не запущена, %s: %s" % (_acc(d), _q(d.get("text"))),
    "avito_webhook_subscribe": lambda d: "Подписка на уведомления Авито включена, %s (заменено прежних: %s)" % (_acc(d), _count(d.get("replaced") or 0)),
    "avito_webhook_unsubscribe": lambda d: "Подписка на уведомления Авито снята, %s (снято: %s)" % (_acc(d), _count(d.get("removed") or 0)),
    "feed_updated_manually": lambda d: "Фиды обновлены вручную",
    "feed_update_failed": _feed_failed,
    "vk_answer_failed": lambda d: "ВКонтакте: ответ клиенту не отправлен%s: %s" % (
        " (точка «%s»)" % d["point"] if d.get("point") else "", _q(d.get("reason"))),
    "vk_catchup": lambda d: "ВКонтакте: сообщение подобрано без уведомления%s, сообщение №%s" % (
        " (точка «%s»)" % d["point"] if d.get("point") else "", _q(d.get("message_id"))),
    "call_listened": lambda d: "Прослушана запись звонка №%s" % _q(d.get("call")),
    "callback": lambda d: "Задача №%s «%s»: %s, до %s, исполнитель %s" % (
        _q(d.get("task")), _task_kind(d.get("kind")), "заказ №%s" % d["order"] if d.get("order") else ("клиент №%s" % d["client"] if d.get("client")
        else ("чат Ч-%s" % d["chat"] if d.get("chat") else "без заказа")), _q(d.get("due")), _q(d.get("to"))),
    "callback_done": lambda d: "Задача №%s выполнена%s" % (_q(d.get("task")), (": " + str(d["result"])) if d.get("result") else ""),
    "sheet_keep_from": lambda d: "Google-таблица: вести заказы, оформленные с %s (было %s)" % (_q(d.get("to") or "всех"), _q(d.get("from") or "всех")),
    "unsent_by_hand": lambda d: "Заказ №%s: сообщение клиенту отправлено вручную (не дошло из панели)" % _q(d.get("order")),
    "order_item_mim": lambda d: "Заказ №%s: номера WebMIM для строки с ОТХ — %s" % (_q(d.get("order")), (
        "№ %s, перемещение %s" % (d.get("number") or "—", d.get("transfer") or "—")) if d.get("number") or d.get("transfer") else "как у источника"),
    "order_mim_extra": lambda d: "Заказ №%s: второй заказ WebMIM для добавленных шин — № %s, перемещение %s" % (
        _q(d.get("order")), d.get("number") or "—", d.get("transfer") or "—"),
    "autosend_add": lambda d: "Заказ №%s: поставлено автосообщение клиенту на %s" % (_q(d.get("order")), _q(str(d.get("due") or "").replace("T", " "))),
    "autosend_update": lambda d: "Заказ №%s: автосообщение клиенту изменено, на %s" % (_q(d.get("order")), _q(str(d.get("due") or "").replace("T", " "))),
    "autosend_cancel": lambda d: "Заказ №%s: автосообщение клиенту отменено" % _q(d.get("order")),
    "supplier_mail": lambda d: "Заказ №%s: открыто письмо поставщику о возврате%s" % (_q(d.get("order")), (" (%s)" % d["to"]) if d.get("to") else " (без адреса)"),   # old audit rows: «Письмо поставщику» removed 2026-11-07
    "callback_cancel": lambda d: "Задача №%s отменена" % _q(d.get("task")),
    "chat_handover": lambda d: "Точка передала клиента из чата Ч-%s владельцу и партнёру (%s), задачи «Взять клиента»: %s" % (     # chat_handover.py
        _q(d.get("chat")), ", ".join(map(str, d.get("to") or [])) if isinstance(d.get("to"), list) else _q(d.get("to")), _count(d.get("tasks"))),
    "chat_handover_taken": lambda d: "Клиент из чата Ч-%s взят в работу (%s)" % (
        _q(d.get("chat")), {"take": "кнопкой в чате", "task": "закрыта задача №%s" % _q(d.get("task"))}.get(d.get("how"), _q(d.get("how")))),
    "client_saved": lambda d: "Изменена карточка клиента №%s" % _q(d.get("client")),
    "client_tagged": lambda d: "Клиент №%s: метка «%s» %s" % (_q(d.get("client")), _q(d.get("tag")), _on(d.get("on"), "поставлена", "снята")),
    "client_pair_skipped": lambda d: "Клиенты №%s и №%s отмечены как разные люди" % (_q(d.get("client")), _q(d.get("to"))),
    "client_merged": lambda d: "Клиент №%s объединён с клиентом №%s" % (_q(d.get("from")), _q(d.get("into"))),
    "client_unmerged": lambda d: "Объединение отменено: клиент №%s снова отдельно от №%s" % (_q(d.get("to")), _q(d.get("client"))),
    "match_pair_forget": lambda d: "Пара поставщиков забыта: %s | %s (память её больше не использует)" % (_q(d.get("code_a")), _q(d.get("code_b"))),
    "match_pair_unforget": lambda d: "Пара поставщиков возвращена: %s | %s (память снова её использует)" % (_q(d.get("code_a")), _q(d.get("code_b"))),
    "match_not_pair": lambda d: "Пары поставщиков: «это не пара» %s | %s (не склеиваются)" % (_q(d.get("code_a")), _q(d.get("code_b"))),
    "match_not_pair_undo": lambda d: "Запрет «это не пара» снят: %s | %s" % (_q(d.get("code_a")), _q(d.get("code_b"))),
    "client_split": _split_text,
    "clients_linked": lambda d: "Клиенты связаны с заказами и чатами: новых клиентов %s, заказов %s, чатов %s, спорных %s" % (
        _count(d.get("clients") or 0), _count(d.get("orders") or 0), _count(d.get("chats") or 0), _count(d.get("conflicts") or 0)),
    "inquiry_merged": lambda d: "Обращение клиента №%s перенесено к клиенту №%s" % (_q(d.get("from")), _q(d.get("into"))),
    "chat_delete": lambda d: "Чат Ч-%s удалён (в «Удалённых»)" % _q(d.get("chat")),
    "chat_restore": lambda d: "Чат Ч-%s восстановлен" % _q(d.get("chat")),
    "order_delete": lambda d: "Заказ №%s удалён (в «Удалённых»)" % _q(d.get("order")),
    "order_restore": lambda d: "Заказ №%s восстановлен" % _q(d.get("order")),
    "order_chat_link": lambda d: "Чат Ч-%s привязан к заказу №%s" % (_q(d.get("chat")), _q(d.get("order"))),
    "order_chat_unlink": lambda d: "Чат Ч-%s отвязан от заказа №%s" % (_q(d.get("chat")), _q(d.get("order"))),
    "order_item_purchase": lambda d: "Заказ №%s: закупочная цена позиции %s → %s" % (_q(d.get("order")), _rub(d.get("from")), _rub(d.get("to"))),
    "orders_bulk_delete": lambda d: "Удаление разом (новый вид, «Список»): в «Удалённые» %d (№%s), не удалены %s%s" % (
        len(d.get("orders") or []), ", №".join(str(i) for i in (d.get("orders") or [])[:50]) or "—", _q(d.get("refused")),
        "; остановлено: база была занята" if d.get("stopped") else ""),
    "orders_bulk_move": lambda d: "Массовый перенос заказов: перенесено %s, пропущено %s, осталось %s%s" % (
        _count(d.get("moved") or 0), _count(d.get("skipped") or 0), _count(d.get("left") or 0), _stopped(d)),
    "orders_bulk_undo": lambda d: "Отмена массового переноса №%s: возвращено %s, пропущено %s, осталось %s%s" % (
        _q(d.get("run")), _count(d.get("back") or 0), _count(d.get("skipped") or 0), _count(d.get("left") or 0), _stopped(d)),
    "orders_cleanup_new": lambda d: "Очистка пустых новых заказов: удалено %s, пропущено %s, осталось %s%s" % (
        _count(d.get("removed") or 0), _count(d.get("skipped") or 0), _count(d.get("left") or 0), _stopped(d)),
    "share_created": lambda d: "Создана подборка №%s: позиций %s, %s" % (_q(d.get("link")), _count(d.get("items")), _on(d.get("price"), "с ценами", "без цен")),
    "share_price_toggled": lambda d: "Подборка №%s: цены %s" % (_q(d.get("link")), _on(d.get("show"), "показаны", "скрыты")),
    "share_revoked": lambda d: "Подборка №%s отозвана" % _q(d.get("link")),
}
