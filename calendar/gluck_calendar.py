"""gluck-calendar — CRUD over a DuckDB-backed event table with per-item ACLs.

Mirrors the gluck-todo design:

- Loopback bind; Caddy sets Remote-User / Remote-Groups from Authelia's
  forward-auth response. Client-supplied Remote-* headers are stripped by
  Caddy before the request arrives.
- If Authorization: Bearer <jwt> is present, validate against Authelia's
  JWKS and stamp Remote-User / Remote-Groups from preferred_username /
  groups claims. Same bearer-bypass shape as gluck-todo.
- Creating events requires the ``calendar-create`` group.
- Per-event ACL rows govern Read/Write/Delete/Share; creator gets all four.

Events store an optional RFC 5545 RRULE; `GET /events?from=&to=` expands
recurring events into concrete instances inside the requested window.
"""

import calendar as _calendar
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import duckdb
import jwt
import requests
from dateutil.rrule import rrulestr
from flask import Flask, jsonify, redirect, render_template_string, request
from jwt import PyJWKClient

DB_PATH = os.environ.get("GLUCK_CALENDAR_DB", "/var/lib/gluck-calendar/calendar.duckdb")
PORT = int(os.environ.get("PORT", "9094"))

OIDC_ISSUER = os.environ.get("GLUCK_CALENDAR_OIDC_ISSUER", "https://auth.kelliher.info")
OIDC_JWKS_URL = os.environ.get(
    "GLUCK_CALENDAR_OIDC_JWKS_URL", "http://127.0.0.1:9091/jwks.json"
)
# Accept tokens from either the legacy `gluck-calendar-cli` client or the
# renamed `kcal` client during the debranding transition. Comma-separated
# so the env var stays a simple string; parsed into a set on startup.
OIDC_CLIENT_IDS = {
    s.strip()
    for s in os.environ.get(
        "GLUCK_CALENDAR_OIDC_CLIENT_IDS", "gluck-calendar-cli,kcal"
    ).split(",")
    if s.strip()
}
# Preserved for backward-compat with any operator setting the singular env
# var; folded into the set above so both configurations work.
_legacy_single = os.environ.get("GLUCK_CALENDAR_OIDC_CLIENT_ID")
if _legacy_single:
    OIDC_CLIENT_IDS.add(_legacy_single)
OIDC_USERINFO_URL = os.environ.get(
    "GLUCK_CALENDAR_OIDC_USERINFO_URL", "http://127.0.0.1:9091/api/oidc/userinfo"
)

# Machine callers, as {client_id: username-to-read-as}.
#
# A client_credentials token has no user at all: no preferred_username, no
# groups, and /userinfo has nothing to say about it. So a service cannot be
# resolved to an identity the way a person is — it must be granted one
# explicitly, here, and the grant is deliberately narrow:
#
#   * READ ONLY. Any method other than GET/HEAD is refused, so a notifier
#     that reads the day's events can never alter them. This is enforced
#     below, before routing, rather than trusted to each handler.
#   * DELEGATED, NOT IMPERSONATING. The service reads the named user's
#     events through the ordinary ACL path; it gains nothing that user
#     lacks, and the audit line records which client asked.
SERVICE_CLIENTS = json.loads(
    os.environ.get("GLUCK_CALENDAR_SERVICE_CLIENTS", "{}")
)

CREATE_GROUP = "calendar-create"
PERMISSIONS = ("Read", "Write", "Delete", "Share")
DEFAULT_WINDOW_DAYS = 90
MAX_EXPANSION = 500  # cap RRULE expansion per event per query

app = Flask(__name__)
db_lock = threading.Lock()
db = duckdb.connect(DB_PATH)

_userinfo_cache: dict = {}
_userinfo_cache_lock = threading.Lock()
USERINFO_TTL = 60

_jwks_client = None
_jwks_lock = threading.Lock()


def jwks_client():
    global _jwks_client
    with _jwks_lock:
        if _jwks_client is None:
            _jwks_client = PyJWKClient(OIDC_JWKS_URL, cache_keys=True, lifespan=3600)
        return _jwks_client


def _decode_jwt_payload(compact: str) -> dict:
    import base64
    import json as _json

    parts = compact.split(".")
    if len(parts) < 2:
        return {}
    pad = "=" * (-len(parts[1]) % 4)
    return _json.loads(base64.urlsafe_b64decode(parts[1] + pad))


