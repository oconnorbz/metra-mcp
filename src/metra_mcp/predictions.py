"""Reconcile GTFS-realtime stop predictions with the static schedule.

The realtime feed is not trusted blindly. Metra's TripUpdates feed has been
seen publishing a terminal-stop arrival about an hour after the stop before
it (2026-11-17, UP-N inbound: Clybourn 09:16:36 then Chicago OTC 10:27:06,
scheduled 11 minutes apart), while the same stop is predicted correctly once
it is the train's next stop. The epoch in the feed is simply wrong; nothing
in this server shifts it.

So every stop_time_update is compared with the static schedule here:

* the delay is always computed by us (predicted - scheduled) and is None when
  there is no schedule to compare against, never a made-up 0;
* a prediction that is far off schedule *and* inconsistent with the stop
  before it is flagged ``suspect`` and replaced by the previous stop's
  prediction plus the scheduled running time.

All time math is done in epoch seconds. A service day is anchored at "noon
minus 12h" in America/Chicago, as the GTFS spec requires, so DST-change days
and times past 24:00:00 come out right.
"""

import logging
import time as _time
from datetime import date, datetime, timedelta
from typing import Any

from .common import CHICAGO_TZ

logger = logging.getLogger(__name__)

# A prediction is suspect when it is this far from schedule ...
SUSPECT_DEVIATION_SEC = 45 * 60
# ... and its deviation jumps by this much relative to the previous stop,
# i.e. the predicted running time from the previous stop is this far from the
# scheduled running time. A genuinely late train fails only the first test.
SUSPECT_JUMP_SEC = 30 * 60
# Feed delay vs. our computed delay: disagreement worth a log line.
DELAY_MISMATCH_SEC = 120

_MISMATCH_LOG_INTERVAL_SEC = 600.0
_last_mismatch_log = float("-inf")


def format_chicago(epoch: float | None) -> str | None:
    """Epoch seconds -> 'YYYY-MM-DD HH:MM:SS TZ' in America/Chicago."""
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, tz=CHICAGO_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def parse_gtfs_time(hms: str | None) -> int | None:
    """GTFS 'HH:MM:SS' -> seconds since the start of the service day.

    Hours may exceed 23 (e.g. '24:32:00') for trips running past midnight.
    """
    parts = (hms or "").strip().split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = (int(p) for p in parts)
    except ValueError:
        return None
    if h < 0 or not (0 <= m < 60 and 0 <= s < 60):
        return None
    return h * 3600 + m * 60 + s


def parse_service_date(yyyymmdd: str | None) -> date | None:
    """GTFS 'YYYYMMDD' -> date, or None when absent or malformed."""
    text = (yyyymmdd or "").strip()
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None


def service_day_start(service_date: date) -> int:
    """Epoch of 'noon minus 12h' local time on the service date.

    This is midnight on ordinary days and differs from it by an hour on the
    two DST-change days, which is exactly why GTFS defines it this way.
    """
    noon = datetime(
        service_date.year, service_date.month, service_date.day, 12, tzinfo=CHICAGO_TZ
    )
    return int(noon.timestamp()) - 12 * 3600


def scheduled_epoch(service_date: date, hms: str | None) -> int | None:
    """Static GTFS stop time on a service date -> epoch seconds."""
    secs = parse_gtfs_time(hms)
    if secs is None:
        return None
    return service_day_start(service_date) + secs


def scheduled_datetime(service_date: date, hms: str | None) -> datetime | None:
    """Static GTFS stop time on a service date -> aware Chicago datetime."""
    epoch = scheduled_epoch(service_date, hms)
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=CHICAGO_TZ)


def infer_service_date(ref_epoch: float, hms: str | None) -> date:
    """Service date for a trip that carries no start_date.

    Picks the date that puts the scheduled time ``hms`` closest to a known
    realtime instant, so a 24:32:00 stop seen at 00:30 maps to yesterday.
    """
    local = datetime.fromtimestamp(ref_epoch, tz=CHICAGO_TZ).date()
    if parse_gtfs_time(hms) is None:
        return local
    candidates = [local + timedelta(days=d) for d in (-1, 0, 1)]
    return min(candidates, key=lambda d: abs(scheduled_epoch(d, hms) - ref_epoch))


def _match_static(
    static_rows: list[dict[str, str]], stop_sequence: int | None, stop_id: str
) -> dict[str, str] | None:
    """Static stop_times row for a realtime stop: by sequence, else stop_id."""
    by_stop = None
    for row in static_rows:
        if row.get("stop_id") != stop_id:
            continue
        if stop_sequence is not None and row.get("stop_sequence") == str(stop_sequence):
            return row
        if by_stop is None:
            by_stop = row
    return by_stop


