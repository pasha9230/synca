#!/usr/bin/env python3
"""Снимок кода для публичного репозитория и его проверка (этап 5 docs/plans/2026-10-24-open-repo.md).

Сборка: из коммита (по умолчанию origin/master) в пустую папку попадают только файлы публичного набора
(«закрыто всё, кроме разрешённого»: KEEP_DIRS и KEEP_FILES). Всё остальное (CLAUDE.md, .claude/, документы,
снимок сервера, .DS_Store) в папку не попадает и печатается списком: это состав tires-private.

    python3 scripts/public-snapshot.py --out ../synca-snapshot [--commit origin/master] [--removed-list FILE]
    python3 scripts/public-snapshot.py --check ../synca-snapshot [--deny FILE] [--strict]
    python3 scripts/public-snapshot.py --out DIR --check          # собрать и сразу проверить

Проверка печатает только «файл:строка: вид находки», значения не печатает никогда. Код выхода: 0 чисто,
1 есть находки, 2 ошибка запуска. Ищет: файлы вне публичного набора и запрещённые файлы (.env, ключи, базы,
xlsx, .gs), локальные пути, почту и имя компьютера автора, адреса вне списка известных, значения из
приватного файла --deny (телефоны, Id, домены: файл лежит в tires-private, в публичном коде их нет)
и секреты (gitleaks, если установлен; иначе встроенные шаблоны).
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ---- публичный набор (всё остальное приватное) ----
KEEP_DIRS = ("src/", "tests/", "agent/", "scripts/", "deploy/", ".github/")
KEEP_FILES = {
    "pyproject.toml", "README.md", "SECURITY.md", "CONTRIBUTING.md", "LICENSE",
    ".gitignore", ".gitleaks.toml",
    "docs/feed-rules.md", "docs/feed-check-rules.md",
}
# закрыто и внутри разрешённых папок: уходит в tires-private (схемы XML поставщика 4tochki — чужие, решение владельца 2026-10-25)
PRIVATE_IN_KEPT = ("tests/fixtures/tochki/",)
DROP_NAMES = {".DS_Store"}                       # не берётся нигде, даже в разрешённых папках
NOT_TO_PRIVATE = ("tires-server/",)              # убирается и в tires-private не переносится (остаётся в архиве)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DENY = ROOT / "private" / "public-snapshot-deny.txt"


def is_public(path: str) -> bool:
    if Path(path).name in DROP_NAMES or path.startswith(PRIVATE_IN_KEPT):
        return False
    return path in KEEP_FILES or path.startswith(KEEP_DIRS)


def goes_to_private(path: str) -> bool:
    return Path(path).name not in DROP_NAMES and not path.startswith(NOT_TO_PRIVATE)


def group_of(path: str) -> str:
    parts = path.split("/")
    if parts[0] == "docs" and len(parts) > 2:
        return "docs/" + parts[1] + "/"
    if len(parts) > 1:
        return parts[0] + "/"
    return path


# ---- сборка ----
def git(repo: Path, *args: str, binary: bool = False):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if r.returncode != 0:
        raise SystemExit(f"[snapshot] git {' '.join(args[:2])}: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout if binary else r.stdout.decode("utf-8")


def build(repo: Path, commit: str, out: Path, removed_list: Path | None) -> int:
    sha = git(repo, "rev-parse", "--verify", commit + "^{commit}").strip()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        print(f"[snapshot] папка {out} не пустая: нужна новая или пустая", file=sys.stderr)
        return 2
    files = [p for p in git(repo, "ls-tree", "-r", "-z", "--name-only", sha).split("\0") if p]
    keep = [p for p in files if is_public(p)]
    removed = [p for p in files if not is_public(p)]
    out.mkdir(parents=True, exist_ok=True)
    if keep:
        # в архив идут только публичные пути: приватные файлы на диск снимка не попадают совсем
        arch = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", sha, "--",
                               *(":(literal)" + p for p in keep)], capture_output=True)
        if arch.returncode != 0:
            print(f"[snapshot] git archive: {arch.stderr.decode(errors='replace').strip()}", file=sys.stderr)
            return 2
        tar = subprocess.run(["tar", "-x", "-C", str(out)], input=arch.stdout, capture_output=True)
        if tar.returncode != 0:
            print(f"[snapshot] tar: {tar.stderr.decode(errors='replace').strip()}", file=sys.stderr)
            return 2
        # файлы снимка байт в байт как в коммите: атрибуты (export-subst, export-ignore) ничего не подставили и не убрали
        want = {}
        for entry in git(repo, "ls-tree", "-r", "-z", sha).split("\0"):
            if entry:
                meta, path = entry.split("\t", 1)
                want[path] = meta.split()[2]
        paths = [str((out / p).resolve()) for p in keep]
        hashed = subprocess.run(["git", "-C", str(repo), "hash-object", "--no-filters", "--stdin-paths"],
                                input="\n".join(paths) + "\n", capture_output=True, text=True)
        got = hashed.stdout.split()
        if hashed.returncode != 0 or len(got) != len(keep):
            print("[snapshot] не удалось сверить файлы снимка с коммитом", file=sys.stderr)
            return 2
        bad = [p for p, h in zip(keep, got) if want.get(p) != h]
        extra = sorted(str(f.relative_to(out)) for f in out.rglob("*") if f.is_file() and str(f.relative_to(out)) not in want)
        if bad or extra:
            for p in bad + extra:
                print(f"[snapshot] файл не совпадает с коммитом: {p}", file=sys.stderr)
            return 2
    to_private = [p for p in removed if goes_to_private(p)]
    if removed_list:
        removed_list.write_text("".join(p + "\n" for p in to_private), encoding="utf-8")
    print(f"[snapshot] коммит {sha[:12]}: всего {len(files)}, в снимке {len(keep)}, убрано {len(removed)} "
          f"(в tires-private {len(to_private)}, не переносится {len(removed) - len(to_private)})")
    groups: dict[str, int] = {}
    for p in removed:
        g = group_of(p) + ("" if goes_to_private(p) else "  [не переносится]")
        groups[g] = groups.get(g, 0) + 1
    for g in sorted(groups):
        print(f"  убрано {groups[g]:4d}  {g}")
    if removed_list:
        print(f"[snapshot] список для tires-private: {removed_list}")
    return 0


# ---- проверка ----
# Шаблоны собраны из частей, чтобы скрипт и его тест не находили сами себя в снимке.
FORBIDDEN_NAME = re.compile(
    r"(^\.env($|\.)|\.(key|pem|p12|pfx|db|db-wal|db-shm|sqlite3?|xlsx?|gs)$|^id_(rsa|dsa|ecdsa|ed25519)|^\.DS_Store$)",
    re.I)
LOCAL_PATH = re.compile("/Us" "ers/[^/\\s'\"`]+|(?i:[a-z]:\\\\us" "ers\\\\)")   # /Users с большой: /users/… это адрес панели
AUTHOR = [
    (re.compile(r"[\w.+-]+@[\w-]+\.local\b", re.I), "почта автора (имя компьютера .local)"),
    (re.compile(r"[\w.+-]+@gm" r"ail\.com\b", re.I), "личная почта (gmail)"),
    (re.compile(r"\bMac" r"Book-(Air|Pro)[\w-]*", re.I), "имя компьютера"),
]
SECRETS = [
    (re.compile("-----BEGIN" r" ([A-Z0-9 ]+ )?PRIVATE KEY-----"), "приватный ключ"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "ключ AWS"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}"), "токен GitHub"),
    (re.compile(r"\bsk-(proj-)?[A-Za-z0-9_-]{32,}"), "ключ OpenAI"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "токен Slack"),
    (re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}"), "токен бота Telegram"),
    (re.compile(r"\bvk1\.a\.[A-Za-z0-9_-]{20,}"), "токен VK"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "ключ Google"),
]
# Известные публичные сервисы и примеры. Адрес вне списка = находка (так ловится свой домен, не называя его).
ALLOWED_DOMAINS = (
    "github.com", "githubusercontent.com", "avito.ru", "avito.st", "svrauto.ru", "4tochki.ru",
    "datacontract.org", "microsoft.com", "w3.org", "xmlsoap.org", "google.com", "googleusercontent.com",
    "googleapis.com", "yandex.ru", "yandex.com", "ya.ru", "yandexcloud.net", "telegram.org", "t.me",
    "openai.com", "amazonaws.com", "vk.com", "vk.ru", "userapi.com", "sil.org", "python.org", "pypi.org",
    "crates.io", "rust-lang.org", "docs.rs", "mail.ru", "sentry.io", "яндекс.рф",
    # примеры и заглушки тестов
    "example.com", "example.org", "example.net", "example.ru", "evil.com", "b.ru",
)
ALLOWED_LABELS = {"example", "test", "invalid", "localhost"}   # x.example, feeds.example.test, … (последняя часть)
URL_HOST = re.compile(r"\b[a-z][a-z0-9+.-]*://(?:[^\s/@'\"<>`]+@)?([a-z0-9а-яё.-]+\.[a-zа-яё]{2,})", re.I)
EMAIL_HOST = re.compile(r"[\w.%+-]+@([a-z0-9.-]+\.[a-z]{2,})\b", re.I)
SERVER_NAME = re.compile(r"\bserver_name\s+([^;]+);")
BARE_RU = re.compile(r"(?<![\w.@/-])((?:[a-z0-9а-яё-]+\.)+(?:ru|su|рф))(?![\w-])", re.I)
GOOGLE_ID = re.compile(r"/(spreadsheets|document|forms|presentation)/d/[\w-]{40,}|/macros/s/[\w-]{40,}")   # настоящие 44+ знаков; заглушки тестов короче
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
ALLOWED_IPS = {"1.1.1.1", "1.2.3.4", "8.8.8.8", "9.9.9.9", "93.184.216.34", "93.184.216.35"}   # публичные DNS и example.com
WORKFLOW = [
    (re.compile(r"self-hosted|tires-(prod|mac)\b"), "workflow: свой раннер (в публичном репозитории запрещён)"),
    (re.compile(r"\benvironment:\s*production\b"), "workflow: окружение production"),
    (re.compile(r"\bpull_request_target\b"), "workflow: pull_request_target"),
    (re.compile(r"\bvars\.\w*(URL|HOST|DOMAIN)\w*", re.I), "workflow: адрес из переменной (виден в журнале, нужен секрет)"),
]
BINARY_OK = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".otf"}


def host_allowed(host: str) -> bool:
    h = host.lower().strip(".")
    if h.endswith(".local"):
        return False
    if h.split(".")[-1] in ALLOWED_LABELS:          # только последняя часть: test.<свой домен>.ru не пропускается
        return True
    return any(h == d or h.endswith("." + d) for d in ALLOWED_DOMAINS)


def load_deny(path: Path):
    """Строка файла: телефон (только цифры и + ( ) - пробел, 10–11 цифр) или значение целиком; # в начале — комментарий."""
    phones, literals = {}, []
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if " #" in line:
            raise ValueError(f"строка {n}: комментарий в строке значения не поддерживается (только строкой с # в начале)")
        digits = re.sub(r"\D", "", line)
        if re.fullmatch(r"[+\d\s().\-\u2013\u2014]+", line) and 10 <= len(digits) <= 11:
            phones[digits[-10:]] = n
        else:
            literals.append((line.lower(), n))
    return phones, literals


