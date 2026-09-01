"""Metra MCP Server - GTFS realtime and schedule data for Metra commuter rail."""

import asyncio
import base64
import hmac
import ipaddress
import json
import logging
import os
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import jsonschema
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    Icon,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

from . import __version__, stats
from .client import MetraAPIError, MetraRealtimeClient
from .gtfs import GTFSData

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
# httpx logs every request URL at INFO — including the Metra api_token query
# parameter. Keep those loggers at WARNING so credentials never hit the log.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_ASSET_DIR = Path(__file__).parent
_METRA_LOGO_BYTES = (_ASSET_DIR / "Logo_Metra.png").read_bytes()
_METRA_SQUARE_PNG_BYTES = (_ASSET_DIR / "favicon-square.png").read_bytes()
_METRA_ICO_BYTES = (_ASSET_DIR / "favicon.ico").read_bytes()
_METRA_ICON_SRC = "data:image/png;base64," + base64.b64encode(_METRA_SQUARE_PNG_BYTES).decode("ascii")

server = Server(
    "metra",
    version=__version__,
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
    # Compact: this text is a fallback copy of structuredContent for clients
    # that don't read structured output, so every byte here is sent twice.
    return json.dumps(data, separators=(",", ":"), default=str)


def _clamp_int(val: Any, *, default: int, lo: int, hi: int) -> int:
    """Coerce an optional numeric arg to an int within [lo, hi]."""
    if val is None:
        return default
    try:
        n = int(val)
    except (TypeError, ValueError):
        return default
    return max(lo, min(n, hi))


# --- /api/chat abuse guards ---
# The chat endpoint proxies to the Anthropic API on the server's own key, so
# it must not be an open, unmetered relay. We cap request size, pin the model
# server-side to an allowlist, and rate-limit per client IP.
_CHAT_MODEL_ALLOWLIST = {
    "claude-sonnet-5",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
    "claude-haiku-4-5-20251001",
}
_CHAT_DEFAULT_MODEL = os.environ.get("METRA_CHAT_MODEL", "claude-sonnet-5")
if _CHAT_DEFAULT_MODEL not in _CHAT_MODEL_ALLOWLIST:
    _CHAT_MODEL_ALLOWLIST.add(_CHAT_DEFAULT_MODEL)
_CHAT_MAX_TOKENS_CAP = 4096
_CHAT_MAX_MESSAGES = 40
_CHAT_MAX_BODY_BYTES = 256 * 1024
# Cap on total message text the proxy will forward. Input tokens are the
# real cost lever on our API key; a chat session needs nowhere near 256KB.
_CHAT_MAX_CONTENT_CHARS = 32 * 1024

# System prompt is pinned server-side — the browser only picks the theme.
# A client-supplied "system" field is ignored, so the endpoint can't be
# repurposed as a general-purpose LLM proxy on our key.
_CHAT_SYSTEM_PROMPT = """You are Metra Copilot — a real-time Chicago commuter rail assistant with access to live Metra MCP tools. Always use Metra tools to fetch live data before responding.

CRITICAL RULE: Your ENTIRE response must be a single valid HTML fragment using Tailwind CSS utility classes. Never output plain text, markdown, or explanation outside of HTML. The HTML will be rendered directly in a transit UI that supports both light and dark themes.

THEME: The UI uses Tailwind's class-based dark mode. ALWAYS include both base (light) AND dark: prefixed classes for every color so the HTML adapts when the user toggles themes. Current theme is {theme}.

DESIGN SYSTEM (follow exactly — every color needs base + dark: variant):
- Outer wrapper: <div class="font-mono text-sm">
- Cards: bg-white dark:bg-zinc-900 rounded-xl border border-zinc-200 dark:border-zinc-700/60 p-4
- Section headers: text-amber-600 dark:text-amber-400 font-bold text-xs tracking-widest uppercase mb-3
- Line names: text-zinc-900 dark:text-white font-bold
- On-time / good: text-emerald-600 dark:text-emerald-400
- Delayed / alert: text-red-600 dark:text-red-400
- Warning: text-amber-600 dark:text-amber-400
- Muted info: text-zinc-600 dark:text-zinc-400
- Departure rows: flex justify-between border-b border-zinc-200 dark:border-zinc-800 py-2
- Status badges: inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-bold

STATUS BADGE COLORS (always include both variants):
- On Time: bg-emerald-100 dark:bg-emerald-950 text-emerald-700 dark:text-emerald-400 border border-emerald-300 dark:border-emerald-800
- Delayed: bg-red-100 dark:bg-red-950 text-red-700 dark:text-red-400 border border-red-300 dark:border-red-800
- Alert: bg-amber-100 dark:bg-amber-950 text-amber-700 dark:text-amber-400 border border-amber-300 dark:border-amber-800
- Cancelled: bg-zinc-200 dark:bg-zinc-800 text-zinc-600 dark:text-zinc-400 border border-zinc-400 dark:border-zinc-600

For departures/schedules, use a departure board layout:
<div class="font-mono">
  <div class="grid grid-cols-3 text-zinc-500 dark:text-zinc-500 text-xs border-b border-zinc-200 dark:border-zinc-700 pb-2 mb-1">
    <span>DEPARTS</span><span>TRAIN</span><span class="text-right">STATUS</span>
  </div>
  [rows]
</div>

For alerts, stack colored alert cards.
For line status overview, use a grid of line status cards.
For route/stop info, use clean list layouts.

TOOL EFFICIENCY: Use the fewest tool calls that answer the question — typically one search_stops per station name the user gives, then one data call (get_next_trains, get_schedule, or get_alerts). Do not re-verify results with extra calls. Common station shorthand: "OTC" is Ogilvie Transportation Center, "CUS" is Chicago Union Station.

Always end responses with a subtle footer showing the data timestamp if available.
Keep HTML compact but data-rich. No lorem ipsum or placeholder text — use real fetched data only."""
# Sliding-window per-IP limiter: max requests per window.
_CHAT_RATE_MAX = int(os.environ.get("METRA_CHAT_RATE_MAX", "20"))
_CHAT_RATE_WINDOW_SEC = float(os.environ.get("METRA_CHAT_RATE_WINDOW_SEC", "60"))
_chat_hits: dict[str, list[float]] = {}
_chat_rate_lock = asyncio.Lock()
# Global budget: per-IP limits don't bound spend when IPs are cheap, so also
# cap total chat requests per rolling 24h across all callers. 0 disables.
_CHAT_GLOBAL_DAILY_MAX = int(os.environ.get("METRA_CHAT_GLOBAL_DAILY_MAX", "400"))
_chat_global_hits: list[float] = []

# Shared HTTP client for the chat proxy so successive messages (and retries)
# reuse pooled connections instead of paying a TLS handshake each time.
_chat_http: httpx.AsyncClient | None = None


def _get_chat_http() -> httpx.AsyncClient:
    global _chat_http
    if _chat_http is None:
        # Tool-heavy queries round-trip Anthropic -> tunnel -> /mcp several
        # times per answer; a transient MCP-connector stall can push a request
        # well past 120s. 240s read keeps us under the browser's ~300s fetch
        # ceiling while not 502ing slow-but-successful generations.
        _chat_http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=240.0, write=30.0, pool=10.0)
        )
    return _chat_http


