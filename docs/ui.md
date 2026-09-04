---
name: calendar-ui
description: How gluck-calendar's web view is built and what it may not do. Read before changing calendar/templates/month.html.in, adding a zanni effect, touching the month grid layout, or wondering why the interface is a template file instead of a Python string.
---

# The calendar's interface

The month view is a Jinja template built at Nix time, not a string in Python.
`calendar/templates/month.html.in` is the source; `templates/month.html` in the
derivation is the built artefact. Two checkers run over the built file and the
build fails if either does.

## Where the defs come from

`filter: url(#id)` resolves only inside the same document. An external
reference (`sprite.svg#id`) renders pixel-identical to no filter at all in
Chrome. So the SVG filter defs must be in the served HTML.

gluck-calendar is not a static site, so `zanni.lib.mkBoiledSite` does not
apply. Two routes were available:

| route | cost |
|---|---|
| **bake at build time** (chosen) | one derivation, defs frozen in the store, `zanni-check` runs in the build, template cached by Jinja |
| inline at render time | defs re-read or re-spliced per request, and the guard has to run somewhere the build cannot see |

`zanni-inline` is text substitution on a marker, so Jinja braces pass through
untouched. Nothing is interpolated into the page as a variable, so autoescaping
never sees the injected block and no `|safe` is needed anywhere.

Startup refuses to serve if the four `<!-- zanni:NAME begin` markers are absent
from the built template. A UI whose effect vanished silently is worse than none.

## The two checkers

| checker | owns |
|---|---|
| `zanni-check` (from zanni) | the components: defs present, classes worn, variants in band, no SMIL, no dangling selector list |
| `bin/cal-check` (here) | the calendar's own laws, below |

Run both locally with `bin/build-ui`. Nix runs both in `installPhase`.

`zanni-check` asserts `body` is still its own rule. This template has one. If a
future edit removes it the check fails for a reason that has nothing to do with
the calendar; that assertion is overfitted to figar.org.

## Filter placement

A filtered subtree is one raster unit and everything inside re-rasterises
together on any change. The cost tracks **area**, not element count. Measured
by The Zanni in Chrome 145, 5s windows:

| filtered | raster |
|---|---|
| 1 element | 11 ms |
| 42 day numbers | 396 ms |
| 126 numbers and chips | 1928 ms |
| the grid container, once | 3003 ms |

Boiling the wrapper is eight times worse than boiling 42 things inside it.

Four elements carry a filter, all of them small:

| element | class | why it is allowed |
|---|---|---|
| month name | `boil-text-squiggle` | one element, display size, never changes |
| weekday strip | `boil-ui-fine` | one thin static row |
| today's number | `boil-text-fine` | one element |
| docket day number | `boil-text-fine` | one element |

Nothing else may. `cal-check` enforces this by name for `.weeks .week .month
.cell .chips .bands .band .chip .slots .slot .axis .docket .page .frame`, and
caps the total boiled elements at six.

Two further consequences of a filter, both asserted:

- A filter is a containing block for `fixed` and `absolute` descendants. The
  band overlay is `position: absolute` inside `.week`, so `.week` must stay
  unfiltered. `<dialog>` lives at body level for the same reason.
- Match the variant to the type size. `squiggle` has a ~20px wavelength: lively
  on the 4rem month name, damage on a 2.4rem two-digit numeral. Hence `fine`
  on the docket number.

## Legibility

A calendar is read at a glance. Chrome, dates and headings may carry effects.
The things a person scans for may not: event chips, band titles and docket
slots are never boiled, and `cal-check` asserts it.

## The palette

gesso owns the primitives. The calendar derives its semantics into a `--cal-`
namespace and never overwrites a gesso variable globally.

| variable | from | used for |
|---|---|---|
| `--cal-ground` `--cal-ground-2` | `--paper` `--paper-2` | the ruled page |
| `--cal-ink` `--cal-muted` | `--ink` `--muted` | type |
| `--cal-rule` `--cal-rule-firm` | `--rule` | grid lines, two weights |
| `--cal-focus` | `--gold` | today, selection, the create action |
| `--cal-alarm` | `--oxblood` | the now line, delete |

Gold is for focus and is spent on at most two things per view. The now line is
oxblood so it does not compete.

The dark register remaps only `--cal-*`, under `html:root` so it outranks
gesso's own `:root` block, which `zanni-inline` injects after this stylesheet.
The docket overrides `--stage` locally because gesso's stage is nearly black
and phosphor multiplies over it.

## Layout

Six week rows only when the month needs six.

**Bands.** A multi-day or all-day event is drawn across the days it actually
occupies. Each `.week` is a positioned 7-column grid with an absolutely
positioned band overlay above it; bands take `grid-column: start / span n` and
a lane assigned by first-fit over occupied columns. Day cells reserve
`--lanes * --lane-h` above their chips so nothing collides. An event clipped by
the week edge gets a dashed border and a chevron on that side.

At most `LANE_CAP` lanes; the rest are counted, not dropped.

**Chips.** Timed single-day events only. Three per cell, two when the week
carries bands. Past the cap the last slot becomes `+N more`, which links to
that day's docket. The count is real.

**The docket.** All-day banners, then a time axis for the selected day. The
window is the data plus an hour, floored at a working day and at least
`DOCKET_MIN_SPAN` hours wide, widened to include now when the day is today.
Concurrent events get side-by-side lanes: cluster on transitive overlap, give
each event the first free column, then widen it rightwards while the columns it
would cover stay clear.

The now line is server-rendered and nudged by JS once a minute. It is outside
every filtered subtree, so moving it costs one composite.

## Time

The API stores and returns `TIMESTAMPTZ` and is unchanged. The web view renders
in `GLUCK_CALENDAR_TZ` (`services.gluck-calendar.timeZone`), which decides which
civil day an instance lands on. Before this the grid bucketed in UTC, so an
evening event showed on the following day.

The create form sends an offset computed by the browser for that specific
datetime, so a time entered across a DST boundary is stored correctly.

## Keyboard

| key | does |
|---|---|
| `t` | today |
| `p` `n` | previous, next month |
| arrows | move the day focus, no server trip |
| `Enter` | load that day's docket |
| `c` | new event on the focused day |
| `Esc` | close the dialog |

Day cells carry a roving tabindex: the selected one is `0`, the rest `-1`.
Arrow keys move focus only; navigation is an explicit `Enter`. Nothing in the
page builds DOM from an HTML string.

## Deploying

`gluck-calendar` is a flake input of spain-flake, so a UI change costs: commit
here, bump the lock there, deploy spain. See the estate's deploy rules; land it
as its own single-purpose watched window.
