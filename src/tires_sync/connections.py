from __future__ import annotations

import dataclasses
import json
import math
import re
import secrets
from datetime import datetime
from typing import Dict, List, Optional

from . import store
from .secretbox import SecretBox, SecretKeyError, mask

CHECK_INTERVAL_SECONDS = 10
STATUS_LIMIT = 200


def supplier_owner(supplier_id: int) -> int:
    """Connection owner id of a supplier: the first supplier keeps the original row (owner 0)."""
    return 0 if supplier_id == 1 else supplier_id


def _ensure_first_supplier(conn) -> None:
    if conn.execute("SELECT 1 FROM supplier WHERE id = 1").fetchone() is None:
        conn.execute("INSERT INTO supplier (id, name, created) VALUES (1, 'WebMIM', ?)", ("",))
        conn.commit()


def list_suppliers(conn) -> List[dict]:
    _ensure_first_supplier(conn)
    return [{"id": r[0], "name": r[1], "created": r[2]} for r in conn.execute("SELECT id, name, created FROM supplier ORDER BY id")]


def get_supplier(conn, supplier_id: int) -> Optional[dict]:
    _ensure_first_supplier(conn)
    row = conn.execute("SELECT id, name, created FROM supplier WHERE id = ?", (supplier_id,)).fetchone()
    return {"id": row[0], "name": row[1], "created": row[2]} if row else None


def create_supplier(conn, name: str, now: str, author: str) -> int:
    name = name.strip()
    if not 1 <= len(name) <= 60:
        raise ValueError("Название поставщика: от 1 до 60 символов.")
    _ensure_first_supplier(conn)
    cur = conn.execute("INSERT INTO supplier (name, created) VALUES (?, ?)", (name, now))
    supplier_id = cur.lastrowid
    store.write_audit(conn, now, author, "supplier_created", {"supplier_id": supplier_id, "name": name})
    return supplier_id


def tochki_supplier_choices(conn) -> List[dict]:
    """Supplier rows that may hold the 4tochki positions: not the first one and without a WebMIM address of their own."""
    return [s for s in list_suppliers(conn) if s["id"] != 1 and not get_config(conn, "webmim", supplier_owner(s["id"])).get("base_url")]


def set_tochki_config(conn, login: str, supplier_id: Optional[int], now: str, author: str) -> None:
    """The 4tochki login and the supplier row its positions belong to. The switch «в фид» is kept as it is (the panel never sets it)."""
    if supplier_id is not None and supplier_id not in {s["id"] for s in tochki_supplier_choices(conn)}:
        raise ValueError("Этот поставщик не подходит для 4 точек: выберите поставщика без адреса WebMIM.")
    config = dict(get_config(conn, "tochki", 0), login=login)
    config["supplier_id"] = supplier_id
    if config != get_config(conn, "tochki", 0):
        set_config(conn, "tochki", 0, config, now, author)


def supplier_territory(conn, supplier_id: int, settings) -> str:
    """The supplier's territory: the first one uses the general setting, others their own."""
    if supplier_id == 1:
        return settings.territory_rn
    territory = str(get_config(conn, "webmim", supplier_owner(supplier_id)).get("territory_rn", "")).strip()
    if not territory:
        raise ValueError("У поставщика не задана территория выгрузки.")
    return territory


def create_account(conn, name: str, now: str, author: str) -> int:
    name = name.strip()
    if not 1 <= len(name) <= 60:
        raise ValueError("Имя аккаунта: от 1 до 60 символов.")
    cur = conn.execute(
        "INSERT INTO account (name, feed_token, live, created) VALUES (?,?,0,?)",
        (name, secrets.token_hex(16), now),
    )
    account_id = cur.lastrowid
    store.write_audit(conn, now, author, "account_created", {"account_id": account_id, "name": name})
    return account_id


ACCOUNT_COLUMNS = "id, name, feed_token, live, created, description_template, chats_enabled, feed_enabled, feed_mode, chats_send"


def _account(row) -> dict:
    return {"id": row[0], "name": row[1], "feed_token": row[2], "live": row[3], "created": row[4],
            "description_template": row[5], "chats_enabled": row[6], "feed_enabled": row[7], "feed_mode": row[8], "chats_send": row[9]}


def list_accounts(conn) -> List[dict]:
    rows = conn.execute("SELECT %s FROM account ORDER BY id" % ACCOUNT_COLUMNS)
    return [_account(r) for r in rows]