def fetch_userinfo(access_token: str, sub: str) -> dict:
    now = time.time()
    with _userinfo_cache_lock:
        hit = _userinfo_cache.get(sub)
        if hit and hit[0] > now:
            return hit[1]
    r = requests.get(
        OIDC_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=5,
    )
    if r.status_code != 200:
        raise RuntimeError(f"userinfo {r.status_code}: {r.text}")
    data = (
        r.json()
        if r.headers.get("content-type", "").startswith("application/json")
        else _decode_jwt_payload(r.text)
    )
    with _userinfo_cache_lock:
        _userinfo_cache[sub] = (now + USERINFO_TTL, data)
    return data


@app.before_request
def bearer_to_remote_headers():
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    token = auth.split(None, 1)[1].strip()
    try:
        signing_key = jwks_client().get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            issuer=OIDC_ISSUER,
            options={
                "require": ["exp", "iat", "iss", "sub", "client_id"],
                "verify_aud": False,
            },
        )
    except Exception as e:  # noqa: BLE001
        return jsonify(error=f"invalid bearer token: {e}"), 401

    client_id = claims.get("client_id")

    # Machine callers take the delegated, read-only path.
    if client_id in SERVICE_CLIENTS:
        if request.method not in ("GET", "HEAD"):
            app.logger.info(
                "refused %s %s: service client %s is read-only",
                request.method, request.path, client_id,
            )
            return jsonify(error="service clients are read-only"), 403
        request.environ["HTTP_REMOTE_USER"] = SERVICE_CLIENTS[client_id]
        request.environ["HTTP_REMOTE_GROUPS"] = ""
        request.environ["gluck.client_id"] = client_id
        return None

    if client_id not in OIDC_CLIENT_IDS:
        return jsonify(error="token not issued for this client"), 401

    userinfo = fetch_userinfo(token, claims["sub"])
    username = (
        userinfo.get("preferred_username")
        or userinfo.get("sub")
        or claims.get("sub")
        or ""
    )
    groups = userinfo.get("groups") or []
    if isinstance(groups, str):
        groups = [g.strip() for g in groups.split(",") if g.strip()]

    request.environ["HTTP_REMOTE_USER"] = username
    request.environ["HTTP_REMOTE_GROUPS"] = ",".join(groups)
    return None


# ── Schema ────────────────────────────────────────────────────────────────
db.execute(
    """CREATE TABLE IF NOT EXISTS event (
        id BIGINT PRIMARY KEY,
        uid TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        description TEXT,
        location TEXT,
        dtstart TIMESTAMPTZ NOT NULL,
        dtend TIMESTAMPTZ,
        all_day BOOLEAN NOT NULL DEFAULT FALSE,
        rrule TEXT,
        source TEXT,
        created_by TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS acl (
        event_id BIGINT NOT NULL,
        username TEXT NOT NULL,
        permission TEXT NOT NULL,
        UNIQUE (event_id, username, permission)
    )"""
)


# ── Helpers ───────────────────────────────────────────────────────────────
def caller():
    return request.headers.get("Remote-User", "").strip()


def caller_groups():
    raw = request.headers.get("Remote-Groups", "")
    return [g.strip() for g in raw.split(",") if g.strip()]


def has_perm(event_id, username, permission):
    row = db.execute(
        "SELECT 1 FROM acl WHERE event_id = ? AND username = ? AND permission = ?",
        [event_id, username, permission],
    ).fetchone()
    return row is not None


def event_row(event_id):
    return db.execute(
        """SELECT id, uid, title, description, location, dtstart, dtend,
                  all_day, rrule, source, created_by, created_at, updated_at
           FROM event WHERE id = ?""",
        [event_id],
    ).fetchone()


def as_dict(row):
    return {
        "id": row[0],
        "uid": row[1],
        "title": row[2],
        "description": row[3],
        "location": row[4],
        "dtstart": row[5].isoformat() if row[5] else None,
        "dtend": row[6].isoformat() if row[6] else None,
        "all_day": row[7],
        "rrule": row[8],
        "source": row[9],
        "created_by": row[10],
        "created_at": row[11].isoformat() if row[11] else None,
        "updated_at": row[12].isoformat() if row[12] else None,
    }


