"""The journal «История» is written by two clocks and shown in Moscow time by panel.audit_dt: an action written in Moscow time must be in
panel.MOSCOW_AUDIT, else it is shown 3 hours late (the owner 2026-11-08: «inquiry», «staff_note», «point_action» out of order). Every
action written to the journal is found here by reading the code: each one must be on one side, Moscow (MOSCOW_AUDIT) or UTC (below), and
a write whose clock is in sight (now_iso / local_iso / moscow_iso …, in its function) must be on its clock's side."""
from __future__ import annotations

import ast
from pathlib import Path

from tires_sync.panel import MOSCOW_AUDIT

SRC = Path(__file__).resolve().parents[1] / "src" / "tires_sync"

UTC_AUDIT = frozenset({
    "account_addresses", "account_chats_send", "account_created", "account_modes", "account_own_photos", "account_pricing", "alert",
    "as_point_start", "as_point_stop", "avito_price_config", "avito_price_queued", "avito_stock_config", "avito_upload_refused",
    "avito_upload_started", "brand_mapping_confirm", "call_listened", "client_merged", "client_pair_skipped", "client_pct_changed",
    "client_pct_followed", "client_saved", "client_split", "client_tagged", "client_unmerged", "connection_config", "daily_summary",
    "description_template_set", "economics_params", "feed_now_refused", "feed_now_started", "feed_update_failed", "feed_updated_manually",
    "invite_created", "invite_revoked", "login", "login_failed", "login_reset_created", "login_reset_revoked", "login_reset_used",
    "main_display_name", "mapping_confirm", "market_switch", "market_token", "match_not_pair", "match_not_pair_undo", "match_pair_forget",
    "match_pair_unforget", "message_template_changed", "notice", "old_model_name", "oth_manual",
    "own_price_confirmed", "owner_login", "point_rule", "secret_cleared", "secret_set", "secrets_cleared", "service_pause", "settings_cancel",
    "settings_confirm", "settings_init", "settings_rollback", "share_created", "share_labels", "share_price_toggled", "share_revoked",
    "sheet_keep_from", "shop_responsible", "shop_tochki_stale", "supplier_created", "term_deleted", "term_saved", "term_updated",
    "terms_imported", "ui_new", "user_added", "user_off", "user_on", "user_password", "user_removed", "user_role", "user_sheet_label",
    "vk_answer_failed", "vk_catchup",
    "sale_added", "sale_reversed",            # by both clocks, shown as UTC (panel.audit_dt): a sale typed by hand
    "ids_bound", "account_live",              # by both clocks, told by the author in panel.audit_dt
})
PREFIXES = {"callback_": ("done", "cancel", "extend")}     # tasks.close_callback writes "callback_" + how
UTC_CLOCKS, MOSCOW_CLOCKS = {"now_iso", "utcnow", "_now"}, {"local_iso", "moscow_iso", "moscow_now", "local_now"}


def _name(call):
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None