def list_feed_accounts(conn) -> List[dict]:
    """Accounts whose feed we build and upload (an account that only reads messages is not among them)."""
    return [a for a in list_accounts(conn) if a["feed_enabled"]]


def list_chat_accounts(conn) -> List[dict]:
    return [a for a in list_accounts(conn) if a["chats_enabled"]]


def get_account(conn, account_id: int) -> Optional[dict]:
    row = conn.execute("SELECT %s FROM account WHERE id = ?" % ACCOUNT_COLUMNS, (account_id,)).fetchone()
    return _account(row) if row else None


def set_live(conn, account_id: int, live: bool, now: str, author: str) -> None:
    conn.execute("UPDATE account SET live = ? WHERE id = ?", (1 if live else 0, account_id))
    store.write_audit(conn, now, author, "account_live", {"account_id": account_id, "live": bool(live)})


def set_chats_send(conn, account_id: int, enabled: bool, now: str, author: str) -> None:
    """Whether replies to clients may be sent from the panel through this account (off until the owner has tried it on a test chat)."""
    conn.execute("UPDATE account SET chats_send = ? WHERE id = ?", (1 if enabled else 0, account_id))
    store.write_audit(conn, now, author, "account_chats_send", {"account_id": account_id, "enabled": bool(enabled)})


def set_modes(conn, account_id: int, chats: bool, feed: bool, now: str, author: str) -> None:
    """What the account is used for: its chats are read, its feed is built and uploaded (an account may do only the first)."""
    current = get_account(conn, account_id)
    if current is None:
        raise ValueError("Такого аккаунта нет.")
    if current["live"] and not feed:
        raise ValueError("Сначала выключите публикацию: иначе Авито продолжит брать устаревший фид.")
    if chats and not current["chats_enabled"]:
        conn.execute("DELETE FROM app_state WHERE key = ?", ("chats_baseline_%d" % account_id,))        # what was written while chats were off is the past
    conn.execute("UPDATE account SET chats_enabled = ?, feed_enabled = ? WHERE id = ?", (1 if chats else 0, 1 if feed else 0, account_id))
    if not chats:
        conn.execute("UPDATE account SET chats_send = 0 WHERE id = ?", (account_id,))          # no reading of chats, no sending either
    store.write_audit(conn, now, author, "account_modes", {"account_id": account_id, "chats": bool(chats), "feed": bool(feed)})


ADDRESS_ID = re.compile(r"^[0-9]{1,20}$")


def list_addresses(conn, account_id: int) -> List[dict]:
    rows = conn.execute(
        "SELECT name, address_id, enabled, share, address_text FROM avito_address WHERE account_id = ? ORDER BY position",
        (account_id,))
    return [{"name": r[0], "address_id": r[1], "enabled": bool(r[2]), "share": float(r[3]), "address_text": r[4] or ""}
            for r in rows]


def get_address_mode(conn, account_id: int) -> str:
    row = conn.execute("SELECT address_mode FROM account WHERE id = ?", (account_id,)).fetchone()
    return (row[0] if row else None) or "id"


def get_point_config(conn, account_id: int):
    row = conn.execute("SELECT point_mode, point_rest FROM account WHERE id = ?", (account_id,)).fetchone()
    return (row[0] or "percent", row[1]) if row else ("percent", None)


def _check_addresses(mode: str, rest: Optional[str], rows: List[dict], address_mode: str = "id") -> List[dict]:
    if address_mode not in ("id", "text"):
        raise ValueError("Способ передачи адреса: идентификатор или текст.")
    if mode not in ("percent", "count"):
        raise ValueError("Режим распределения: проценты или количество.")
    clean, seen = [], set()
    for row in rows:
        address_id = str(row["address_id"]).strip()
        name = str(row["name"]).strip()
        if not ADDRESS_ID.match(address_id):
            raise ValueError("Адрес «%s»: идентификатор Авито — число из цифр." % (name or address_id or "без названия"))
        if address_id in seen:
            raise ValueError("Идентификатор адреса %s указан повторно." % address_id)
        seen.add(address_id)
        share = row["share"]
        if not isinstance(share, (int, float)) or not math.isfinite(share) or share < 0 or (mode == "count" and share != int(share)):
            raise ValueError("Адрес «%s»: доля должна быть неотрицательным числом." % (name or address_id))
        clean.append({"name": name[:60] or address_id, "address_id": address_id,
                      "enabled": bool(row["enabled"]), "share": float(share),
                      "address_text": str(row.get("address_text", "")).strip()[:256]})
    enabled = [r for r in clean if r["enabled"]]
    if not enabled:
        raise ValueError("Нужен хотя бы один включённый адрес.")
    if address_mode == "text" and any(not r["address_text"] for r in enabled):
        raise ValueError("В режиме «адрес текстом» у каждого включённого адреса нужен полный адрес.")
    if mode == "percent" and abs(sum(r["share"] for r in enabled) - 100) > 0.01:
        raise ValueError("Доли включённых адресов в процентах должны давать в сумме 100.")
    if mode == "count" and rest is not None and rest not in {r["address_id"] for r in enabled}:
        raise ValueError("Адрес «остаток» должен быть среди включённых.")
    return clean


