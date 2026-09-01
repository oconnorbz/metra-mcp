"""GTFS static schedule data manager.

Downloads and parses the Metra GTFS static schedule zip file,
providing structured access to routes, stops, trips, and stop_times.
"""

import asyncio
import csv
import io
import logging
import os
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from .common import CHICAGO_TZ, get_ssl_context


def _chicago_now() -> datetime:
    return datetime.now(tz=CHICAGO_TZ)


def _chicago_today() -> date:
    return _chicago_now().date()


def _direction_label(direction_id: str) -> str:
    """Human-readable direction from Metra's raw GTFS direction_id.

    NOTE: Metra inverts the common GTFS convention. In Metra's trips.txt,
    direction_id="1" is *inbound* (toward Chicago, e.g. Chicago OTC/Union
    Station) and direction_id="0" is *outbound* (away from downtown). This is
    the opposite of the textbook GTFS guidance, so the raw flag alone reads as
    "backwards" unless you know the convention.
    """
    return {"1": "inbound", "0": "outbound"}.get((direction_id or "").strip(), "")


logger = logging.getLogger(__name__)

SCHEDULE_URL = "https://schedules.metrarail.com/gtfs/schedule.zip"
PUBLISHED_URL = "https://schedules.metrarail.com/gtfs/published.txt"

# Only the columns the query methods actually read. stop_times.txt is by far
# the largest table (~10^5 rows); dropping the unused columns (pickup_type,
# drop_off_type, shape_dist_traveled, notice, ...) roughly halves resident
# memory. None means "keep every column".
_KEEP_COLUMNS: dict[str, frozenset[str] | None] = {
    "routes.txt": None,
    "stops.txt": frozenset({"stop_id", "stop_name", "stop_lat", "stop_lon"}),
    "trips.txt": frozenset({"trip_id", "route_id", "service_id", "trip_headsign", "direction_id"}),
    "stop_times.txt": frozenset(
        {"trip_id", "stop_id", "arrival_time", "departure_time", "stop_sequence"}
    ),
    "calendar.txt": None,
    "calendar_dates.txt": None,
}


def default_cache_dir() -> Path:
    """Where the schedule zip is cached.

    Precedence: METRA_CACHE_DIR, then systemd's $STATE_DIRECTORY (set when the
    unit uses StateDirectory=, which is the only writable location under
    DynamicUser/ProtectSystem=strict), then ~/.cache/metra-mcp.
    """
    env = os.environ.get("METRA_CACHE_DIR")
    if env:
        return Path(env)
    state = os.environ.get("STATE_DIRECTORY")
    if state:
        # STATE_DIRECTORY may be a colon-separated list; use the first.
        return Path(state.split(":")[0]) / "cache"
    return Path.home() / ".cache" / "metra-mcp"


@dataclass(frozen=True)
class _Snapshot:
    """One fully parsed + indexed schedule.

    Built off the event loop in a worker thread and then swapped into
    GTFSData with a single attribute assignment, so readers only ever see a
    schedule whose tables and indexes agree with each other.
    """

    routes: list[dict[str, str]] = field(default_factory=list)
    stops: list[dict[str, str]] = field(default_factory=list)
    trips: list[dict[str, str]] = field(default_factory=list)
    calendar: list[dict[str, str]] = field(default_factory=list)
    calendar_dates: list[dict[str, str]] = field(default_factory=list)
    stop_times_count: int = 0
    stop_by_id: dict[str, dict[str, str]] = field(default_factory=dict)
    stop_times_by_trip: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    stop_times_by_stop: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    trips_by_route: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    trip_by_id: dict[str, dict[str, str]] = field(default_factory=dict)


_EMPTY = _Snapshot()


