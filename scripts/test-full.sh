#!/usr/bin/env bash
# Полный прогон тестов быстрым способом: окружение Apple Silicon (.venv-arm, без `arch -x86_64`) и pytest-xdist
# (`-n auto`, по воркеру на ядро), через ту же блокировку, что и раньше (scripts/with-test-lock.sh).
#
# Запуск из корня копии (worktree), тесты и код берутся из текущей папки:
#   scripts/test-full.sh                      # все тесты
#   scripts/test-full.sh -k chats             # аргументы pytest
#   scripts/test-full.sh tests/test_roles.py  # свои пути: только они
#
# Нет .venv-arm или в нём нет xdist — прогон идёт по-старому (x86 .venv, последовательно) и говорит почему.
# Окружение: python3 -m venv .venv-arm && .venv-arm/bin/pip install -e '.[dev]' (см. docs/runs/2026-10-21-fast-tests).
set -euo pipefail

MAIN="${TIRES_MAIN:-$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")}"   # the main copy (its .venvs and lock), from any worktree
ARM="$MAIN/.venv-arm/bin/python"
X86="$MAIN/.venv/bin/python"
LOCK="$MAIN/scripts/with-test-lock.sh"

if [[ ! -d tests || ! -f pyproject.toml ]]; then
  echo "[test-full] запускайте из корня копии (нет tests/ или pyproject.toml в $PWD)" >&2
  exit 2
fi
echo "[test-full] тесты и код из $PWD" >&2
if [[ ! -x "$ARM" && ! -x "$X86" ]]; then
  echo "[test-full] в $MAIN нет ни .venv-arm, ни .venv: укажите основную копию, TIRES_MAIN=<путь> (копия раннера, клон без окружений)" >&2
  exit 2
fi

targets=(tests)                                           # свои пути (tests/…) вместо всего набора
for arg in "$@"; do
  if [[ "$arg" == tests* ]]; then targets=(); break; fi
done

if [[ -x "$ARM" ]] && why=$("$ARM" -c "import xdist, cryptography.fernet" 2>&1); then
  exec "$LOCK" "$ARM" -m pytest ${targets[@]+"${targets[@]}"} -q --tb=short -n auto "$@"
fi
echo "[test-full] быстрое окружение недоступно (${why:-нет $ARM}): последовательный прогон через x86 .venv" >&2
exec "$LOCK" arch -x86_64 "$X86" -m pytest ${targets[@]+"${targets[@]}"} -q --tb=short "$@"
