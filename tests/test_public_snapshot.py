"""scripts/public-snapshot.py: the public snapshot keeps only the public set, its check finds local paths,
the author, unknown domains, the private --deny values and secrets, and never prints the values themselves."""
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "public-snapshot.py"
# planted values are glued from parts: the snapshot of this repository runs the same check over this file
LOCAL = "/" + "Users/x/project"
DOMAIN = "panel." + "realshop" + ".ru"
PHONE = "+7 912 " + "345-67-89"
TOKEN = "gh" + "p_" + "A1b2C3d4" * 5

PRIVATE = {
    "CLAUDE.md": "rules\n",
    ".claude/settings.json": "{}\n",
    "docs/STATE.md": "state\n",
    "docs/DECISIONS.md": "decisions\n",
    "docs/runs/r1/report.md": "report\n",
    "docs/superpowers/plans/p.md": "plan\n",
    "docs/integrations/apps-script.md": "script\n",
    "docs/plan-x.md": "plan\n",
    "docs/avito-data.xlsx": "xlsx\n",
    "tires-server/opt/a.py": "old\n",
    "tests/fixtures/tochki/methods.xsd": "<xs:schema/>\n",          # third-party schemas: private even inside tests/
    ".DS_Store": "junk\n",
    "src/pkg/.DS_Store": "junk\n",
}
PUBLIC = {
    "src/pkg/__init__.py": "x = 'https://panel.example.com/login'\n",
    "tests/test_a.py": "def test_a():\n    assert 'own@mail.ru'\n",
    "scripts/run.sh": "#!/bin/sh\necho ok\n",
    "deploy/nginx.conf": "server_name panel.example.com;\n",
    ".github/workflows/t.yml": "on: push\n",
    "docs/feed-rules.md": "rules\n",
    "pyproject.toml": "[project]\n",
    "README.md": "readme\n",
}


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def make_repo(tmp_path, files):
    repo = tmp_path / "repo"
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "add", "-A", "-f")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init")
    return repo


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True)


def files_in(d):
    return sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file())


def build(tmp_path, files=None):
    repo = make_repo(tmp_path, files or {**PRIVATE, **PUBLIC})
    out = tmp_path / "snap"
    r = run("--repo", repo, "--commit", "HEAD", "--out", out, "--removed-list", tmp_path / "private.txt")
    assert r.returncode == 0, r.stderr
    return out, r


def check(snap, deny=None, *extra):
    args = ["--check", snap, "--no-gitleaks", *extra]
    if deny is not None:
        args += ["--deny", deny]
    return run(*args)


def test_private_removed_public_kept(tmp_path):
    out, r = build(tmp_path)
    assert files_in(out) == sorted(PUBLIC)
    moved = (tmp_path / "private.txt").read_text(encoding="utf-8").split()
    # the private set goes to tires-private; the old server copy and .DS_Store are not carried over
    assert sorted(moved) == sorted(p for p in PRIVATE if not p.startswith("tires-server/") and not p.endswith(".DS_Store"))
    assert "в снимке 8, убрано 13 (в tires-private 10, не переносится 3)" in r.stdout


def test_out_must_be_empty(tmp_path):
    repo = make_repo(tmp_path, PUBLIC)
    out = tmp_path / "snap"
    out.mkdir()
    (out / "old.txt").write_text("x", encoding="utf-8")
    assert run("--repo", repo, "--commit", "HEAD", "--out", out).returncode == 2


def test_check_passes_on_clean(tmp_path):
    out, _ = build(tmp_path)
    deny = tmp_path / "deny.txt"
    deny.write_text(f"# real values\n{PHONE}\n{DOMAIN}\n", encoding="utf-8")
    r = check(out, deny)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "находок 0" in r.stdout


def test_check_fails_on_local_path_without_printing_it(tmp_path):
    out, _ = build(tmp_path)
    (out / "scripts" / "run.sh").write_text(f"#!/bin/sh\ncd {LOCAL}\n", encoding="utf-8")
    r = check(out)
    assert r.returncode == 1
    assert "scripts/run.sh:2: локальный путь" in r.stdout
    assert LOCAL not in r.stdout + r.stderr


def test_panel_route_users_is_not_a_local_path(tmp_path):
    out, _ = build(tmp_path, PUBLIC)
    (out / "src" / "pkg" / "routes.py").write_text('@app.route("/users/invite")\n', encoding="utf-8")
    assert check(out).returncode == 0


def test_check_finds_deny_values_unknown_domain_and_secret(tmp_path):
    out, _ = build(tmp_path)
    (out / "tests" / "test_a.py").write_text(
        f"PHONE = '{PHONE.replace(' ', '')}'\nURL = 'https://{DOMAIN}/hook'\nT = '{TOKEN}'\n", encoding="utf-8")
    (out / "deploy" / "nginx.conf").write_text(f"server_name {DOMAIN};\n", encoding="utf-8")
    deny = tmp_path / "deny.txt"
    deny.write_text(f"# real values\n{PHONE}\n", encoding="utf-8")
    r = check(out, deny)
    assert r.returncode == 1
    lines = r.stdout.splitlines()
    assert "tests/test_a.py:1: значение из --deny, строка 2 (телефон)" in lines
    assert "tests/test_a.py:2: адрес вне списка известных (свой домен?)" in lines
    assert "tests/test_a.py:3: похоже на секрет: токен GitHub" in lines
    assert "deploy/nginx.conf:1: адрес вне списка известных (свой домен?)" in lines
    out_all = r.stdout + r.stderr
    for value in (PHONE, PHONE.replace(" ", ""), "345-67-89", DOMAIN, "realshop", TOKEN):
        assert value not in out_all


