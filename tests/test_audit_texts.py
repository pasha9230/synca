"""The journal of actions speaks Russian for every action the code writes (run «match2», lane small-tails)."""
from __future__ import annotations

import ast
import re
from functools import lru_cache
from pathlib import Path

import pytest

from tires_sync.panel import texts

SRC = Path(__file__).resolve().parents[1] / "src" / "tires_sync"

# The places whose action is computed (not a word in the code): what each of them can write. A new such place makes the test fail
# until it is named here, a place gone from the code too.
COMPUTED = {
    "inquiry.close_callback": {"callback_done", "callback_cancel"},
}


def _consts(tree) -> dict:
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node.value.value
    return found


def _words(node, consts):
    """The action names an expression gives: a word, a constant of the module, either side of «a if x else b»; None when computed."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name) and node.id in consts:
        return {consts[node.id]}
    if isinstance(node, ast.IfExp):
        one, two = _words(node.body, consts), _words(node.orelse, consts)
        return one | two if one is not None and two is not None else None
    return None


COMPUTED_HERE = object()           # a place that writes the journal, its action not read from the code: it must be named in COMPUTED
_INSERT_AUDIT = re.compile(r"insert\s+(?:or\s+\w+\s+)?into\s+audit\b", re.I)


def _functions(tree):
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _action_param(fn):
    """(index of the parameter `action`, its default) of a function, None when it has none."""
    names = [a.arg for a in fn.args.posonlyargs + fn.args.args]
    if "action" in names:
        defaults = dict(zip(names[len(names) - len(fn.args.defaults):], fn.args.defaults))
        return names.index("action"), defaults.get("action")
    kw = [a.arg for a in fn.args.kwonlyargs]
    if "action" in kw:
        return None, fn.args.kw_defaults[kw.index("action")]
    return None


def _imports(module, tree, modules) -> dict:
    """{a name in this module: (our module, None) for a module, (our module, function) for a function} from its imports of our code."""
    found, package = {}, module.split(".")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[:len(package) - (node.level - 1)] if node.level > 1 else package
                source = ".".join(base + (node.module.split(".") if node.module else []))
            elif (node.module or "").startswith("tires_sync"):
                source = ".".join(node.module.split(".")[1:])
            else:
                continue
            for alias in node.names:
                sub = (source + "." if source else "") + alias.name
                found[alias.asname or alias.name] = (sub, None) if sub in modules else (source, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "tires_sync" and len(parts) > 1 and alias.asname:
                    found[alias.asname] = (".".join(parts[1:]), None)
    return found


def _target(node, module, imports):
    """(module, function) a call names, or (None, its name) when it cannot be told (a method, an unknown module)."""
    func = node.func
    if isinstance(func, ast.Name):
        source, name = imports.get(func.id, (module, func.id))
        return (source, name) if name else (None, func.id)
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name) and func.value.id in imports and imports[func.value.id][1] is None:
            return imports[func.value.id][0], func.attr
        return None, func.attr
    return None, None


def _insert_audit(node):
    """For a call that writes the journal in SQL: the expression of its action, COMPUTED_HERE when it cannot be read; None otherwise."""
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr in ("execute", "executemany") and node.args):
        return None
    sql = node.args[0]
    if not _INSERT_AUDIT.search(ast.unparse(sql) if not isinstance(sql, ast.Constant) else str(sql.value)):
        return None
    values = node.args[1] if len(node.args) > 1 else None
    if func.attr == "execute" and isinstance(sql, ast.Constant) and isinstance(values, (ast.Tuple, ast.List)) and len(values.elts) > 2:
        return values.elts[2]
    return COMPUTED_HERE


def _call_action(node, key, writers):
    index, default = writers[key]
    arg = next((k.value for k in node.keywords if k.arg == "action"), None)
    if arg is None and index is not None and len(node.args) > index:
        arg = node.args[index]
    if arg is None and (any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords)):
        return COMPUTED_HERE
    return arg if arg is not None else default


def _own_nodes(fn):
    """The nodes of a function without those of the functions defined in it (a call belongs to the innermost function)."""
    nested = (ast.FunctionDef, ast.AsyncFunctionDef)
    todo = [n for n in fn.body if not isinstance(n, nested)]
    while todo:
        node = todo.pop()
        yield node
        todo.extend(c for c in ast.iter_child_nodes(node) if not isinstance(c, nested))


@lru_cache(maxsize=None)
def _scan(root: Path = SRC):
    """(the action names the code writes, {function: how many places in it write a computed name}).
    The writers: the functions with «audit» in their name that take `action`, and every function that passes its own `action` on to one."""
    trees = {".".join(p.relative_to(root).with_suffix("").parts): ast.parse(p.read_text(encoding="utf-8")) for p in root.rglob("*.py")}
    imports = {m: _imports(m, t, set(trees)) for m, t in trees.items()}
    writers = {(m, fn.name): _action_param(fn) for m, t in trees.items() for fn in _functions(t) if "audit" in fn.name and _action_param(fn)}
    names = lambda: {f for _, f in writers}                                     # noqa: E731
    changed = True
    while changed:                                                             # a wrapper of a writer is a writer
        changed = False
        for module, tree in trees.items():
            for fn in _functions(tree):
                if (module, fn.name) in writers or not _action_param(fn):
                    continue
                for node in _own_nodes(fn):
                    if isinstance(node, ast.Call):
                        key = _target(node, module, imports[module])
                        arg = _insert_audit(node)
                        if arg is None and key in writers:
                            arg = _call_action(node, key, writers)
                        if isinstance(arg, ast.Name) and arg.id == "action":
                            writers[module, fn.name] = _action_param(fn)
                            changed = True
                            break
    actions, computed = set(), {}
    for module, tree in trees.items():
        consts = _consts(tree)
        for fn in _functions(tree) + [tree]:
            where = "%s.%s" % (module, getattr(fn, "name", "<module>"))
            for node in _own_nodes(fn):
                if not isinstance(node, ast.Call):
                    continue
                arg = _insert_audit(node)
                if arg is None:
                    key = _target(node, module, imports[module])
                    if key in writers:
                        arg = _call_action(node, key, writers)
                    elif key[0] is None and key[1] in names():                # a writer's name on something unknown: say so
                        arg = COMPUTED_HERE
                    else:
                        continue
                if isinstance(arg, ast.Name) and arg.id == "action" and (module, getattr(fn, "name", None)) in writers:
                    continue                                                   # a writer passing its own `action` on: its callers are read
                words = None if arg is COMPUTED_HERE or arg is None else _words(arg, consts)
                if words is None:
                    computed[where] = computed.get(where, 0) + 1
                else:
                    actions |= words
    return frozenset(actions), computed


def test_every_action_written_by_the_code_has_a_text():
    actions, computed = _scan()
    actions = set(actions)
    assert set(computed) == set(COMPUTED), "a place writes a computed action: name what it writes in COMPUTED"
    for words in COMPUTED.values():
        actions |= words
    assert len(actions) > 80                                               # the scan sees the code (on 2026-10-25: 93 actions)
    missing = sorted(a for a in actions if texts.audit_text({"action": a, "detail": {}}) == a)
    assert missing == []


def test_the_scan_sees_the_places_of_every_kind():
    actions, _ = _scan()
    assert {"login", "settings_confirm",                                   # store.write_audit, settings_store._audit
            "order_item_purchase", "orders_bulk_move", "orders_bulk_undo",  # INSERT INTO audit with a word, with a constant
            "avito_stock_push", "avito_stock_restore",                     # a default of `action`, a positional one
            "user_on", "user_off", "alert", "notice", "orders_cleanup_new"} <= actions


def test_the_scan_follows_wrappers_and_imports_and_says_what_it_cannot_read(tmp_path):
    """A wrapper of a writer, a writer under another name, SQL it cannot read, a method: found, or named as computed, never skipped."""
    (tmp_path / "store.py").write_text("def write_audit(conn, at, author, action, detail):\n    pass\n", encoding="utf-8")
    (tmp_path / "panel").mkdir()
    (tmp_path / "panel" / "routes_x.py").write_text(
        "from .. import store\n"
        "from ..store import write_audit as w\n"
        "def _log(conn, action):\n"
        "    store.write_audit(conn, 'now', 'o', action, {})\n"
        "def page(conn, table, self):\n"
        "    _log(conn, 'through_a_wrapper')\n"
        "    w(conn, 'now', 'o', 'through_an_alias', {})\n"
        "def odd(conn, table, self, rows):\n"
        "    conn.execute(f'insert into audit (at, author, action, detail) VALUES (?,?,?,?)', rows)\n"
        "    self._log(conn, 'x')\n", encoding="utf-8")
    actions, computed = _scan.__wrapped__(tmp_path)
    assert actions == {"through_a_wrapper", "through_an_alias"}
    assert computed == {"panel.routes_x.odd": 2}


@pytest.mark.parametrize("action", sorted(_scan()[0] | set().union(*COMPUTED.values())))
def test_a_record_without_its_fields_does_not_break_the_journal(action):
    for detail in ({}, {"account_id": None, "ids": None, "lines": None, "count": "x"}, [1], "text"):
        assert isinstance(texts.audit_text({"action": action, "detail": detail}), str)
    if action in texts._MORE_TEXTS:                                        # a list where a word is looked up is no key: «?», no TypeError
        odd = {k: [1] for k in ("season", "size_class", "mode", "scheme", "role", "from", "to")}
        assert isinstance(texts.audit_text({"action": action, "detail": odd}), str)


@pytest.mark.parametrize("action, detail, text", [
    ("login", {"ip": "10.0.0.5"}, "Вход в панель с адреса 10.0.0.5"),
    ("user_added", {"login": "ivan", "role": "chats"}, "Добавлен пользователь ivan (Менеджер чатов)"),
    ("user_role", {"login": "ivan", "from": "viewer", "to": "partner", "point_id": None}, "Роль пользователя ivan: Наблюдатель → Партнёр"),
    ("user_password", {"login": "ivan", "self": True}, "Пользователь ivan сменил свой пароль"),
    ("user_password", {"login": "ivan"}, "Пароль пользователя ivan изменён"),
    ("mirror_adopt", {"account_id": 2, "ids": 120, "images": 98}, "Аккаунт №2: перенесены прежние Id (120) и фото (98)"),
    ("ids_rebound", {"account_id": 2, "count": 3, "shared": ["A1"]}, "Аккаунт №2: Id перепривязаны (3, из них общих 1)"),
    ("mirror_reissue", {"account_id": 2, "ids": {"1": "1_r1", "2": "2_r1"}}, "Аккаунт №2: новые Id для удалённых объявлений (2)"),
    ("avito_stock_push", {"account_id": 1, "sent": 10, "failed": 1, "unknown": 0, "not_sent": 2},
     "Остатки отправлены на Авито, аккаунт №1: отправлено 10, ошибок 1, неизвестно 0, не отправлено 2"),
    ("order_item_purchase", {"order": 7, "item": 3, "from": 4500.0, "to": 4650.5, "source": "manual"},
     "Заказ №7: закупочная цена позиции 4500 ₽ → 4650.5 ₽"),
    ("order_item_purchase", {"order": 7, "item": 3, "from": None, "to": 4000}, "Заказ №7: закупочная цена позиции — → 4000 ₽"),
    ("client_merged", {"into": 5, "from": 9}, "Клиент №9 объединён с клиентом №5"),
    ("client_split", {"from": 5, "to": 11, "order": 40}, "Из клиента №5 выделен клиент №11 (заказ №40)"),
    ("order_chat_link", {"order": 40, "chat": 12, "how": "manual"}, "Чат Ч-12 привязан к заказу №40"),
    ("share_created", {"link": 3, "items": 4, "price": False}, "Создана подборка №3: позиций 4, без цен"),
    ("callback", {"task": 2, "client": 5, "order": None, "chat": None, "due": "2026-10-26 10:00", "to": "ivan"},
     "Задача перезвонить №2: клиент №5, до 2026-10-26 10:00, ответственный ivan"),
    ("orders_bulk_move", {"stage": 4, "moved": 6, "skipped": 1, "left": 0, "cards": [1, 2]},
     "Массовый перенос заказов: перенесено 6, пропущено 1, осталось 0"),
    ("orders_bulk_move", {"stage": 4, "moved": 2, "skipped": 0, "left": 4, "stopped": "database is locked"},
     "Массовый перенос заказов: перенесено 2, пропущено 0, осталось 4, прервано (база занята)"),
    ("mirror_adopt", {"account_id": 2, "ids": 1200000, "images": 0}, "Аккаунт №2: перенесены прежние Id (1200000) и фото (0)"),
    ("feed_update_failed", {"lines": ["Фид не пересобран: нет выгрузки", "второе"]}, "Ручное обновление фидов не удалось: Фид не пересобран: нет выгрузки"),
    ("term_updated", {"brand_key": "IKON", "season": "winter", "size_class": "small", "valid_from": "2026-11-01", "before": {}},
     "Изменены условия поставщика: IKON · зима · до R17, с 2026-11-01"),
])
def test_journal_texts(action, detail, text):
    assert texts.audit_text({"action": action, "detail": detail}) == text


def test_the_journal_shows_no_typed_login_no_client_text_no_address():
    failed = texts.audit_text({"action": "login_failed", "detail": {"login": "Secret-Pass-1", "ip": "10.0.0.5"}})
    assert failed == "Неудачная попытка входа с адреса 10.0.0.5"                 # a password typed in the login field stays in the base only
    vk = texts.audit_text({"action": "vk_answer_failed", "detail": {"point": "Центр", "peer": "77", "reason": "нет токена",
                                                                    "error": "HTTP 500 token=abc", "answer": "Здравствуйте, Иван"}})
    assert vk == "ВКонтакте: ответ клиенту не отправлен (точка «Центр»): нет токена"
    hook = texts.audit_text({"action": "avito_webhook_subscribe", "detail": {"account_id": 1, "url": "https://x/hook/***", "replaced": 1}})
    assert "hook" not in hook and hook == "Подписка на уведомления Авито включена, аккаунт №1 (заменено прежних: 1)"
