"""Bearer path tests: who may call, and what a token must carry.

Authelia omits `sub` from a client_credentials token in 4.39.12 and includes it
in 4.39.22, so the middleware must not depend on it for a machine caller. It
must still demand one from a person.

Run: python tests/test_bearer.py
"""

import http.server
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

# In the Nix build the module and its built template live in $out together;
# in a checkout they live in calendar/.
sys.path.insert(0, os.environ.get(
    "GLUCK_CALENDAR_MODULE",
    os.path.join(os.path.dirname(__file__), "..", "calendar")))

import jwt  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "test-key"
ISSUER = "https://auth.test"


def jwks_body():
    pub = KEY.public_key().public_numbers()

    def b64(n):
        import base64
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return {"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": KID,
                      "n": b64(pub.n), "e": b64(pub.e)}]}


class JWKS(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        raw = json.dumps(jwks_body()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def mint(**claims):
    now = datetime.now(timezone.utc)
    body = {"iss": ISSUER, "iat": now, "exp": now + timedelta(minutes=10),
            "nbf": now, "aud": []}
    body.update(claims)
    pem = KEY.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    return jwt.encode(body, pem, algorithm="RS256", headers={"kid": KID})


CHECKS = []


def check(name):
    def wrap(fn):
        CHECKS.append((name, fn))
        return fn
    return wrap


srv = http.server.HTTPServer(("127.0.0.1", 0), JWKS)
threading.Thread(target=srv.serve_forever, daemon=True).start()
JWKS_URL = f"http://127.0.0.1:{srv.server_address[1]}/jwks.json"

os.environ.update(
    GLUCK_CALENDAR_DB=tempfile.mkdtemp() + "/t.duckdb",
    GLUCK_CALENDAR_OIDC_ISSUER=ISSUER,
    GLUCK_CALENDAR_OIDC_JWKS_URL=JWKS_URL,
    GLUCK_CALENDAR_SERVICE_CLIENTS=json.dumps({"kcal-notify": "gluck"}),
    GLUCK_CALENDAR_OIDC_CLIENT_IDS="kcal",
    GLUCK_CALENDAR_TZ="America/New_York",
)

import gluck_calendar as gc  # noqa: E402

gc.app.config["TESTING"] = True
client = gc.app.test_client()


def call(token, method="GET", path="/whoami"):
    return client.open(path, method=method,
                       headers={"Authorization": "Bearer " + token})


@check("a service client with no sub is accepted: Authelia 4.39.12 omits it")
def _():
    r = call(mint(client_id="kcal-notify"))
    assert r.status_code == 200, (r.status_code, r.get_data(as_text=True))
    assert r.get_json()["user"] == "gluck", r.get_json()


@check("a service client with a sub is accepted too: 4.39.22 includes it")
def _():
    r = call(mint(client_id="kcal-notify", sub="kcal-notify"))
    assert r.status_code == 200, (r.status_code, r.get_data(as_text=True))
    assert r.get_json()["user"] == "gluck", r.get_json()


@check("a service client is read-only, refused before routing")
def _():
    r = call(mint(client_id="kcal-notify"), method="POST", path="/events")
    assert r.status_code == 403, (r.status_code, r.get_data(as_text=True))
    assert "read-only" in r.get_json()["error"], r.get_json()


@check("a service client reads events, which is the whole point")
def _():
    r = call(mint(client_id="kcal-notify"), path="/events")
    assert r.status_code == 200, (r.status_code, r.get_data(as_text=True))
    assert isinstance(r.get_json(), list), r.get_json()


@check("a person's token still must carry a sub")
def _():
    r = call(mint(client_id="kcal"))
    assert r.status_code == 401, (r.status_code, r.get_data(as_text=True))
    assert "sub" in r.get_json()["error"], r.get_json()


@check("an unknown client is refused whatever it carries")
def _():
    r = call(mint(client_id="stranger", sub="stranger"))
    assert r.status_code == 401, (r.status_code, r.get_data(as_text=True))


@check("a token with no client_id is refused")
def _():
    r = call(mint(sub="someone"))
    assert r.status_code == 401, (r.status_code, r.get_data(as_text=True))


@check("a token from another issuer is refused")
def _():
    now = datetime.now(timezone.utc)
    pem = KEY.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    bad = jwt.encode({"iss": "https://evil.test", "iat": now,
                      "exp": now + timedelta(minutes=10),
                      "client_id": "kcal-notify"}, pem,
                     algorithm="RS256", headers={"kid": KID})
    r = call(bad)
    assert r.status_code == 401, (r.status_code, r.get_data(as_text=True))


@check("an expired token is refused")
def _():
    now = datetime.now(timezone.utc)
    r = call(mint(client_id="kcal-notify", iat=now - timedelta(hours=2),
                  exp=now - timedelta(hours=1)))
    assert r.status_code == 401, (r.status_code, r.get_data(as_text=True))


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
    print(f"[bearer] {len(CHECKS) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
