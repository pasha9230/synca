"""Panel users: a login and a password hash each, a role and an «on/off» switch. The old shared password (panel.env) stays valid for the owner.

The role (panel_user_role, a table of its own so that the old code keeps working after a revert) decides what a person sees and changes:
every section of the panel at a level «нет / смотреть / менять» plus the dangerous rights apart. A user without a row is an owner
(the users made before the roles). The table of rights is here; which page belongs to which section is in panel/common.py."""
from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from werkzeug.security import check_password_hash, generate_password_hash

from . import client_terms, store

RESERVED = frozenset({"owner", "server", "system", "cli"})      # names of the shared password and of server-side actions in the journal
LOGIN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
MIN_PASSWORD = 10
HASH_METHOD = "pbkdf2:sha256:600000"      # portable (no scrypt needed); tests lower the cost
_dummy = {}


def _hash(password: str) -> str:
    return generate_password_hash(password, method=HASH_METHOD)


def warm() -> None:
    """Compute the dummy hash once at start, so that the first unknown login is not slower than the others."""
    _dummy_hash()


def _dummy_hash() -> str:
    """Checked for unknown logins too, so that the answer takes as long as for a wrong password."""
    if HASH_METHOD not in _dummy:
        _dummy[HASH_METHOD] = _hash("no-such-user-password")
    return _dummy[HASH_METHOD]

class UserError(ValueError):
    pass


class WrongPassword(UserError):
    """The current password did not match: the panel counts it as a failed sign-in (whatever the text of the message)."""


def normalize(login: str) -> str:
    return (login or "").strip().lower()


def _check_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise UserError("Пароль: не короче %d символов." % MIN_PASSWORD)


def add_user(conn, login: str, password: str, now: str, role: str = "owner", point_id: Optional[int] = None, display_name: str = "",
             author: str = "server", commit: bool = True) -> str:
    """`commit=False`: inside the caller's transaction (accept_invite writes the user and a client's conditions at once)."""
    login = normalize(login)
    _check_role(None, role, point_id)
    if not LOGIN.match(login):
        raise UserError("Логин: 2–32 символа, латинские буквы, цифры, точка, дефис, подчёркивание.")
    if login in RESERVED:
        raise UserError("Логин «%s» зарезервирован." % login)
    _check_password(password)
    if conn.execute("SELECT 1 FROM panel_user WHERE login = ?", (login,)).fetchone():
        raise UserError("Пользователь «%s» уже есть." % login)
    conn.execute("INSERT INTO panel_user VALUES (?,?,?)", (login, _hash(password), now))
    conn.execute("INSERT OR REPLACE INTO panel_user_role (login, role, active, point_id, display_name, updated, updated_by) VALUES (?,?,1,?,?,?,?)",
                 (login, role, point_id if role == "point" else None, _name(display_name), now, author))
    client_terms.audit(conn, now, author, "user_added", {"login": login, "role": role}, commit)
    return login


def set_password(conn, login: str, password: str, now: str) -> None:
    login = normalize(login)
    _check_password(password)
    if not conn.execute("SELECT 1 FROM panel_user WHERE login = ?", (login,)).fetchone():
        raise UserError("Пользователя «%s» нет." % login)
    conn.execute("UPDATE panel_user SET password_hash = ? WHERE login = ?", (_hash(password), login))
    store.write_audit(conn, now, "server", "user_password", {"login": login})
    store.bump_session_epoch(conn, login)          # every open session of this user ends


def remove_user(conn, login: str, now: str) -> None:
    login = normalize(login)
    if conn.execute("DELETE FROM panel_user WHERE login = ?", (login,)).rowcount == 0:
        raise UserError("Пользователя «%s» нет." % login)
    conn.execute("DELETE FROM panel_user_role WHERE login = ?", (login,))
    conn.execute("DELETE FROM client_terms WHERE subject = ?", (login,))           # a client's own conditions ('*' is the role's, never a login)
    conn.execute("DELETE FROM client_brand WHERE subject = ?", (login,))
    conn.execute("DELETE FROM client_brand_pct WHERE login = ?", (login,))        # the client's own % of the brands
    conn.execute("DELETE FROM user_pref WHERE login = ?", (login,))                  # the person's own settings (the columns of «Заказы»)
    store.write_audit(conn, now, "server", "user_removed", {"login": login})
    store.bump_session_epoch(conn, login)


