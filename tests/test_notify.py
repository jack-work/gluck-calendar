"""Reminder tests. Fake token endpoint, fake herald, real ledger and real tick.

Run: python tests/test_notify.py
"""

import os
import sys
import tempfile
import threading
from datetime import date, datetime, time as clock, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "notify"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "calendar"))

import duckdb  # noqa: E402
import reminders as notify  # noqa: E402

TZ = ZoneInfo("America/New_York")
DAY = date(2026, 9, 11)


class FakeIdentity:
    """Stands in for the token endpoint, the calendar API and /v1/say."""

    def __init__(self, events=()):
        self.sent = []
        self.fail_with = None
        self._events = list(events)

    def events(self, window_from, window_to):
        return [e for e in self._events
                if window_from <= (e["end"] or e["start"]) and e["start"] <= window_to]

    def say(self, recipient, text):
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        self.sent.append((recipient, text))
        return {"ok": True, "to": recipient}


def at(hour, minute=0, day=DAY):
    return datetime.combine(day, clock(hour, minute), TZ)


def event(uid, title, start, end=None, all_day=False, location=None, recurring=False):
    return {
        "id": abs(hash(uid)) % 10000, "uid": uid, "title": title,
        "start": start, "end": end, "all_day": all_day,
        "location": location, "recurring": recurring,
    }


def harness(events):
    db = duckdb.connect(tempfile.mkdtemp() + "/t.duckdb")
    lock = threading.Lock()
    notify.ensure_schema(db, lock)
    cfg = notify.Config(
        client_id="kcal-notify", client_secret="x",
        token_url="http://unused", herald_url="http://unused",
        calendar_url="http://unused",
        recipient="gluck", user="gluck", tick_seconds=60, lead_minutes=60,
    )
    herald = FakeIdentity(events)

    def run(now):
        return notify.tick(db, lock, cfg, TZ, herald, now)

    return db, lock, cfg, herald, run


def rows(db, kind=None):
    q = "SELECT kind, occurrence_key, status, attempts FROM reminder_sent"
    if kind:
        q += f" WHERE kind = '{kind}'"
    return db.execute(q + " ORDER BY kind, occurrence_key").fetchall()


CHECKS = []


def check(name):
    def wrap(fn):
        CHECKS.append((name, fn))
        return fn
    return wrap


@check("the morning digest sends once and never again")
def _():
    events = [event("a", "Standup", at(9), at(9, 15)),
              event("b", "Dentist", at(14), at(15), location="Dr. Bartolo")]
    db, lock, cfg, herald, run = harness(events)
    run(at(7, 59))
    assert herald.sent == [], "sent before 08:00"
    run(at(8, 0))
    digests = [t for _, t in herald.sent if t.startswith("**Today")]
    assert len(digests) == 1, herald.sent
    text = digests[0]
    assert "Today: Friday 11 September" in text, text
    assert "09:00-09:15 Standup" in text, text
    assert "14:00-15:00 Dentist (Dr. Bartolo)" in text, text
    for later in (at(8, 1), at(8, 30), at(11, 0)):
        run(later)
    assert len([t for _, t in herald.sent if t.startswith("**Today")]) == 1, "resent the digest"
    assert rows(db, "digest-today") == [("digest-today", "2026-09-11", "sent", 1)]


@check("a restart does not resend: the ledger outlives the process")
def _():
    events = [event("a", "Standup", at(9), at(9, 15))]
    db, lock, cfg, herald, run = harness(events)
    run(at(8, 0))
    assert len([t for _, t in herald.sent if t.startswith("**Today")]) == 1, herald.sent
    # a fresh tick with a fresh Herald, as after a deploy at 08:00:30
    herald2 = FakeIdentity(events)
    notify.tick(db, lock, cfg, TZ, herald2, at(8, 0, DAY))
    assert herald2.sent == [], "a restart resent the day"