def save_addresses(
    conn, account_id: int, mode: str, rest: Optional[str], rows: List[dict], now: str, author: str,
    address_mode: str = "id",
) -> None:
    clean = _check_addresses(mode, rest or None, rows, address_mode)
    with conn:
        conn.execute("DELETE FROM avito_address WHERE account_id = ?", (account_id,))
        for position, row in enumerate(clean):
            conn.execute(
                "INSERT INTO avito_address (account_id, position, name, address_id, enabled, share, address_text) VALUES (?,?,?,?,?,?,?)",
                (account_id, position, row["name"], row["address_id"], 1 if row["enabled"] else 0, row["share"],
                 row["address_text"]))
        conn.execute("UPDATE account SET point_mode = ?, point_rest = ?, address_mode = ? WHERE id = ?",
                     (mode, rest or None, address_mode, account_id))
    store.write_audit(conn, now, author, "account_addresses", {
        "account_id": account_id, "mode": mode, "rest": rest or None, "address_mode": address_mode,
        "addresses": [[r["address_id"], r["enabled"], r["share"]] for r in clean]})


def list_point_rules(conn, account_id: int) -> List[dict]:
    rows = conn.execute(
        "SELECT brand_key, season, address_id FROM point_rule WHERE account_id = ? ORDER BY brand_key, season", (account_id,))
    return [{"brand_key": r[0], "season": r[1], "address_id": r[2]} for r in rows]


def set_point_rule(conn, account_id: int, brand_key: str, season: str, address_id: str, now: str, author: str) -> None:
    if address_id not in {r["address_id"] for r in list_addresses(conn, account_id) if r["enabled"]}:
        raise ValueError("Адрес %s не найден среди включённых адресов этого аккаунта." % address_id)
    conn.execute("INSERT OR REPLACE INTO point_rule (account_id, brand_key, season, address_id) VALUES (?,?,?,?)",
                 (account_id, brand_key, season, address_id))
    store.write_audit(conn, now, author, "point_rule", {
        "account_id": account_id, "brand": brand_key, "season": season, "address_id": address_id})


def clear_point_rule(conn, account_id: int, brand_key: str, season: str, now: str, author: str) -> None:
    conn.execute("DELETE FROM point_rule WHERE account_id = ? AND brand_key = ? AND season = ?",
                 (account_id, brand_key, season))
    store.write_audit(conn, now, author, "point_rule", {
        "account_id": account_id, "brand": brand_key, "season": season, "address_id": None})


def get_account_pricing(conn, account_id: int) -> dict:
    row = conn.execute("SELECT price_delta, min_stock FROM account WHERE id = ?", (account_id,)).fetchone()
    return {"delta": float(row[0] or 0.0), "min_stock": row[1]} if row else {"delta": 0.0, "min_stock": None}


def set_account_pricing(conn, account_id: int, delta, min_stock, now: str, author: str) -> None:
    if isinstance(delta, bool) or not isinstance(delta, (int, float)) or not math.isfinite(delta) or not -10 <= delta <= 10:
        raise ValueError("Поправка к проценту: число от −10 до +10 процентных пунктов.")
    if min_stock is not None and (isinstance(min_stock, bool) or not isinstance(min_stock, int) or not 1 <= min_stock <= 1000):
        raise ValueError("Порог остатка: целое число от 1 до 1000 или пусто (общий).")
    conn.execute("UPDATE account SET price_delta = ?, min_stock = ? WHERE id = ?", (float(delta), min_stock, account_id))
    store.write_audit(conn, now, author, "account_pricing", {"account_id": account_id, "delta": float(delta), "min_stock": min_stock})


def _clean_brand(text: str) -> str:
    """The brand as the pricing rules key it: upper case, one space, no supplier prefix («S2:KUMHO» is KUMHO), a Russian name read as the Latin one."""
    from .description import BRAND_RU
    text = re.sub(r"^[A-Za-z0-9]+:", "", str(text or "").strip())
    clean = " ".join(text.upper().split())
    for latin, russian in BRAND_RU.items():
        if clean == " ".join(russian.upper().split()):
            return " ".join(latin.upper().split())
    return clean


