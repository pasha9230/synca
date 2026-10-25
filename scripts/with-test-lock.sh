#!/usr/bin/env bash
# Сериализует прогоны тестов между параллельными сессиями (дорожками захода).
#
# Зачем: если тесты сбрасывают общую тестовую базу (или другой общий ресурс), два параллельных
# прогона перетирают друг друга, и падение выглядит как случайный флейк, а не как чужой след.
# Через эту обёртку идёт ЛЮБОЙ запуск тестов с общей базой — полный, один файл, «юнит без базы» —
# и любое изменение тестовых баз (накат схемы, сид). Ревьюеров это касается так же.
#
# Использование — по абсолютному пути, чтобы работало из любой копии (worktree):
#   /абсолютный/путь/scripts/with-test-lock.sh npm test
#   /абсолютный/путь/scripts/with-test-lock.sh npx vitest run path/to/file.spec.ts
#
# Ограничения (это рекомендательная блокировка для нескольких локальных сессий, не мьютекс):
# - kill обёртки не останавливает уже идущие тесты — база занята, пока они идут.
# - Зависшая блокировка снимается руками: rm -rf "$LOCK_DIR"
set -euo pipefail

# Имя блокировки — одно на проект. Поменяйте, если на машине несколько проектов.
LOCK_DIR="${TEST_LOCK_DIR:-/tmp/tires-tests.lock}"

# Уже внутри обёртки (вложенный вызов из точки входа тестов) — не вставать в очередь к себе.
if [[ -n "${TEST_LOCK_HELD:-}" ]]; then
  exec "$@"
fi

cleanup() {
  # Снимаем блокировку, только если она наша.
  if [[ "$(cat "$LOCK_DIR/pid" 2>/dev/null)" == "$$" ]]; then
    rm -rf "$LOCK_DIR"
  fi
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

now() { date +%s; }
mtime() { stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || now; }

take_over_stale() {
  # mv выигрывает ровно один ожидающий; остальные пойдут на новый круг.
  if mv "$LOCK_DIR" "$LOCK_DIR.stale.$$" 2>/dev/null; then
    rm -rf "$LOCK_DIR.stale.$$"
  fi
}

while ! mkdir "$LOCK_DIR" 2>/dev/null; do
  owner="$(cat "$LOCK_DIR/pid" 2>/dev/null || echo '')"
  if [[ -n "$owner" ]] && ! kill -0 "$owner" 2>/dev/null; then
    take_over_stale   # владелец умер, не успев снять блокировку
    continue
  fi
  if [[ -z "$owner" ]] && (( $(now) - $(mtime "$LOCK_DIR") > 60 )); then
    take_over_stale   # блокировка без владельца старше минуты
    continue
  fi
  echo "[with-test-lock] тесты заняты (PID ${owner:-?}), жду 10 с…" >&2
  sleep 10
done

echo $$ > "$LOCK_DIR/pid"
export TEST_LOCK_HELD=$$

"$@"