def list_users(conn) -> List[dict]:
    rows = conn.execute("SELECT u.login, u.created, COALESCE(r.role, 'owner'), COALESCE(r.active, 1), r.point_id, COALESCE(r.display_name, '') "
                        "FROM panel_user u LEFT JOIN panel_user_role r ON r.login = u.login ORDER BY u.login")
    return [{"login": r[0], "created": r[1], "role": r[2], "active": bool(r[3]), "point_id": r[4], "display_name": r[5]} for r in rows]


def exists(conn, login: str) -> bool:
    return conn.execute("SELECT 1 FROM panel_user WHERE login = ?", (normalize(login),)).fetchone() is not None


def verify(conn, login: str, password: str) -> Optional[str]:
    """The normalized login if the password is right, otherwise None."""
    login = normalize(login)
    row = conn.execute("SELECT password_hash FROM panel_user WHERE login = ?", (login,)).fetchone()
    stored = row[0] if row else _dummy_hash()
    ok = check_password_hash(stored, password or "")
    return login if row and ok and is_active(conn, login) else None


# ---- roles and rights -------------------------------------------------------------------------------------------------------------

NONE, VIEW, EDIT = 0, 1, 2
ROLES = {"owner": "Владелец", "partner": "Партнёр", "chats": "Менеджер чатов", "point": "Менеджер точки", "viewer": "Наблюдатель",
         "client": "Клиент"}
SECTIONS = {"home": "Главная", "orders": "Заказы", "chats": "Чаты", "calls": "Звонки", "clients": "CRM", "alerts": "Уведомления", "catalog": "Товар и фид",
            "analytics": "Аналитика (деньги)", "setup": "Настройка", "users": "Пользователи", "self": "Свой пароль",
            "mypoint": "Рабочее место", "shop": "Каталог для клиента"}
RIGHTS = {"money": "видеть закупку и прибыль", "delete": "удалять", "bulk": "массово закрывать", "publish": "публиковать и обновлять фид",
          "send": "отправлять клиенту", "connections": "менять подключения и ключи", "supplier": "оформлять заказ у поставщика",
          "listen": "слушать записи звонков"}
# The partner looks at everything and changes the sales; never a key, never the connections nor the users (the owner 2026-10-21).
# «supplier»: «Оформить в WebMIM» — the owner and the partner only (the owner 2026-10-19).
# The manager of a point sees the communications, the orders and the alerts of the own point only, and works with the chats of the point's
# Avito account: answers the clients, makes and links the orders of the point (the owner 2026-10-21; panel/common.py narrows the pages,
# the cards, the chats and the alerts).
# «Звонки»: the recordings are for the operator, the admins and the owners; the manager of a point hears the calls of the point's account
# only; the observer sees the list and never the recordings (the owner 2026-10-21, the lead 2026-10-23).
# «CRM» is not the point's (the owner 2026-10-29; before, its own part of a client, 2026-10-26): the section is not in its row below, and the
# order's card, the chats and the calls show it no link to a client.
# «Рабочее место» (mypoint) is the point's start page; «История точки» is in «Пользователи»: the owner and the partner only.
# The client (a buyer, the owner 2026-10-29): «Каталог для клиента» (shop) with the own prices and the own password, nothing else
# (panel/common.CLIENT_PAGES); no other role has «shop»: the owner looks at a client's catalog through a preview in «Пользователи».
MATRIX: Dict[str, dict] = {
    "owner": {"sections": {s: EDIT for s in SECTIONS if s != "shop"}, "rights": set(RIGHTS)},
    "partner": {"sections": dict(home=VIEW, orders=EDIT, chats=EDIT, calls=EDIT, clients=EDIT, alerts=EDIT, catalog=VIEW, analytics=VIEW, setup=VIEW, users=VIEW,
                                 self=EDIT, mypoint=EDIT),
                "rights": {"money", "delete", "bulk", "send", "supplier", "listen"}},
    "chats": {"sections": dict(home=VIEW, orders=EDIT, chats=EDIT, calls=EDIT, clients=EDIT, alerts=EDIT, catalog=VIEW, self=EDIT),
              "rights": {"send", "listen"}},
    "point": {"sections": dict(orders=EDIT, chats=EDIT, calls=EDIT, alerts=EDIT, self=EDIT, mypoint=EDIT), "rights": {"send", "listen"}},
    "viewer": {"sections": dict(home=VIEW, orders=VIEW, chats=VIEW, calls=VIEW, clients=VIEW, alerts=EDIT, catalog=VIEW, self=EDIT), "rights": set()},
    "client": {"sections": dict(shop=EDIT, self=EDIT), "rights": set()},          # EDIT: the own cart and orders (the owner 2026-10-30)
}
INVITE_HOURS = 48
NAME_MAX = 60


