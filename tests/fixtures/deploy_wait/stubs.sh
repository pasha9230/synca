# Заглушки для tests/test_deploy_wait.py: подменяют systemctl, date и sleep функциями bash.
# Часы — файл $FAKE_CLOCK (секунды эпохи), sleep двигает их вперёд без реального ожидания.
# Сценарий в переменных (секунды от старта $FAKE_T0):
#   CYCLE_FROM, CYCLE_BUSY_UNTIL  tires-cycle.service в activating при CYCLE_FROM <= t < CYCLE_BUSY_UNTIL
#   ORDERS_FROM, ORDERS_BUSY_UNTIL  то же для tires-orders.service
#   STOCK_FROM, STOCK_BUSY_UNTIL    то же для tires-stock.service (проверка остатков между циклами)
#   NEXT_EMPTY_UNTIL   таймер не отдаёт время следующего цикла, пока t < значения; пока идёт цикл — тоже
#                      (так ведёт себя systemd, ошибка выкладки 2026-10-20)
#   NEXT_AT            время следующего цикла (от старта), когда оно известно;
#   NEXT_AT_AFTER      оно же после конца цикла (по умолчанию NEXT_AT)
#   NEXT_EMPTY2=a-b    ещё одно окно без времени: a <= t < b
#   NEXT_AT_LATE=t:v   с момента t время следующего цикла v (от старта)
#   OLD_SYSTEMD=1      --timestamp=unix не понимается, значение в микросекундах
#   STOCK_NEXT_AT      время следующей проверки остатков (tires-stock.timer, от старта); не задано — таймера нет;
#   STOCK_NEXT_AFTER   оно же после конца прохода (по умолчанию STOCK_NEXT_AT); пока идёт проход — пусто
fake_t() { echo $(( $(cat "$FAKE_CLOCK") - FAKE_T0 )); }

date() {
  case "${1:-}" in
    +%s) cat "$FAKE_CLOCK" ;;
    *) echo "t+$(fake_t)" ;;
  esac
}

sleep() { echo $(( $(cat "$FAKE_CLOCK") + $1 )) > "$FAKE_CLOCK"; }

systemctl() {
  local t unit at
  t=$(fake_t)
  unit=${!#}
  case "$1" in
    is-active) echo active; return 0 ;;
    show) ;;
    *) echo "stub systemctl: неожиданный вызов $*" >&2; return 1 ;;
  esac
  case "$unit" in
    tires-cycle.service)
      if [ "$t" -ge "${CYCLE_FROM:-0}" ] && [ "$t" -lt "${CYCLE_BUSY_UNTIL:-0}" ]; then echo activating; else echo inactive; fi ;;
    tires-orders.service)
      if [ "$t" -ge "${ORDERS_FROM:-0}" ] && [ "$t" -lt "${ORDERS_BUSY_UNTIL:-0}" ]; then echo activating; else echo inactive; fi ;;
    tires-stock.service)
      if [ "$t" -ge "${STOCK_FROM:-0}" ] && [ "$t" -lt "${STOCK_BUSY_UNTIL:-0}" ]; then echo activating; else echo inactive; fi ;;
    tires-mirror-*.service) echo inactive ;;
    tires-cycle.timer)
      at=$NEXT_AT
      if [ "${CYCLE_BUSY_UNTIL:-0}" -gt 0 ] && [ "$t" -ge "$CYCLE_BUSY_UNTIL" ]; then at=${NEXT_AT_AFTER:-$NEXT_AT}; fi
      if [[ " $* " == *" --timestamp=unix "* ]] && [ "${OLD_SYSTEMD:-0}" = 1 ]; then
        echo "Unknown option --timestamp" >&2; return 1
      fi
      if [ -n "${NEXT_AT_LATE:-}" ] && [ "$t" -ge "${NEXT_AT_LATE%%:*}" ]; then at=${NEXT_AT_LATE#*:}; fi
      if [ "$t" -lt "${NEXT_EMPTY_UNTIL:-0}" ] \
          || { [ -n "${NEXT_EMPTY2:-}" ] && [ "$t" -ge "${NEXT_EMPTY2%-*}" ] && [ "$t" -lt "${NEXT_EMPTY2#*-}" ]; } || [ "$(systemctl show -p ActiveState --value tires-cycle.service)" = activating ]; then echo ""
      elif [[ " $* " == *" --timestamp=unix "* ]]; then echo "@$(( FAKE_T0 + at ))"
      else echo $(( (FAKE_T0 + at) * 1000000 ))
      fi ;;
    tires-stock.timer)
      at=${STOCK_NEXT_AT:-}
      if [ -n "$at" ] && [ "${STOCK_BUSY_UNTIL:-0}" -gt 0 ] && [ "$t" -ge "$STOCK_BUSY_UNTIL" ]; then at=${STOCK_NEXT_AFTER:-$at}; fi
      if [ -z "$at" ] || [ "$(systemctl show -p ActiveState --value tires-stock.service)" = activating ]; then echo ""
      elif [[ " $* " == *" --timestamp=unix "* ]]; then echo "@$(( FAKE_T0 + at ))"
      else echo $(( (FAKE_T0 + at) * 1000000 ))
      fi ;;
    *) echo "stub systemctl: неизвестная служба $unit" >&2; return 1 ;;
  esac
}
