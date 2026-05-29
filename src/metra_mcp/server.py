"""Metra MCP Server - GTFS realtime and schedule data for Metra commuter rail."""

import asyncio
import base64
import ipaddress
import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, Icon, TextContent, Tool

from .client import MetraRealtimeClient
from .gtfs import GTFSData
from . import stats

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_ASSET_DIR = Path(__file__).parent
_METRA_LOGO_BYTES = (_ASSET_DIR / "Logo_Metra.png").read_bytes()
_METRA_SQUARE_PNG_BYTES = (_ASSET_DIR / "favicon-square.png").read_bytes()
_METRA_ICO_BYTES = (_ASSET_DIR / "favicon.ico").read_bytes()
_METRA_ICON_SRC = "data:image/png;base64," + base64.b64encode(_METRA_SQUARE_PNG_BYTES).decode("ascii")

server = Server(
    "metra",
    instructions=(
        "Metra commuter rail MCP server. Provides real-time train positions, "
        "arrival predictions, service alerts, and static schedule data for all "
        "Metra lines in the Chicago area."
    ),
    icons=[Icon(src=_METRA_ICON_SRC, mimeType="image/png", sizes=["512x512"])],
)

_rt_client: MetraRealtimeClient | None = None
_gtfs: GTFSData | None = None
_init_lock = asyncio.Lock()


async def get_rt_client() -> MetraRealtimeClient:
    global _rt_client
    if _rt_client is not None:
        return _rt_client
    async with _init_lock:
        if _rt_client is None:
            api_token = os.environ.get("METRA_API_TOKEN", "")
            if not api_token:
                raise ValueError("METRA_API_TOKEN environment variable is required")
            _rt_client = MetraRealtimeClient(api_token)
    return _rt_client


async def get_gtfs() -> GTFSData:
    global _gtfs
    if _gtfs is not None:
        return _gtfs
    async with _init_lock:
        if _gtfs is None:
            _gtfs = GTFSData()
    return _gtfs