def gitleaks_allow(snap: Path):
    conf = snap / ".gitleaks.toml"
    if not conf.is_file():
        return []
    text = conf.read_text(encoding="utf-8")
    m = re.search(r"paths\s*=\s*\[(.*?)\]", text, re.S)
    return [re.compile(p) for p in re.findall(r"'''(.*?)'''", m.group(1))] if m else []


def run_gitleaks(snap: Path, findings: list):
    exe = shutil.which("gitleaks") or next(
        (p for p in ("/opt/homebrew/bin/gitleaks", "/usr/local/bin/gitleaks") if os.access(p, os.X_OK)), None)
    if not exe:
        return False
    with tempfile.TemporaryDirectory() as td:
        rep = Path(td) / "report.json"
        # вывод gitleaks не печатается (в нём строки с находками), только файл:строка:правило из отчёта
        common = ["--redact", "--no-banner", "--report-format", "json", "--report-path", str(rep), "--exit-code", "1"]
        if (snap / ".gitleaks.toml").is_file():
            common += ["--config", ".gitleaks.toml"]
        # из папки снимка и с путём «.»: пути в отчёте относительные, как в исключениях .gitleaks.toml (^tests/…)
        r = subprocess.run([exe, "dir", *common, "."], capture_output=True, cwd=snap)            # 8.19+
        if r.returncode not in (0, 1) and b"unknown command" in r.stderr.lower():
            r = subprocess.run([exe, "detect", "--no-git", "--source", ".", *common], capture_output=True, cwd=snap)
        try:
            items = json.loads(rep.read_text(encoding="utf-8") or "[]") if rep.exists() else []
        except ValueError:
            items = []
        if r.returncode not in (0, 1):
            findings.append(("-", 0, f"gitleaks завершился с кодом {r.returncode}"))
        for it in items:
            f = str(it.get("File", "?"))
            f = os.path.relpath(f, snap) if os.path.isabs(f) else os.path.normpath(f)
            findings.append((f, it.get("StartLine", 0), f"gitleaks: {it.get('RuleID', '?')}"))
        if r.returncode == 1 and not items:
            findings.append(("-", 0, "gitleaks нашёл утечку (подробности: gitleaks detect --no-git --redact)"))
    return True