def level(role: str, section: str) -> int:
    return MATRIX.get(role, {"sections": {}})["sections"].get(section, NONE)


def has_right(role: str, right: str) -> bool:
    return right in MATRIX.get(role, {"rights": set()})["rights"]


def _check_role(conn, role: str, point_id) -> None:
    """The role is known; a manager of a point gets a point, and with `conn` a point that is there (an invitation, accepting it and a
    change of the role: a deleted point is refused, the owner 2026-10-26; the server's own add_user does not look)."""
    if role not in ROLES:
        raise UserError("Нет такой роли.")
    if role == "point" and not point_id:
        raise UserError("Менеджеру точки нужна точка.")
    if role == "point" and conn is not None and not conn.execute("SELECT 1 FROM point WHERE id = ?", (point_id,)).fetchone():
        raise UserError("Такой точки нет: она удалена или не заведена.")


def _name(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:NAME_MAX]


MAIN_NAME_KEY = "main_display_name"      # app_state: the name of the owner's main login (the shared password; not a panel user)


def main_name(conn) -> str:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (MAIN_NAME_KEY,)).fetchone()
    return _name(row[0]) if row else ""


def set_main_name(conn, name: str, now: str, author: str) -> None:
    """The name the owner's main login is shown by (the owner 2026-10-30: «Пользователи → Имя основного входа»); empty: its login."""
    conn.execute("INSERT OR REPLACE INTO app_state VALUES (?, ?)", (MAIN_NAME_KEY, _name(name)))
    store.write_audit(conn, now, author, "main_display_name", {"name": _name(name)})
    conn.commit()


def person_name(conn, login: str, main_login: str = "") -> str:
    """How a person is shown (the journal, the history, the responsible, «Рабочее место»): the display name of the panel user; the owner's
    main login (`main_login`, the panel's AUTHOR) by main_name; else the login."""
    login = str(login or "")
    if not login:
        return ""
    shown = get_role(conn, login)["display_name"]
    if not shown and main_login and login == main_login:
        shown = main_name(conn)
    return shown or login


def get_role(conn, login: str) -> dict:
    """{"role", "active", "point_id", "display_name"} of a panel user; a user made before the roles is an active owner."""
    row = conn.execute("SELECT role, active, point_id, display_name FROM panel_user_role WHERE login = ?", (normalize(login),)).fetchone()
    if row is None:
        return {"role": "owner", "active": True, "point_id": None, "display_name": ""}
    return {"role": row[0], "active": bool(row[1]), "point_id": row[2], "display_name": row[3]}


def is_active(conn, login: str) -> bool:
    return get_role(conn, login)["active"]