def _writes(tree):
    """(at, action) expressions of every write to the journal: store.write_audit and INSERT INTO audit."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _name(node) == "write_audit" and len(node.args) >= 4:
            yield node, node.args[1], node.args[3]
        elif (_name(node) == "execute" and len(node.args) > 1 and isinstance(node.args[0], ast.Constant)
              and "INSERT INTO audit" in str(node.args[0].value) and isinstance(node.args[1], ast.Tuple)):
            yield node, node.args[1].elts[0], node.args[1].elts[2]


def _parents(tree):
    up = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            up[child] = node
    return up


def _function(node, up):
    while node in up:
        node = up[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


def _module(path):
    return path.parent.name if path.stem == "__init__" else path.stem


def _target(call, module, imported):
    """(module, function) a call goes to: a bare name of this module or imported from another, or module.function."""
    f = call.func
    if isinstance(f, ast.Name):
        found = imported.get(f.id)
        return (found[1], found[2]) if found and found[0] == "function" else (module, f.id)
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        found = imported.get(f.value.id)
        if found and found[0] == "module":
            return (found[1], f.attr)
    return None


def _consts(tree):
    """The module's string constants (name -> {value}) and its tables of tuples (name -> [{strings at index 0}, {… at 1}, …])."""
    flat, tables = {}, {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name, value = node.targets[0].id, node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                flat[name] = {value.value}
            elif isinstance(value, ast.Dict):
                rows = [v.elts for v in value.values if isinstance(v, ast.Tuple)]
                if rows:
                    tables[name] = [{r[i].value for r in rows if i < len(r) and isinstance(r[i], ast.Constant)} for i in range(max(map(len, rows)))]
    return flat, tables


class Code:
    """Every function, call and constant of the package, to tell the action names a write of the journal can carry."""

    def __init__(self):
        self.trees = {p: ast.parse(p.read_text(encoding="utf-8")) for p in sorted(SRC.rglob("*.py"))}
        self.ups = {p: _parents(t) for p, t in self.trees.items()}
        self.consts = {p: _consts(t) for p, t in self.trees.items()}
        self.calls = {}                                       # (module, function) -> [(path, enclosing function, call)]
        for path, tree in self.trees.items():
            imported = {}                                     # a local name -> ("module", m) or ("function", m, name)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level:
                    for alias in node.names:
                        imported[alias.asname or alias.name] = (("function", node.module.split(".")[-1], alias.name) if node.module
                                                                else ("module", alias.name))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    target = _target(node, _module(path), imported)
                    if target:
                        self.calls.setdefault(target, []).append((path, _function(node, self.ups[path]), node))

    def actions(self, expr, path, fn, depth=0):
        """The action names the expression can be: literals, module constants, an item of a module table, and through the callers of a
        helper whose parameter it is. None: cannot be told (the test fails then, so a write is never skipped silently)."""
        flat, tables = self.consts[path]
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return {expr.value}
        if isinstance(expr, ast.IfExp):
            a, b = self.actions(expr.body, path, fn, depth), self.actions(expr.orelse, path, fn, depth)
            return None if a is None or b is None else a | b
        if isinstance(expr, ast.BinOp) and isinstance(expr.left, ast.Constant) and expr.left.value in PREFIXES:
            return {expr.left.value + how for how in PREFIXES[expr.left.value]}
        if not isinstance(expr, ast.Name):
            return None
        if expr.id in flat:
            return set(flat[expr.id])
        if fn is None or depth > 4:
            return None
        for node in ast.walk(fn):                                         # key_of, text, action, why = _NOT_SENT[kind]
            if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Tuple) and isinstance(node.value, ast.Subscript)
                    and isinstance(node.value.value, ast.Name) and node.value.value.id in tables):
                names = [t.id if isinstance(t, ast.Name) else "" for t in node.targets[0].elts]
                if expr.id in names:
                    return set(tables[node.value.value.id][names.index(expr.id)])
        params = [a.arg for a in fn.args.args]
        if expr.id not in params:
            return None
        found = set()
        for call_path, caller, call in self.calls.get((_module(path), fn.name), ()):
            i = params.index(expr.id)
            arg = next((k.value for k in call.keywords if k.arg == expr.id), call.args[i] if i < len(call.args) else None)
            if arg is None and i >= len(params) - len(fn.args.defaults):         # not passed: the parameter's default
                arg, call_path, caller = fn.args.defaults[i - len(params) + len(fn.args.defaults)], path, None
            got = None if arg is None else self.actions(arg, call_path, caller, depth + 1)
            if got is None:
                return None
            found |= got
        return found or None


def _clock(at):
    """«utc» or «moscow» when the clock is called right in the write (now_iso(), local_iso(), moscow_now().isoformat() …), else ''."""
    seen = at.value if isinstance(at, ast.Subscript) else at
    if isinstance(seen, ast.Call) and _name(seen) == "isoformat" and isinstance(seen.func, ast.Attribute):
        seen = seen.func.value
    name = _name(seen) if isinstance(seen, ast.Call) else None
    return "utc" if name in UTC_CLOCKS else "moscow" if name in MOSCOW_CLOCKS else ""


def _all_writes():
    """[(where, clock or '', actions or None)] of every write to the journal (store.write_audit itself is the writer, not a write)."""
    code, out = Code(), []
    for path, tree in code.trees.items():
        for node, at, action in _writes(tree):
            fn = _function(node, code.ups[path])
            if path.name == "store.py" and fn is not None and fn.name == "write_audit":
                continue
            out.append(("%s:%d" % (path.relative_to(SRC), node.lineno), _clock(at), code.actions(action, path, fn)))
    return out


def test_every_action_of_the_journal_has_its_clock():
    writes = _all_writes()
    assert len(writes) > 100
    unknown = [where for where, _clock, actions in writes if actions is None]
    assert not unknown, "the action of these writes cannot be told: name it in a constant or teach this test: %s" % unknown
    named = set().union(*(a for _w, _c, a in writes))
    missing = sorted(named - MOSCOW_AUDIT - UTC_AUDIT)
    assert not missing, "new actions of the journal: put each in panel.MOSCOW_AUDIT (Moscow time) or UTC_AUDIT here: %s" % missing


def test_a_write_with_its_clock_in_sight_is_on_its_side():
    wrong = []
    for where, clock, actions in _all_writes():
        for action in actions or ():
            if clock == "moscow" and action not in MOSCOW_AUDIT:
                wrong.append((where, action, "Moscow, not in MOSCOW_AUDIT"))
            if clock == "utc" and action in MOSCOW_AUDIT:
                wrong.append((where, action, "UTC, in MOSCOW_AUDIT"))
    assert not wrong


def test_no_action_is_on_both_sides():
    assert not MOSCOW_AUDIT & UTC_AUDIT


def test_every_listed_action_is_still_written():
    named = set().union(*(a for _w, _c, a in _all_writes() if a))
    assert not (MOSCOW_AUDIT | UTC_AUDIT) - named