@check("an empty digest sends nothing and does not retry all day")
def _():
    db, lock, cfg, herald, run = harness([])
    run(at(8, 0))
    assert herald.sent == [], herald.sent
    assert rows(db, "digest-today") == [("digest-today", "2026-09-11", "empty", 1)]
    run(at(8, 5))
    assert rows(db, "digest-today") == [("digest-today", "2026-09-11", "empty", 1)]


@check("the evening digest is about tomorrow")
def _():
    tomorrow = DAY + timedelta(days=1)
    events = [event("a", "Today thing", at(9)),
              event("b", "Flight", at(6, 30, tomorrow), at(10, 0, tomorrow))]
    db, lock, cfg, herald, run = harness(events)
    run(at(22, 0))
    texts = [t for _, t in herald.sent]
    assert len(texts) == 1, texts
    assert "Tomorrow: Saturday 12 September" in texts[0], texts[0]
    assert "06:30-10:00 Flight" in texts[0], texts[0]
    assert "Today thing" not in texts[0], texts[0]


@check("after midnight the 22:00 digest is not sent late: tomorrow means another day")
def _():
    tomorrow = DAY + timedelta(days=1)
    events = [event("b", "Flight", at(6, 30, tomorrow), at(10, 0, tomorrow))]
    db, lock, cfg, herald, run = harness(events)
    run(at(0, 30, tomorrow))
    assert herald.sent == [], "sent a stale tomorrow-digest after midnight"
    assert rows(db, "digest-tomorrow") == []


@check("a digest inside the grace window is sent and marked late")
def _():
    events = [event("a", "Standup", at(9), at(9, 15))]
    db, lock, cfg, herald, run = harness(events)
    run(at(9, 30))
    assert len(herald.sent) == 1
    assert "late by 90 minutes" in herald.sent[0][1], herald.sent[0][1]


@check("a digest past the grace window is skipped, not sent hours stale")
def _():
    events = [event("a", "Standup", at(9), at(9, 15))]
    db, lock, cfg, herald, run = harness(events)
    run(at(15, 0))
    assert herald.sent == [], "sent a seven-hour-late digest"
    assert rows(db, "digest-today") == [("digest-today", "2026-09-11", "skipped", 1)]


@check("the lead warning fires an hour ahead, once")
def _():
    events = [event("a", "Lunch with Rosina", at(12), at(13, 30), location="Il Barbiere")]
    db, lock, cfg, herald, run = harness(events)
    run(at(10, 55))
    assert herald.sent == [], "fired before T-60"
    run(at(11, 1))
    assert len(herald.sent) == 1, herald.sent
    text = herald.sent[0][1]
    assert "**Lunch with Rosina** starts in 1 hour" in text, text
    assert "12:00-13:30" in text and "Il Barbiere" in text, text
    for later in (at(11, 2), at(11, 30), at(11, 59)):
        run(later)
    assert len(herald.sent) == 1, "repeated the lead warning every tick"


@check("an all-day event gets digests but no lead warning")
def _():
    events = [event("a", "Mother's birthday", at(0), None, all_day=True)]
    db, lock, cfg, herald, run = harness(events)
    for hour in range(0, 24):
        run(at(hour, 0))
    leads = [r for r in rows(db) if r[0] == "lead"]
    assert leads == [], leads
    digests = [t for _, t in herald.sent]
    assert any("all day: Mother's birthday" in t for t in digests), digests


@check("two occurrences of one recurring event are two different reminders")
def _():
    tomorrow = DAY + timedelta(days=1)
    events = [
        event("standup@cal", "Standup", at(9), at(9, 15), recurring=True),
        event("standup@cal", "Standup", at(9, 0, tomorrow), at(9, 15, tomorrow), recurring=True),
    ]
    db, lock, cfg, herald, run = harness(events)
    run(at(8, 5))
    run(at(8, 5, tomorrow))
    leads = rows(db, "lead")
    assert len(leads) == 2, leads
    assert leads[0][1] != leads[1][1], "the same key for two occurrences"
    assert leads[0][1].startswith("standup@cal|"), leads[0][1]


