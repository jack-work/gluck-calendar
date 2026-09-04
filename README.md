# gluck-calendar

Authenticated calendar API served behind the
[kelliher-web](https://github.com/jack-work/kelliher-web) platform and gated
by Authelia forward-auth (password + 2FA). Same trust model as
[gluck-todo](https://github.com/jack-work/gluck-services): the service binds
to loopback, Caddy sets `Remote-User`/`Remote-Groups` from Authelia, and
`Authorization: Bearer <jwt>` is accepted for CLI/agent clients (validated
against Authelia's JWKS).

## Web view

A month grid with a day docket, at `/calendar/<year>/<month>?day=<iso>`.
Multi-day events span the days they occupy, concurrent events get side-by-side
lanes, and the whole page is keyboard-navigable.

The interface is a Jinja template built at Nix time: `zanni` components are
inlined into it and two checkers run over the result, so an effect cannot
vanish silently. Build it locally with `bin/build-ui`.

**Read [docs/ui.md](docs/ui.md) before changing anything visual.** It carries
the filter-placement budget, the palette convention and the layout rules, all
of which are enforced by `bin/cal-check` in the build.

## Data model

DuckDB, single-writer, per-item ACL.

- **event** — `id`, `uid` (iCal-style), `title`, `description`, `location`,
  `dtstart`, `dtend`, `all_day`, `rrule` (RFC 5545 RRULE text, optional),
  `source` (opaque tag, e.g. `gmail:<msgId>`), `created_by`, `created_at`,
  `updated_at`.
- **acl** — `(event_id, username, permission)` with permissions
  `Read`/`Write`/`Delete`/`Share`. Creator gets all four.

Recurring events are stored as one row with an RRULE; instances are
expanded on read within the requested window.

## Endpoints

```
POST   /events                       body: {title, dtstart, dtend?, ...}
GET    /events?from=ISO&to=ISO       list expanded instances in window
GET    /events/<id>                  raw event row
PUT    /events/<id>                  update fields
DELETE /events/<id>
POST   /events/<id>/share            body: {username, permissions: [...]}
GET    /health
```

Items you cannot Read return **404** (no existence leak). Creating an
event requires the `calendar-create` group.

## Deployment

```nix
inputs.gluck-calendar.url = "github:jack-work/gluck-calendar";

imports = [ inputs.gluck-calendar.nixosModules.default ];
services.gluck-calendar.enable = true;
services.gluck-calendar.timeZone = "America/New_York";
```

`timeZone` decides which civil day an instance lands on in the web view. The
API is unaffected; it stores and returns `TIMESTAMPTZ`.

Registers `cal.kelliher.info` (`requireAuth = true`) into the platform.
