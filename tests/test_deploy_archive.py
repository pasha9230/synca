"""Что попадает в выпуск (deploy/tires-deploy.sh, cmd_deploy): src всегда, docs только если она есть в коммите.

Как в tests/test_deploy_backup.py, скрипт целиком не запускается: из него берутся log, die и cmd_deploy как есть,
шаги вокруг (замок, копия базы, переключение, проверка панели, feed-check, таймеры) заменены заглушками.
Настоящие bash, git и tar: коммит в настоящем репозитории, выпуск распаковывается настоящим `git archive | tar`.
После открытия кода документы уходят в приватный репозиторий: коммит без docs/ должен выкладываться под set -euo pipefail.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "tires-deploy.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None, reason="нужны bash и git")

OLD = "a" * 40

STUBS = r'''
require_enabled() { :; }
lock() { :; }
fetch_master() { MASTER=$(git -C "$REPO" rev-parse HEAD); }
deployed_sha() { echo "$OLD"; }
classify() { echo "обычная выкладка"; }
wait_quiet() { :; }
backup_db() { :; }
adopt_layout() { :; }
chown() { :; }
current_release() { echo "$RELEASES/prev"; }
switch_to() { echo "switch $1" >> "$CALLS"; }
restart_and_check() { :; }
prune_releases() { :; }
feed_check() { :; }
sync_timers() { :; }
'''


def _extract():
    text = SCRIPT.read_text(encoding="utf-8")
    parts = []
    m = re.search(r"^die\(\) \{.*\}$", text, re.M)
    assert m
    parts.append(m.group(0))
    for name in ("log", "cmd_deploy"):
        m = re.search(rf"^{name}\(\) \{{.*?^\}}$", text, re.M | re.S)
        assert m, name
        parts.append(m.group(0))
    return "\n".join(parts)


def git_env():
    """Only the test repository: no user or system config, no GIT_DIR of a hook that runs pytest."""
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    return {**env, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=git_env()).stdout.strip()


def commit(tmp_path, files):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    for name, body in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "выпуск")
    return repo, git(repo, "rev-parse", "HEAD")


def run_deploy(tmp_path, repo, sha):
    releases, state = tmp_path / "releases", tmp_path / "state"
    releases.mkdir()
    state.mkdir()
    calls = tmp_path / "calls"
    calls.write_text("")
    program = "\n".join([
        "set -euo pipefail", "shopt -s inherit_errexit 2>/dev/null || true",   # as on the server (bash 5); the Mac has bash 3.2
        f"REPO={repo}", f"RELEASES={releases}", f"STATE={state}", f"CALLS={calls}", f"OLD={OLD}", "LOGFILE=/dev/null",
        STUBS, _extract(),
        f"cmd_deploy {sha}",
        'echo "docs снаружи: ${docs-нет}"',                                # `local docs`: the variable stays inside cmd_deploy
    ])
    proc = subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=60, env=git_env())
    rels = [p for p in releases.iterdir() if p.name.endswith(sha[:7])]
    return proc, rels, calls.read_text()


def tree(path):
    return sorted(str(p.relative_to(path)) for p in path.rglob("*") if p.is_file())


CODE = {
    "src/tires_sync/__init__.py": "",
    "src/tires_sync/feed.py": "x = 1\n",
    "src/tires_sync/__pycache__/feed.cpython-312.pyc": "мусор",
    "deploy/tires-deploy.sh": "#!/bin/sh\n",
    "README.md": "readme\n",
    "docs-old/old.md": "соседняя папка\n",
}


def test_a_commit_with_docs_gives_a_release_of_src_and_docs_and_nothing_else(tmp_path):
    repo, sha = commit(tmp_path, {**CODE, "docs/feed-rules.md": "правила\n", "docs/integrations/sheet.md": "скрипт\n"})
    proc, rels, calls = run_deploy(tmp_path, repo, sha)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "готово" in proc.stdout and "docs снаружи: нет" in proc.stdout
    [rel] = rels
    assert f"switch {rel}" in calls
    assert sorted(p.name for p in rel.iterdir()) == ["docs", "sha", "src"]               # no docs-old, deploy/, README
    assert tree(rel) == ["docs/feed-rules.md", "docs/integrations/sheet.md", "sha",
                         "src/tires_sync/__init__.py", "src/tires_sync/feed.py"]
    assert (rel / "docs" / "feed-rules.md").read_text(encoding="utf-8") == "правила\n"
    assert (rel / "sha").read_text().strip() == sha


def test_a_commit_without_docs_gives_src_only_and_the_deploy_goes_on(tmp_path):
    repo, sha = commit(tmp_path, CODE)                                                   # docs/ lives in the private repository
    proc, rels, calls = run_deploy(tmp_path, repo, sha)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "готово" in proc.stdout and "СТОП" not in proc.stdout + proc.stderr
    [rel] = rels
    assert f"switch {rel}" in calls
    assert sorted(p.name for p in rel.iterdir()) == ["docs", "sha", "src"]               # docs/ is the empty folder the link points to
    assert list((rel / "docs").iterdir()) == []
    assert tree(rel) == ["sha", "src/tires_sync/__init__.py", "src/tires_sync/feed.py"]


def test_a_commit_without_src_stops_before_the_switch(tmp_path):
    repo, sha = commit(tmp_path, {"docs/feed-rules.md": "правила\n", "README.md": "readme\n"})
    proc, _, calls = run_deploy(tmp_path, repo, sha)
    assert proc.returncode != 0 and "готово" not in proc.stdout                         # git archive fails on «src»; pipefail stops the script
    assert "switch" not in calls