def parse_dt(value):
    """Accept ISO 8601 (with or without TZ); assume UTC when naive."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def gate(event_id, permission):
    user = caller()
    if not user:
        return jsonify(error="unauthenticated"), 401
    if event_row(event_id) is None or not has_perm(event_id, user, "Read"):
        return jsonify(error="not found"), 404
    if permission != "Read" and not has_perm(event_id, user, permission):
        return jsonify(error=f"requires {permission} permission"), 403
    return None


def instance_dict(row, start, duration):
    end = (start + duration) if duration else None
    return {
        "id": row[0],
        "uid": row[1],
        "title": row[2],
        "description": row[3],
        "location": row[4],
        "dtstart": start.isoformat(),
        "dtend": end.isoformat() if end else None,
        "all_day": row[7],
        "rrule": row[8],
        "source": row[9],
        "created_by": row[10],
        "recurring": row[8] is not None,
    }


def expand(row, window_from, window_to):
    """Yield concrete instance dicts for `row` inside [window_from, window_to]."""
    start, end = row[5], row[6]
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    duration = (end - start) if end else timedelta(0)
    if end and end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    if not row[8]:
        # single-shot event; include if it overlaps window at all
        eff_end = end or start
        if eff_end >= window_from and start <= window_to:
            yield instance_dict(row, start, duration)
        return

    # recurring — expand
    try:
        rr = rrulestr(row[8], dtstart=start)
    except Exception:  # noqa: BLE001 — bad RRULE, skip expansion
        yield instance_dict(row, start, duration)
        return

    count = 0
    for occ in rr.between(window_from, window_to, inc=True):
        if occ.tzinfo is None:
            occ = occ.replace(tzinfo=timezone.utc)
        yield instance_dict(row, occ, duration)
        count += 1
        if count >= MAX_EXPANSION:
            break


# ── Routes ────────────────────────────────────────────────────────────────
@app.get("/")
def index():
    now = datetime.now(timezone.utc)
    return redirect(f"/calendar/{now.year}/{now.month:02d}")


@app.get("/api")
def api_index():
    return jsonify(
        service="gluck-calendar",
        endpoints=[
            "GET  /health",
            "GET  /whoami",
            "GET  /calendar",
            "GET  /calendar/<year>/<month>",
            "GET  /events?from=ISO&to=ISO",
            "POST /events",
            "GET  /events/<id>",
            "PUT  /events/<id>",
            "DELETE /events/<id>",
            "POST /events/<id>/share",
        ],
    )


# ── HTML calendar view ────────────────────────────────────────────────────
CALENDAR_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ month_name }} {{ year }} · gluck-calendar</title>
<style>
  :root {
    --bg: #fafaf9;
    --fg: #111;
    --muted: #999;
    --line: #e5e5e2;
    --accent: #b45309;
    --today: #fef3c7;
    --event: #1c1917;
    --event-bg: #f5f5f4;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0c0a09;
      --fg: #f5f5f4;
      --muted: #78716c;
      --line: #292524;
      --accent: #fbbf24;
      --today: #422006;
      --event: #fafaf9;
      --event-bg: #1c1917;
    }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--bg); color: var(--fg); font: 15px/1.45 ui-sans-serif, system-ui, -apple-system, sans-serif; }
  header { display: flex; align-items: baseline; justify-content: space-between; padding: 2.5rem 3rem 1.5rem; max-width: 1200px; margin: 0 auto; }
  h1 { font-size: 2rem; font-weight: 500; letter-spacing: -0.02em; margin: 0; }
  h1 .year { color: var(--muted); margin-left: 0.5rem; font-weight: 400; }
  nav { display: flex; gap: 0.75rem; align-items: center; }
  nav a, nav button { color: var(--muted); text-decoration: none; font-size: 0.9rem; padding: 0.35rem 0.75rem; border: 1px solid var(--line); background: transparent; border-radius: 4px; cursor: pointer; font: inherit; }
  nav a:hover, nav button:hover { color: var(--fg); border-color: var(--muted); }
  nav a.today-btn { color: var(--accent); border-color: var(--accent); }
  main { max-width: 1200px; margin: 0 auto; padding: 0 3rem 3rem; }
  .grid { display: grid; grid-template-columns: repeat(7, 1fr); border-top: 1px solid var(--line); border-left: 1px solid var(--line); }
  .grid .weekday { padding: 0.5rem 0.75rem; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); border-right: 1px solid var(--line); border-bottom: 1px solid var(--line); background: transparent; }
  .day { min-height: 110px; padding: 0.5rem 0.6rem; border-right: 1px solid var(--line); border-bottom: 1px solid var(--line); cursor: pointer; position: relative; }
  .day:hover { background: var(--event-bg); }
  .day.other-month { color: var(--muted); background: transparent; }
  .day.other-month .day-num { opacity: 0.4; }
  .day.today { background: var(--today); }
  .day-num { font-size: 0.85rem; font-variant-numeric: tabular-nums; margin-bottom: 0.25rem; }
  .day.today .day-num { color: var(--accent); font-weight: 600; }
  .event { display: block; font-size: 0.75rem; padding: 0.15rem 0.35rem; margin: 0.15rem 0; background: var(--event-bg); color: var(--event); border-left: 2px solid var(--accent); border-radius: 2px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
  .event.instance { cursor: pointer; }
  .event .time { color: var(--muted); font-variant-numeric: tabular-nums; margin-right: 0.35rem; }
  footer { text-align: center; color: var(--muted); font-size: 0.75rem; padding: 1.5rem; }
  /* Modal */
  .modal-bg { position: fixed; inset: 0; background: rgba(0,0,0,0.5); display: none; align-items: center; justify-content: center; z-index: 10; }
  .modal-bg.open { display: flex; }
  .modal { background: var(--bg); color: var(--fg); border: 1px solid var(--line); border-radius: 6px; padding: 1.5rem; width: min(440px, 92vw); max-height: 88vh; overflow: auto; }
  .modal h2 { margin: 0 0 1rem; font-size: 1.15rem; font-weight: 500; letter-spacing: -0.01em; }
  .modal label { display: block; font-size: 0.75rem; color: var(--muted); margin: 0.75rem 0 0.25rem; text-transform: uppercase; letter-spacing: 0.06em; }
  .modal input, .modal textarea { width: 100%; padding: 0.5rem; border: 1px solid var(--line); background: transparent; color: var(--fg); border-radius: 4px; font: inherit; }
  .modal textarea { min-height: 4rem; resize: vertical; }
  .modal .actions { display: flex; gap: 0.5rem; justify-content: flex-end; margin-top: 1.25rem; }
  .modal button { padding: 0.45rem 0.9rem; border: 1px solid var(--line); background: transparent; color: var(--fg); border-radius: 4px; cursor: pointer; font: inherit; }
  .modal button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
  .modal button.danger { color: #dc2626; border-color: #dc2626; }
  .modal .meta { color: var(--muted); font-size: 0.8rem; margin-top: 0.5rem; word-break: break-all; }
  @media (max-width: 640px) {
    header { padding: 1.25rem 1rem 1rem; flex-direction: column; align-items: stretch; gap: 0.75rem; }
    main { padding: 0 0.5rem 2rem; }
    .day { min-height: 72px; padding: 0.35rem; }
    .event { font-size: 0.68rem; }
  }
</style>
</head>
<body>
<header>
  <h1>{{ month_name }}<span class="year">{{ year }}</span></h1>
  <nav>
    <a href="/calendar/{{ prev_year }}/{{ '%02d' % prev_month }}" title="Previous month">←</a>
    <a class="today-btn" href="/calendar">Today</a>
    <a href="/calendar/{{ next_year }}/{{ '%02d' % next_month }}" title="Next month">→</a>
  </nav>
</header>
<main>
  <div class="grid">
    <div class="weekday">Sun</div><div class="weekday">Mon</div><div class="weekday">Tue</div><div class="weekday">Wed</div><div class="weekday">Thu</div><div class="weekday">Fri</div><div class="weekday">Sat</div>
    {% for day in days %}
      <div class="day{% if not day.in_month %} other-month{% endif %}{% if day.is_today %} today{% endif %}"
           data-date="{{ day.iso }}" onclick="openCreate('{{ day.iso }}')">
        <div class="day-num">{{ day.day }}</div>
        {% for e in day.events %}
          <span class="event instance" data-id="{{ e.id }}" onclick="event.stopPropagation(); openView({{ e.id }})">
            {% if not e.all_day %}<span class="time">{{ e.time }}</span>{% endif %}{{ e.title }}
          </span>
        {% endfor %}
      </div>
    {% endfor %}
  </div>
</main>
<footer>{{ event_count }} event{{ 's' if event_count != 1 else '' }} in view · <a href="/api" style="color:inherit">api</a></footer>

<div class="modal-bg" id="modal" onclick="if(event.target.id=='modal') closeModal()">
  <div class="modal">
    <h2 id="modal-title">Event</h2>
    <div id="modal-body"></div>
  </div>
</div>

<script>
const PERMS = ['Read','Write','Delete','Share'];
function el(t, a={}, ...c) { const n = document.createElement(t); for (const [k,v] of Object.entries(a)) { if (k==='onclick') n.onclick=v; else n.setAttribute(k,v); } for (const x of c) n.append(x); return n; }
function closeModal(){ document.getElementById('modal').classList.remove('open'); }
function openCreate(iso){
  const m = document.getElementById('modal');
  document.getElementById('modal-title').textContent = 'New event';
  const body = document.getElementById('modal-body');
  body.innerHTML = '';
  body.append(
    el('label',{for:'e-title'},'Title'),         el('input',{id:'e-title',type:'text',placeholder:'What?'}),
    el('label',{for:'e-dtstart'},'Start'),      el('input',{id:'e-dtstart',type:'datetime-local',value:iso+'T09:00'}),
    el('label',{for:'e-dtend'},'End (optional)'), el('input',{id:'e-dtend',type:'datetime-local'}),
    el('label',{for:'e-location'},'Location'),   el('input',{id:'e-location',type:'text'}),
    el('label',{for:'e-desc'},'Description'),    el('textarea',{id:'e-desc'}),
  );
  const actions = el('div',{class:'actions'},
    el('button',{onclick:closeModal},'Cancel'),
    el('button',{class:'primary',onclick:submitCreate},'Create'),
  );
  body.append(actions);
  m.classList.add('open');
  setTimeout(()=>document.getElementById('e-title').focus(), 50);
}
async function submitCreate(){
  const b = {
    title: document.getElementById('e-title').value.trim(),
    dtstart: document.getElementById('e-dtstart').value ? document.getElementById('e-dtstart').value+':00' : null,
    dtend: document.getElementById('e-dtend').value ? document.getElementById('e-dtend').value+':00' : null,
    location: document.getElementById('e-location').value || null,
    description: document.getElementById('e-desc').value || null,
  };
  if (!b.title || !b.dtstart) { alert('Title and start are required'); return; }
  const r = await fetch('/events', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(b)});
  if (!r.ok) { alert('Create failed: '+r.status+' '+await r.text()); return; }
  location.reload();
}
async function openView(id){
  const r = await fetch('/events/'+id);
  if (!r.ok) { alert('Load failed: '+r.status); return; }
  const e = await r.json();
  document.getElementById('modal-title').textContent = e.title;
  const body = document.getElementById('modal-body');
  body.innerHTML = '';
  const when = e.all_day ? `${e.dtstart.slice(0,10)} (all day)` : `${e.dtstart} → ${e.dtend || '—'}`;
  body.append(
    el('div',{class:'meta'}, when),
  );
  if (e.location) body.append(el('div',{class:'meta'}, '📍 '+e.location));
  if (e.description) body.append(el('div',{class:'meta',style:'white-space:pre-wrap'}, e.description));
  if (e.rrule) body.append(el('div',{class:'meta'}, 'RRULE: '+e.rrule));
  if (e.source) body.append(el('div',{class:'meta'}, 'source: '+e.source));
  body.append(el('div',{class:'meta'}, 'id '+e.id+' · created by '+e.created_by));
  const actions = el('div',{class:'actions'},
    el('button',{class:'danger',onclick:()=>del(id)},'Delete'),
    el('button',{onclick:closeModal},'Close'),
  );
  body.append(actions);
  document.getElementById('modal').classList.add('open');
}
async function del(id){
  if (!confirm('Delete this event?')) return;
  const r = await fetch('/events/'+id, {method:'DELETE'});
  if (!r.ok) { alert('Delete failed: '+r.status); return; }
  location.reload();
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
</script>
</body>
</html>
"""