@check("herald being down retries, then gives up loudly instead of silently")
def _():
    events = [event("a", "Lunch", at(12), at(13))]
    db, lock, cfg, herald, run = harness(events)
    herald.fail_with = "herald 502: connection refused"
    run(at(11, 1))
    assert rows(db, "lead")[0][2:] == ("failed", 1), rows(db, "lead")
    run(at(11, 2))
    assert rows(db, "lead")[0][2:] == ("failed", 2), rows(db, "lead")
    run(at(11, 3))
    assert rows(db, "lead")[0][2:] == ("failed", 3), rows(db, "lead")
    run(at(11, 4))
    assert rows(db, "lead")[0][2] == "gave-up", rows(db, "lead")
    assert herald.sent == []
    # and it stays given up rather than retrying forever
    run(at(11, 5))
    assert rows(db, "lead")[0][2] == "gave-up"


@check("herald recovering mid-retry delivers rather than dropping")
def _():
    events = [event("a", "Lunch", at(12), at(13))]
    db, lock, cfg, herald, run = harness(events)
    herald.fail_with = "herald 502"
    run(at(11, 1))
    herald.fail_with = None
    run(at(11, 2))
    assert len(herald.sent) == 1, herald.sent
    assert rows(db, "lead")[0][2] == "sent"


@check("the claim is atomic: a racing ticker sends nothing")
def _():
    events = [event("a", "Lunch", at(12), at(13))]
    db, lock, cfg, herald, run = harness(events)
    key = "a|" + at(12).isoformat()
    now = at(11, 1)
    first = notify.claim(db, lock, "lead", key, "gluck", now)
    second = notify.claim(db, lock, "lead", key, "gluck", now)
    assert first is True and second is False, (first, second)
    # the whole tick then finds it claimed and does not duplicate
    run(now)
    assert herald.sent == [], "a second ticker duplicated a claimed send"


@check("a claim abandoned mid-send is retried, not lost")
def _():
    events = [event("a", "Lunch", at(12), at(13))]
    db, lock, cfg, herald, run = harness(events)
    key = "a|" + at(12).isoformat()
    assert notify.claim(db, lock, "lead", key, "gluck", at(11, 1)) is True
    # the process died here, leaving the row pending
    assert notify.claim(db, lock, "lead", key, "gluck", at(11, 5)) is False
    assert notify.claim(db, lock, "lead", key, "gluck", at(11, 30)) is True


@check("status reports a heartbeat and unhealthy rows")
def _():
    events = [event("a", "Lunch", at(12), at(13))]
    db, lock, cfg, herald, run = harness(events)
    herald.fail_with = "down"
    notify.heartbeat(db, lock, run(at(11, 1)), at(11, 1))
    report = notify.status_report(db, lock)
    assert report["last_tick"] is not None
    assert report["unhealthy_rows"] == 1, report
    failed = [r for r in report["recent"] if r["status"] == "failed"]
    assert failed and failed[0]["detail"] == "down", report["recent"]