# Optional token gating full detail on /api/stats. Without a valid token the
# endpoints still work but client IPs and user-agents are redacted — the same
# data we deliberately chmod 600 in the SQLite file shouldn't be world-readable
# over HTTP.
_STATS_TOKEN = os.environ.get("METRA_STATS_TOKEN", "")


async def _chat_rate_limited(ip: str) -> str | None:
    """Return a reason string if this request must be refused, else None.

    Checks the global daily budget first (cheapest to exhaust, most costly to
    miss), then the per-IP sliding window. Only a request that passes both is
    counted, so a refused request never consumes budget.
    """
    now = time.monotonic()
    cutoff = now - _CHAT_RATE_WINDOW_SEC
    async with _chat_rate_lock:
        if _CHAT_GLOBAL_DAILY_MAX > 0:
            day_cutoff = now - 86400.0
            while _chat_global_hits and _chat_global_hits[0] <= day_cutoff:
                _chat_global_hits.pop(0)
            if len(_chat_global_hits) >= _CHAT_GLOBAL_DAILY_MAX:
                return "Daily chat budget for this server is exhausted. Try again tomorrow."
        hits = [t for t in _chat_hits.get(ip, ()) if t > cutoff]
        if len(hits) >= _CHAT_RATE_MAX:
            _chat_hits[ip] = hits
            return "Rate limit exceeded. Try again shortly."
        hits.append(now)
        _chat_hits[ip] = hits
        if _CHAT_GLOBAL_DAILY_MAX > 0:
            _chat_global_hits.append(now)
        # Opportunistic cleanup so the dict doesn't grow unbounded.
        if len(_chat_hits) > 10_000:
            for k in [k for k, v in _chat_hits.items() if not v or v[-1] <= cutoff]:
                _chat_hits.pop(k, None)
    return None


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


