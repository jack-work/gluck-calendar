---
name: calendar-reminders
description: How kcal-notify delivers calendar reminders to Telegram through herald, and why it is an independent unit reading the calendar over HTTP rather than opening its database. Read before changing notify/reminders.py, adding a trigger, debugging a missing or duplicated reminder, or wiring another service to gluck-calendar's data.
---

# kcal-notify

Three triggers, delivered to Telegram through herald.

| trigger | when | subject |
|---|---|---|
| `digest-today` | 08:00 local | everything on today |
| `digest-tomorrow` | 22:00 local | everything on tomorrow |
| `lead` | 60 minutes before a start time | that one occurrence |

All-day events appear in both digests and never raise a lead warning, because
they have no start time to be an hour before.

## The shape

`kcal-notify` is its own systemd unit with its own state directory. It reads
the calendar over HTTP and says to herald over HTTP, and shares nothing with
either process.

```
kcal-notify.timer ──▶ kcal-notify.service
                        │  GET  /events      gluck-calendar :9094  (read-only service client)
                        │  POST /v1/say      gluck-herald   :9098  (role: say)
                        └─ ledger            /var/lib/kcal-notify/reminders.duckdb
```

## The storage fact that made it this shape

**DuckDB refuses a second process, even read-only, while another holds the
file.** gluck-calendar's Flask app holds `calendar.duckdb` open for the life of
the unit, so nothing else on the box can open it at all:

```
writer holds the file (as the Flask app does)
  read_write: REFUSED -> IOException: Could not set lock on file ... Conflicting lock is held
  read_only:  REFUSED -> IOException: Could not set lock on file ... Conflicting lock is held
```

This is a fact about gluck-calendar's storage, not about reminders. Any future
work that wants that data from outside the web process has three options and
only three: run inside the process, go through the HTTP API, or move off
DuckDB. **This service takes the second.** A unit that opened
`calendar.duckdb` directly would fail on every fire.

## Observability

| want | run |
|---|---|
| is it firing | `systemctl list-timers kcal-notify.timer` |
| what did it do | `journalctl -u kcal-notify` |
| what does the ledger say | `kcal-notify status` (JSON: heartbeat, counts, recent, unhealthy) |

Each pass writes a heartbeat and one journald line. A pass that raises exits
non-zero, so a failing timer is visible to systemd rather than silent.

## The ledger

One table, `reminder_sent`, in this service's own database.

| column | carries |
|---|---|
| `kind` | which trigger |
| `occurrence_key` | what it is about |
| `recipient` | the herald route |
| `status` | `pending` `sent` `failed` `empty` `skipped` `gave-up` |
| `attempts` | retries so far |

`UNIQUE (kind, occurrence_key, recipient)`.

`occurrence_key` is the event **occurrence**, never the event id: for a lead it
is `uid|<occurrence start iso>`, so this Tuesday's standup and next Tuesday's
are different rows. For a digest it is the date the digest is about.

**The constraint is the concurrency control.** The claim is one statement,
`INSERT ... ON CONFLICT DO UPDATE ... WHERE ... RETURNING`, and a caller owns
the send only if a row comes back. There is no select-then-insert gap. Today
systemd will not start a second pass while one is running, so nothing races; if
a pass ever overruns its interval, the overlap degrades into a harmless lost
race instead of into two copies of every reminder.

**One clock.** Every ledger write takes the caller's `now` rather than calling
SQL `now()`. Mixing the two made the stale-claim comparison compare a simulated
clock against a wall clock, which was invisible in production and caught by a
test. Do not reintroduce `now()` in a statement.

Each pass claims, releases, sends, then records the result. The lock is never
held across a call to herald or to the calendar.

## Failure

Send fails, row goes `failed`, next tick retries. After `MAX_ATTEMPTS` (3) the
row is retired to `gave-up` with an error line in the journal. With a 60s tick
that is about three minutes of trying, then a loud stop. Never a silent drop.

A claim abandoned mid-send leaves a `pending` row, which is reclaimed after
`STALE_CLAIM` (10 minutes) so a crash between claim and send does not lose the
reminder.

An empty digest sends nothing and records `empty`, so it neither retries all
day nor teaches him to ignore the 08:00 message.

## Lateness

| situation | what happens |
|---|---|
| digest under 15 minutes late | sent normally |
| digest 15 minutes to 2 hours late | sent, marked late in the text |
| digest over 2 hours late | `skipped`, logged, not sent |
| 22:00 digest after midnight | never sent |

The last row is not the same rule as the one above it. After midnight the word
"tomorrow" names a different day, so a late send would be **wrong** rather than
merely stale. It falls out of the code for free: `today` has advanced, so the
scheduled time is the new day's 22:00 and `now` is before it.

## Resolution

Reminder resolution equals the timer interval, `services.kcal-notify.schedule`,
default `minutely`. A lead warning lands within one firing of T-60, never
before it. Polling is deliberate: per-event timers would be stateful and would
fail silently whenever the run that should have armed them was missed.

## The credential

One machine credential, write-only, and no calendar OIDC client at all: the
notifier reads its own database directly.

One identity, one token, two services: it reads the calendar and says to
herald with the same credential.

| piece | where |
|---|---|
| client | Authelia `kcal-notify`, confidential, `client_credentials`, `one_factor` |
| calendar access | `services.gluck-calendar.serviceClients.kcal-notify = "gluck"`, which is **read-only, enforced before routing**: any method but GET or HEAD is refused |
| digest | inline in spain-flake `identity.nix`, on the `kmatrix` pattern |
| plaintext | sops `secrets/identity.yaml` |
| delivery | systemd `LoadCredential`, read from `$CREDENTIALS_DIRECTORY` |
| grant | herald `policy.kcal-notify = [ "say" ]` |
| destination | herald `routes.gluck`; the notifier names the route, never a chat id |

Those last two entries predate this service. Aria `69e15f1a` ("deploy
kcal-notify: service identity end to end") added them to spain-flake and then
stopped before deploying, so they were already in place when the notifier was
written. They are load-bearing, not leftovers: do not remove them as
unexplained.

`one_factor` is not a weakening: `client_credentials` has no user, so there is
no second factor it could apply to.

If the secret is absent the unit exits non-zero and says so in the journal. The
calendar is unaffected either way: it does not know this service exists.

## Changing a trigger

`notify/reminders.py` holds the triggers; `tests/test_notify.py` runs in the Nix
build against a fake token endpoint and a fake herald, with a simulated clock.
Add the test first: every rule in the two tables above has one, including the
ones about lateness and about not resending after a restart.
