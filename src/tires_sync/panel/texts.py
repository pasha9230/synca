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
    if action == "oth_detach":
        return "Заказ №%s на ОТХ: клиент №%s отвязан" % (d.get("order", "?"), d.get("client", "?"))
    if action == "oth_reattach":
        return "Заказ №%s ушёл с ОТХ: клиент №%s снова привязан" % (d.get("order", "?"), d.get("client", "?"))
    if action == "oth_return":
        return "ОТХ: заказ №%s, позиция возвращена поставщику (%s шт.)" % (d.get("order", "?"), d.get("qty", "?"))
    if action == "oth_return_undo":
        return "ОТХ: заказ №%s, отметка «возвращено поставщику» снята%s" % (d.get("order", "?"), " (%s шт.)" % d["qty"] if d.get("qty") else "")
    if action == "oth_return_agreed":
        return "ОТХ: заказ №%s, возврат согласован с поставщиком на %s" % (d.get("order", "?"), d.get("date", "?"))
    if action == "oth_act_signed":
        return "ОТХ: заказ №%s, точка подписала акт МХ-3 и отдала шины поставщику" % d.get("order", "?")
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
    if action == "service_pause":
        return (d.get("text") or "Служба приостановлена").replace("Tires Sync: ", "", 1).replace("\n", " ")
    words = _MORE_TEXTS.get(action)
    return words(d) if words else action


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
    was, now = d.get("from") or {}, d.get("to") or {}
    parts = ["%s %s → %s %%" % (label, pct(was.get(k)), pct(now.get(k))) for k, label in (("small", "до R17"), ("large", "с R18"))
             if was.get(k) != now.get(k)]
    if was.get("mode") != now.get("mode"):
        parts.append("бренды %s" % ("свой список" if now.get("mode") == "own" else "как у роли"))
    added, removed = d.get("brands_added") or [], d.get("brands_removed") or []
    if added or removed:
        parts.append("бренды +%d −%d" % (len(added), len(removed)))
    if d.get("brand_pct"):
        parts.append("%% брендов изменён: %d" % len(d["brand_pct"]))
    who = "роли «Клиент»" if d.get("subject") == "*" else "клиента %s" % _q(d.get("subject"))
    return "Условия %s: %s" % (who, ", ".join(parts) or "без изменений")


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
    "client_terms_changed": lambda d: _client_terms_text(d),
    "supplier_created": lambda d: "Добавлен поставщик «%s»" % _q(d.get("name")),
    "account_chats_send": lambda d: "Отправка сообщений из панели, %s: %s" % (_acc(d), _on(d.get("enabled"), "включена", "выключена")),
    "account_modes": lambda d: "Режимы, %s: чаты %s, фид %s" % (_acc(d), _on(d.get("chats"), "вкл.", "выкл."), _on(d.get("feed"), "вкл.", "выкл.")),
    "description_template_set": lambda d: "Шаблон описания, %s: %s" % (
        _acc(d), "по умолчанию" if d.get("default") else "свой (%s знаков)" % _count(d.get("length"))),
    "connection_config": lambda d: "Изменены настройки подключения «%s»" % _q(d.get("kind")),
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
    "callback": lambda d: "Задача перезвонить №%s: %s, до %s, ответственный %s" % (
        _q(d.get("task")), "клиент №%s" % d["client"] if d.get("client") else ("заказ №%s" % d["order"] if d.get("order") else "чат Ч-%s" % _q(d.get("chat"))),
        _q(d.get("due")), _q(d.get("to"))),
    "callback_done": lambda d: "Задача перезвонить №%s выполнена" % _q(d.get("task")),
    "callback_cancel": lambda d: "Задача перезвонить №%s отменена" % _q(d.get("task")),
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
