"""Layout for the month grid and the day docket. See docs/ui.md."""

from datetime import date, datetime, timedelta

LANE_CAP = 4
CHIP_CAP_PLAIN = 3
CHIP_CAP_BANDED = 2
DOCKET_FLOOR_HOUR = 7
DOCKET_MIN_SPAN = 8


def _as_date(value):
    return value.date() if isinstance(value, datetime) else value


def classify(inst, tz):
    """Split an instance into ('band' | 'timed', payload)."""
    start = inst["start"].astimezone(tz)
    end = inst["end"].astimezone(tz) if inst["end"] else start
    first = start.date()
    last = end.date()
    if end > start and end.time() == datetime.min.time():
        last = (end - timedelta(microseconds=1)).date()
    if inst["all_day"] or last > first:
        return "band", (first, last)
    return "timed", (start, end)


def week_bands(segments, week_start):
    """Lane assignment for segments clipped to one week. See docs/ui.md § Layout."""
    week_end = week_start + timedelta(days=6)
    clipped = []
    for first, last, payload in segments:
        if last < week_start or first > week_end:
            continue
        left = max(first, week_start)
        right = min(last, week_end)
        clipped.append(
            {
                "col": (left - week_start).days,
                "span": (right - left).days + 1,
                "continues_before": first < week_start,
                "continues_after": last > week_end,
                "payload": payload,
                "_sort": (first, -(last - first).days),
            }
        )
    clipped.sort(key=lambda s: s["_sort"])

    lanes = []
    rows, hidden = [], 0
    for seg in clipped:
        cols = set(range(seg["col"], seg["col"] + seg["span"]))
        for index, taken in enumerate(lanes):
            if not (taken & cols):
                taken |= cols
                seg["lane"] = index
                break
        else:
            if len(lanes) >= LANE_CAP:
                hidden += 1
                continue
            lanes.append(set(cols))
            seg["lane"] = len(lanes) - 1
        del seg["_sort"]
        rows.append(seg)
    return rows, len(lanes), hidden


def stack(events):
    """Side-by-side lanes for concurrent events, as fractional left/width."""
    ordered = sorted(events, key=lambda e: (e["start"], -(e["end"] - e["start"])))
    placed = []
    cluster, cluster_end = [], None
    for ev in ordered:
        if cluster_end is not None and ev["start"] >= cluster_end:
            placed.extend(_lay(cluster))
            cluster, cluster_end = [], None
        cluster.append(ev)
        cluster_end = ev["end"] if cluster_end is None else max(cluster_end, ev["end"])
    placed.extend(_lay(cluster))
    return placed


def _lay(cluster):
    if not cluster:
        return []
    columns = []
    for ev in cluster:
        for col in columns:
            if col[-1]["end"] <= ev["start"]:
                col.append(ev)
                break
        else:
            columns.append([ev])
    total = len(columns)
    out = []
    for index, col in enumerate(columns):
        for ev in col:
            span = 1
            while index + span < total and _free(columns[index + span], ev):
                span += 1
            out.append(dict(ev, left=index / total, width=span / total))
    return out


def _free(column, ev):
    return all(other["end"] <= ev["start"] or other["start"] >= ev["end"] for other in column)


def docket_window(timed, now=None):
    """Hour bounds for the day axis."""
    lo, hi = DOCKET_FLOOR_HOUR, DOCKET_FLOOR_HOUR + DOCKET_MIN_SPAN
    for ev in timed:
        lo = min(lo, ev["start"].hour)
        end = ev["end"]
        hi = max(hi, end.hour + (1 if end.minute or end.second else 0))
    if now is not None:
        lo, hi = min(lo, now.hour), max(hi, now.hour + 1)
    return max(0, lo), min(24, max(hi, lo + DOCKET_MIN_SPAN))


def fraction(moment, day, lo, hi):
    """Where `moment` sits in the axis window, as 0..1."""
    minutes = (moment - datetime.combine(day, datetime.min.time(), moment.tzinfo)).total_seconds() / 60
    span = (hi - lo) * 60
    return (minutes - lo * 60) / span


def month_grid(year, month, tz, today):
    """The dates of a Sunday-first month page, as week rows."""
    first = date(year, month, 1)
    lead = (first.weekday() + 1) % 7
    start = first - timedelta(days=lead)
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    rows = -(-((last - start).days + 1) // 7)
    return [[start + timedelta(days=w * 7 + d) for d in range(7)] for w in range(rows)]
