"""For scripts/test-affected.sh: the test files to run for the changed non-Python files (argv[1]: the changed paths, one per line).
Run from the root of a copy.

- feed_check_rules.json: every tests/test_feed_check*.py and the tests going through the check before publishing (feed_check, gate);
- src/tires_sync/data/avito-templates/…: the tests naming it and the tests reading the template of the cycle (TEMPLATE_PATH, gate, feed_check);
- a template of the panel: the tests that ask for an address whose route draws it (the function with render_template of the template,
  or of a template that includes / extends it, and its @app.route; a helper without a route: the routes of its file that call it),
  the tests that name the template, the tests scanning every template (glob("*.html")); error.html / message.html (drawn by the
  error handlers, no route): the tests expecting 403 / 404;
- a file under tests/fixtures/<dir>/: the tests naming the file or the folder;
- anything else: the tests that name the file;
- a Python file of src/: the tests hashing sources (inspect.getsource) that name its module (testmon cannot see them).
Limits: a route decorator split over lines is not read; a helper is followed one call deep, in its own file.
"""
import re
import sys
from pathlib import Path

TEMPLATES = Path("src/tires_sync/panel/templates")
ROUTE = re.compile(r"""@app\.route\(\s*["']([^"']+)["']""")
DEF = re.compile(r"^(\s*)def (\w+)\(")

tests = sorted(Path("tests").glob("test_*.py"))
texts = {t: t.read_text(encoding="utf-8", errors="ignore") for t in tests}


def naming(fragment: str) -> set:
    return {str(t) for t, text in texts.items() if fragment in text}


def asking(path: str) -> set:
    """The tests that ask for the address: the exact one, or (with <parameters>) every address under its fixed head."""
    head = path.split("<")[0]
    exact = re.compile(r"""["']%s(?=["'?#])""" % re.escape(head)) if head == path else re.compile(r"""["']%s""" % re.escape(head))
    return {str(t) for t, text in texts.items() if exact.search(text)}


def family(name: str) -> set:
    """The template and every template that includes / extends / imports it, transitively."""
    names, seen = {name}, set()
    while names - seen:
        current = (names - seen).pop()
        seen.add(current)
        mark = re.compile(r"\{%-?\s*(include|extends|import|from)\s+[\"']" + re.escape(current) + r"[\"']")
        names |= {other.name for other in TEMPLATES.glob("*.html") if mark.search(other.read_text(encoding="utf-8"))}
    return names


def functions(lines):
    """[(name, first line, last line, routes)] of the functions of a file, nested ones included."""
    found = []
    for i, line in enumerate(lines):
        m = DEF.match(line)
        if not m:
            continue
        indent, end = len(m.group(1)), len(lines)
        for j in range(i + 1, len(lines)):
            if lines[j].strip() and len(lines[j]) - len(lines[j].lstrip()) <= indent and not lines[j].lstrip().startswith((")", "#")):
                end = j
                break
        routes, k = [], i - 1
        while k >= 0 and lines[k].strip().startswith("@"):
            routes += ROUTE.findall(lines[k])
            k -= 1
        found.append((m.group(2), i, end, routes))
    return found


def routes_drawing(names: set) -> set:
    paths = set()
    quoted = re.compile(r"[\"'](%s)[\"']" % "|".join(re.escape(n) for n in names))
    for source in Path("src").rglob("*.py"):
        lines = source.read_text(encoding="utf-8").splitlines()
        defs = functions(lines)
        for n, line in enumerate(lines):
            if not quoted.search(line):
                continue
            inner = [d for d in defs if d[1] <= n < d[2]]
            if not inner:
                continue
            name, _first, _last, routes = min(inner, key=lambda d: d[2] - d[1])        # the innermost function
            if routes:
                paths |= set(routes)
                continue
            call = re.compile(r"\b%s\(" % re.escape(name))                             # a helper: the routes of the file calling it
            for other, first, last, other_routes in defs:
                if other_routes and any(call.search(lines[x]) for x in range(first + 1, last)):
                    paths |= set(other_routes)
    return paths


def matching(pattern: str) -> set:
    found = re.compile(pattern)
    return {str(t) for t, text in texts.items() if found.search(text)}


THROUGH_CHECK = r"feed_check|\bgate\(|TEMPLATE_PATH|run_for_accounts|run_cycle|\bcycle\."      # the cycle checks every feed before publishing

picked = set()
for changed in [c for c in sys.argv[1].splitlines() if c.startswith("src/") and c.endswith(".py")]:
    stem = Path(changed).stem                       # a guard hashing the source (inspect.getsource) executes nothing testmon could see
    picked |= {str(t) for t, text in texts.items() if "getsource" in text and re.search(r"\b%s\b" % re.escape(stem), text)}
for changed in [c for c in sys.argv[1].splitlines() if c and not c.endswith(".py")]:
    name = Path(changed).name
    if name == "feed_check_rules.json":
        picked |= {str(t) for t in tests if t.name.startswith("test_feed_check")} | matching(THROUGH_CHECK)
        continue
    picked |= naming(name)
    if changed.startswith("src/tires_sync/data/avito-templates/"):
        picked |= matching(THROUGH_CHECK)
    if changed.startswith("tests/fixtures/"):
        picked |= naming(Path(changed).parent.name)
    if changed.startswith(str(TEMPLATES) + "/") and name.endswith(".html"):
        picked |= matching(r"""\.glob\(\s*["']\*\.html["']""")                        # the tests scanning every template
        if name in ("error.html", "message.html"):                                     # drawn by the error handlers only
            picked |= matching(r"==\s*40[34]\b")
        for path in routes_drawing(family(name)):
            picked |= asking(path)
print("\n".join(sorted(picked)))
