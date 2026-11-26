"""Backfill CRM tags for recorded customer refusals. Dry run by default."""
import argparse
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from tires_sync import clients, order_wait


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error("database must be an existing file")

    database = args.database.resolve().as_uri()
    with closing(sqlite3.connect(database + ("?mode=rw" if args.apply else "?mode=ro"), uri=True)) as conn, conn:
        rows = conn.execute("""
            SELECT DISTINCT co.client_id
            FROM client_order co JOIN order_card oc ON oc.id = co.order_id
            JOIN order_stage st ON st.id = oc.stage_id
            WHERE (st.code = 'cancel' AND (
                      oc.cancel_reason = 'отменил клиент'
                      OR oc.cancel_reason LIKE 'отменил клиент:%'
                      OR oc.cancel_reason LIKE 'Отказ клиента на точке:%'
                      OR oc.cancel_reason LIKE 'отказ клиента: Отказ клиента на точке:%'
                  ))
               OR EXISTS (SELECT 1 FROM order_task t
                          WHERE t.order_id = oc.id AND t.rule_id = ?
                            AND t.status = 'done' AND t.result = ?)
               OR EXISTS (SELECT 1 FROM order_event e
                          WHERE e.order_id = oc.id AND e.kind = 'point'
                            AND e.text LIKE 'Отказ клиента на точке «%')
        """, (order_wait.TASK_SILENT, order_wait.REFUSED)).fetchall()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if args.apply:
            for (client_id,) in rows:
                clients.set_tag(conn, client_id, clients.REFUSED_ORDER_TAG, True, now, "backfill: order refusal")
        print(f"Clients with confirmed refusals: {len(rows)}. Apply: {args.apply}")


if __name__ == "__main__":
    main()