def format_response(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


# --- /api/chat abuse guards ---
# The chat endpoint proxies to the Anthropic API on the server's own key, so
# it must not be an open, unmetered relay. We cap request size, pin the model
# server-side to an allowlist, and rate-limit per client IP.
_CHAT_MODEL_ALLOWLIST = {
    "claude-sonnet-4-5",
    "claude-haiku-4-5-20251001",
}
_CHAT_DEFAULT_MODEL = "claude-sonnet-4-5"
_CHAT_MAX_TOKENS_CAP = 4096
_CHAT_MAX_MESSAGES = 40
_CHAT_MAX_BODY_BYTES = 256 * 1024
# Sliding-window per-IP limiter: max requests per window.
_CHAT_RATE_MAX = int(os.environ.get("METRA_CHAT_RATE_MAX", "20"))
_CHAT_RATE_WINDOW_SEC = float(os.environ.get("METRA_CHAT_RATE_WINDOW_SEC", "60"))
_chat_hits: dict[str, list[float]] = {}
_chat_rate_lock = asyncio.Lock()


async def _chat_rate_limited(ip: str) -> bool:
    """Return True if this IP has exceeded the chat rate limit."""
    import time as _time

    now = _time.monotonic()
    cutoff = now - _CHAT_RATE_WINDOW_SEC
    async with _chat_rate_lock:
        hits = [t for t in _chat_hits.get(ip, ()) if t > cutoff]
        if len(hits) >= _CHAT_RATE_MAX:
            _chat_hits[ip] = hits
            return True
        hits.append(now)
        _chat_hits[ip] = hits
        # Opportunistic cleanup so the dict doesn't grow unbounded.
        if len(_chat_hits) > 10_000:
            for k in [k for k, v in _chat_hits.items() if not v or v[-1] <= cutoff]:
                _chat_hits.pop(k, None)
    return False


_STOP_SCHEMA = {
    "type": "object",
    "properties": {
        "stop_id": {"type": "string"},
        "stop_name": {"type": "string"},
        "stop_lat": {"type": "string"},
        "stop_lon": {"type": "string"},
    },
}

_ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "route_id": {"type": "string"},
        "route_short_name": {"type": "string"},
        "route_long_name": {"type": "string"},
        "route_color": {"type": "string"},
    },
}


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List all available tools."""
    return [
        Tool(
            name="get_routes",
            description="List all Metra routes/lines (e.g. BNSF, UP-N, Metra Electric, etc.).",
            inputSchema={"type": "object", "properties": {}},
            outputSchema={
                "type": "object",
                "properties": {
                    "routes": {"type": "array", "items": _ROUTE_SCHEMA},
                    "count": {"type": "integer"},
                },
                "required": ["routes", "count"],
            },
        ),
        Tool(
            name="get_stops",
            description="List Metra stops/stations. Optionally filter by route_id (e.g. 'BNSF', 'UP-N').",
            inputSchema={
                "type": "object",
                "properties": {
                    "route_id": {
                        "type": "string",
                        "description": "Optional route ID to filter stops (e.g. 'BNSF', 'UP-N')",
                    },
                },
            },
            outputSchema={
                "type": "object",
                "properties": {
                    "stops": {"type": "array", "items": _STOP_SCHEMA},
                    "count": {"type": "integer"},
                },
                "required": ["stops", "count"],
            },
        ),
        Tool(
            name="search_stops",
            description="Search for Metra stops by name (case-insensitive partial match).",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search string (e.g. 'union', 'oak park', 'evanston')",
                    },
                },
                "required": ["query"],
            },
            outputSchema={
                "type": "object",
                "properties": {
                    "stops": {"type": "array", "items": _STOP_SCHEMA},
                    "count": {"type": "integer"},
                    "query": {"type": "string"},
                },
                "required": ["stops", "count", "query"],
            },
        ),
        Tool(
            name="get_schedule",
            description="Get scheduled trips for a Metra route, optionally at a specific stop.",
            inputSchema={
                "type": "object",
                "properties": {
                    "route_id": {
                        "type": "string",
                        "description": "Route ID (e.g. 'BNSF', 'UP-N', 'ME')",
                    },
                    "stop_id": {
                        "type": "string",
                        "description": "Optional stop ID to show times at a specific station",
                    },
                    "direction": {
                        "type": "string",
                        "description": "'0' for inbound (to Chicago), '1' for outbound (from Chicago)",
                    },
                    "date_str": {
                        "type": "string",
                        "description": "Optional date in YYYY-MM-DD format. Defaults to today.",
                    },
                },
                "required": ["route_id"],
            },
            outputSchema={
                "type": "object",
                "properties": {
                    "route_id": {"type": "string"},
                    "stop_id": {"type": ["string", "null"]},
                    "direction": {"type": ["string", "null"]},
                    "trips": {"type": "array"},
                    "count": {"type": "integer"},
                },
                "required": ["route_id", "trips", "count"],
            },
        ),
        Tool(
            name="get_next_trains",
            description="Get the next scheduled trains departing from a stop.",
            inputSchema={
                "type": "object",
                "properties": {
                    "stop_id": {
                        "type": "string",
                        "description": "The stop ID (use search_stops to find it)",
                    },
                    "route_id": {
                        "type": "string",
                        "description": "Optional route filter",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results (default 5)",
                        "default": 5,
                    },
                },
                "required": ["stop_id"],
            },
            outputSchema={
                "type": "object",
                "properties": {
                    "stop_id": {"type": "string"},
                    "stop_name": {"type": "string"},
                    "upcoming_trains": {"type": "array"},
                    "count": {"type": "integer"},
                },
                "required": ["stop_id", "upcoming_trains", "count"],
            },
        ),
        Tool(
            name="refresh_schedule",
            description=(
                "Force re-download of the GTFS static schedule data. "
                "Normally not needed — the server refreshes automatically on "
                "startup and daily, plus on cache miss. Use this only after "
                "Metra publishes a known mid-day schedule change."
            ),
            inputSchema={"type": "object", "properties": {}},
            outputSchema={
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["status", "message"],
            },
        ),
        Tool(
            name="get_train_positions",
            description="Get real-time GPS positions of active Metra trains.",
            inputSchema={
                "type": "object",
                "properties": {
                    "route_id": {
                        "type": "string",
                        "description": "Optional route filter (e.g. 'BNSF', 'UP-N')",
                    },
                },
            },
            outputSchema={
                "type": "object",
                "properties": {
                    "positions": {"type": "array"},
                    "count": {"type": "integer"},
                    "route_filter": {"type": ["string", "null"]},
                },
                "required": ["positions", "count"],
            },
        ),
        Tool(
            name="get_trip_updates",
            description="Get real-time arrival/departure predictions for Metra trains.",
            inputSchema={
                "type": "object",
                "properties": {
                    "route_id": {
                        "type": "string",
                        "description": "Optional route filter (e.g. 'BNSF')",
                    },
                    "trip_id": {
                        "type": "string",
                        "description": "Optional specific trip ID filter",
                    },
                },
            },
            outputSchema={
                "type": "object",
                "properties": {
                    "trip_updates": {"type": "array"},
                    "count": {"type": "integer"},
                    "route_filter": {"type": ["string", "null"]},
                    "trip_filter": {"type": ["string", "null"]},
                },
                "required": ["trip_updates", "count"],
            },
        ),
        Tool(
            name="get_alerts",
            description="Get active Metra service alerts (delays, cancellations, etc.).",
            inputSchema={
                "type": "object",
                "properties": {
                    "route_id": {
                        "type": "string",
                        "description": "Optional route filter. If omitted, returns all alerts.",
                    },
                },
            },
            outputSchema={
                "type": "object",
                "properties": {
                    "alerts": {"type": "array"},
                    "count": {"type": "integer"},
                    "route_filter": {"type": ["string", "null"]},
                },
                "required": ["alerts", "count"],
            },
        ),
        Tool(
            name="get_train_status",
            description="Get a combined status view for a Metra line: positions, delays, and alerts.",
            inputSchema={
                "type": "object",
                "properties": {
                    "route_id": {
                        "type": "string",
                        "description": "Route ID (e.g. 'BNSF', 'UP-N', 'ME')",
                    },
                },
                "required": ["route_id"],
            },
            outputSchema={
                "type": "object",
                "properties": {
                    "route_id": {"type": "string"},
                    "active_trains": {"type": "integer"},
                    "positions": {"type": "array"},
                    "delayed_trips": {"type": "array"},
                    "alerts": {"type": "array"},
                },
                "required": ["route_id", "active_trains", "positions", "delayed_trips", "alerts"],
            },
        ),
    ]


def _tool_result(summary: str, data: dict[str, Any]) -> CallToolResult:
    """Create a CallToolResult with text summary + JSON data and structuredContent for widget rendering."""
    return CallToolResult(
        content=[TextContent(type="text", text=f"{summary}\n\n{format_response(data)}")],
        structuredContent=data,
    )


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
    """Handle tool calls."""
    import time
    t0 = time.monotonic()
    try:
        result = await _dispatch(name, arguments)
        stats.record_mcp_call(
            name, arguments,
            success=not getattr(result, "isError", False),
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        return result
    except Exception as e:
        logger.exception("Error in tool %s", name)
        stats.record_mcp_call(
            name, arguments,
            success=False, error=str(e),
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        return CallToolResult(
            content=[TextContent(type="text", text=f"Error: {e}")],
            isError=True,
        )


async def _dispatch(name: str, args: dict[str, Any]) -> CallToolResult:
    gtfs = await get_gtfs()

    if name == "get_routes":
        await gtfs.ensure_loaded()
        routes = gtfs.get_routes()
        names = ", ".join(r["route_id"] for r in routes)
        return _tool_result(
            f"Found {len(routes)} Metra routes: {names}",
            {"routes": routes, "count": len(routes)},
        )

    elif name == "get_stops":
        await gtfs.ensure_loaded()
        stops = gtfs.get_stops(args.get("route_id"))
        route_label = f" on {args['route_id']}" if args.get("route_id") else ""
        return _tool_result(
            f"Found {len(stops)} stops{route_label}.",
            {"stops": stops, "count": len(stops)},
        )

    elif name == "search_stops":
        await gtfs.ensure_loaded()
        stops = gtfs.search_stops(args["query"])
        return _tool_result(
            f"Found {len(stops)} stops matching '{args['query']}'.",
            {"stops": stops, "count": len(stops), "query": args["query"]},
        )

    elif name == "get_schedule":
        await gtfs.ensure_loaded()
        date_str = args.get("date_str")
        if date_str:
            try:
                query_date = date.fromisoformat(date_str)
            except ValueError:
                return CallToolResult(
                    content=[TextContent(
                        type="text",
                        text=f"Invalid date_str '{date_str}'. Use YYYY-MM-DD.",
                    )],
                    isError=True,
                )
        else:
            query_date = None
        schedule = gtfs.get_schedule(
            args["route_id"], args.get("stop_id"), args.get("direction"), query_date
        )
        where = f" at {args.get('stop_id')}" if args.get("stop_id") else ""
        return _tool_result(
            f"Found {len(schedule)} scheduled trips for {args['route_id']}{where}.",
            {
                "route_id": args["route_id"],
                "stop_id": args.get("stop_id"),
                "direction": args.get("direction"),
                "trips": schedule,
                "count": len(schedule),
            },
        )

    elif name == "get_next_trains":
        await gtfs.ensure_loaded()
        stop_id = args["stop_id"]
        trains = gtfs.get_next_trains(stop_id, args.get("route_id"), args.get("limit", 5))
        stop_name = gtfs.get_stop_name(stop_id)
        display_name = stop_name or stop_id
        return _tool_result(
            f"Found {len(trains)} upcoming trains at {display_name}.",
            {"stop_id": stop_id, "stop_name": stop_name, "upcoming_trains": trains, "count": len(trains)},
        )

    elif name == "refresh_schedule":
        result = await gtfs.refresh()
        return _tool_result(
            "Schedule data refreshed.",
            {"status": "refreshed", "message": result},
        )

    elif name == "get_train_positions":
        client = await get_rt_client()
        positions = await client.get_positions(args.get("route_id"))
        route_label = f" on {args['route_id']}" if args.get("route_id") else ""
        return _tool_result(
            f"Found {len(positions)} active trains{route_label}.",
            {"positions": positions, "count": len(positions), "route_filter": args.get("route_id")},
        )

    elif name == "get_trip_updates":
        client = await get_rt_client()
        updates = await client.get_trip_updates(args.get("route_id"), args.get("trip_id"))
        return _tool_result(
            f"Found {len(updates)} trip updates.",
            {
                "trip_updates": updates,
                "count": len(updates),
                "route_filter": args.get("route_id"),
                "trip_filter": args.get("trip_id"),
            },
        )

    elif name == "get_alerts":
        client = await get_rt_client()
        alerts_data = await client.get_alerts(args.get("route_id"))
        route_label = f" for {args['route_id']}" if args.get("route_id") else ""
        return _tool_result(
            f"Found {len(alerts_data)} active alerts{route_label}.",
            {"alerts": alerts_data, "count": len(alerts_data), "route_filter": args.get("route_id")},
        )

    elif name == "get_train_status":
        client = await get_rt_client()
        route_id = args["route_id"]
        # Three independent realtime feeds — fetch concurrently.
        positions, updates, alerts_data = await asyncio.gather(
            client.get_positions(route_id),
            client.get_trip_updates(route_id),
            client.get_alerts(route_id),
        )
        delayed_trips = []
        for u in updates:
            max_delay = 0
            for stu in u.get("stop_time_updates", []):
                delay = stu.get("arrival_delay", 0) or stu.get("departure_delay", 0)
                if abs(delay) > abs(max_delay):
                    max_delay = delay
            if max_delay != 0:
                delayed_trips.append(
                    {
                        "trip_id": u["trip_id"],
                        "delay_seconds": max_delay,
                        "delay_minutes": round(max_delay / 60, 1),
                        "vehicle_id": u.get("vehicle_id"),
                    }
                )
        delay_summary = f", {len(delayed_trips)} delayed" if delayed_trips else ", no delays"
        alert_summary = f", {len(alerts_data)} alerts" if alerts_data else ", no alerts"
        return _tool_result(
            f"{route_id} status: {len(positions)} active trains{delay_summary}{alert_summary}.",
            {
                "route_id": route_id,
                "active_trains": len(positions),
                "positions": positions,
                "delayed_trips": delayed_trips,
                "alerts": alerts_data,
            },
        )

    else:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Unknown tool: {name}")],
            isError=True,
        )


async def run_server():
    """Run the MCP server in stdio mode."""
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        if _rt_client is not None:
            try:
                await _rt_client.close()
            except Exception:
                logger.exception("Error closing realtime client")
        stats.flush_stats()


def _parse_trusted_proxies(spec: str) -> list[ipaddress._BaseNetwork]:
    """Parse a comma-separated list of IPs/CIDRs into network objects.

    Accepts bare IPs (treated as /32 or /128) and CIDR ranges. Invalid
    entries are logged and skipped.
    """
    nets: list[ipaddress._BaseNetwork] = []
    for raw in spec.split(","):
        s = raw.strip()
        if not s:
            continue
        try:
            nets.append(ipaddress.ip_network(s, strict=False))
        except ValueError as e:
            logger.warning("Ignoring invalid METRA_TRUSTED_PROXIES entry %r: %s", s, e)
    return nets


def main():
    """Main entry point. Use --sse or --http for remote transport."""
    import sys

    if "--sse" in sys.argv or "--http" in sys.argv:
        from starlette.applications import Starlette
        from starlette.routing import Mount, Route
        from starlette.middleware import Middleware
        import uvicorn

        # Fail fast on missing config rather than waiting for the first
        # tool call to surface the error.
        if not os.environ.get("METRA_API_TOKEN"):
            raise SystemExit(
                "METRA_API_TOKEN environment variable is required. "
                "Get one at https://metra.com/metra-gtfs-api"
            )

        # IPs/CIDRs allowed to set X-Forwarded-For / X-Real-IP /
        # CF-Connecting-IP. Defaults to loopback so a local reverse proxy
        # (Caddy, cloudflared, nginx) Just Works while preventing direct
        # callers from spoofing source IPs in /stats.
        trusted_proxies = _parse_trusted_proxies(
            os.environ.get("METRA_TRUSTED_PROXIES", "127.0.0.1,::1")
        )

        def _ip_in_trusted(addr: str) -> bool:
            if not addr or not trusted_proxies:
                return False
            try:
                parsed = ipaddress.ip_address(addr)
            except ValueError:
                return False
            return any(parsed in net for net in trusted_proxies)

        routes = []

        def _ip_from_scope(scope) -> str:
            client = scope.get("client")
            peer = client[0] if client else ""
            if not _ip_in_trusted(peer):
                # Untrusted peer: ignore forwarding headers entirely.
                return peer
            hdrs = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            for h in ("cf-connecting-ip", "x-real-ip", "x-forwarded-for"):
                v = hdrs.get(h)
                if v:
                    return v.split(",")[0].strip()
            return peer

        class RequestCtxMiddleware:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                if scope["type"] == "http":
                    hdrs = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
                    stats.set_ctx(stats.RequestCtx(
                        ip=_ip_from_scope(scope),
                        user_agent=hdrs.get("user-agent", ""),
                        path=scope.get("path", ""),
                    ))
                await self.app(scope, receive, send)

        # SSE transport on /sse
        from mcp.server.sse import SseServerTransport

        sse = SseServerTransport("/messages/")

        async def handle_sse(request):
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                await server.run(
                    streams[0],
                    streams[1],
                    server.create_initialization_options(),
                )

        routes.append(Route("/sse", endpoint=handle_sse))
        routes.append(Mount("/messages/", app=sse.handle_post_message))

        # Streamable HTTP transport on /mcp
        import contextlib
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

        session_manager = StreamableHTTPSessionManager(
            app=server,
            json_response=True,
            stateless=True,
        )

        from starlette.responses import Response

        async def handle_mcp(request):
            # Capture the response by wrapping send
            response_started = False
            response_headers = {}
            response_body = bytearray()
            status_code = 200

            async def capture_send(message):
                nonlocal response_started, status_code
                if message["type"] == "http.response.start":
                    response_started = True
                    status_code = message["status"]
                    for k, v in message.get("headers", []):
                        response_headers[k.decode()] = v.decode()
                elif message["type"] == "http.response.body":
                    response_body.extend(message.get("body", b""))

            await session_manager.handle_request(
                request.scope, request.receive, capture_send
            )

            return Response(
                content=bytes(response_body),
                status_code=status_code,
                headers=response_headers,
            )

        routes.append(Route("/mcp", endpoint=handle_mcp, methods=["GET", "POST", "DELETE"]))

        # --- Web frontend: chat UI + /api/chat proxy to Anthropic API ---
        from pathlib import Path as _Path
        from starlette.responses import FileResponse, JSONResponse
        import httpx as _httpx

        _web_dir = _Path(__file__).parent / "web"
        _docs_html = _web_dir / "docs.html"
        _copilot_html = _web_dir / "index.html"

        _stats_html = _web_dir / "stats.html"

        async def handle_docs(request):
            stats.record_dashboard_event("page_view", {"page": "docs"})
            return FileResponse(_docs_html, media_type="text/html")

        async def handle_copilot(request):
            stats.record_dashboard_event("page_view", {"page": "copilot"})
            return FileResponse(_copilot_html, media_type="text/html")

        async def handle_stats_page(request):
            stats.record_dashboard_event("page_view", {"page": "stats"})
            return FileResponse(_stats_html, media_type="text/html")

        async def handle_health(request):
            """Liveness + basic readiness for reverse proxies / monitoring."""
            gtfs_loaded = _gtfs is not None and _gtfs.loaded
            return JSONResponse(
                {
                    "status": "ok",
                    "gtfs_loaded": gtfs_loaded,
                    "rt_client_initialized": _rt_client is not None,
                }
            )

        async def handle_stats_api(request):
            kind = request.path_params.get("kind", "summary")
            if kind == "summary":
                return JSONResponse(stats.summary())
            if kind == "mcp":
                limit = int(request.query_params.get("limit", "200"))
                return JSONResponse({"calls": stats.query_mcp_calls(limit)})
            if kind == "dashboard":
                limit = int(request.query_params.get("limit", "200"))
                return JSONResponse({"events": stats.query_dashboard_events(limit)})
            return JSONResponse({"error": "unknown kind"}, status_code=404)

        _FAVICON_HEADERS = {"Cache-Control": "public, max-age=300"}

        async def handle_favicon_ico(request):
            return Response(
                content=_METRA_ICO_BYTES,
                media_type="image/x-icon",
                headers=_FAVICON_HEADERS,
            )

        async def handle_favicon_png(request):
            return Response(
                content=_METRA_SQUARE_PNG_BYTES,
                media_type="image/png",
                headers=_FAVICON_HEADERS,
            )

        async def handle_chat(request):
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                return JSONResponse(
                    {"error": "ANTHROPIC_API_KEY not configured on server"},
                    status_code=500,
                )

            # Rate-limit per client IP (resolved with proxy-spoof protection
            # by RequestCtxMiddleware).
            ctx = stats.get_ctx()
            client_ip = (ctx.ip if ctx else None) or "unknown"
            if await _chat_rate_limited(client_ip):
                return JSONResponse(
                    {"error": "Rate limit exceeded. Try again shortly."},
                    status_code=429,
                    headers={"Retry-After": str(int(_CHAT_RATE_WINDOW_SEC))},
                )

            # Reject oversized bodies before buffering/parsing them.
            try:
                clen = int(request.headers.get("content-length", "0"))
            except ValueError:
                clen = 0
            if clen > _CHAT_MAX_BODY_BYTES:
                return JSONResponse({"error": "Request too large"}, status_code=413)

            try:
                raw = await request.body()
            except Exception as e:
                return JSONResponse({"error": f"Could not read body: {e}"}, status_code=400)
            if len(raw) > _CHAT_MAX_BODY_BYTES:
                return JSONResponse({"error": "Request too large"}, status_code=413)
            try:
                body = json.loads(raw)
            except Exception as e:
                return JSONResponse({"error": f"Invalid JSON: {e}"}, status_code=400)

            messages = body.get("messages", [])
            if not isinstance(messages, list) or not messages:
                return JSONResponse({"error": "messages must be a non-empty list"}, status_code=400)
            if len(messages) > _CHAT_MAX_MESSAGES:
                return JSONResponse(
                    {"error": f"Too many messages (max {_CHAT_MAX_MESSAGES})"},
                    status_code=400,
                )
            last_user = ""
            for m in reversed(messages):
                if m.get("role") == "user":
                    c = m.get("content", "")
                    last_user = c if isinstance(c, str) else json.dumps(c)[:500]
                    break
            # Pin model to the allowlist; ignore client-supplied unknowns so
            # callers can't select an arbitrary/expensive model on our key.
            requested_model = body.get("model", _CHAT_DEFAULT_MODEL)
            model = requested_model if requested_model in _CHAT_MODEL_ALLOWLIST else _CHAT_DEFAULT_MODEL
            # Cap output tokens regardless of what the client asked for.
            try:
                max_tokens = int(body.get("max_tokens", _CHAT_MAX_TOKENS_CAP))
            except (TypeError, ValueError):
                max_tokens = _CHAT_MAX_TOKENS_CAP
            max_tokens = max(1, min(max_tokens, _CHAT_MAX_TOKENS_CAP))

            stats.record_dashboard_event("chat_query", {
                "query": last_user,
                "model": model,
                "message_count": len(messages),
            })

            public_mcp_url = os.environ.get("METRA_PUBLIC_MCP_URL")
            if not public_mcp_url:
                scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
                host = request.headers.get("host", request.url.netloc)
                public_mcp_url = f"{scheme}://{host}/mcp"

            payload = {
                "model": model,
                "max_tokens": max_tokens,
                "system": body.get("system", ""),
                "messages": messages,
                "mcp_servers": [
                    {
                        "type": "url",
                        "url": public_mcp_url,
                        "name": "metra",
                    }
                ],
            }

            try:
                async with _httpx.AsyncClient(timeout=120.0) as client:
                    resp = await client.post(
                        "https://api.anthropic.com/v1/messages",
                        headers={
                            "Content-Type": "application/json",
                            "x-api-key": api_key,
                            "anthropic-version": "2023-06-01",
                            "anthropic-beta": "mcp-client-2025-04-04",
                        },
                        json=payload,
                    )
                data = resp.json()
                return JSONResponse(data, status_code=resp.status_code)
            except Exception as e:
                logger.exception("Error proxying chat request")
                return JSONResponse({"error": str(e)}, status_code=500)

        routes.append(Route("/", endpoint=handle_docs))
        routes.append(Route("/copilot", endpoint=handle_copilot))
        routes.append(Route("/stats", endpoint=handle_stats_page))
        routes.append(Route("/health", endpoint=handle_health))
        routes.append(Route("/api/stats/{kind}", endpoint=handle_stats_api))
        routes.append(Route("/favicon.ico", endpoint=handle_favicon_ico))
        routes.append(Route("/favicon.png", endpoint=handle_favicon_png))
        routes.append(Route("/favicon.svg", endpoint=handle_favicon_png))
        routes.append(Route("/api/chat", endpoint=handle_chat, methods=["POST"]))

        async def _periodic_refresh():
            """Re-check the published GTFS timestamp every 6 hours so the
            cache stays current without anyone having to call refresh_schedule.
            """
            interval = int(os.environ.get("METRA_REFRESH_INTERVAL_SEC", "21600"))
            while True:
                try:
                    await asyncio.sleep(interval)
                    g = await get_gtfs()
                    # Reloads only if published.txt changed; keeps serving the
                    # current schedule otherwise (and during the reload).
                    await g.reload_if_stale()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Background GTFS refresh failed")

        @contextlib.asynccontextmanager
        async def lifespan(app):
            # Warm the schedule cache so the first user request isn't slow.
            try:
                g = await get_gtfs()
                await g.ensure_loaded()
            except Exception:
                logger.exception("Initial GTFS load failed; will retry on demand")
            refresh_task = asyncio.create_task(_periodic_refresh())
            try:
                async with session_manager.run():
                    yield
            finally:
                refresh_task.cancel()
                try:
                    await refresh_task
                except (asyncio.CancelledError, Exception):
                    pass
                # Close the realtime HTTP client and flush stats.
                if _rt_client is not None:
                    try:
                        await _rt_client.close()
                    except Exception:
                        logger.exception("Error closing realtime client")
                stats.flush_stats()

        app = Starlette(
            routes=routes,
            lifespan=lifespan,
            middleware=[Middleware(RequestCtxMiddleware)],
        )

        host = os.environ.get("MCP_HOST", "0.0.0.0")
        port = int(os.environ.get("MCP_PORT", "8080"))
        uvicorn.run(app, host=host, port=port)
    else:
        asyncio.run(run_server())


if __name__ == "__main__":
    main()