@app.get("/calendar")
def calendar_today():
    now = datetime.now(timezone.utc)
    return redirect(f"/calendar/{now.year}/{now.month:02d}")


@app.get("/calendar/<int:year>/<int:month>")
def calendar_month(year, month):
    if not caller():
        return jsonify(error="unauthenticated"), 401
    if month < 1 or month > 12 or year < 1970 or year > 3000:
        return jsonify(error="invalid month/year"), 400

    # Compute the visible window: leading days from prev month + this month + trailing days
    # Sunday-first grid.
    first = datetime(year, month, 1, tzinfo=timezone.utc)
    # Sunday=6 in weekday(); we want the previous Sunday as the grid start.
    lead = (first.weekday() + 1) % 7
    grid_start = first - timedelta(days=lead)
    grid_end = grid_start + timedelta(days=42)  # always render 6 rows

    # Pull events in that window
    user = caller()
    with db_lock:
        rows = db.execute(
            """SELECT e.id, e.uid, e.title, e.description, e.location, e.dtstart,
                      e.dtend, e.all_day, e.rrule, e.source, e.created_by,
                      e.created_at, e.updated_at
               FROM event e JOIN acl a ON a.event_id = e.id
               WHERE a.username = ? AND a.permission = 'Read'
               ORDER BY e.dtstart""",
            [user],
        ).fetchall()

    # Bucket instances by local date (using event's own tz — the dtstart column
    # is TIMESTAMPTZ; render in its stored offset).
    buckets: dict = {}
    total = 0
    for row in rows:
        for inst in expand(row, grid_start, grid_end):
            dt = datetime.fromisoformat(inst["dtstart"])
            key = dt.date().isoformat()
            buckets.setdefault(key, []).append(
                {
                    "id": inst["id"],
                    "title": inst["title"],
                    "all_day": inst["all_day"],
                    "time": dt.strftime("%H:%M"),
                }
            )
            total += 1

    today = datetime.now(timezone.utc).date()
    days = []
    for i in range(42):
        d = (grid_start + timedelta(days=i)).date()
        days.append(
            {
                "iso": d.isoformat(),
                "day": d.day,
                "in_month": d.month == month,
                "is_today": d == today,
                "events": sorted(buckets.get(d.isoformat(), []), key=lambda e: (not e["all_day"], e["time"])),
            }
        )

    prev_month = 12 if month == 1 else month - 1
    prev_year = year - 1 if month == 1 else year
    next_month = 1 if month == 12 else month + 1
    next_year = year + 1 if month == 12 else year

    return render_template_string(
        CALENDAR_TEMPLATE,
        year=year,
        month=month,
        month_name=_calendar.month_name[month],
        days=days,
        event_count=total,
        prev_year=prev_year,
        prev_month=prev_month,
        next_year=next_year,
        next_month=next_month,
    )


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/whoami")
def whoami():
    return jsonify(user=caller(), groups=caller_groups())


