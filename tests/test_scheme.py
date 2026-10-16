"""The month renders the reader's light/dark choice from the zanni-scheme
cookie that settings.kelliher.info sets, and nothing for any other value, so
a forged cookie cannot reach the markup. Runs against the built template.

Run: GLUCK_CALENDAR_MODULE=$out python tests/test_scheme.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.environ.get(
    "GLUCK_CALENDAR_MODULE",
    os.path.join(os.path.dirname(__file__), "..", "calendar")))

os.environ.update(
    GLUCK_CALENDAR_DB=tempfile.mkdtemp() + "/t.duckdb",
    GLUCK_CALENDAR_TZ="America/New_York",
)

import gluck_calendar as gc  # noqa: E402

client = gc.app.test_client()
USER = {"Remote-User": "gluck", "Remote-Groups": "admin"}
failed = 0


def check(name, ok, detail=""):
    global failed
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"\n       {detail}"))
    failed += 0 if ok else 1


def month(cookie=None):
    if cookie is None:
        client.delete_cookie("zanni-scheme")
    else:
        client.set_cookie("zanni-scheme", cookie)
    r = client.get("/calendar/2026/10", headers=USER)
    return r, r.get_data(as_text=True)


r, body = month()
check("no cookie: the month follows the OS",
      r.status_code == 200 and "data-scheme" not in body.split("<head>")[0], body[:120])
check("the month varies by cookie", "Cookie" in r.headers.get("Vary", ""))
for s in ("dark", "light"):
    r, body = month(s)
    check(f"{s}: pinned on <html>", f'<html lang="en" data-scheme="{s}">' in body, body[:120])
for bad in ("system", "", '"><script>', "Dark"):
    r, body = month(bad)
    check(f"{bad!r}: ignored", "data-scheme=" not in body.split("<head>")[0], body[:120])
print(f"[scheme] {'all passed' if not failed else f'{failed} failed'}")
sys.exit(1 if failed else 0)