class GTFSData:
    """Manages GTFS static schedule data."""

    def __init__(self, cache_dir: Path | None = None):
        self.cache_dir = cache_dir or default_cache_dir()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # All schedule state lives in one immutable snapshot; see _Snapshot.
        self._snap: _Snapshot = _EMPTY
        self._loaded = False
        # Serializes downloads/parses so concurrent first-requests (and the
        # periodic refresh) don't all redownload at once.
        self._load_lock = asyncio.Lock()

    @property
    def loaded(self) -> bool:
        return self._loaded

    # Thin accessors so the query methods below read naturally.
    @property
    def _routes(self) -> list[dict[str, str]]:
        return self._snap.routes

    @property
    def _stops(self) -> list[dict[str, str]]:
        return self._snap.stops

    @property
    def _trips(self) -> list[dict[str, str]]:
        return self._snap.trips

    @property
    def _calendar(self) -> list[dict[str, str]]:
        return self._snap.calendar

    @property
    def _calendar_dates(self) -> list[dict[str, str]]:
        return self._snap.calendar_dates

    @property
    def _stop_by_id(self) -> dict[str, dict[str, str]]:
        return self._snap.stop_by_id

    @property
    def _stop_times_by_trip(self) -> dict[str, list[dict[str, str]]]:
        return self._snap.stop_times_by_trip

    @property
    def _stop_times_by_stop(self) -> dict[str, list[dict[str, str]]]:
        return self._snap.stop_times_by_stop

    @property
    def _trips_by_route(self) -> dict[str, list[dict[str, str]]]:
        return self._snap.trips_by_route

    @property
    def _trip_by_id(self) -> dict[str, dict[str, str]]:
        return self._snap.trip_by_id

    async def ensure_loaded(self) -> None:
        """Load schedule data, downloading if needed.

        Guarded by a lock with a double-check so concurrent first-requests
        (and the periodic refresh) collapse into a single download/parse.
        """
        if self._loaded:
            return
        async with self._load_lock:
            if self._loaded:
                return
            cache_file = self.cache_dir / "schedule.zip"
            needs_download = True
            if cache_file.exists():
                try:
                    remote_ts = await self._get_published_timestamp()
                    local_ts_file = self.cache_dir / "published.txt"
                    if local_ts_file.exists() and local_ts_file.read_text().strip() == remote_ts:
                        needs_download = False
                except Exception as e:
                    # If the publisher endpoint is down but we have a cached zip,
                    # use what we have rather than refusing to serve.
                    logger.warning("Failed to check schedule freshness: %s; using cached zip", e)
                    needs_download = False
            if needs_download:
                await self._download_schedule(cache_file)
            await self._load_snapshot(cache_file)
            logger.info(
                "GTFS data loaded: %d routes, %d stops, %d trips, %d stop_times",
                len(self._snap.routes),
                len(self._snap.stops),
                len(self._snap.trips),
                self._snap.stop_times_count,
            )

    async def _load_snapshot(self, cache_file: Path) -> None:
        """Parse + index off the event loop, then publish atomically.

        Parsing is seconds of CPU over ~10^5 stop_times rows, so it runs in a
        worker thread; the single assignment at the end is what makes a
        mid-refresh reader see either the old or the new schedule, never a
        mix. Caller must hold _load_lock.
        """
        snap = await asyncio.to_thread(self._parse_zip, cache_file)
        self._snap = snap
        self._loaded = True

    async def _get_published_timestamp(self) -> str:
        """Check when the static schedule was last published."""
        async with httpx.AsyncClient(timeout=10.0, verify=get_ssl_context()) as client:
            resp = await client.get(PUBLISHED_URL)
            resp.raise_for_status()
            return resp.text.strip()

    async def _download_schedule(self, dest: Path) -> None:
        """Download the GTFS schedule zip atomically (temp file + rename).

        We fetch zip + published.txt to temp paths, validate the zip can be
        opened, then rename both into place. If anything fails partway, the
        previous cache is left untouched so the next call retries cleanly.
        """
        logger.info("Downloading GTFS schedule from %s", SCHEDULE_URL)
        tmp_zip = dest.with_suffix(dest.suffix + ".tmp")
        tmp_ts = self.cache_dir / "published.txt.tmp"
        try:
            async with httpx.AsyncClient(timeout=60.0, verify=get_ssl_context()) as client:
                resp = await client.get(SCHEDULE_URL)
                resp.raise_for_status()
                await asyncio.to_thread(tmp_zip.write_bytes, resp.content)

            # Validate the download before committing (CRC over the whole
            # archive — CPU-bound, keep it off the event loop).
            def _validate() -> None:
                with zipfile.ZipFile(tmp_zip) as zf:
                    bad = zf.testzip()
                    if bad is not None:
                        raise RuntimeError(f"Downloaded GTFS zip is corrupt: {bad}")

            await asyncio.to_thread(_validate)
            remote_ts = await self._get_published_timestamp()
            tmp_ts.write_text(remote_ts)
            os.replace(tmp_zip, dest)
            os.replace(tmp_ts, self.cache_dir / "published.txt")
            logger.info("Schedule downloaded and cached")
        finally:
            for p in (tmp_zip, tmp_ts):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass

    def _parse_zip(self, zip_path: Path) -> _Snapshot:
        """Parse GTFS text files from the zip into a new snapshot (pure; no
        instance state is touched, so it is safe to run in a worker thread
        while requests keep reading the current snapshot)."""
        with zipfile.ZipFile(zip_path) as zf:
            routes = self._read_csv(zf, "routes.txt")
            stops = self._read_csv(zf, "stops.txt")
            trips = self._read_csv(zf, "trips.txt")
            stop_times = self._read_csv(zf, "stop_times.txt")
            calendar = self._read_csv(zf, "calendar.txt")
            calendar_dates = self._read_csv(zf, "calendar_dates.txt")
        return self._build_indexes(routes, stops, trips, stop_times, calendar, calendar_dates)

    @staticmethod
    def _build_indexes(
        routes: list[dict[str, str]],
        stops: list[dict[str, str]],
        trips: list[dict[str, str]],
        stop_times: list[dict[str, str]],
        calendar: list[dict[str, str]],
        calendar_dates: list[dict[str, str]],
    ) -> _Snapshot:
        """Precompute lookup indexes so per-query work avoids full scans.

        stop_times is the largest table; without these, get_schedule is
        O(trips × stop_times) and get_next_trains scans every row per call.
        """
        stop_by_id = {s["stop_id"]: s for s in stops if s.get("stop_id")}

        by_trip: dict[str, list[dict[str, str]]] = defaultdict(list)
        by_stop: dict[str, list[dict[str, str]]] = defaultdict(list)
        for st in stop_times:
            tid = st.get("trip_id")
            if tid:
                by_trip[tid].append(st)
            sid = st.get("stop_id")
            if sid:
                by_stop[sid].append(st)
        # Sort each trip's stop_times by sequence once, so get_schedule can
        # rely on order without re-sorting per call.
        for sts in by_trip.values():
            sts.sort(key=lambda x: int(x.get("stop_sequence", "0")))

        by_route: dict[str, list[dict[str, str]]] = defaultdict(list)
        trip_by_id: dict[str, dict[str, str]] = {}
        for t in trips:
            rid = t.get("route_id")
            if rid:
                by_route[rid].append(t)
            tid = t.get("trip_id")
            if tid:
                trip_by_id[tid] = t

        return _Snapshot(
            routes=routes,
            stops=stops,
            trips=trips,
            calendar=calendar,
            calendar_dates=calendar_dates,
            stop_times_count=len(stop_times),
            stop_by_id=stop_by_id,
            stop_times_by_trip=dict(by_trip),
            stop_times_by_stop=dict(by_stop),
            trips_by_route=dict(by_route),
            trip_by_id=trip_by_id,
        )

    @staticmethod
    def _read_csv(zf: zipfile.ZipFile, filename: str) -> list[dict[str, str]]:
        """Read a CSV file from the zip archive.

        Metra's GTFS files have leading spaces in column headers and values,
        so we strip all keys and values during parsing. Columns not listed in
        _KEEP_COLUMNS for the file are dropped to save memory.
        """
        keep = _KEEP_COLUMNS.get(filename)
        try:
            with zf.open(filename) as f:
                text = io.TextIOWrapper(f, encoding="utf-8-sig")
                reader = csv.DictReader(text)
                if reader.fieldnames is None:
                    return []
                # Strip header names once rather than per row.
                cols = [(raw, raw.strip()) for raw in reader.fieldnames]
                if keep is not None:
                    cols = [(raw, name) for raw, name in cols if name in keep]
                rows: list[dict[str, str]] = []
                for row in reader:
                    rows.append({name: (row.get(raw) or "").strip() for raw, name in cols})
                return rows
        except KeyError:
            logger.warning("File %s not found in schedule zip", filename)
            return []

    def get_routes(self) -> list[dict[str, str]]:
        """Get all routes."""
        return [
            {
                "route_id": r["route_id"],
                "route_short_name": r.get("route_short_name", ""),
                "route_long_name": r.get("route_long_name", ""),
                "route_color": r.get("route_color", ""),
            }
            for r in self._routes
        ]

    def get_stops(self, route_id: str | None = None) -> list[dict[str, str]]:
        """Get stops, optionally filtered by route."""
        if route_id is None:
            return [
                {
                    "stop_id": s["stop_id"],
                    "stop_name": s.get("stop_name", ""),
                    "stop_lat": s.get("stop_lat", ""),
                    "stop_lon": s.get("stop_lon", ""),
                }
                for s in self._stops
            ]
        # Find stops served by trips on this route, via indexes.
        trip_ids = {t["trip_id"] for t in self._trips_by_route.get(route_id, [])}
        stop_ids = {
            st["stop_id"]
            for tid in trip_ids
            for st in self._stop_times_by_trip.get(tid, [])
        }
        return [
            {
                "stop_id": s["stop_id"],
                "stop_name": s.get("stop_name", ""),
                "stop_lat": s.get("stop_lat", ""),
                "stop_lon": s.get("stop_lon", ""),
            }
            for s in self._stops
            if s["stop_id"] in stop_ids
        ]

    def get_active_service_ids(self, query_date: date | None = None) -> set[str]:
        """Get service IDs active on the given date."""
        if query_date is None:
            query_date = _chicago_today()
        day_name = query_date.strftime("%A").lower()
        date_str = query_date.strftime("%Y%m%d")
        active = set()
        for cal in self._calendar:
            start = cal.get("start_date", "")
            end = cal.get("end_date", "")
            if start <= date_str <= end and cal.get(day_name, "0") == "1":
                active.add(cal["service_id"])
        # Apply calendar_dates exceptions
        for cd in self._calendar_dates:
            if cd.get("date") == date_str:
                if cd.get("exception_type") == "1":
                    active.add(cd["service_id"])
                elif cd.get("exception_type") == "2":
                    active.discard(cd["service_id"])
        return active

    def get_schedule(
        self,
        route_id: str,
        stop_id: str | None = None,
        direction: str | None = None,
        query_date: date | None = None,
    ) -> list[dict[str, Any]]:
        """Get scheduled trips for a route, optionally at a specific stop.

        Args:
            route_id: The Metra route ID (e.g. "BNSF", "UP-N").
            stop_id: Optional stop ID to filter by.
            direction: Optional raw GTFS direction_id. NOTE: Metra inverts the
                usual convention -- "1" is inbound (toward Chicago) and "0" is
                outbound. Each result also carries a derived "direction"
                ("inbound"/"outbound") for clarity. See _direction_label.
            query_date: Date to check service; defaults to today.
        """
        active_services = self.get_active_service_ids(query_date)
        trips = [
            t
            for t in self._trips_by_route.get(route_id, [])
            if t.get("service_id", "") in active_services
            and (direction is None or t.get("direction_id") == direction)
        ]
        results = []
        for trip in trips:
            # Already sorted by stop_sequence in _build_indexes.
            stop_times = self._stop_times_by_trip.get(trip["trip_id"], [])
            if stop_id:
                matching = [st for st in stop_times if st["stop_id"] == stop_id]
                if not matching:
                    continue
                stop_info = matching[0]
                results.append(
                    {
                        "trip_id": trip["trip_id"],
                        "trip_headsign": trip.get("trip_headsign", ""),
                        "direction_id": trip.get("direction_id", ""),
                        "direction": _direction_label(trip.get("direction_id", "")),
                        "arrival_time": stop_info.get("arrival_time", ""),
                        "departure_time": stop_info.get("departure_time", ""),
                    }
                )
            else:
                results.append(
                    {
                        "trip_id": trip["trip_id"],
                        "trip_headsign": trip.get("trip_headsign", ""),
                        "direction_id": trip.get("direction_id", ""),
                        "direction": _direction_label(trip.get("direction_id", "")),
                        "first_stop": stop_times[0].get("departure_time", "")
                        if stop_times
                        else "",
                        "last_stop": stop_times[-1].get("arrival_time", "")
                        if stop_times
                        else "",
                        "num_stops": len(stop_times),
                    }
                )
        results.sort(
            key=lambda x: x.get("departure_time") or x.get("first_stop") or ""
        )
        return results

    def get_next_trains(
        self,
        stop_id: str,
        route_id: str | None = None,
        limit: int = 5,
        query_date: date | None = None,
    ) -> list[dict[str, Any]]:
        """Get next scheduled trains at a stop.

        GTFS encodes trips running past midnight with hours >= 24 (e.g.
        "25:10:00" is 1:10am the *next* calendar day, belonging to the prior
        service day). So just after midnight we must also consider yesterday's
        active services and shift those times back by 24h, or we'd miss the
        late-night trains that are actually the soonest departures.
        """
        if query_date is None:
            query_date = _chicago_today()
        now = _chicago_now()
        current_time_minutes = now.hour * 60 + now.minute

        # (service_ids, minute_offset): today's services as-is, plus
        # yesterday's services shifted back a day so their >=24:00 times line
        # up with this calendar morning.
        today_services = self.get_active_service_ids(query_date)
        yesterday_services = self.get_active_service_ids(query_date - timedelta(days=1))

        def _trip_ok(tid: str, services: set[str]) -> dict[str, str] | None:
            t = self._trip_by_id.get(tid)
            if t is None or t.get("service_id", "") not in services:
                return None
            if route_id is not None and t["route_id"] != route_id:
                return None
            return t

        upcoming = []
        for st in self._stop_times_by_stop.get(stop_id, []):
            dep_time = st.get("departure_time", "")
            if not dep_time:
                continue
            parts = dep_time.split(":")
            if len(parts) < 2:
                continue
            try:
                dep_minutes = int(parts[0]) * 60 + int(parts[1])
            except ValueError:
                continue

            # Same-day departures from today's services.
            trip = _trip_ok(st["trip_id"], today_services)
            effective = dep_minutes
            if trip is None and dep_minutes >= 1440:
                # After-midnight tail of yesterday's service day.
                trip = _trip_ok(st["trip_id"], yesterday_services)
                effective = dep_minutes - 1440
            if trip is None:
                continue
            if effective < current_time_minutes:
                continue
            upcoming.append(
                {
                    "trip_id": st["trip_id"],
                    "route_id": trip["route_id"],
                    "trip_headsign": trip.get("trip_headsign", ""),
                    "direction_id": trip.get("direction_id", ""),
                    "direction": _direction_label(trip.get("direction_id", "")),
                    "departure_time": dep_time,
                    "minutes_until": effective - current_time_minutes,
                }
            )
        upcoming.sort(key=lambda x: x["minutes_until"])
        # JSON Schema "integer" admits 5.0, and a negative limit would slice
        # from the wrong end; normalize to a sane positive int.
        try:
            n = int(limit)
        except (TypeError, ValueError):
            n = 5
        return upcoming[: max(1, n)]

    def get_stop_name(self, stop_id: str) -> str:
        """O(1) stop name lookup. Returns "" if stop_id is unknown."""
        s = self._stop_by_id.get(stop_id)
        return s.get("stop_name", "") if s else ""

    def search_stops(self, query: str) -> list[dict[str, str]]:
        """Search stops by name (case-insensitive partial match)."""
        q = query.lower()
        return [
            {
                "stop_id": s["stop_id"],
                "stop_name": s.get("stop_name", ""),
                "stop_lat": s.get("stop_lat", ""),
                "stop_lon": s.get("stop_lon", ""),
            }
            for s in self._stops
            if q in s.get("stop_name", "").lower()
        ]

    async def refresh(self) -> str:
        """Force re-download of schedule data.

        Holds the load lock; the new snapshot is parsed in a worker thread and
        swapped in atomically, so concurrent readers keep serving the
        previous schedule until the new one is complete.
        """
        async with self._load_lock:
            cache_file = self.cache_dir / "schedule.zip"
            await self._download_schedule(cache_file)
            await self._load_snapshot(cache_file)
        return f"Schedule refreshed. {len(self._routes)} routes, {len(self._stops)} stops loaded."

    async def reload_if_stale(self) -> bool:
        """Re-download only if Metra published a newer schedule.

        Safe to call from a background task: checks published.txt against the
        cached timestamp under the load lock and reparses only on change.
        Returns True if the schedule was reloaded.
        """
        async with self._load_lock:
            try:
                remote_ts = await self._get_published_timestamp()
            except Exception as e:
                logger.warning("Freshness check failed; keeping current schedule: %s", e)
                return False
            local_ts_file = self.cache_dir / "published.txt"
            if local_ts_file.exists() and local_ts_file.read_text().strip() == remote_ts:
                return False
            cache_file = self.cache_dir / "schedule.zip"
            await self._download_schedule(cache_file)
            await self._load_snapshot(cache_file)
            logger.info("Schedule reloaded after publish change (%s)", remote_ts)
            return True
