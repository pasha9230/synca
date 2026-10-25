#!/usr/bin/env bash
# Только затронутые тесты (между раундами ревью дорожки): pytest-testmon выбирает тесты по изменённому Python-коду,
# а для изменённых не-Python файлов (шаблоны, css/js, правила фида, шаблоны Авито) добавляются тестовые файлы по правилам ниже.
# Полный прогон перед слиянием — по-прежнему scripts/test-full.sh.
#
# Запуск из корня копии (worktree):
#   scripts/test-affected.sh            # база ветки — merge-base с origin/master
#   BASE=<коммит> scripts/test-affected.sh -x
#
# База testmon (.testmondata) — своя у каждой копии (лежит в её корне, в .gitignore). Нет базы — первый запуск
# собирает её полным прогоном (все тесты, ~3–4 мин), дальше — только затронутые.
# Через ту же блокировку (with-test-lock.sh), что и полные прогоны: тесты сами по себе ничего общего не делят, но при
# занятых чужим полным прогоном ядрах тесты с подпроцессами упираются в тайм-ауты; ждать полный прогон — до ~3 мин.
#
# Не-Python файлы в изменениях (git diff от базы ветки, плюс неотслеживаемые) добавляют тестовые файлы, правила —
# в scripts/test_affected_extra.py: правила фида → test_feed_check* и тесты проверки перед публикацией; шаблон панели →
# тесты адресов, которые его рисуют (с include/extends), тесты, называющие его, и тесты, проверяющие все шаблоны;
# шаблон Авито → тесты, называющие его, и тесты проверки перед публикацией; прочее → тесты, где встречается имя файла.
# База ветки — merge-base: не-Python файлы копятся за всю дорожку (безопасно, но дороже к концу); влитое из master не видно.
#
# С путями, -k, -m, --lf testmon отключает свой выбор и гоняет всё указанное — для этого есть scripts/test-full.sh.
set -euo pipefail

MAIN="${TIRES_MAIN:-$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")}"   # the main copy (its .venvs and lock), from any worktree
ARM="$MAIN/.venv-arm/bin/python"
LOCK="$MAIN/scripts/with-test-lock.sh"

if [[ ! -d tests || ! -f pyproject.toml ]]; then
  echo "[test-affected] запускайте из корня копии (нет tests/ или pyproject.toml в $PWD)" >&2
  exit 2
fi
if ! why=$("$ARM" -c "import xdist, testmon, cryptography.fernet" 2>&1); then
  echo "[test-affected] нет .venv-arm с pytest-xdist и pytest-testmon ($why): запустите scripts/test-full.sh" >&2
  exit 2
fi
if [[ -z "${TEST_AFFECTED_INNER:-}" ]]; then                  # one wait for the lock covers both runs below
  exec "$LOCK" env TEST_AFFECTED_INNER=1 "$0" "$@"
fi
echo "[test-affected] тесты и код из $PWD" >&2
export TESTMON_DATAFILE=.testmondata                              # the copy's own base, whatever the environment says

if [[ ! -f .testmondata ]]; then
  echo "[test-affected] базы testmon нет: собираю её полным прогоном" >&2
  exec "$ARM" -m pytest tests -q --tb=short -n auto --testmon "$@"
fi

if [[ -z "${BASE:-}" ]] && ! BASE=$(git merge-base HEAD origin/master 2>/dev/null); then
  echo "[test-affected] нет общей точки с origin/master: укажите BASE=<коммит>" >&2
  exit 2
fi
if ! git rev-parse --verify --quiet "$BASE^{commit}" >/dev/null; then
  echo "[test-affected] BASE=$BASE — не коммит" >&2
  exit 2
fi
tracked=$(git -c core.quotePath=false diff --name-only --no-renames "$BASE")
untracked=$(git -c core.quotePath=false ls-files --others --exclude-standard)
changed=$(printf '%s\n%s\n' "$tracked" "$untracked" | sort -u)
extra=$("$ARM" "$(cd "$(dirname "$0")" && pwd)/test_affected_extra.py" "$changed")

status=0
echo "[test-affected] по изменённому Python-коду (testmon):" >&2
"$ARM" -m pytest tests -q --tb=short -n auto --testmon "$@" || status=$?
[[ $status -eq 5 ]] && status=0                                   # 5: nothing to run
if [[ -n "$extra" ]]; then
  echo "[test-affected] по изменённым шаблонам и файлам данных: $(echo "$extra" | tr '\n' ' ')" >&2
  # shellcheck disable=SC2086
  "$ARM" -m pytest $extra -q --tb=short -n auto --testmon-noselect "$@" || status=$?
fi
exit $status