def known_brands(settings) -> list:
    """The brands that have a price rule (the markup of a brand without a rule would do nothing)."""
    return sorted({_clean_brand(key[0]) for key in settings.pricing})


def get_brand_deltas(conn, account_id: int) -> dict:
    """{BRAND KEY: percentage points} added to the account's markup of that brand (on top of the account's general delta)."""
    raw = get_config(conn, "avito", account_id).get("brand_delta") or {}
    return {str(k): float(v) for k, v in raw.items()}


def parse_brand_deltas(text: str) -> dict:
    """«Kumho: 1.5» or «KUMHO = -2» per line; an empty line is skipped, a zero value removes the brand."""
    out: dict = {}
    for number, line in enumerate(str(text or "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        found = re.fullmatch(r"(.+?)\s*[:=]\s*([+\-−]?\s*\d+(?:[.,]\d+)?)", line)
        if not found:
            raise ValueError("Строка %d: ожидается «Бренд: число», например «Kumho: 1.5»." % number)
        brand = _clean_brand(found.group(1))
        value = float(found.group(2).replace("−", "-").replace(",", ".").replace(" ", ""))
        if not math.isfinite(value) or not -10 <= value <= 10:
            raise ValueError("Строка %d: поправка бренда от −10 до +10 процентных пунктов." % number)
        out[brand] = value
    return out


def set_brand_deltas(conn, account_id: int, deltas: dict, now: str, author: str) -> None:
    config = get_config(conn, "avito", account_id)
    clean = {_clean_brand(k): float(v) for k, v in deltas.items() if float(v) != 0.0}
    if clean:
        config["brand_delta"] = clean
    else:
        config.pop("brand_delta", None)
    set_config(conn, "avito", account_id, config, now, author)


def get_photo_style(conn, account_id: int) -> dict:
    return dict(get_config(conn, "avito", account_id).get("photo_style") or {})


def set_photo_style(conn, account_id: int, raw: dict, now: str, author: str) -> dict:
    from . import photo_style
    style = photo_style.normalize(raw)
    config = get_config(conn, "avito", account_id)
    if style:
        config["photo_style"] = style
    else:
        config.pop("photo_style", None)
    set_config(conn, "avito", account_id, config, now, author)
    return style


def get_title_template(conn, account_id: int) -> str:
    return str(get_config(conn, "avito", account_id).get("title_template") or "")


def set_title_template(conn, account_id: int, template: str, now: str, author: str) -> None:
    from .feed import validate_title_template
    template = " ".join(str(template or "").split())
    errors = validate_title_template(template) if template else []
    if errors:
        raise ValueError("; ".join(errors))
    config = get_config(conn, "avito", account_id)
    if template:
        config["title_template"] = template
    else:
        config.pop("title_template", None)
    set_config(conn, "avito", account_id, config, now, author)


def get_photo_base(conn, account_id: int) -> str:
    return str(get_config(conn, "avito", account_id).get("photo_base") or "")


def set_photo_base(conn, account_id: int, base: str, now: str, author: str) -> None:
    base = str(base or "").strip().rstrip("/")
    if base and not re.fullmatch(r"https://[A-Za-z0-9.\-]+(:\d+)?(/[A-Za-z0-9._~\-/]*)?", base):
        raise ValueError("Адрес для фото: https://домен или https://домен/путь, без пробелов и параметров.")
    config = get_config(conn, "avito", account_id)
    if base:
        config["photo_base"] = base
    else:
        config.pop("photo_base", None)
    set_config(conn, "avito", account_id, config, now, author)


def _file_phone(conn, account_id: int) -> str:
    """The phone of the first ad of a mirrored file (the owner chose to keep it); empty when the account carries no file."""
    row = conn.execute("SELECT raw FROM imported_ad WHERE account_id = ? ORDER BY position LIMIT 1", (account_id,)).fetchone()
    if row is None or get_avito_identity(conn, account_id)["phone_source"] != "file":
        return ""
    import xml.etree.ElementTree as ET
    try:
        return (ET.fromstring(row[0]).findtext("ContactPhone") or "").strip()
    except ET.ParseError:
        return ""


def effective_settings(conn, account_id: int, settings):
    """General settings with this account's own addresses, phone, price delta and stock threshold."""
    pricing = get_account_pricing(conn, account_id)
    changes = {}
    identity = get_avito_identity(conn, account_id)
    phone = identity["phone"] if identity["phone_source"] == "cabinet" else ""
    if phone:                                                      # the ads carry the phone of the cabinet they are published in
        changes["contact_phone"] = phone
    elif account_id != PRIMARY_ACCOUNT:                            # the owner's general number belongs to the primary account only
        changes["contact_phone"] = _file_phone(conn, account_id)
    brand_delta = get_brand_deltas(conn, account_id)
    if pricing["delta"] or brand_delta:
        def total(key):
            value = pricing["delta"] + brand_delta.get(_clean_brand(key[0]), 0.0)        # the account's delta plus its own markup of this brand
            return round(max(-10.0, min(10.0, value)), 4)                                # never more than ten points either way, no float dust
        changes["pricing"] = {
            key: dataclasses.replace(rule, pct_small=rule.pct_small + total(key), pct_large=rule.pct_large + total(key))
            for key, rule in settings.pricing.items()}
    if pricing["min_stock"] is not None:
        changes["min_stock"] = pricing["min_stock"]
    enabled = [r for r in list_addresses(conn, account_id) if r["enabled"]]
    if enabled:
        mode, rest = get_point_config(conn, account_id)
        changes.update(
            point_mode=mode, point_rest=rest, point_values={r["address_id"]: r["share"] for r in enabled},
            point_text=({r["address_id"]: r["address_text"] for r in enabled} if get_address_mode(conn, account_id) == "text" else {}))
    return dataclasses.replace(settings, **changes) if changes else settings


def set_description_template(conn, account_id: int, template: Optional[str], now: str, author: str) -> None:
    conn.execute("UPDATE account SET description_template = ? WHERE id = ?", (template, account_id))
    store.write_audit(conn, now, author, "description_template_set", {
        "account_id": account_id, "length": len(template) if template else 0, "default": template is None})


def set_config(conn, kind: str, owner_id: int, config: dict, now: str, author: str) -> None:
    conn.execute(
        "INSERT INTO connection (kind, owner_id, config) VALUES (?,?,?) "
        "ON CONFLICT(kind, owner_id) DO UPDATE SET config = excluded.config",
        (kind, owner_id, json.dumps(config, ensure_ascii=False)),
    )
    store.write_audit(conn, now, author, "connection_config", {"kind": kind, "owner_id": owner_id})


def normalize_phone(raw) -> str:
    """A phone the way the feed writes it, +7XXXXXXXXXX; empty when it is not a Russian mobile or city number."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    return "+7" + digits if len(digits) == 10 else ""


def format_phone(raw) -> str:
    """+7 900 000-00-11 for a phone that normalizes; the text as it is otherwise."""
    phone = normalize_phone(raw)
    if not phone:
        return str(raw or "")
    d = phone[2:]
    return "+7 %s %s-%s-%s" % (d[:3], d[3:6], d[6:8], d[8:])


def set_avito_identity(conn, account_id: int, user_id, name, phone=None, phones=None) -> None:
    """Remember whose cabinet the keys open; `phone` (the cabinet's main number, as Avito gives it) is kept only when it is a real number and the
    owner has not pinned another one of the cabinet's numbers; `phones` is the whole list of the cabinet.
    A check decides a pin (the team lead 2026-10-23): the pinned number is in the cabinet's list — it stays (the same keys pasted again); it is
    not, or there is none — the pin goes and the main number (else the only one) goes into the ads. A list Avito did not give (empty) proves
    nothing: the pin stays, after new keys only for the same cabinet (its id). Without a list at all (the chats' tick) a pin is decided by
    nothing but the cabinet's id: the same cabinet after new keys gets its pinned number back, otherwise the pin is left for the check."""
    config = get_config(conn, "avito", account_id)
    config["avito_user_id"], config["avito_name"] = user_id, name
    main = normalize_phone(phone)
    if phones is not None:
        config["avito_phones"] = [p for p in (normalize_phone(x) for x in phones) if p]
        if not main and len(config["avito_phones"]) == 1:
            main = config["avito_phones"][0]
    if config.get("phone_pinned"):
        kept = bool(config.get("avito_phone"))                # no new keys since: the number is the cabinet's own
        pinned = config.get("avito_phone") or config.get("pinned_phone") or ""
        same = bool(config.get("pinned_phone")) and str(config.get("pinned_user")) == str(user_id)
        if phones is None:
            if not kept and same:                            # the same keys again, seen by the chats' tick: the pinned number is back
                config["avito_phone"] = pinned
                config.pop("pinned_phone", None)
                config.pop("pinned_user", None)
        else:
            known = config["avito_phones"]
            if pinned and (pinned in known if known else kept or same):
                config["avito_phone"] = pinned
            elif main:
                config.pop("phone_pinned", None)
            config.pop("pinned_phone", None)
            config.pop("pinned_user", None)
    if main and not config.get("phone_pinned"):
        config["avito_phone"] = main
    conn.execute(
        "INSERT INTO connection (kind, owner_id, config) VALUES ('avito', ?, ?) "
        "ON CONFLICT(kind, owner_id) DO UPDATE SET config = excluded.config",
        (account_id, json.dumps(config, ensure_ascii=False)))
    conn.commit()


def get_avito_identity(conn, account_id: int) -> dict:
    """`phone_source`: «cabinet» (the cabinet's own number goes into the ads) or «file» (the owner chose to keep the phone of a mirrored file)."""
    config = get_config(conn, "avito", account_id)
    account = get_account(conn, account_id)
    mirror = account is not None and account.get("feed_mode") == "mirror"
    source = "file" if config.get("phone_source") == "file" and mirror else "cabinet"      # «from the file» means something only for a mirrored file
    return {"id": config.get("avito_user_id"), "name": config.get("avito_name"), "phone": config.get("avito_phone") or "", "phone_source": source,
            "phones": list(config.get("avito_phones") or []), "pinned": bool(config.get("phone_pinned"))}


CONTACT_NAME_MAX = 40


def get_contact_name(conn, account_id: int) -> str:
    """The contact person's name that goes into `ManagerName` of this account's ads (empty: the element is not written)."""
    return str(get_config(conn, "avito", account_id).get("contact_name") or "")


def set_contact_name(conn, account_id: int, name: str, now: str, author: str) -> None:
    name = " ".join(str(name or "").split())
    if len(name) > CONTACT_NAME_MAX:
        raise ValueError("Имя контактного лица: не длиннее %d знаков." % CONTACT_NAME_MAX)
    if any(ord(ch) < 32 for ch in name) or "<" in name or ">" in name:
        raise ValueError("Имя контактного лица: только буквы, цифры и обычные знаки.")
    config = get_config(conn, "avito", account_id)
    if name:
        config["contact_name"] = name
    else:
        config.pop("contact_name", None)
    set_config(conn, "avito", account_id, config, now, author)


def pin_phone(conn, account_id: int, phone: str, now: str, author: str) -> None:
    """The owner chooses which of the cabinet's numbers goes into the ads; a later connection check does not replace it. An empty value unpins."""
    config = get_config(conn, "avito", account_id)
    if not phone:
        for key in ("phone_pinned", "pinned_phone", "pinned_user"):
            config.pop(key, None)
        set_config(conn, "avito", account_id, config, now, author)
        return
    number = normalize_phone(phone)
    known = config.get("avito_phones") or []
    if not number:
        raise ValueError("Номер телефона не распознан.")
    if not config.get("avito_user_id"):                      # new keys: the cabinet is not read yet, a number of the old one must not be pinned
        raise ValueError("Сначала нажмите «Проверить подключение»: номера кабинета Авито ещё не прочитаны.")
    if not known:                                            # the chats' tick gives no list: the page offers no choice, a hand-made POST pins nothing
        raise ValueError("Список номеров кабинета пуст: нажмите «Проверить подключение». Если Авито не даёт номера и после неё, "
                         "в объявлениях основной номер кабинета.")
    if number not in known:
        raise ValueError("Этого номера нет среди номеров кабинета Авито.")
    config["avito_phone"], config["phone_pinned"] = number, True
    set_config(conn, "avito", account_id, config, now, author)


def set_phone_source(conn, account_id: int, source: str, now: str, author: str) -> None:
    if source not in ("cabinet", "file"):
        raise ValueError("Источник телефона: кабинет или файл.")
    config = get_config(conn, "avito", account_id)
    config["phone_source"] = source
    set_config(conn, "avito", account_id, config, now, author)


NO_CABINET_ID = "в ответе нет Id кабинета"


def cabinet_user_id(conn, client, account_id: int, retry: bool = True):
    """The Avito user id of the account's cabinet: the one recorded at the connection check, otherwise asked once and remembered
    (chats and statistics run every minute and must not ask Avito who we are each time). An answer without the id is Avito's odd answer
    (AvitoError): its callers treat it as one, nothing is remembered."""
    known = get_avito_identity(conn, account_id)["id"]
    if known:
        return known
    me = client.get_self() if retry else client.get_self(retry=False)
    if not me.get("id"):                                     # a 200 without the id: no request may go to /accounts/None/
        from .avito import AvitoError
        raise AvitoError(200, None, NO_CABINET_ID)
    set_avito_identity(conn, account_id, me["id"], me.get("name"), me.get("phone"))
    return me["id"]


def forget_identity(conn, account_id: int) -> None:
    """New keys may open another cabinet: its id, name and phone are read again at the next check (no number goes into the ads until then).
    A pinned number is put aside: the check keeps it when the cabinet still has it (set_avito_identity)."""
    config = get_config(conn, "avito", account_id)
    if config.get("phone_pinned") and config.get("avito_phone"):
        config["pinned_phone"], config["pinned_user"] = config["avito_phone"], config.get("avito_user_id")
    for key in ("avito_user_id", "avito_name", "avito_phone", "avito_phones"):      # the old cabinet's list must not let its number be pinned
        config.pop(key, None)
    if config or conn.execute("SELECT 1 FROM connection WHERE kind = 'avito' AND owner_id = ?", (account_id,)).fetchone():
        conn.execute("INSERT INTO connection (kind, owner_id, config) VALUES ('avito', ?, ?) "
                     "ON CONFLICT(kind, owner_id) DO UPDATE SET config = excluded.config", (account_id, json.dumps(config, ensure_ascii=False)))
        conn.commit()


TIRE_TYPES = ("Зимние шипованные", "Зимние нешипованные", "Летние", "Всесезонные")


def get_excluded_types(conn, account_id: int) -> list:
    """The tire types (`TireType` values) the owner left out of this account's feed."""
    return [t for t in get_config(conn, "avito", account_id).get("exclude_tire_types", []) if t in TIRE_TYPES]


def set_excluded_types(conn, account_id: int, types, now: str, author: str) -> None:
    types = [t for t in dict.fromkeys(types or [])]
    bad = [t for t in types if t not in TIRE_TYPES]
    if bad:
        raise ValueError("Неизвестный тип шин: %s." % ", ".join(bad))
    config = get_config(conn, "avito", account_id)
    config["exclude_tire_types"] = types
    set_config(conn, "avito", account_id, config, now, author)


PRIMARY_ACCOUNT = 1                  # the first account keeps the plain Ids of the positions; the others get their own


def photo_address(conn, account_id: int) -> str:
    """The address the account's photo links start with: its own address of photos, else ours; "" when there is none."""
    return (get_photo_base(conn, account_id) or store.get_state(conn, "public_base") or "").strip().rstrip("/")


def feed_style(conn, account_id: int):
    """(Id suffix, photo link prefix) of an account's own feed. Accounts must not repeat Ids, and the photo link of every account is its own
    (`<public address>/img/<feed token>/`); without a public address the supplier's link stays."""
    suffix = "" if account_id == PRIMARY_ACCOUNT else "_a%d" % account_id
    base = photo_address(conn, account_id)
    account = get_account(conn, account_id)
    prefix = "%s/img/%s/" % (base, account["feed_token"]) if base and account else ""
    return suffix, prefix


def has_avito_keys(conn, account_id: int) -> bool:
    """The account has been connected to Avito: its client id is stored (nothing is decrypted)."""
    row = conn.execute("SELECT 1 FROM secret WHERE kind = 'avito' AND owner_id = ? AND name = 'client_id'", (account_id,)).fetchone()
    return row is not None


def card_view(conn, account: dict) -> dict:
    """The head of an Avito account's card in the one look of the cards (_entity.html), a dict ready to be JSON (no token, no key in it)."""
    identity = get_avito_identity(conn, account["id"])
    if not account["feed_enabled"]:
        status = {"text": "Фид не выгружается", "tone": "neutral"}
    else:
        status = {"text": "Публикация включена", "tone": "ok"} if account["live"] else {"text": "Публикация выключена", "tone": "warn"}
    return {"type": "account", "kind": "Аккаунт Авито", "ref": "№%d" % account["id"], "title": display_name(conn, account["id"]), "status": status,
            "crumbs": [{"label": "Подключения", "href": "/connections"}, {"label": "Аккаунты Авито", "href": "/connections/avito"},
                       {"label": display_name(conn, account["id"])}],
            "fields": [{"label": "ID кабинета", "value": identity["id"], "mono": True}, {"label": "Имя в кабинете", "value": identity["name"]},
                       {"label": "Телефон в объявлениях", "value": identity["phone"] or "появится после «Проверить подключение»",
                        "fmt": "phone" if identity["phone"] else None},
                       {"label": "Внутреннее название", "value": account["name"]},
                       {"label": "Фид", "value": "перенос чужого файла" if account["feed_mode"] == "mirror" else "своя сборка"},
                       {"label": "Чаты", "value": ("читаются и отправляются" if account["chats_send"] else "только читаются") if account["chats_enabled"] else "выключены"}]}


def display_name(conn, account_id: int) -> str:
    """How an account is shown in the panel: its Avito id once known, otherwise the name given on creation."""
    identity = get_avito_identity(conn, account_id)
    if identity["id"]:
        return "ID %s" % identity["id"]
    account = get_account(conn, account_id)
    return account["name"] if account else str(account_id)


def get_config(conn, kind: str, owner_id: int) -> dict:
    row = conn.execute("SELECT config FROM connection WHERE kind = ? AND owner_id = ?", (kind, owner_id)).fetchone()
    return json.loads(row[0]) if row else {}


def set_secret(conn, box: Optional[SecretBox], kind: str, owner_id: int, name: str, value: str, now: str, author: str) -> None:
    if box is None:
        raise SecretKeyError("master key is not available")
    value = value.strip()
    if not value:
        raise ValueError("Пустое значение секрета не сохраняется.")
    conn.execute(
        "INSERT INTO secret (kind, owner_id, name, ciphertext, updated) VALUES (?,?,?,?,?) "
        "ON CONFLICT(kind, owner_id, name) DO UPDATE SET ciphertext = excluded.ciphertext, updated = excluded.updated",
        (kind, owner_id, name, box.encrypt(value), now),
    )
    store.write_audit(conn, now, author, "secret_set", {"kind": kind, "owner_id": owner_id, "name": name})


def clear_secrets(conn, kind: str, owner_id: int, now: str, author: str) -> None:
    conn.execute("DELETE FROM secret WHERE kind = ? AND owner_id = ?", (kind, owner_id))
    store.write_audit(conn, now, author, "secrets_cleared", {"kind": kind, "owner_id": owner_id})


def get_secret(conn, box: Optional[SecretBox], kind: str, owner_id: int, name: str) -> Optional[str]:
    if box is None:
        raise SecretKeyError("master key is not available")
    row = conn.execute(
        "SELECT ciphertext FROM secret WHERE kind = ? AND owner_id = ? AND name = ?", (kind, owner_id, name)
    ).fetchone()
    return box.decrypt(row[0]) if row else None


def secret_status(conn, box: Optional[SecretBox], kind: str, owner_id: int) -> Dict[str, dict]:
    result: Dict[str, dict] = {}
    rows = conn.execute(
        "SELECT name, ciphertext, updated FROM secret WHERE kind = ? AND owner_id = ? ORDER BY name", (kind, owner_id)
    )
    for name, ciphertext, updated in rows:
        readable, shown = False, ""
        if box is not None:
            try:
                shown = mask(name, box.decrypt(ciphertext))
                readable = True
            except SecretKeyError:
                shown = ""
        result[name] = {"updated": updated, "mask": shown, "readable": readable}
    return result


def record_check(conn, kind: str, owner_id: int, ok: bool, message: str, now: str) -> None:
    status = ("ok: " if ok else "error: ") + message[:STATUS_LIMIT]
    conn.execute(
        "INSERT INTO connection (kind, owner_id, last_check_at, last_check_status) VALUES (?,?,?,?) "
        "ON CONFLICT(kind, owner_id) DO UPDATE SET last_check_at = excluded.last_check_at, "
        "last_check_status = excluded.last_check_status",
        (kind, owner_id, now, status),
    )
    conn.commit()


def get_check(conn, kind: str, owner_id: int) -> Optional[dict]:
    row = conn.execute(
        "SELECT last_check_at, last_check_status FROM connection WHERE kind = ? AND owner_id = ?", (kind, owner_id)
    ).fetchone()
    if not row or row[0] is None:
        return None
    return {"at": row[0], "status": row[1]}


def check_allowed(conn, kind: str, owner_id: int, now: str, seconds: int = CHECK_INTERVAL_SECONDS) -> bool:
    check = get_check(conn, kind, owner_id)
    if check is None:
        return True
    elapsed = (datetime.fromisoformat(now) - datetime.fromisoformat(check["at"])).total_seconds()
    return elapsed >= seconds
