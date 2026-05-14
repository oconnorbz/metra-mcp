"""GTFS static schedule data manager.

Downloads and parses the Metra GTFS static schedule zip file,
providing structured access to routes, stops, trips, and stop_times.
"""

import csv
import io
import logging
import os
import ssl
import zipfile
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
        self._loaded = False
        self._published_timestamp: str | None = None

    async def ensure_loaded(self) -> None:
        """Load schedule data, downloading if needed."""
        if self._loaded:
            return
        cache_file = self.cache_dir / "schedule.zip"
        needs_download = True
        if cache_file.exists():
            remote_ts = await self._get_published_timestamp()
            local_ts_file = self.cache_dir / "published.txt"
            if local_ts_file.exists() and local_ts_file.read_text().strip() == remote_ts:
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
        """Download the GTFS schedule zip."""
        logger.info("Downloading GTFS schedule from %s", SCHEDULE_URL)
        async with httpx.AsyncClient(timeout=60.0, verify=_get_ssl_context()) as client:
            resp = await client.get(SCHEDULE_URL)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
        remote_ts = await self._get_published_timestamp()
        (self.cache_dir / "published.txt").write_text(remote_ts)
        logger.info("Schedule downloaded and cached")

    def _parse_zip(self, zip_path: Path) -> None:
        """Parse GTFS text files from the zip."""
        with zipfile.ZipFile(zip_path) as zf:
            self._routes = self._read_csv(zf, "routes.txt")
            self._stops = self._read_csv(zf, "stops.txt")
            self._trips = self._read_csv(zf, "trips.txt")
            self._stop_times = self._read_csv(zf, "stop_times.txt")
            self._calendar = self._read_csv(zf, "calendar.txt")
            self._calendar_dates = self._read_csv(zf, "calendar_dates.txt")

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
        # Find stops served by trips on this route
        trip_ids = {t["trip_id"] for t in self._trips if t["route_id"] == route_id}
        stop_ids = {
            st["stop_id"] for st in self._stop_times if st["trip_id"] in trip_ids
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
            for t in self._trips
            if t["route_id"] == route_id
            and t.get("service_id", "") in active_services
            and (direction is None or t.get("direction_id") == direction)
        ]
        results = []
        for trip in trips:
            stop_times = [
                st for st in self._stop_times if st["trip_id"] == trip["trip_id"]
            ]
            stop_times.sort(key=lambda x: int(x.get("stop_sequence", "0")))
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
        """Get next scheduled trains at a stop."""
        if query_date is None:
            query_date = _chicago_today()
        now = _chicago_now()
        current_time_minutes = now.hour * 60 + now.minute
        active_services = self.get_active_service_ids(query_date)
        trip_map = {}
        for t in self._trips:
            if t.get("service_id", "") in active_services:
                if route_id is None or t["route_id"] == route_id:
                    trip_map[t["trip_id"]] = t
        upcoming = []
        for st in self._stop_times:
            if st["stop_id"] != stop_id:
                continue
            if st["trip_id"] not in trip_map:
                continue
            dep_time = st.get("departure_time", "")
            if not dep_time:
                continue
            parts = dep_time.split(":")
            if len(parts) >= 2:
                dep_minutes = int(parts[0]) * 60 + int(parts[1])
                if dep_minutes >= current_time_minutes:
                    trip = trip_map[st["trip_id"]]
                    upcoming.append(
                        {
                            "trip_id": st["trip_id"],
                            "route_id": trip["route_id"],
                            "trip_headsign": trip.get("trip_headsign", ""),
                            "direction_id": trip.get("direction_id", ""),
                            "departure_time": dep_time,
                            "minutes_until": dep_minutes - current_time_minutes,
                        }
                    )
        upcoming.sort(key=lambda x: x["departure_time"])
        return upcoming[:limit]

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
        """Force re-download of schedule data."""
        self._loaded = False
        cache_file = self.cache_dir / "schedule.zip"
        await self._download_schedule(cache_file)
        self._parse_zip(cache_file)
        self._loaded = True
        return f"Schedule refreshed. {len(self._routes)} routes, {len(self._stops)} stops loaded."