def set_own_name(conn, login: str, name: str, now: str) -> None:
    """A client names itself in «Профиль» (the owner 2026-10-30): the name of its orders; nothing else of the user changes."""
    name = _name(name)
    if not name:
        raise UserError("Название или имя: впишите его.")
    old = get_role(conn, login)
    if old["role"] != "client":
        raise UserError("Имя меняет владелец.")
    conn.execute("UPDATE panel_user_role SET display_name = ?, updated = ?, updated_by = ? WHERE login = ?", (name, now, normalize(login), normalize(login)))
    store.write_audit(conn, now, normalize(login), "client_own_name", {"login": normalize(login), "from": old["display_name"], "to": name})
    conn.commit()


def set_role(conn, login: str, role: str, point_id, display_name: str, now: str, author: str) -> None:
    login = normalize(login)
    if not exists(conn, login):
        raise UserError("Пользователя «%s» нет." % login)
    _check_role(conn, role, point_id)
    old = get_role(conn, login)
    if login == normalize(author) and role != old["role"]:
        raise UserError("Свою роль поменять нельзя: попросите другого владельца или войдите общим паролем.")
    conn.execute("INSERT INTO panel_user_role (login, role, active, point_id, display_name, updated, updated_by) VALUES (?,?,?,?,?,?,?) "
                 "ON CONFLICT(login) DO UPDATE SET role = excluded.role, point_id = excluded.point_id, display_name = excluded.display_name, "
                 "updated = excluded.updated, updated_by = excluded.updated_by",
                 (login, role, int(old["active"]), point_id if role == "point" else None, _name(display_name), now, author))
    store.write_audit(conn, now, author, "user_role", {"login": login, "from": old["role"], "to": role,
                                                       "point_id": point_id if role == "point" else None})


def set_active(conn, login: str, active: bool, now: str, author: str) -> None:
    """Switch a user off (the history stays, the sessions end at once) or back on."""
    login = normalize(login)
    if not exists(conn, login):
        raise UserError("Пользователя «%s» нет." % login)
    if login == normalize(author) and not active:
        raise UserError("Себя выключить нельзя.")
    old = get_role(conn, login)
    conn.execute("INSERT INTO panel_user_role (login, role, active, point_id, display_name, updated, updated_by) VALUES (?,?,?,?,?,?,?) "
                 "ON CONFLICT(login) DO UPDATE SET active = excluded.active, updated = excluded.updated, updated_by = excluded.updated_by",
                 (login, old["role"], int(bool(active)), old["point_id"], old["display_name"], now, author))
    store.write_audit(conn, now, author, "user_on" if active else "user_off", {"login": login})
    if not active:
        store.bump_session_epoch(conn, login)


def change_own_password(conn, login: str, old_password: str, new_password: str, now: str) -> None:
    login = normalize(login)
    if verify(conn, login, old_password) is None:
        raise WrongPassword("Текущий пароль не подошёл.")
    _check_password(new_password)
    conn.execute("UPDATE panel_user SET password_hash = ? WHERE login = ?", (_hash(new_password), login))
    store.write_audit(conn, now, login, "user_password", {"login": login, "self": True})
    store.bump_session_epoch(conn, login)          # the other open sessions end; the caller signs this one in again


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_invite(conn, role: str, point_id, display_name: str, now: str, author: str, terms: str = "") -> str:
    """A one-time link for 48 hours; only its hash is kept. Returns the token (shown once, the panel sends nothing). `terms`: a client's
    conditions as client_terms.invite_terms made them (checked there); kept for a client's invitation only."""
    _check_role(conn, role, point_id)
    terms = terms if role == "client" else ""
    if terms:
        client_terms.from_invite(terms)
    token = secrets.token_urlsafe(32)
    expires = (datetime.fromisoformat(now) + timedelta(hours=INVITE_HOURS)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO panel_invite (token_hash, role, point_id, display_name, created, expires, created_by, client_terms) "
                 "VALUES (?,?,?,?,?,?,?,?)", (_token_hash(token), role, point_id if role == "point" else None, _name(display_name), now, expires,
                                              author, terms))
    store.write_audit(conn, now, author, "invite_created", {"role": role, "expires": expires})
    return token


