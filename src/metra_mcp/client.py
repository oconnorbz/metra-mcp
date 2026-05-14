"""Metra GTFS Realtime API client.

Fetches and parses GTFS Realtime protobuf feeds for vehicle positions,
trip updates (arrival predictions), and service alerts.
"""

import logging
import os
import ssl
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import httpx
from google.transit import gtfs_realtime_pb2

try:
    from zoneinfo import ZoneInfo
    CHICAGO_TZ = ZoneInfo("America/Chicago")
except ImportError:
    CHICAGO_TZ = timezone(timedelta(hours=-5))


def _ts_to_chicago(unix_ts: int | None) -> str | None:
    """Convert a Unix timestamp to America/Chicago datetime string."""
    if not unix_ts:
        return None
    dt = datetime.fromtimestamp(unix_ts, tz=CHICAGO_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def _get_ssl_context() -> ssl.SSLContext | bool:
    """Get SSL context using SSL_CERT_FILE if set, for Netskope compatibility."""
    cert_file = os.environ.get("SSL_CERT_FILE")
    if cert_file and Path(cert_file).exists():
        ctx = ssl.create_default_context(cafile=cert_file)
        return ctx
    return True

logger = logging.getLogger(__name__)

BASE_URL = "https://gtfspublic.metrarr.com/gtfs/public"


class MetraRealtimeClient:
    """Client for Metra GTFS Realtime API."""

    def __init__(self, api_token: str):
        self.api_token = api_token
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0, verify=_get_ssl_context())
        return self._client

    async def _fetch_feed(self, endpoint: str) -> gtfs_realtime_pb2.FeedMessage:
        """Fetch and parse a GTFS Realtime protobuf feed."""
        client = await self._get_client()
        url = f"{BASE_URL}/{endpoint}"
        resp = await client.get(url, params={"api_token": self.api_token})
        resp.raise_for_status()
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(resp.content)
        return feed

    async def get_positions(self, route_id: str | None = None) -> list[dict[str, Any]]:
        """Get real-time vehicle positions.

        Args:
            route_id: Optional route filter (e.g. "BNSF", "UP-N").
        """
        feed = await self._fetch_feed("positions")
        positions = []
        for entity in feed.entity:
            vp = entity.vehicle
            if not vp.HasField("position"):
                continue
            trip_route = vp.trip.route_id if vp.HasField("trip") else ""
            if route_id and trip_route != route_id:
                continue
            pos = {
                "vehicle_id": vp.vehicle.id if vp.HasField("vehicle") else entity.id,
                "label": vp.vehicle.label if vp.HasField("vehicle") else "",
                "route_id": trip_route,
                "trip_id": vp.trip.trip_id if vp.HasField("trip") else "",
                "latitude": vp.position.latitude,
                "longitude": vp.position.longitude,
                "bearing": vp.position.bearing if vp.position.bearing else None,
                "speed": vp.position.speed if vp.position.speed else None,
                "current_stop_sequence": vp.current_stop_sequence or None,
                "stop_id": vp.stop_id or None,
                "current_status": _vehicle_status(vp.current_status),
                "timestamp": _ts_to_chicago(vp.timestamp),
            }
            positions.append(pos)
        return positions

    async def get_trip_updates(
        self, route_id: str | None = None, trip_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Get real-time trip updates (arrival/departure predictions).

        Args:
            route_id: Optional route filter.
            trip_id: Optional specific trip filter.
        """
        feed = await self._fetch_feed("tripupdates")
        updates = []
        for entity in feed.entity:
            tu = entity.trip_update
            t_route = tu.trip.route_id
            t_trip = tu.trip.trip_id
            if route_id and t_route != route_id:
                continue
            if trip_id and t_trip != trip_id:
                continue
            stop_updates = []
            for stu in tu.stop_time_update:
                su = {
                    "stop_sequence": stu.stop_sequence,
                    "stop_id": stu.stop_id,
                }
                if stu.HasField("arrival"):
                    su["arrival_delay"] = stu.arrival.delay if stu.arrival.delay else 0
                    su["arrival_time"] = _ts_to_chicago(stu.arrival.time)
                if stu.HasField("departure"):
                    su["departure_delay"] = (
                        stu.departure.delay if stu.departure.delay else 0
                    )
                    su["departure_time"] = _ts_to_chicago(stu.departure.time)
                su["schedule_relationship"] = _stop_schedule_rel(
                    stu.schedule_relationship
                )
                stop_updates.append(su)
            updates.append(
                {
                    "trip_id": t_trip,
                    "route_id": t_route,
                    "direction_id": tu.trip.direction_id if tu.trip.direction_id else None,
                    "start_date": tu.trip.start_date or None,
                    "start_time": tu.trip.start_time or None,
                    "vehicle_id": tu.vehicle.id if tu.HasField("vehicle") else None,
                    "timestamp": _ts_to_chicago(tu.timestamp),
                    "stop_time_updates": stop_updates,
                }
            )
        return updates

    async def get_alerts(self, route_id: str | None = None) -> list[dict[str, Any]]:
        """Get active service alerts.

        Args:
            route_id: Optional route filter.
        """
        feed = await self._fetch_feed("alerts")
        alerts = []
        for entity in feed.entity:
            alert = entity.alert
            informed = []
            for ie in alert.informed_entity:
                informed.append(
                    {
                        "route_id": ie.route_id or None,
                        "trip_id": ie.trip.trip_id if ie.HasField("trip") else None,
                        "stop_id": ie.stop_id or None,
                    }
                )
            if route_id:
                route_match = any(
                    e.get("route_id") == route_id for e in informed
                )
                if not route_match:
                    continue
            periods = []
            for ap in alert.active_period:
                periods.append({"start": _ts_to_chicago(ap.start), "end": _ts_to_chicago(ap.end)})

            alerts.append(
                {
                    "id": entity.id,
                    "cause": _alert_cause(alert.cause),
                    "effect": _alert_effect(alert.effect),
                    "header_text": _translated_text(alert.header_text),
                    "description_text": _translated_text(alert.description_text),
                    "url": _translated_text(alert.url) if alert.HasField("url") else None,
                    "active_periods": periods,
                    "informed_entities": informed,
                }
            )
        return alerts

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None


def _vehicle_status(status: int) -> str:
    mapping = {
        0: "INCOMING_AT",
        1: "STOPPED_AT",
        2: "IN_TRANSIT_TO",
    }
    return mapping.get(status, f"UNKNOWN({status})")


def _stop_schedule_rel(rel: int) -> str:
    mapping = {
        0: "SCHEDULED",
        1: "SKIPPED",
        2: "NO_DATA",
    }
    return mapping.get(rel, f"UNKNOWN({rel})")


def _alert_cause(cause: int) -> str:
    mapping = {
        1: "OTHER_CAUSE",
        2: "TECHNICAL_PROBLEM",
        3: "STRIKE",
        4: "DEMONSTRATION",
        5: "ACCIDENT",
        6: "HOLIDAY",
        7: "WEATHER",
        8: "MAINTENANCE",
        9: "CONSTRUCTION",
        10: "POLICE_ACTIVITY",
        11: "MEDICAL_EMERGENCY",
    }
    return mapping.get(cause, "UNKNOWN_CAUSE")


def _alert_effect(effect: int) -> str:
    mapping = {
        1: "NO_SERVICE",
        2: "REDUCED_SERVICE",
        3: "SIGNIFICANT_DELAYS",
        4: "DETOUR",
        5: "ADDITIONAL_SERVICE",
        6: "MODIFIED_SERVICE",
        7: "OTHER_EFFECT",
        8: "UNKNOWN_EFFECT",
        9: "STOP_MOVED",
    }
    return mapping.get(effect, "UNKNOWN_EFFECT")


def _translated_text(ts) -> str | None:
    """Extract text from a GTFS TranslatedString."""
    if ts and ts.translation:
        return ts.translation[0].text
    return None