def _resolve_event(
    feed_time: int | None,
    feed_delay: int | None,
    sched: int | None,
    prev: tuple[int, int] | None,
    skipped: bool,
) -> tuple[int | None, str | None, bool]:
    """One arrival or departure -> (predicted epoch, source, suspect).

    ``prev`` is the previous stop's (predicted, scheduled) epochs.
    """
    if feed_time:
        if sched is not None and prev is not None:
            deviation = feed_time - sched
            prev_deviation = prev[0] - prev[1]
            if (
                abs(deviation) > SUSPECT_DEVIATION_SEC
                and abs(deviation - prev_deviation) > SUSPECT_JUMP_SEC
            ):
                # Previous prediction + scheduled running time.
                return sched + prev_deviation, "derived", True
        return feed_time, "feed_time", False
    if feed_delay is not None and sched is not None:
        return sched + feed_delay, "feed_delay", False
    if sched is not None and not skipped:
        return sched, "scheduled", False
    return None, None, False


def resolve_stop_updates(
    raw_stops: list[dict[str, Any]],
    static_rows: list[dict[str, str]] | None,
    service_date: date | None,
    trip_id: str = "",
) -> list[dict[str, Any]]:
    """Turn raw stop_time_updates into the tool's stop_time_updates.

    Each raw stop is ``{"stop_sequence", "stop_id", "schedule_relationship",
    "arrival": {"time", "delay"} | None, "departure": {...} | None}`` with
    ``time``/``delay`` set to None when the feed omitted the field.

    Every stop, the terminal included, goes through the same path. Without
    static data (``static_rows``/``service_date`` None) the feed's own values
    are passed through and delays the feed did not send stay None.
    """
    out: list[dict[str, Any]] = []
    mismatches: list[str] = []
    prev: tuple[int, int] | None = None

    for raw in raw_stops:
        seq = raw.get("stop_sequence")
        stop_id = raw.get("stop_id") or ""
        rel = raw.get("schedule_relationship") or "SCHEDULED"
        row = None
        if static_rows and service_date is not None:
            row = _match_static(static_rows, seq, stop_id)

        su: dict[str, Any] = {"stop_sequence": seq, "stop_id": stop_id}
        sources: list[str | None] = []
        suspect = False
        stop_prev = prev
        next_prev = None

        for kind in ("arrival", "departure"):
            event = raw.get(kind)
            if event is None:
                continue
            feed_time = event.get("time") or None
            feed_delay = event.get("delay")
            sched = scheduled_epoch(service_date, row.get(f"{kind}_time")) if row else None

            predicted, source, bad = _resolve_event(
                feed_time, feed_delay, sched, stop_prev, rel == "SKIPPED"
            )
            if source == "scheduled" or predicted is None:
                # Nothing realtime to base a delay on.
                delay = None
            elif sched is not None:
                delay = predicted - sched
            else:
                delay = feed_delay

            if (
                feed_delay is not None
                and delay is not None
                and abs(feed_delay - delay) > DELAY_MISMATCH_SEC
            ):
                mismatches.append(
                    f"{trip_id or '?'} {stop_id} {kind}: feed {feed_delay}s, computed {delay}s"
                )

            su[f"{kind}_delay"] = delay
            su[f"{kind}_time"] = format_chicago(predicted)
            if sched is not None:
                su[f"scheduled_{kind}_time"] = format_chicago(sched)
            if bad:
                suspect = True
                su[f"feed_{kind}_time"] = format_chicago(feed_time)
            sources.append(source)
            if source in ("feed_time", "feed_delay", "derived") and sched is not None:
                next_prev = (predicted, sched)

        if next_prev is not None:
            prev = next_prev
        su["prediction_source"] = next((s for s in sources if s), None)
        su["suspect"] = suspect
        su["schedule_relationship"] = rel
        out.append(su)

    if mismatches:
        _log_mismatches(mismatches)
    return out


def _log_mismatches(mismatches: list[str]) -> None:
    """Warn about feed delays that disagree with ours, at most every 10 min."""
    global _last_mismatch_log
    now = _time.monotonic()
    if now - _last_mismatch_log < _MISMATCH_LOG_INTERVAL_SEC:
        return
    _last_mismatch_log = now
    logger.warning(
        "Feed delay differs from computed delay by >%ds on %d stop event(s), e.g. %s",
        DELAY_MISMATCH_SEC,
        len(mismatches),
        "; ".join(mismatches[:3]),
    )
