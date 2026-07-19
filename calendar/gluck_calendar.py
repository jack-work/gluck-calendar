"""gluck-calendar — CRUD over a DuckDB-backed event table with per-item ACLs.

Mirrors the gluck-todo design:

- Loopback bind; Caddy sets Remote-User / Remote-Groups from Authelia's
  forward-auth response. Client-supplied Remote-* headers are stripped by
  Caddy before the request arrives.
- If Authorization: Bearer <jwt> is present, validate against Authelia's
  JWKS and stamp Remote-User / Remote-Groups from preferred_username /
  groups claims. Same bearer-bypass shape as gluck-todo.
- Creating events requires the ``gluck-calendar-create`` group.
- Per-event ACL rows govern Read/Write/Delete/Share; creator gets all four.

Events store an optional RFC 5545 RRULE; `GET /events?from=&to=` expands
recurring events into concrete instances inside the requested window.
"""

import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import duckdb
import jwt
import requests
from dateutil.rrule import rrulestr
from flask import Flask, jsonify, request
from jwt import PyJWKClient

DB_PATH = os.environ.get("GLUCK_CALENDAR_DB", "/var/lib/gluck-calendar/calendar.duckdb")
PORT = int(os.environ.get("PORT", "9094"))

OIDC_ISSUER = os.environ.get("GLUCK_CALENDAR_OIDC_ISSUER", "https://auth.kelliher.info")
OIDC_JWKS_URL = os.environ.get(
    "GLUCK_CALENDAR_OIDC_JWKS_URL", "http://127.0.0.1:9091/jwks.json"
)
OIDC_CLIENT_ID = os.environ.get("GLUCK_CALENDAR_OIDC_CLIENT_ID", "gluck-calendar-cli")
OIDC_USERINFO_URL = os.environ.get(
    "GLUCK_CALENDAR_OIDC_USERINFO_URL", "http://127.0.0.1:9091/api/oidc/userinfo"
)

CREATE_GROUP = "gluck-calendar-create"
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

    if claims.get("client_id") != OIDC_CLIENT_ID:
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