def find_invite(conn, token: str, now: str) -> Optional[dict]:
    """The invitation if the link is still good (not used, not revoked, not expired)."""
    row = conn.execute("SELECT role, point_id, display_name, expires, client_terms FROM panel_invite WHERE token_hash = ? AND used_at IS NULL "
                       "AND revoked_at IS NULL AND expires > ?", (_token_hash(str(token or "")), now)).fetchone()
    return None if row is None else {"role": row[0], "point_id": row[1], "display_name": row[2], "expires": row[3], "client_terms": row[4]}


def accept_invite(conn, token: str, login: str, password: str, now: str) -> str:
    """The person sets their own login and password. The link works once: the claim is atomic. One transaction: the claim, the user and
    a client's conditions (client_terms); a failure of any step rolls everything back (the link stays good, no user, no conditions)."""
    invite = find_invite(conn, token, now)
    if invite is None:
        raise UserError("Ссылка недействительна: она уже использована, отозвана или истекла.")
    _check_role(conn, invite["role"], invite["point_id"])            # the point was deleted after the invitation: refused, the link stays
    try:
        terms = client_terms.from_invite(invite["client_terms"]) if invite["role"] == "client" else None
    except client_terms.TermsError as err:
        raise UserError(str(err)) from err
    login = normalize(login)
    if not LOGIN.match(login) or login in RESERVED or exists(conn, login):
        raise UserError("Логин занят или не подходит: 2–32 символа, латинские буквы, цифры, точка, дефис, подчёркивание.")
    _check_password(password)
    claim = conn.execute("UPDATE panel_invite SET used_at = ?, used_by = ? WHERE token_hash = ? AND used_at IS NULL AND revoked_at IS NULL "
                         "AND expires > ?", (now, login, _token_hash(token), now))
    if claim.rowcount != 1:
        conn.rollback()
        raise UserError("Ссылка недействительна: она уже использована, отозвана или истекла.")
    try:
        add_user(conn, login, password, now, role=invite["role"], point_id=invite["point_id"], display_name=invite["display_name"],
                 author=login, commit=False)
        if terms is not None:
            try:
                client_terms.save_rows(conn, login, terms["rows"], now, login, commit=False, whole=True)
            except client_terms.TermsError as err:   # our % changed since: a client's % would leave −50…300
                raise client_terms.TermsError("Условия приглашения устарели: попросите новую ссылку.") from err
        conn.commit()
        return login
    except sqlite3.IntegrityError:                        # the same login taken by another invitation a moment ago
        conn.rollback()
        raise UserError("Логин занят: выберите другой.")
    except client_terms.TermsError as err:
        conn.rollback()
        raise UserError(str(err)) from err
    except Exception:
        conn.rollback()
        raise


def list_invites(conn, now: str) -> List[dict]:
    rows = conn.execute("SELECT rowid, role, point_id, display_name, created, expires, created_by, client_terms FROM panel_invite "
                        "WHERE used_at IS NULL AND revoked_at IS NULL AND expires > ? ORDER BY created DESC", (now,))
    out = []
    for r in rows:
        item = dict(zip(("id", "role", "point_id", "display_name", "created", "expires", "created_by"), r[:7]))
        try:
            item["terms"] = client_terms.from_invite(r[7]) if r[1] == "client" else None
        except client_terms.TermsError:
            item["terms"] = None
        out.append(item)
    return out


def revoke_invite(conn, invite_id: int, now: str, author: str) -> None:
    if conn.execute("UPDATE panel_invite SET revoked_at = ? WHERE rowid = ? AND used_at IS NULL AND revoked_at IS NULL",
                    (now, int(invite_id))).rowcount != 1:
        raise UserError("Приглашение уже использовано или отозвано.")
    store.write_audit(conn, now, author, "invite_revoked", {"id": int(invite_id)})