_TOOL_DEFS: list[Tool] = [
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
                    "description": (
                        "Raw GTFS direction_id. Metra inverts the usual "
                        "convention: '1' is inbound (toward Chicago), '0' "
                        "is outbound (away from Chicago). Each trip also "
                        "returns a derived 'direction' label."
                    ),
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
            "Re-check Metra's published GTFS timestamp and re-download the "
            "static schedule only if it changed. Normally not needed — the "
            "server refreshes automatically on startup and every few hours. "
            "Use this only after Metra publishes a known mid-day schedule "
            "change."
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
                "limit": {
                    "type": "integer",
                    "description": (
                        "Maximum number of trips to return (default 50, max 500). "
                        "Unfiltered, the feed covers every active train system-wide, "
                        "so prefer route_id or trip_id filters."
                    ),
                    "default": 50,
                },
            },
        },
        outputSchema={
            "type": "object",
            "properties": {
                "trip_updates": {"type": "array"},
                "count": {"type": "integer"},
                "total_matching": {"type": "integer"},
                "truncated": {"type": "boolean"},
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

# Compile each input schema once; jsonschema.validate() would rebuild the
# validator (and re-check the schema itself) on every call.
_INPUT_VALIDATORS: dict[str, jsonschema.protocols.Validator] = {
    t.name: jsonschema.Draft202012Validator(t.input_schema) for t in _TOOL_DEFS
}


async def list_tools(
    ctx: ServerRequestContext, params: PaginatedRequestParams | None
) -> ListToolsResult:
    """List all available tools."""
    return ListToolsResult(tools=_TOOL_DEFS)


def _tool_result(summary: str, data: dict[str, Any]) -> CallToolResult:
    """Create a CallToolResult with text summary + JSON data and structuredContent for widget rendering."""
    return CallToolResult(
        content=[TextContent(type="text", text=f"{summary}\n\n{format_response(data)}")],
        structuredContent=data,
    )


async def call_tool(
    ctx: ServerRequestContext, params: CallToolRequestParams
) -> CallToolResult:
    """Handle tool calls."""
    name = params.name
    arguments = params.arguments or {}

    # mcp 1.x validated arguments against inputSchema inside the
    # @server.call_tool() decorator; 2.x handlers must do it themselves.
    validator = _INPUT_VALIDATORS.get(name)
    if validator is not None:
        err = jsonschema.exceptions.best_match(validator.iter_errors(arguments))
        if err is not None:
            return CallToolResult(
                content=[
                    TextContent(type="text", text=f"Input validation error: {err.message}")
                ],
                isError=True,
            )

    t0 = time.monotonic()
    try:
        result = await _dispatch(name, arguments)
        stats.record_mcp_call(
            name, arguments,
            success=not result.is_error,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        return result
    except MetraAPIError as e:
        # Expected operational failure (Metra's feed is down/slow): one line,
        # no traceback, and the message is already credential-free.
        logger.warning("Tool %s failed upstream: %s", name, e)
        stats.record_mcp_call(
            name, arguments,
            success=False, error=str(e),
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        return CallToolResult(
            content=[TextContent(type="text", text=f"Error: {e}")],
            isError=True,
        )
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


# mcp 2.x registers handlers explicitly; the 1.x decorators are gone.
server.add_request_handler("tools/list", PaginatedRequestParams, list_tools)
server.add_request_handler("tools/call", CallToolRequestParams, call_tool)


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
        limit = _clamp_int(args.get("limit"), default=5, lo=1, hi=100)
        trains = gtfs.get_next_trains(stop_id, args.get("route_id"), limit)
        stop_name = gtfs.get_stop_name(stop_id)
        display_name = stop_name or stop_id
        return _tool_result(
            f"Found {len(trains)} upcoming trains at {display_name}.",
            {"stop_id": stop_id, "stop_name": stop_name, "upcoming_trains": trains, "count": len(trains)},
        )

    elif name == "refresh_schedule":
        # The MCP endpoint is public, so this must not be a free lever to
        # force full re-downloads from Metra. reload_if_stale only downloads
        # when the published timestamp actually changed.
        reloaded = await gtfs.reload_if_stale()
        if reloaded:
            return _tool_result(
                "Schedule data refreshed.",
                {"status": "refreshed", "message": "Metra published a new schedule; data reloaded."},
            )
        return _tool_result(
            "Schedule already current.",
            {"status": "current", "message": "Published schedule unchanged; no download needed."},
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
        total = len(updates)
        limit = _clamp_int(args.get("limit"), default=50, lo=1, hi=500)
        updates = updates[:limit]
        truncated = total > len(updates)
        note = f" (showing first {len(updates)}; add a route_id filter)" if truncated else ""
        return _tool_result(
            f"Found {total} trip updates{note}.",
            {
                "trip_updates": updates,
                "count": len(updates),
                "total_matching": total,
                "truncated": truncated,
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
        import uvicorn
        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.routing import Mount, Route

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
        # Which forwarding header to believe from a trusted proxy. Pin this
        # to the one your proxy sets authoritatively (cloudflared/Cloudflare:
        # cf-connecting-ip). Unset = try the common ones in order, which is
        # spoofable if the proxy passes unknown client headers through.
        client_ip_header = os.environ.get("METRA_CLIENT_IP_HEADER", "").lower().strip()
        ip_header_order = (
            [client_ip_header] if client_ip_header
            else ["cf-connecting-ip", "x-real-ip", "x-forwarded-for"]
        )

        # Host allowlist for the MCP transport endpoints (DNS-rebinding /
        # Host-spoofing protection). Comma-separated hostnames, optionally with
        # port; "host:*" matches any port. Empty = disabled.
        allowed_hosts = [
            h.strip().lower()
            for h in os.environ.get("METRA_ALLOWED_HOSTS", "").split(",")
            if h.strip()
        ]

        def _host_allowed(host: str) -> bool:
            if not allowed_hosts:
                return True
            host = host.lower()
            for allowed in allowed_hosts:
                if host == allowed:
                    return True
                if allowed.endswith(":*") and host.split(":")[0] == allowed[:-2]:
                    return True
            return False

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
            for h in ip_header_order:
                v = hdrs.get(h)
                if v:
                    return v.split(",")[0].strip()
            return peer

        _MCP_PATHS = ("/mcp", "/sse", "/messages/")

        # Browser-facing hardening. The copilot renders model output, so the
        # CSP matters most there: scripts only from this origin (React,
        # DOMPurify, the prebuilt app), inline styles allowed because Tailwind
        # injects a <style> at runtime and the UI carries a small inline block.
        _SECURITY_HEADERS = [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"no-referrer"),
            (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
        ]
        # Google Fonts is the one third party allowed, and only for
        # stylesheets/font files, which cannot execute script.
        _HTML_CSP = (
            b"default-src 'self'; script-src 'self'; "
            b"style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            b"font-src 'self' https://fonts.gstatic.com; "
            b"img-src 'self' data:; connect-src 'self'; "
            b"object-src 'none'; base-uri 'self'; form-action 'none'; frame-ancestors 'none'"
        )

        class RequestCtxMiddleware:
            """Per-request plumbing: client IP resolution for stats, Host
            allowlisting on the MCP endpoints, and security headers."""

            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                if scope["type"] != "http":
                    await self.app(scope, receive, send)
                    return
                hdrs = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
                path = scope.get("path", "")
                stats.set_ctx(stats.RequestCtx(
                    ip=_ip_from_scope(scope),
                    user_agent=hdrs.get("user-agent", ""),
                    path=path,
                ))

                if allowed_hosts and path.startswith(_MCP_PATHS):
                    if not _host_allowed(hdrs.get("host", "")):
                        logger.warning("Rejected MCP request with Host %r", hdrs.get("host"))
                        await send({
                            "type": "http.response.start",
                            "status": 421,
                            "headers": [(b"content-type", b"text/plain")],
                        })
                        await send({"type": "http.response.body", "body": b"Invalid Host header"})
                        return

                async def send_with_headers(message):
                    if message["type"] == "http.response.start":
                        headers = list(message.get("headers", []))
                        present = {k.lower() for k, _ in headers}
                        for k, v in _SECURITY_HEADERS:
                            if k not in present:
                                headers.append((k, v))
                        ctype = b""
                        for k, v in headers:
                            if k.lower() == b"content-type":
                                ctype = v
                                break
                        if ctype.startswith(b"text/html") and b"content-security-policy" not in present:
                            headers.append((b"content-security-policy", _HTML_CSP))
                        message = {**message, "headers": headers}
                    await send(message)

                await self.app(scope, receive, send_with_headers)

        from starlette.responses import Response

        # Legacy HTTP+SSE transport (2024-11-05 spec) on /sse + /messages/.
        # Deprecated in favor of streamable HTTP; opt-in because in months of
        # production traffic no client ever completed a session on it.
        if os.environ.get("METRA_ENABLE_LEGACY_SSE", "").lower() in ("1", "true", "yes"):
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
                # The SSE response has already been sent through request._send.
                # Starlette still expects a Response object back from a Route
                # endpoint; returning None raised "'NoneType' object is not
                # callable" on every client disconnect.
                return Response()

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

        class _McpEndpoint:
            """Raw ASGI endpoint: Starlette passes non-function endpoints
            straight through, so the session manager writes the response
            itself instead of us buffering and re-emitting it."""

            async def __call__(self, scope, receive, send):
                await session_manager.handle_request(scope, receive, send)

        routes.append(Route("/mcp", endpoint=_McpEndpoint(), methods=["GET", "POST", "DELETE"]))

        # --- Web frontend: chat UI + /api/chat proxy to Anthropic API ---
        from pathlib import Path as _Path

        from starlette.responses import FileResponse, JSONResponse

        _web_dir = _Path(__file__).parent / "web"
        _docs_html = _web_dir / "docs.html"
        _copilot_html = _web_dir / "index.html"

        _stats_html = _web_dir / "stats.html"

        # no-cache so UI/API changes take effect on the next page load instead
        # of stale JS hitting a newer API contract.
        _HTML_HEADERS = {"Cache-Control": "no-cache"}
        # First-party JS: the app bundles are revalidated each load (they track
        # the API contract); vendored libraries are version-pinned in their
        # filenames, so they can cache for a day.
        _APP_JS_HEADERS = {"Cache-Control": "no-cache"}
        _VENDOR_HEADERS = {"Cache-Control": "public, max-age=86400"}
        _VENDOR_DIR = _web_dir / "vendor"
        _VENDOR_FILES = {p.name for p in _VENDOR_DIR.glob("*.js")} if _VENDOR_DIR.is_dir() else set()

        async def handle_app_js(request):
            return FileResponse(_web_dir / "app.js", media_type="text/javascript", headers=_APP_JS_HEADERS)

        async def handle_stats_js(request):
            return FileResponse(_web_dir / "stats.js", media_type="text/javascript", headers=_APP_JS_HEADERS)

        async def handle_vendor(request):
            # Allowlist by exact filename: no path traversal, no directory listing.
            name = request.path_params.get("name", "")
            if name not in _VENDOR_FILES:
                return Response("Not found", status_code=404)
            return FileResponse(_VENDOR_DIR / name, media_type="text/javascript", headers=_VENDOR_HEADERS)

        async def handle_docs(request):
            stats.record_dashboard_event("page_view", {"page": "docs"})
            return FileResponse(_docs_html, media_type="text/html", headers=_HTML_HEADERS)

        async def handle_copilot(request):
            stats.record_dashboard_event("page_view", {"page": "copilot"})
            return FileResponse(_copilot_html, media_type="text/html", headers=_HTML_HEADERS)

        async def handle_stats_page(request):
            stats.record_dashboard_event("page_view", {"page": "stats"})
            return FileResponse(_stats_html, media_type="text/html", headers=_HTML_HEADERS)

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

        def _stats_full_access(request) -> bool:
            """True when the caller presented the stats token (if configured)."""
            if not _STATS_TOKEN:
                return False
            supplied = (
                request.headers.get("x-stats-token")
                or request.query_params.get("token")
                or ""
            )
            return hmac.compare_digest(supplied, _STATS_TOKEN)

        async def handle_stats_api(request):
            kind = request.path_params.get("kind", "summary")
            try:
                limit = int(request.query_params.get("limit", "200"))
            except ValueError:
                limit = 200
            limit = max(1, min(limit, 1000))
            full = _stats_full_access(request)

            # SQLite work runs in a worker thread so a slow aggregate can't
            # stall the event loop that's also serving /mcp.
            if kind == "summary":
                data = await asyncio.to_thread(stats.summary)
                if not full:
                    data["mcp"].pop("top_ips", None)
                    data["dashboard"].pop("top_ips", None)
                return JSONResponse(data)
            if kind == "mcp":
                calls = await asyncio.to_thread(stats.query_mcp_calls, limit)
                if not full:
                    for c in calls:
                        c.pop("ip", None)
                        c.pop("user_agent", None)
                return JSONResponse({"calls": calls})
            if kind == "dashboard":
                events = await asyncio.to_thread(stats.query_dashboard_events, limit)
                if not full:
                    for e in events:
                        e.pop("ip", None)
                        e.pop("user_agent", None)
                        # What people typed into the copilot is theirs, not
                        # public; keep the event but drop the text.
                        if e.get("event_type") == "chat_query":
                            e["details"] = json.dumps({"query": "<redacted>"})
                return JSONResponse({"events": events})
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
            refused = await _chat_rate_limited(client_ip)
            if refused:
                return JSONResponse(
                    {"error": refused},
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
            total_chars = 0
            for m in messages:
                c = m.get("content", "") if isinstance(m, dict) else ""
                total_chars += len(c) if isinstance(c, str) else len(json.dumps(c, default=str))
            if total_chars > _CHAT_MAX_CONTENT_CHARS:
                return JSONResponse(
                    {"error": f"Conversation too long (max {_CHAT_MAX_CONTENT_CHARS} characters)"},
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
                # Derived from the request; only safe when Host can't be
                # attacker-chosen (i.e. METRA_ALLOWED_HOSTS is set or the
                # server is only reachable through a proxy that pins Host).
                scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
                host = request.headers.get("host", request.url.netloc)
                if allowed_hosts and not _host_allowed(host):
                    return JSONResponse({"error": "Invalid Host header"}, status_code=421)
                public_mcp_url = f"{scheme}://{host}/mcp"

            theme = body.get("theme")
            if theme not in ("light", "dark"):
                theme = "dark"

            payload = {
                "model": model,
                "max_tokens": max_tokens,
                "system": _CHAT_SYSTEM_PROMPT.replace("{theme}", theme),
                "messages": messages,
                # Stream the upstream response and pass SSE straight through.
                # Tool-heavy MCP queries can sit minutes between tool rounds;
                # without bytes on the wire, Cloudflare's ~100s first-byte
                # limit kills the request with a 524. Streaming keeps the
                # connection alive end to end.
                "stream": True,
                "mcp_servers": [
                    {
                        "type": "url",
                        "url": public_mcp_url,
                        "name": "metra",
                    }
                ],
            }

            headers = {
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "mcp-client-2025-04-04",
            }
            from starlette.responses import StreamingResponse

            async def event_stream():
                """Pass the Anthropic SSE stream through verbatim.

                Connection attempts are retried (a brief upstream blip would
                otherwise surface as a hard error); once bytes are flowing
                there is no retry — the client sees the upstream error event.
                """
                last_exc: Exception | None = None
                for attempt in range(3):
                    try:
                        async with _get_chat_http().stream(
                            "POST",
                            "https://api.anthropic.com/v1/messages",
                            headers=headers,
                            json=payload,
                        ) as resp:
                            if resp.status_code != 200:
                                # Error responses are small JSON bodies; wrap
                                # them in an SSE error event for the client.
                                body = (await resp.aread()).decode("utf-8", "replace")
                                yield f"event: error\ndata: {body}\n\n".encode()
                                return
                            async for chunk in resp.aiter_bytes():
                                yield chunk
                            return
                    except (
                        httpx.ConnectError,
                        httpx.ConnectTimeout,
                        httpx.ReadError,
                        httpx.RemoteProtocolError,
                    ) as e:
                        last_exc = e
                        logger.warning(
                            "Chat proxy transport error (attempt %d/3): %s", attempt + 1, e
                        )
                        await asyncio.sleep(0.5 * (attempt + 1))
                    except Exception as e:
                        logger.exception("Error proxying chat request")
                        err = json.dumps({"type": "error", "error": {"type": "proxy_error", "message": str(e)}})
                        yield f"event: error\ndata: {err}\n\n".encode()
                        return
                logger.error("Chat proxy failed after retries: %s", last_exc)
                err = json.dumps({
                    "type": "error",
                    "error": {"type": "proxy_error", "message": f"Upstream API unreachable: {last_exc}"},
                })
                yield f"event: error\ndata: {err}\n\n".encode()

            return StreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    # Defeat proxy buffering so keepalive bytes actually flow.
                    "X-Accel-Buffering": "no",
                },
            )

        routes.append(Route("/", endpoint=handle_docs))
        routes.append(Route("/copilot", endpoint=handle_copilot))
        routes.append(Route("/stats", endpoint=handle_stats_page))
        routes.append(Route("/health", endpoint=handle_health))
        routes.append(Route("/api/stats/{kind}", endpoint=handle_stats_api))
        routes.append(Route("/favicon.ico", endpoint=handle_favicon_ico))
        routes.append(Route("/favicon.png", endpoint=handle_favicon_png))
        routes.append(Route("/favicon.svg", endpoint=handle_favicon_png))
        routes.append(Route("/app.js", endpoint=handle_app_js))
        routes.append(Route("/stats.js", endpoint=handle_stats_js))
        routes.append(Route("/vendor/{name}", endpoint=handle_vendor))
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
                except BaseException:  # noqa: BLE001 - shutdown path; nothing to do
                    pass
                # Close the realtime + chat HTTP clients and flush stats.
                if _rt_client is not None:
                    try:
                        await _rt_client.close()
                    except Exception:
                        logger.exception("Error closing realtime client")
                if _chat_http is not None:
                    try:
                        await _chat_http.aclose()
                    except Exception:
                        logger.exception("Error closing chat HTTP client")
                stats.flush_stats()

        app = Starlette(
            routes=routes,
            lifespan=lifespan,
            middleware=[Middleware(RequestCtxMiddleware)],
        )

        host = os.environ.get("MCP_HOST", "0.0.0.0")
        port = int(os.environ.get("MCP_PORT", "8080"))
        if not os.environ.get("METRA_PUBLIC_MCP_URL") and not allowed_hosts:
            logger.warning(
                "Neither METRA_PUBLIC_MCP_URL nor METRA_ALLOWED_HOSTS is set; the "
                "copilot's MCP callback URL will be derived from the request Host header."
            )
        uvicorn.run(app, host=host, port=port)
    else:
        asyncio.run(run_server())


if __name__ == "__main__":
    main()
