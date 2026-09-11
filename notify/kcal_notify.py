"""kcal-notify: the calendar's reminders, as a service in its own right.

One pass per invocation, then exit. See gluck-calendar/docs/notify.md.

    kcal-notify            evaluate the triggers once and deliver what is due
    kcal-notify status     what the ledger says, as JSON
"""

import json
import logging
import os
import sys
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import duckdb
import reminders

LEDGER = os.environ.get("KCAL_NOTIFY_DB", "/var/lib/kcal-notify/reminders.duckdb")
TZ = ZoneInfo(os.environ.get("KCAL_NOTIFY_TZ", "America/New_York"))

log = logging.getLogger("kcal-notify")


def open_ledger():
    parent = os.path.dirname(LEDGER)
    if parent:
        os.makedirs(parent, exist_ok=True)
    db = duckdb.connect(LEDGER)
    lock = threading.Lock()
    reminders.ensure_schema(db, lock)
    return db, lock


def main(argv):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = reminders.config_from_env()
    if cfg is None:
        log.error("no client secret: expected $CREDENTIALS_DIRECTORY/herald-client-secret")
        return 1

    db, lock = open_ledger()

    if argv[1:2] == ["status"]:
        report = reminders.status_report(db, lock)
        report.update(recipient=cfg.recipient, reads_as=cfg.user,
                      calendar=cfg.calendar_url, herald=cfg.herald_url,
                      timezone=str(TZ))
        json.dump(report, sys.stdout, indent=1)
        sys.stdout.write("\n")
        return 0

    identity = reminders.Identity(cfg)
    now = datetime.now(TZ)
    try:
        summary = reminders.tick(db, lock, cfg, TZ, identity, now)
    except Exception:  # noqa: BLE001
        log.exception("reminder pass failed")
        reminders.heartbeat(db, lock, "pass raised; see the journal", now)
        return 1
    reminders.heartbeat(db, lock, summary, now)
    log.info("%s", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