@app.post("/events")
def create_event():
    user = caller()
    if not user:
        return jsonify(error="unauthenticated"), 401
    if CREATE_GROUP not in caller_groups():
        return jsonify(error=f"requires group {CREATE_GROUP}"), 403
    body = request.get_json(silent=True) or {}
    title = str(body.get("title", "")).strip()
    if not title:
        return jsonify(error="title is required"), 400
    try:
        dtstart = parse_dt(body.get("dtstart"))
        dtend = parse_dt(body.get("dtend"))
    except (ValueError, TypeError) as e:
        return jsonify(error=f"invalid datetime: {e}"), 400
    if dtstart is None:
        return jsonify(error="dtstart is required"), 400
    if dtend and dtend < dtstart:
        return jsonify(error="dtend must be >= dtstart"), 400

    uid = str(body.get("uid") or f"{uuid.uuid4()}@gluck-calendar")

    with db_lock:
        # idempotency-by-uid: if a caller re-posts the same uid, return existing
        existing = db.execute("SELECT id FROM event WHERE uid = ?", [uid]).fetchone()
        if existing:
            eid = existing[0]
            if has_perm(eid, user, "Read"):
                return jsonify(as_dict(event_row(eid))), 200
            return jsonify(error="uid already in use"), 409

        event_id = (
            db.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM event").fetchone()[0]
        )
        db.execute(
            """INSERT INTO event (id, uid, title, description, location,
                                   dtstart, dtend, all_day, rrule, source, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                event_id,
                uid,
                title,
                body.get("description") or None,
                body.get("location") or None,
                dtstart,
                dtend,
                bool(body.get("all_day", False)),
                body.get("rrule") or None,
                body.get("source") or None,
                user,
            ],
        )
        for perm in PERMISSIONS:
            db.execute(
                "INSERT INTO acl (event_id, username, permission) VALUES (?, ?, ?)",
                [event_id, user, perm],
            )
        return jsonify(as_dict(event_row(event_id))), 201


@app.get("/events")
def list_events():
    user = caller()
    if not user:
        return jsonify(error="unauthenticated"), 401
    now = datetime.now(timezone.utc)
    try:
        window_from = parse_dt(request.args.get("from")) or (
            now - timedelta(days=DEFAULT_WINDOW_DAYS)
        )
        window_to = parse_dt(request.args.get("to")) or (
            now + timedelta(days=DEFAULT_WINDOW_DAYS)
        )
    except ValueError as e:
        return jsonify(error=f"invalid window: {e}"), 400
    if window_to < window_from:
        return jsonify(error="to must be >= from"), 400

    with db_lock:
        rows = db.execute(
            """SELECT e.id, e.uid, e.title, e.description, e.location, e.dtstart,
                      e.dtend, e.all_day, e.rrule, e.source, e.created_by,
                      e.created_at, e.updated_at
               FROM event e JOIN acl a ON a.event_id = e.id
               WHERE a.username = ? AND a.permission = 'Read'
               ORDER BY e.dtstart""",
            [user],
        ).fetchall()

    instances = []
    for row in rows:
        for inst in expand(row, window_from, window_to):
            instances.append(inst)
    instances.sort(key=lambda i: i["dtstart"])
    return jsonify(instances)


@app.get("/events/<int:event_id>")
def get_event(event_id):
    with db_lock:
        denied = gate(event_id, "Read")
        if denied:
            return denied
        return jsonify(as_dict(event_row(event_id)))


@app.put("/events/<int:event_id>")
def update_event(event_id):
    with db_lock:
        denied = gate(event_id, "Write")
        if denied:
            return denied
        body = request.get_json(silent=True) or {}
        current = event_row(event_id)
        try:
            dtstart = parse_dt(body["dtstart"]) if "dtstart" in body else current[5]
            dtend = parse_dt(body["dtend"]) if "dtend" in body else current[6]
        except (ValueError, TypeError) as e:
            return jsonify(error=f"invalid datetime: {e}"), 400
        title = body.get("title", current[2])
        if not str(title).strip():
            return jsonify(error="title must not be empty"), 400
        if dtend and dtstart and dtend < dtstart:
            return jsonify(error="dtend must be >= dtstart"), 400
        db.execute(
            """UPDATE event SET title = ?, description = ?, location = ?,
                                dtstart = ?, dtend = ?, all_day = ?, rrule = ?,
                                source = ?, updated_at = now()
               WHERE id = ?""",
            [
                title,
                body.get("description", current[3]),
                body.get("location", current[4]),
                dtstart,
                dtend,
                bool(body.get("all_day", current[7])),
                body.get("rrule", current[8]),
                body.get("source", current[9]),
                event_id,
            ],
        )
        return jsonify(as_dict(event_row(event_id)))


@app.delete("/events/<int:event_id>")
def delete_event(event_id):
    with db_lock:
        denied = gate(event_id, "Delete")
        if denied:
            return denied
        db.execute("DELETE FROM acl WHERE event_id = ?", [event_id])
        db.execute("DELETE FROM event WHERE id = ?", [event_id])
        return jsonify(deleted=event_id)


@app.post("/events/<int:event_id>/share")
def share_event(event_id):
    with db_lock:
        denied = gate(event_id, "Share")
        if denied:
            return denied
        body = request.get_json(silent=True) or {}
        grantee = str(body.get("username", "")).strip()
        permissions = body.get("permissions", [])
        if not grantee:
            return jsonify(error="username is required"), 400
        if (
            not isinstance(permissions, list)
            or not permissions
            or any(p not in PERMISSIONS for p in permissions)
        ):
            return (
                jsonify(error=f"permissions must be a non-empty subset of {PERMISSIONS}"),
                400,
            )
        for perm in permissions:
            db.execute(
                """INSERT INTO acl (event_id, username, permission)
                   SELECT ?, ?, ?
                   WHERE NOT EXISTS (
                     SELECT 1 FROM acl
                     WHERE event_id = ? AND username = ? AND permission = ?
                   )""",
                [event_id, grantee, perm, event_id, grantee, perm],
            )
        return jsonify(event_id=event_id, username=grantee, permissions=permissions)


if __name__ == "__main__":
    from waitress import serve

    serve(app, host="127.0.0.1", port=PORT, threads=4)
