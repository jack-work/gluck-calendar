"""Reminder logic: what is due, how it reads, and the ledger. See docs/notify.md."""

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as clock, timedelta

import monthview
import requests
from urllib.parse import urlsplit

log = logging.getLogger("gluck_calendar.notify")

DIGEST_TODAY = "digest-today"
DIGEST_TOMORROW = "digest-tomorrow"
LEAD = "lead"
KINDS = (DIGEST_TODAY, DIGEST_TOMORROW, LEAD)

MAX_ATTEMPTS = 3
LATE_NOTE_AFTER = timedelta(minutes=15)
STALE_CLAIM = timedelta(minutes=10)

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday")
MONTHS = ("January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December")


@dataclass
class Config:
    client_id: str
    client_secret: str
    token_url: str
    herald_url: str
    calendar_url: str
    recipient: str
    user: str
    issuer: str = "https://auth.kelliher.info"
    tick_seconds: int = 60
    lead_minutes: int = 60
    morning: clock = clock(8, 0)
    evening: clock = clock(22, 0)
    grace: timedelta = timedelta(hours=2)


def config_from_env():
    secret = _read_secret()
    if not secret:
        return None
    return Config(
        client_id=os.environ.get("KCAL_NOTIFY_CLIENT_ID", "kcal-notify"),
        client_secret=secret,
        token_url=os.environ.get(
            "KCAL_NOTIFY_TOKEN_URL", "http://127.0.0.1:9091/api/oidc/token"
        ),
        herald_url=os.environ.get("KCAL_NOTIFY_HERALD_URL", "http://127.0.0.1:9098"),
        calendar_url=os.environ.get("KCAL_NOTIFY_CALENDAR_URL", "http://127.0.0.1:9094"),
        issuer=os.environ.get("KCAL_NOTIFY_ISSUER", "https://auth.kelliher.info"),
        recipient=os.environ.get("KCAL_NOTIFY_TO", "gluck"),
        user=os.environ.get("KCAL_NOTIFY_USER", "gluck"),
        tick_seconds=int(os.environ.get("KCAL_NOTIFY_TICK", "60")),
        lead_minutes=int(os.environ.get("KCAL_NOTIFY_LEAD", "60")),
        morning=_parse_clock(os.environ.get("KCAL_NOTIFY_MORNING", "08:00")),
        evening=_parse_clock(os.environ.get("KCAL_NOTIFY_EVENING", "22:00")),
    )


def _read_secret():
    explicit = os.environ.get("KCAL_NOTIFY_SECRET_FILE")
    if not explicit:
        creds = os.environ.get("CREDENTIALS_DIRECTORY")
        explicit = os.path.join(creds, "herald-client-secret") if creds else None
    if not explicit or not os.path.exists(explicit):
        return None
    with open(explicit, encoding="utf-8") as fh:
        return fh.read().strip()


def _parse_clock(text):
    hour, _, minute = text.partition(":")
    return clock(int(hour), int(minute or 0))


