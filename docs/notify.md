---
name: calendar-reminders
description: How gluck-calendar delivers reminders to Telegram through herald, and why the notifier is a thread rather than a systemd timer. Read before changing calendar/notify.py, adding a trigger, debugging a missing or duplicated reminder, or wondering why nothing on spain shows up in systemctl list-timers.
---

# Reminders

Three triggers, delivered to Telegram through herald.

| trigger | when | subject |
|---|---|---|
| `digest-today` | 08:00 local | everything on today |
| `digest-tomorrow` | 22:00 local | everything on tomorrow |
| `lead` | 60 minutes before a start time | that one occurrence |

All-day events appear in both digests and never raise a lead warning, because
they have no start time to be an hour before.

## The storage fact that shapes everything

**DuckDB refuses a second process, even read-only, while another holds the
file.** gluck-calendar's Flask app holds it open for the life of the unit, so
nothing else on the box can open `calendar.duckdb` at all:

```
writer holds the file (as the Flask app does)
  read_write: REFUSED -> IOException: Could not set lock on file ... Conflicting lock is held
  read_only:  REFUSED -> IOException: Could not set lock on file ... Conflicting lock is held
```

This is a fact about gluck-calendar's storage, not about reminders. Any future
work that wants this data from outside the web process has three options and
only three: run inside the process, go through the HTTP API, or move off
DuckDB. A `mkScheduledJob` oneshot touching the database directly cannot work
and will fail on every fire.

That is why the notifier is a thread inside the Flask process, started from
`__main__`, and why there is **no timer in `systemctl list-timers`**. Looking
for one and finding nothing is expected.

## Observability, since there is no timer

`GET /notify/status` reports the last tick, whether the thread is alive, counts
by kind and status, the ten most recent rows, and how many are unhealthy. It is
reachable with an Authelia session or a bearer token, so an aria can check it.

Every tick also writes a heartbeat row and a journald line. The tick loop body
catches everything, so no single failure can kill the loop.

## The ledger

One table, `reminder_sent`, in the database the calendar already owns.

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
waitress runs one process with four threads and a single ticker, so nothing
races; if anyone ever moves to multiple workers, the extra tickers degrade into
a harmless lost race instead of into N copies of every reminder.

**One clock.** Every ledger write takes the caller's `now` rather than calling
SQL `now()`. Mixing the two made the stale-claim comparison compare a simulated
clock against a wall clock, which was invisible in production and caught by a
test. Do not reintroduce `now()` in a statement.

The tick thread shares the app's single connection and takes `db_lock` exactly
as request handlers do. It claims under the lock, releases, sends, then takes
the lock again to record the result: holding it across a call to herald would
stall the web UI for the duration.

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

Reminder resolution equals the tick interval, `tickSeconds`, default 60. A lead
warning lands within one tick of T-60, never before it. Polling is deliberate:
per-event timers would be stateful and would fail silently whenever the run
that should have armed them was missed.

## The credential

One machine credential, write-only, and no calendar OIDC client at all: the
notifier reads its own database directly.

| piece | where |
|---|---|
| client | Authelia `kcal-notify`, confidential, `client_credentials`, `one_factor` |
| digest | inline in spain-flake `identity.nix`, on the `kmatrix` pattern |
| plaintext | sops `secrets/identity.yaml` |
| delivery | systemd `LoadCredential`, read from `$CREDENTIALS_DIRECTORY` |
| grant | herald `policy.kcal-notify = [ "say" ]` |
| destination | herald `routes.gluck`; the notifier names the route, never a chat id |

`one_factor` is not a weakening: `client_credentials` has no user, so there is
no second factor it could apply to.

If the secret is absent the notifier does not start and says so in the journal;
the web view is unaffected.

## Changing a trigger

`calendar/notify.py` holds the triggers; `tests/test_notify.py` runs in the Nix
build against a fake token endpoint and a fake herald, with a simulated clock.
Add the test first: every rule in the two tables above has one, including the
ones about lateness and about not resending after a restart.
