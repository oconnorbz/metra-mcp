"""GTFS static schedule data manager.

Downloads and parses the Metra GTFS static schedule zip file,
providing structured access to routes, stops, trips, and stop_times.
"""

import asyncio
import csv
import io
import logging
import os
import ssl
import zipfile
from collections import defaultdict
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

try:
    from zoneinfo import ZoneInfo
    CHICAGO_TZ = ZoneInfo("America/Chicago")
except ImportError:
    CHICAGO_TZ = timezone(timedelta(hours=-5))


def _chicago_now() -> datetime:
    return datetime.now(tz=CHICAGO_TZ)


def _chicago_today() -> date:
    return _chicago_now().date()


def _get_ssl_context() -> ssl.SSLContext | bool:
    """Get SSL context using SSL_CERT_FILE if set, for Netskope compatibility."""
    cert_file = os.environ.get("SSL_CERT_FILE")
    if cert_file and Path(cert_file).exists():
        ctx = ssl.create_default_context(cafile=cert_file)
        return ctx
    return True

logger = logging.getLogger(__name__)

SCHEDULE_URL = "https://schedules.metrarail.com/gtfs/schedule.zip"
PUBLISHED_URL = "https://schedules.metrarail.com/gtfs/published.txt"


class GTFSData:
    """Manages GTFS static schedule data."""

    def __init__(self, cache_dir: Path | None = None):
        self.cache_dir = cache_dir or Path.home() / ".cache" / "metra-mcp"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._routes: list[dict[str, str]] = []
        self._stops: list[dict[str, str]] = []
        self._trips: list[dict[str, str]] = []
        self._stop_times: list[dict[str, str]] = []
        self._calendar: list[dict[str, str]] = []
        self._calendar_dates: list[dict[str, str]] = []
        self._shapes: list[dict[str, str]] = []
        self._stop_by_id: dict[str, dict[str, str]] = {}
        # Indexes built once at parse time so per-query work stays small even
        # though stop_times can be hundreds of thousands of rows.
        self._stop_times_by_trip: dict[str, list[dict[str, str]]] = {}
        self._stop_times_by_stop: dict[str, list[dict[str, str]]] = {}
        self._trips_by_route: dict[str, list[dict[str, str]]] = {}
        self._trip_by_id: dict[str, dict[str, str]] = {}
        self._loaded = False
        self._published_timestamp: str | None = None
        # Serializes downloads/parses so concurrent first-requests (and the
        # periodic refresh) don't all redownload at once.
        self._load_lock = asyncio.Lock()

    @property
    def loaded(self) -> bool:
        return self._loaded

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
            self._parse_zip(cache_file)
            self._loaded = True
            logger.info(
                "GTFS data loaded: %d routes, %d stops, %d trips, %d stop_times",
                len(self._routes),
                len(self._stops),
                len(self._trips),
                len(self._stop_times),
            )

    async def _get_published_timestamp(self) -> str:
        """Check when the static schedule was last published."""
        async with httpx.AsyncClient(timeout=10.0, verify=_get_ssl_context()) as client:
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
            async with httpx.AsyncClient(timeout=60.0, verify=_get_ssl_context()) as client:
                resp = await client.get(SCHEDULE_URL)
                resp.raise_for_status()
                tmp_zip.write_bytes(resp.content)
            # Validate the download before committing.
            with zipfile.ZipFile(tmp_zip) as zf:
                bad = zf.testzip()
                if bad is not None:
                    raise RuntimeError(f"Downloaded GTFS zip is corrupt: {bad}")
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

    def _parse_zip(self, zip_path: Path) -> None:
        """Parse GTFS text files from the zip."""
        with zipfile.ZipFile(zip_path) as zf:
            self._routes = self._read_csv(zf, "routes.txt")
            self._stops = self._read_csv(zf, "stops.txt")
            self._trips = self._read_csv(zf, "trips.txt")
            self._stop_times = self._read_csv(zf, "stop_times.txt")
            self._calendar = self._read_csv(zf, "calendar.txt")
            self._calendar_dates = self._read_csv(zf, "calendar_dates.txt")
        self._build_indexes()

    def _build_indexes(self) -> None:
        """Precompute lookup indexes so per-query work avoids full scans.

        stop_times is the largest table; without these, get_schedule is
        O(trips × stop_times) and get_next_trains scans every row per call.
        """
        self._stop_by_id = {s["stop_id"]: s for s in self._stops if s.get("stop_id")}

        by_trip: dict[str, list[dict[str, str]]] = defaultdict(list)
        by_stop: dict[str, list[dict[str, str]]] = defaultdict(list)
        for st in self._stop_times:
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
        for t in self._trips:
            rid = t.get("route_id")
            if rid:
                by_route[rid].append(t)
            tid = t.get("trip_id")
            if tid:
                trip_by_id[tid] = t

        self._stop_times_by_trip = by_trip
        self._stop_times_by_stop = by_stop
        self._trips_by_route = by_route
        self._trip_by_id = trip_by_id

    def _read_csv(self, zf: zipfile.ZipFile, filename: str) -> list[dict[str, str]]:
        """Read a CSV file from the zip archive.

        Metra's GTFS files have leading spaces in column headers and values,
        so we strip all keys and values during parsing.
        """
        try:
            with zf.open(filename) as f:
                text = io.TextIOWrapper(f, encoding="utf-8-sig")
                reader = csv.DictReader(text)
                return [
                    {k.strip(): v.strip() for k, v in row.items()}
                    for row in reader
                ]
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
            direction: Optional direction_id ("0" for inbound, "1" for outbound).
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
                    "departure_time": dep_time,
                    "minutes_until": effective - current_time_minutes,
                }
            )
        upcoming.sort(key=lambda x: x["minutes_until"])
        return upcoming[:limit]

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

        Holds the load lock and reparses in place; `_loaded` stays True
        throughout so concurrent readers keep serving the previous data
        instead of seeing an empty schedule mid-refresh.
        """
        async with self._load_lock:
            cache_file = self.cache_dir / "schedule.zip"
            await self._download_schedule(cache_file)
            self._parse_zip(cache_file)
            self._loaded = True
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
            self._parse_zip(cache_file)
            self._loaded = True
            logger.info("Schedule reloaded after publish change (%s)", remote_ts)
            return True