# ── ledger ────────────────────────────────────────────────────────────────
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS reminder_sent (
        kind TEXT NOT NULL,
        occurrence_key TEXT NOT NULL,
        recipient TEXT NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL,
        claimed_at TIMESTAMPTZ,
        settled_at TIMESTAMPTZ,
        detail TEXT,
        UNIQUE (kind, occurrence_key, recipient)
    )""",
    """CREATE TABLE IF NOT EXISTS notify_heartbeat (
        id INTEGER PRIMARY KEY,
        beat_at TIMESTAMPTZ NOT NULL,
        detail TEXT
    )""",
)


def ensure_schema(db, lock):
    with lock:
        for statement in SCHEMA:
            db.execute(statement)


CLAIM = """INSERT INTO reminder_sent
             (kind, occurrence_key, recipient, status, attempts, created_at, claimed_at)
           VALUES (?, ?, ?, 'pending', 1, ?, ?)
           ON CONFLICT (kind, occurrence_key, recipient) DO UPDATE
             SET status = 'pending',
                 attempts = reminder_sent.attempts + 1,
                 claimed_at = excluded.claimed_at
             WHERE reminder_sent.attempts < ?
               AND (reminder_sent.status = 'failed'
                    OR (reminder_sent.status = 'pending'
                        AND reminder_sent.claimed_at < ?))
           RETURNING attempts"""

REAP = """UPDATE reminder_sent SET status = 'gave-up', settled_at = ?
          WHERE status IN ('failed', 'pending') AND attempts >= ?
          RETURNING kind, occurrence_key, attempts"""


def claim(db, lock, kind, key, recipient, now):
    """Take ownership of one send, in one statement.

    The UNIQUE constraint is the concurrency control: a second ticker that
    raced this one gets no row back and sends nothing.
    """
    with lock:
        got = db.execute(
            CLAIM, [kind, key, recipient, now, now, MAX_ATTEMPTS, now - STALE_CLAIM]
        ).fetchall()
    return bool(got)


def reap(db, lock, now):
    """Retire exhausted rows so they stop being retried, loudly."""
    with lock:
        dead = db.execute(REAP, [now, MAX_ATTEMPTS]).fetchall()
    for kind, key, attempts in dead:
        log.error("giving up on %s %s after %d attempts; it will not be retried",
                  kind, key, attempts)
    return len(dead)


def settle(db, lock, kind, key, recipient, status, now, detail=None):
    with lock:
        db.execute(
            """UPDATE reminder_sent SET status = ?, settled_at = ?, detail = ?
               WHERE kind = ? AND occurrence_key = ? AND recipient = ?""",
            [status, now, detail, kind, key, recipient],
        )


def heartbeat(db, lock, detail, now):
    with lock:
        db.execute("DELETE FROM notify_heartbeat WHERE id = 1")
        db.execute("INSERT INTO notify_heartbeat (id, beat_at, detail) VALUES (1, ?, ?)",
                   [now, detail])


def status_report(db, lock):
    with lock:
        beat = db.execute("SELECT beat_at, detail FROM notify_heartbeat WHERE id = 1").fetchone()
        counts = db.execute(
            "SELECT kind, status, count(*) FROM reminder_sent GROUP BY 1, 2 ORDER BY 1, 2"
        ).fetchall()
        recent = db.execute(
            """SELECT kind, occurrence_key, status, attempts, settled_at, detail
               FROM reminder_sent ORDER BY coalesce(settled_at, created_at) DESC LIMIT 10"""
        ).fetchall()
        stuck = db.execute(
            "SELECT count(*) FROM reminder_sent WHERE status IN ('failed', 'gave-up', 'pending')"
        ).fetchone()[0]
    return {
        "last_tick": beat[0].isoformat() if beat else None,
        "last_tick_detail": beat[1] if beat else None,
        "unhealthy_rows": stuck,
        "counts": [{"kind": k, "status": s, "n": n} for k, s, n in counts],
        "recent": [
            {
                "kind": k, "occurrence": o, "status": s, "attempts": a,
                "settled_at": t.isoformat() if t else None, "detail": d,
            }
            for k, o, s, a, t, d in recent
        ],
    }


# ── herald ────────────────────────────────────────────────────────────────
class Identity:
    """The kcal-notify service identity: one token, two services."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._token = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def _forwarded(self):
        """Name the public issuer: over loopback Authelia cannot infer it."""
        parts = urlsplit(self.cfg.issuer)
        if not parts.hostname:
            return {}
        return {"X-Forwarded-Proto": parts.scheme or "https",
                "X-Forwarded-Host": parts.netloc}

    def token(self, force=False):
        with self._lock:
            if not force and self._token and time.time() < self._expires:
                return self._token
            res = requests.post(
                self.cfg.token_url,
                auth=(self.cfg.client_id, self.cfg.client_secret),
                data={"grant_type": "client_credentials"},
                headers={"Accept": "application/json", **self._forwarded()},
                timeout=15,
            )
            if res.status_code != 200:
                raise RuntimeError(f"token endpoint {res.status_code}: {res.text[:300]}")
            body = res.json()
            self._token = body["access_token"]
            self._expires = time.time() + int(body.get("expires_in", 600)) - 60
            return self._token

    def _call(self, method, url, what, **kw):
        for attempt in (1, 2):
            res = requests.request(
                method, url,
                headers={"Authorization": "Bearer " + self.token(force=attempt == 2)},
                timeout=30, **kw
            )
            if res.status_code == 401 and attempt == 1:
                continue
            if res.status_code != 200:
                raise RuntimeError(f"{what} {res.status_code}: {res.text[:300]}")
            return res.json()
        raise RuntimeError(f"{what} refused the token twice")

    def say(self, recipient, text):
        return self._call("POST", self.cfg.herald_url.rstrip("/") + "/v1/say",
                          "herald", json={"to": recipient, "text": text})

    def events(self, window_from, window_to):
        """Read the window from gluck-calendar over its API, as a service client."""
        raw = self._call(
            "GET", self.cfg.calendar_url.rstrip("/") + "/events", "calendar",
            params={"from": window_from.isoformat(), "to": window_to.isoformat()},
        )
        out = []
        for e in raw:
            out.append({
                "id": e["id"],
                "uid": e["uid"],
                "title": e["title"],
                "location": e.get("location"),
                "all_day": e["all_day"],
                "recurring": e.get("recurring", False),
                "start": datetime.fromisoformat(e["dtstart"]),
                "end": datetime.fromisoformat(e["dtend"]) if e.get("dtend") else None,
            })
        return out


# ── what is due ───────────────────────────────────────────────────────────
def day_items(instances, day, tz):
    """(all-day or spanning, timed) items touching `day`, timed ones sorted."""
    spanning, timed = [], []
    for inst in instances:
        kind, value = monthview.classify(inst, tz)
        if kind == "band":
            if value[0] <= day <= value[1]:
                spanning.append((value[0], value[1], inst))
        elif value[0].date() == day:
            timed.append((value[0], value[1], inst))
    spanning.sort(key=lambda item: (item[0], item[2]["title"]))
    timed.sort(key=lambda item: (item[0], item[2]["title"]))
    return spanning, timed