@check("the real Herald client mints a token, retries one 401, and reports failures")
def _():
    import http.server
    import json
    import threading as th

    state = {"tokens": 0, "says": [], "reject_first": True}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, body):
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/api/oidc/token":
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Basic "):
                    return self._json(401, {"error": "invalid_client"})
                state["tokens"] += 1
                return self._json(200, {"access_token": f"tok{state['tokens']}",
                                        "token_type": "bearer", "expires_in": 600})
            if self.path == "/v1/say":
                tok = self.headers.get("Authorization", "")
                if state["reject_first"] and tok == "Bearer tok1":
                    return self._json(401, {"error": "expired"})
                state["says"].append(json.loads(body))
                return self._json(200, {"ok": True, "to": "gluck"})
            return self._json(404, {"error": "nope"})

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    th.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    cfg = notify.Config(
        client_id="kcal-notify", client_secret="s3cret",
        token_url=base + "/api/oidc/token", herald_url=base, calendar_url=base,
        recipient="gluck", user="gluck",
    )
    herald = notify.Identity(cfg)
    herald.say("gluck", "hello")
    assert state["tokens"] == 2, f"did not remint after 401: {state}"
    assert state["says"] == [{"to": "gluck", "text": "hello"}], state["says"]

    # a second say reuses the cached token rather than minting again
    herald.say("gluck", "again")
    assert state["tokens"] == 2, f"reminted a live token: {state}"
    assert len(state["says"]) == 2

    # herald refusing outright surfaces as an exception, not a silent drop
    cfg2 = notify.Config(client_id="c", client_secret="s",
                         token_url=base + "/api/oidc/token",
                         herald_url=base + "/missing", calendar_url=base,
                         recipient="gluck", user="gluck")
    try:
        notify.Identity(cfg2).say("gluck", "x")
        raise AssertionError("a 404 from herald was swallowed")
    except RuntimeError as exc:
        assert "404" in str(exc), exc
    srv.shutdown()


@check("the token request names the public issuer, which Authelia cannot infer over loopback")
def _():
    import http.server
    import json
    import threading as th

    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path.endswith("/token"):
                seen["proto"] = self.headers.get("X-Forwarded-Proto")
                seen["host"] = self.headers.get("X-Forwarded-Host")
                body = json.dumps({"access_token": "t", "expires_in": 600}).encode()
            else:
                body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    th.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    cfg = notify.Config(client_id="kcal-notify", client_secret="s",
                        token_url=base + "/api/oidc/token", herald_url=base,
                        calendar_url=base, recipient="gluck", user="gluck",
                        issuer="https://auth.kelliher.info")
    notify.Identity(cfg).say("gluck", "x")
    assert seen == {"proto": "https", "host": "auth.kelliher.info"}, seen
    srv.shutdown()


@check("events are read from the calendar API and mapped, with the window passed through")
def _():
    import http.server
    import json
    import threading as th

    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body):
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            seen["path"] = self.path
            seen["auth"] = self.headers.get("Authorization")
            self._send([
                {"id": 1, "uid": "a@cal", "title": "Standup", "location": None,
                 "all_day": False, "recurring": True,
                 "dtstart": "2026-09-11T09:00:00-04:00",
                 "dtend": "2026-09-11T09:15:00-04:00"},
                {"id": 2, "uid": "b@cal", "title": "Birthday", "location": None,
                 "all_day": True, "recurring": False,
                 "dtstart": "2026-09-11T00:00:00-04:00", "dtend": None},
            ])

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self._send({"access_token": "tok", "expires_in": 600})

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    th.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    cfg = notify.Config(client_id="kcal-notify", client_secret="s",
                        token_url=base + "/api/oidc/token", herald_url=base,
                        calendar_url=base, recipient="gluck", user="gluck")
    got = notify.Identity(cfg).events(at(0), at(23, 59))

    assert seen["auth"] == "Bearer tok", seen
    assert "/events?" in seen["path"] and "from=" in seen["path"] and "to=" in seen["path"], seen
    assert [e["title"] for e in got] == ["Standup", "Birthday"], got
    assert got[0]["start"] == at(9), got[0]
    assert got[0]["end"] == at(9, 15) and got[0]["recurring"] is True, got[0]
    assert got[1]["all_day"] is True and got[1]["end"] is None, got[1]
    # and the mapped shape feeds the triggers unchanged
    spanning, timed = notify.day_items(got, DAY, TZ)
    assert [i[2]["title"] for i in spanning] == ["Birthday"], spanning
    assert [i[2]["title"] for i in timed] == ["Standup"], timed
    srv.shutdown()


def main():
    failed = 0
    for name, fn in CHECKS:
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {name}\n       {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {name}\n       {type(exc).__name__}: {exc}")
    print(f"[notify] {len(CHECKS) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