def check(snap: Path, deny: Path | None, use_gitleaks: bool, strict: bool) -> int:
    if not snap.is_dir():
        print(f"[check] нет папки {snap}", file=sys.stderr)
        return 2
    findings: list = []
    warnings: list = []
    phones, literals = ({}, [])
    if deny is not None:
        if not deny.is_file():
            print(f"[check] нет файла --deny {deny}", file=sys.stderr)
            return 2
        try:
            phones, literals = load_deny(deny)
        except ValueError as e:
            print(f"[check] файл --deny: {e}", file=sys.stderr)
            return 2
    else:
        warnings.append("нет файла --deny: реальные телефоны, Id и домены не сверены (файл в tires-private)")
    allow = gitleaks_allow(snap)
    have_gitleaks = use_gitleaks and run_gitleaks(snap, findings)
    if use_gitleaks and not have_gitleaks:
        warnings.append("gitleaks не установлен: только встроенные шаблоны секретов (установка: brew install gitleaks)")

    files = 0
    for dirpath, dirnames, filenames in os.walk(snap):
        rel_dir = os.path.relpath(dirpath, snap)
        if rel_dir == ".":
            dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in dirnames + filenames:
            full = Path(dirpath) / name
            rel = os.path.normpath(os.path.join(rel_dir, name)).replace(os.sep, "/")
            if full.is_symlink():
                findings.append((rel, 0, "символическая ссылка"))
        for name in filenames:
            full = Path(dirpath) / name
            rel = os.path.normpath(os.path.join(rel_dir, name)).replace(os.sep, "/")
            if full.is_symlink():
                continue
            files += 1
            if not is_public(rel):
                findings.append((rel, 0, "файл вне публичного набора"))
            if FORBIDDEN_NAME.search(name):
                findings.append((rel, 0, "запрещённый файл (секреты, база, данные)"))
            try:
                data = full.read_bytes()
            except OSError:
                findings.append((rel, 0, "файл не читается"))
                continue
            ext = Path(name).suffix.lower()
            if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
                text = data.decode("utf-16", errors="replace")
            elif b"\0" in data:
                if ext not in BINARY_OK:
                    findings.append((rel, 0, "двоичный файл не проверен (нет в списке допустимых типов)"))
                continue
            else:
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    if ext in BINARY_OK:
                        continue
                    text = data.decode("cp1251", errors="replace")      # не UTF-8: проверяется как cp1251
            is_workflow = rel.startswith(".github/workflows/")
            secret_skip = any(p.search(rel) for p in allow)
            for n, line in enumerate(text.splitlines(), 1):
                if LOCAL_PATH.search(line):
                    findings.append((rel, n, "локальный путь"))
                for rx, kind in AUTHOR:
                    if rx.search(line):
                        findings.append((rel, n, kind))
                hosts = [m.group(1) for m in URL_HOST.finditer(line)]
                hosts += [m.group(1) for m in EMAIL_HOST.finditer(line)]
                hosts += [h for m in SERVER_NAME.finditer(line) for h in m.group(1).split() if "." in h]
                hosts += [m.group(1) for m in BARE_RU.finditer(line)]
                if any(not host_allowed(h) for h in hosts):
                    findings.append((rel, n, "адрес вне списка известных (свой домен?)"))
                if GOOGLE_ID.search(line):
                    findings.append((rel, n, "Id документа или скрипта Google"))
                for m in IPV4.finditer(line):
                    try:
                        ip = ipaddress.ip_address(m.group(1))
                    except ValueError:
                        continue
                    if ip.is_global and m.group(1) not in ALLOWED_IPS:
                        findings.append((rel, n, "публичный IP-адрес"))
                if is_workflow:
                    code = line.split("#", 1)[0]                # комментарии YAML не в счёт
                    for rx, kind in WORKFLOW:
                        if rx.search(code):
                            findings.append((rel, n, kind))
                if phones:
                    line_digits = re.sub(r"\D", "", line)
                    for k, dn in phones.items():
                        if k in line_digits:
                            findings.append((rel, n, f"значение из --deny, строка {dn} (телефон)"))
                if literals:
                    low = line.lower()
                    for val, dn in literals:
                        if val in low:
                            findings.append((rel, n, f"значение из --deny, строка {dn}"))
                if not have_gitleaks and not secret_skip:
                    for rx, kind in SECRETS:
                        if rx.search(line):
                            findings.append((rel, n, f"похоже на секрет: {kind}"))

    for f, n, kind in sorted(set(findings), key=lambda x: (x[0], x[1], x[2])):
        print(f"{f}:{n}: {kind}")
    for w in warnings:
        print(f"[check] внимание: {w}", file=sys.stderr)
    uniq = len(set(findings))
    print(f"[check] файлов {files}, находок {uniq}" + (", gitleaks: да" if have_gitleaks else ", gitleaks: нет"))
    if uniq:
        return 1
    if strict and warnings:
        print("[check] --strict: без gitleaks или файла --deny проверка не считается пройденной", file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Снимок кода для публичного репозитория и его проверка")
    ap.add_argument("--out", type=Path, help="новая или пустая папка снимка")
    ap.add_argument("--commit", default="origin/master", help="коммит (по умолчанию origin/master)")
    ap.add_argument("--repo", type=Path, default=Path.cwd(), help="репозиторий (по умолчанию текущая папка)")
    ap.add_argument("--removed-list", type=Path, help="куда записать пути для tires-private")
    ap.add_argument("--check", nargs="?", const="", metavar="DIR", help="проверить снимок (без DIR: папку --out)")
    ap.add_argument("--deny", type=Path, help=f"приватный список значений (по умолчанию {DEFAULT_DENY}, если есть)")
    ap.add_argument("--no-gitleaks", action="store_true", help="не запускать gitleaks (тесты)")
    ap.add_argument("--strict", action="store_true", help="падать без gitleaks или файла --deny (переключение)")
    a = ap.parse_args(argv)
    if a.out is None and a.check is None:
        ap.error("нужен --out или --check")
    if a.out is not None:
        rc = build(a.repo, a.commit, a.out, a.removed_list)
        if rc or a.check is None:
            return rc
    snap = Path(a.check) if a.check else a.out
    if snap is None:
        ap.error("--check без папки работает только вместе с --out")
    deny = a.deny if a.deny is not None else (DEFAULT_DENY if DEFAULT_DENY.is_file() else None)
    return check(snap, deny, not a.no_gitleaks, a.strict)


if __name__ == "__main__":
    sys.exit(main())