def due_leads(instances, now, tz, lead):
    """Timed occurrences starting inside the lead window, as (key, start, end, inst)."""
    horizon = now + lead
    out = []
    for inst in instances:
        kind, value = monthview.classify(inst, tz)
        if kind == "band":
            continue
        start, end = value
        if now < start <= horizon:
            out.append((f"{inst['uid']}|{start.isoformat()}", start, end, inst))
    out.sort(key=lambda item: item[1])
    return out


# ── wording ───────────────────────────────────────────────────────────────
def _hhmm(moment):
    return moment.strftime("%H:%M")


def _span(start, end):
    if end and end > start:
        return f"{_hhmm(start)}-{_hhmm(end)}"
    return _hhmm(start)


def _long_date(day):
    return f"{WEEKDAYS[day.weekday()]} {day.day} {MONTHS[day.month - 1]}"


def _suffix(inst):
    return f" ({inst['location']})" if inst.get("location") else ""


def digest_text(day, spanning, timed, heading, late_by=None):
    lines = [f"**{heading}: {_long_date(day)}**"]
    if late_by is not None:
        lines.append(f"_(late by {int(late_by.total_seconds() // 60)} minutes)_")
    lines.append("")
    for first, last, inst in spanning:
        extent = "" if first == last else f" _{first.isoformat()} to {last.isoformat()}_"
        lines.append(f"- all day: {inst['title']}{_suffix(inst)}{extent}")
    for start, end, inst in timed:
        lines.append(f"- {_span(start, end)} {inst['title']}{_suffix(inst)}")
    return "\n".join(lines)


def lead_text(start, end, inst, now):
    minutes = max(1, int((start - now).total_seconds() // 60))
    when = "in 1 hour" if 55 <= minutes <= 65 else f"in {minutes} minutes"
    lines = [f"**{inst['title']}** starts {when}", _span(start, end)]
    if inst.get("location"):
        lines.append(inst["location"])
    return "\n".join(lines)


# ── the tick ──────────────────────────────────────────────────────────────
def tick(db, lock, cfg, tz, herald, now):
    """One evaluation of all three triggers. Returns a short summary string."""
    today = now.date()
    lead = timedelta(minutes=cfg.lead_minutes)
    window_from = datetime.combine(today, clock.min, tz) - timedelta(days=2)
    window_to = datetime.combine(today + timedelta(days=3), clock.min, tz)
    instances = herald.events(window_from, window_to)

    reap(db, lock, now)
    done = []
    done += _digests(db, lock, cfg, tz, herald, now, today, instances)
    done += _leads(db, lock, cfg, tz, herald, now, instances, lead)
    return f"{len(instances)} instances in window; " + (", ".join(done) or "nothing due")


def _digests(db, lock, cfg, tz, herald, now, today, instances):
    plan = (
        (DIGEST_TODAY, cfg.morning, today, "Today"),
        (DIGEST_TOMORROW, cfg.evening, today + timedelta(days=1), "Tomorrow"),
    )
    done = []
    for kind, at, subject, heading in plan:
        scheduled = datetime.combine(today, at, tz)
        if now < scheduled:
            continue
        key = subject.isoformat()
        behind = now - scheduled
        if behind > cfg.grace:
            if claim(db, lock, kind, key, cfg.recipient, now):
                settle(db, lock, kind, key, cfg.recipient, "skipped", now,
                       f"{int(behind.total_seconds() // 60)} minutes late")
                log.warning("skipping %s for %s: %s late", kind, key, behind)
                done.append(f"{kind}=skipped")
            continue
        if not claim(db, lock, kind, key, cfg.recipient, now):
            continue
        spanning, timed = day_items(instances, subject, tz)
        if not spanning and not timed:
            settle(db, lock, kind, key, cfg.recipient, "empty", now)
            log.info("%s for %s: nothing scheduled, sending nothing", kind, key)
            done.append(f"{kind}=empty")
            continue
        text = digest_text(subject, spanning, timed, heading,
                           behind if behind > LATE_NOTE_AFTER else None)
        done.append(f"{kind}=" + _deliver(db, lock, cfg, herald, kind, key, text, now))
    return done


def _leads(db, lock, cfg, tz, herald, now, instances, lead):
    done = []
    for key, start, end, inst in due_leads(instances, now, tz, lead):
        if not claim(db, lock, LEAD, key, cfg.recipient, now):
            continue
        text = lead_text(start, end, inst, now)
        done.append("lead=" + _deliver(db, lock, cfg, herald, LEAD, key, text, now))
    return done


def _deliver(db, lock, cfg, herald, kind, key, text, now):
    try:
        herald.say(cfg.recipient, text)
    except Exception as exc:  # noqa: BLE001
        settle(db, lock, kind, key, cfg.recipient, "failed", now, str(exc)[:400])
        log.warning("%s for %s failed, will retry: %s", kind, key, exc)
        return "failed"
    settle(db, lock, kind, key, cfg.recipient, "sent", now)
    log.info("%s for %s delivered to %s", kind, key, cfg.recipient)
    return "sent"