def test_check_finds_private_and_forbidden_files(tmp_path):
    out, _ = build(tmp_path, PUBLIC)
    (out / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
    (out / "src" / "pkg" / ".env").write_text("A=1\n", encoding="utf-8")
    r = check(out)
    assert r.returncode == 1
    assert "CLAUDE.md:0: файл вне публичного набора" in r.stdout
    assert "src/pkg/.env:0: запрещённый файл (секреты, база, данные)" in r.stdout


def test_strict_needs_deny_file(tmp_path):
    """Without --deny the script takes private/public-snapshot-deny.txt next to itself when it is there (the owner's Mac has it): a copy of
    the script in a folder without that file is what «no deny file» means, wherever the test runs."""
    out, _ = build(tmp_path, PUBLIC)
    alone = tmp_path / "alone" / "scripts" / SCRIPT.name
    alone.parent.mkdir(parents=True)
    alone.write_bytes(SCRIPT.read_bytes())
    assert not (tmp_path / "alone" / "private" / "public-snapshot-deny.txt").exists()
    bare = lambda *args: subprocess.run([sys.executable, str(alone), "--check", str(out), "--no-gitleaks", *args], capture_output=True, text=True)
    assert bare().returncode == 0
    r = bare("--strict")
    assert r.returncode == 1 and "--deny" in r.stderr


def test_gitleaks_used_when_installed(tmp_path):
    import os
    import shutil
    import pytest
    exe = shutil.which("gitleaks") or next(
        (p for p in ("/opt/homebrew/bin/gitleaks", "/usr/local/bin/gitleaks") if os.access(p, os.X_OK)), None)
    if not exe:
        pytest.skip("gitleaks is not installed")
    out, _ = build(tmp_path, PUBLIC)
    (out / ".gitleaks.toml").write_text("[extend]\nuseDefault = true\n[allowlist]\npaths = ['''^tests/test_a\\.py$''']\n",
                                        encoding="utf-8")
    (out / "tests" / "test_a.py").write_text(f"T = '{TOKEN}'\n", encoding="utf-8")       # allowlisted stub
    assert run("--check", out).returncode == 0
    (out / "src" / "pkg" / "__init__.py").write_text(f"T = '{TOKEN}'\n", encoding="utf-8")
    r = run("--check", out)
    assert r.returncode == 1 and "gitleaks: да" in r.stdout
    assert any(line.startswith("src/pkg/__init__.py:1: gitleaks: ") for line in r.stdout.splitlines()), r.stdout
    assert TOKEN not in r.stdout + r.stderr


def test_review_gaps_closed(tmp_path):
    out, _ = build(tmp_path, PUBLIC)
    glued = "tel_7" + "9123456789x"                                 # the phone glued to letters, no separators
    dashed = "+7 (912) \u2013 345\u201367\u201389"                   # en dashes and a 3-character separator
    split = "'+7912' '3456789'"                                     # split across two literals
    cp = ("ПРИМЕР " + PHONE).encode("cp1251")
    (out / "tests" / "test_a.py").write_text(f"A = '{glued}'\nB = '{dashed}'\nC = {split}\n", encoding="utf-8")
    (out / "src" / "pkg" / "legacy.txt").write_bytes(cp)
    (out / "src" / "pkg" / "blob.bin").write_bytes(b"\0\1\2")
    (out / "src" / "pkg" / "hosts.py").write_text(
        "U = 'https://test." + "realshop" + ".ru/x'\nIP = '" + "203.0" + ".113.7 " + "5.6" + ".7.8'\n", encoding="utf-8")
    (out / ".github" / "workflows" / "t.yml").write_text(
        "# self-hosted in a comment is fine\njobs:\n  a:\n    runs-on: [self-" "hosted, tires-prod]\n"
        "    environment: production\n    env:\n      URL: ${{ vars.PANEL_URL }}\n", encoding="utf-8")
    deny = tmp_path / "deny.txt"
    deny.write_text(f"{PHONE}\n", encoding="utf-8")
    r = check(out, deny)
    lines = r.stdout.splitlines()
    assert r.returncode == 1
    for n in (1, 2, 3):
        assert f"tests/test_a.py:{n}: значение из --deny, строка 1 (телефон)" in lines, r.stdout
    assert "src/pkg/legacy.txt:1: значение из --deny, строка 1 (телефон)" in lines
    assert "src/pkg/blob.bin:0: двоичный файл не проверен (нет в списке допустимых типов)" in lines
    assert "src/pkg/hosts.py:1: адрес вне списка известных (свой домен?)" in lines
    assert "src/pkg/hosts.py:2: публичный IP-адрес" in lines                # the second address; the first is a documentation range
    assert ".github/workflows/t.yml:1: workflow: свой раннер (в публичном репозитории запрещён)" not in lines
    assert ".github/workflows/t.yml:4: workflow: свой раннер (в публичном репозитории запрещён)" in lines
    assert ".github/workflows/t.yml:5: workflow: окружение production" in lines
    assert ".github/workflows/t.yml:7: workflow: адрес из переменной (виден в журнале, нужен секрет)" in lines
    assert "realshop" not in r.stdout + r.stderr and "3456789" not in r.stdout + r.stderr


def test_deny_inline_comment_refused(tmp_path):
    out, _ = build(tmp_path, PUBLIC)
    deny = tmp_path / "deny.txt"
    deny.write_text(f"{PHONE} # owner\n", encoding="utf-8")
    r = check(out, deny)
    assert r.returncode == 2 and "строка 1" in r.stderr and PHONE not in r.stderr
