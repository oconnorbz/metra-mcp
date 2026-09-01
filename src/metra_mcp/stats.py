"""Stats tracking for MCP tool calls and dashboard events.

Stores events in a SQLite DB so the /stats page can render historical usage.
Request context (IP, user-agent) is carried from ASGI handlers into the MCP
tool-call handler via a ContextVar.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import sqlite3
import stat
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)



def _default_db_path() -> Path:
    """METRA_STATS_DB, else systemd's $STATE_DIRECTORY, else /var/lib/metra-mcp."""
    env = os.environ.get("METRA_STATS_DB")
    if env:
        return Path(env)
    state = os.environ.get("STATE_DIRECTORY")
    if state:
        return Path(state.split(":")[0]) / "stats.db"
    return Path("/var/lib/metra-mcp/stats.db")


_DB_PATH = _default_db_path()

# Defense in depth: never persist anything that looks like a credential, even
# if an upstream library's error message smuggles one in. Matches the Metra
# api_token query parameter and Anthropic-style keys.
_SECRET_RE = re.compile(r"(api_token=)[^&'\"\s]+|sk-ant-[A-Za-z0-9_\-]{8,}")


def _scrub(val: str | None) -> str | None:
    if not val:
        return val
    return _SECRET_RE.sub(lambda m: (m.group(1) or "") + "<redacted>", val)


# /stats summary is a handful of full-table aggregates; cache it briefly so a
# page refresh storm (or a scraper) can't turn into a SQLite CPU burn.
_SUMMARY_TTL_SEC = 30.0
_summary_cache: tuple[float, dict[str, Any]] | None = None
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

# Write-behind queue: events from any thread/coroutine are enqueued and a
# daemon thread drains them into SQLite in batches. Lazily started on first
# event. Read-path methods still acquire _lock and use the same connection.
_write_q: queue.Queue[tuple[str, tuple]] | None = None
_writer_started = False
_writer_lock = threading.Lock()
_FLUSH_INTERVAL_SEC = 1.0
_BATCH_LIMIT = 200
_QUEUE_MAX = 10_000
# Rate-limit "stats failed" log spam: at most one warning per minute.
_last_err_log = 0.0
_ERR_LOG_INTERVAL_SEC = 60.0


def _log_err(msg: str, exc: BaseException) -> None:
    global _last_err_log
    now = time.monotonic()
    if now - _last_err_log < _ERR_LOG_INTERVAL_SEC:
        return
    _last_err_log = now
    logger.warning("%s: %s", msg, exc, exc_info=exc)


@dataclass
class RequestCtx:
    ip: str
    user_agent: str
    path: str


_current: ContextVar[RequestCtx | None] = ContextVar("metra_request_ctx", default=None)


def set_ctx(ctx: RequestCtx) -> None:
    _current.set(ctx)


def get_ctx() -> RequestCtx | None:
    return _current.get()


def _ensure_db() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_db = not _DB_PATH.exists()
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False, isolation_level=None)
    if new_db:
        # Restrict to owner read/write — request logs include IPs and
        # user-agents, which we don't want world-readable.
        try:
            os.chmod(_DB_PATH, stat.S_IRUSR | stat.S_IWUSR)
        except OSError as e:
            logger.warning("Could not chmod %s: %s", _DB_PATH, e)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mcp_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            ip TEXT,
            user_agent TEXT,
            tool_name TEXT NOT NULL,
            arguments TEXT,
            success INTEGER NOT NULL,
            error TEXT,
            duration_ms INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dashboard_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            ip TEXT,
            user_agent TEXT,
            path TEXT,
            event_type TEXT NOT NULL,
            details TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mcp_ts ON mcp_calls(ts DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dash_ts ON dashboard_events(ts DESC)")
    # Aggregates on the /stats summary group by these.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mcp_tool ON mcp_calls(tool_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mcp_ip ON mcp_calls(ip)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mcp_success ON mcp_calls(success)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dash_ip ON dashboard_events(ip)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dash_path_type ON dashboard_events(path, event_type)")
    _conn = conn
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _truncate(val: str | None, limit: int = 2000) -> str | None:
    if val is None:
        return None
    if len(val) <= limit:
        return val
    return val[:limit] + f"...[+{len(val) - limit} chars]"


_MCP_INSERT = (
    "INSERT INTO mcp_calls (ts, ip, user_agent, tool_name, arguments, success, error, duration_ms) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)
_DASH_INSERT = (
    "INSERT INTO dashboard_events (ts, ip, user_agent, path, event_type, details) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)


def _start_writer_if_needed() -> None:
    global _write_q, _writer_started
    if _writer_started:
        return
    with _writer_lock:
        if _writer_started:
            return
        _write_q = queue.Queue(maxsize=_QUEUE_MAX)
        # Pass the queue explicitly: flush_stats() nulls the global when it
        # retires this writer, and the loop must keep draining its own queue.
        t = threading.Thread(
            target=_writer_loop, args=(_write_q,), name="metra-stats-writer", daemon=True
        )
        t.start()
        _writer_started = True


def _writer_loop(q: queue.Queue) -> None:
    """Drain the write queue into SQLite in batches.

    Coalesces inserts within _FLUSH_INTERVAL_SEC or _BATCH_LIMIT events,
    whichever comes first. The _SHUTDOWN sentinel on the queue ends the loop.
    """
    pending: list[tuple[str, tuple]] = []
    deadline = time.monotonic() + _FLUSH_INTERVAL_SEC
    while True:
        timeout = max(0.0, deadline - time.monotonic())
        try:
            item = q.get(timeout=timeout)
        except queue.Empty:
            item = None  # flush on timeout
        if item is _SHUTDOWN:
            _flush(pending)
            return
        if item is not None:
            pending.append(item)
        if len(pending) >= _BATCH_LIMIT or time.monotonic() >= deadline:
            _flush(pending)
            pending = []
            deadline = time.monotonic() + _FLUSH_INTERVAL_SEC


_SHUTDOWN = object()


def _flush(items: list[tuple[str, tuple]]) -> None:
    if not items:
        return
    try:
        with _lock:
            conn = _ensure_db()
            conn.execute("BEGIN")
            try:
                for sql, params in items:
                    conn.execute(sql, params)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    except Exception as e:
        _log_err("Failed to flush stats batch", e)


def _enqueue(sql: str, params: tuple) -> None:
    _start_writer_if_needed()
    q = _write_q
    if q is None:  # raced with shutdown; drop
        return
    try:
        # Non-blocking; if the queue is full we drop the event rather than
        # blocking a request thread.
        q.put_nowait((sql, params))
    except queue.Full:
        _log_err("stats queue full, dropping event", RuntimeError("queue full"))


def flush_stats(timeout: float = 5.0) -> None:
    """Drain pending writes synchronously. Call from shutdown hooks."""
    global _write_q, _writer_started
    with _writer_lock:
        if not _writer_started or _write_q is None:
            return
        q = _write_q
        # Reset so a later _enqueue (e.g. a request racing shutdown) starts a
        # fresh writer instead of posting into a queue nobody drains.
        _write_q = None
        _writer_started = False
    q.put(_SHUTDOWN)
    # Best-effort: the writer thread is daemon, so we don't join.
    # Give it a moment to drain.
    deadline = time.monotonic() + timeout
    while not q.empty() and time.monotonic() < deadline:
        time.sleep(0.05)


def record_mcp_call(
    tool_name: str,
    arguments: dict[str, Any] | None,
    success: bool,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    ctx = get_ctx()
    params = (
        _now(),
        ctx.ip if ctx else None,
        _truncate(ctx.user_agent if ctx else None, 500),
        tool_name,
        _truncate(_scrub(json.dumps(arguments or {}, default=str))),
        1 if success else 0,
        _truncate(_scrub(error), 1000),
        duration_ms,
    )
    _enqueue(_MCP_INSERT, params)


def record_dashboard_event(
    event_type: str,
    details: dict[str, Any] | None = None,
) -> None:
    ctx = get_ctx()
    params = (
        _now(),
        ctx.ip if ctx else None,
        _truncate(ctx.user_agent if ctx else None, 500),
        ctx.path if ctx else None,
        event_type,
        _truncate(_scrub(json.dumps(details or {}, default=str))),
    )
    _enqueue(_DASH_INSERT, params)


def query_mcp_calls(limit: int = 200) -> list[dict[str, Any]]:
    with _lock:
        conn = _ensure_db()
        cur = conn.execute(
            "SELECT id, ts, ip, user_agent, tool_name, arguments, success, error, duration_ms "
            "FROM mcp_calls ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [
            {
                "id": row[0],
                "ts": row[1],
                "ip": row[2],
                "user_agent": row[3],
                "tool_name": row[4],
                "arguments": row[5],
                "success": bool(row[6]),
                "error": row[7],
                "duration_ms": row[8],
            }
            for row in cur.fetchall()
        ]


def query_dashboard_events(limit: int = 200) -> list[dict[str, Any]]:
    with _lock:
        conn = _ensure_db()
        cur = conn.execute(
            "SELECT id, ts, ip, user_agent, path, event_type, details "
            "FROM dashboard_events ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [
            {
                "id": row[0],
                "ts": row[1],
                "ip": row[2],
                "user_agent": row[3],
                "path": row[4],
                "event_type": row[5],
                "details": row[6],
            }
            for row in cur.fetchall()
        ]


def summary() -> dict[str, Any]:
    """Aggregate counts for /stats, cached for _SUMMARY_TTL_SEC.

    Callers get a fresh dict each time (the handler mutates it to redact IP
    lists for unauthenticated viewers), so the cache is never handed out
    directly.
    """
    global _summary_cache
    now = time.monotonic()
    if _summary_cache is not None and _summary_cache[0] > now:
        return json.loads(json.dumps(_summary_cache[1]))
    data = _summary_uncached()
    _summary_cache = (now + _SUMMARY_TTL_SEC, data)
    return json.loads(json.dumps(data))


def _summary_uncached() -> dict[str, Any]:
    with _lock:
        conn = _ensure_db()
        mcp_total = conn.execute("SELECT COUNT(*) FROM mcp_calls").fetchone()[0]
        mcp_errors = conn.execute("SELECT COUNT(*) FROM mcp_calls WHERE success=0").fetchone()[0]
        mcp_ips = conn.execute("SELECT COUNT(DISTINCT ip) FROM mcp_calls WHERE ip IS NOT NULL").fetchone()[0]
        dash_total = conn.execute("SELECT COUNT(*) FROM dashboard_events").fetchone()[0]
        dash_ips = conn.execute("SELECT COUNT(DISTINCT ip) FROM dashboard_events WHERE ip IS NOT NULL").fetchone()[0]

        top_tools = [
            {"tool_name": r[0], "count": r[1]}
            for r in conn.execute(
                "SELECT tool_name, COUNT(*) c FROM mcp_calls GROUP BY tool_name ORDER BY c DESC LIMIT 20"
            ).fetchall()
        ]
        top_mcp_ips = [
            {"ip": r[0], "count": r[1]}
            for r in conn.execute(
                "SELECT ip, COUNT(*) c FROM mcp_calls WHERE ip IS NOT NULL GROUP BY ip ORDER BY c DESC LIMIT 20"
            ).fetchall()
        ]
        top_dash_paths = [
            {"path": r[0], "event_type": r[1], "count": r[2]}
            for r in conn.execute(
                "SELECT path, event_type, COUNT(*) c FROM dashboard_events "
                "GROUP BY path, event_type ORDER BY c DESC LIMIT 20"
            ).fetchall()
        ]
        top_dash_ips = [
            {"ip": r[0], "count": r[1]}
            for r in conn.execute(
                "SELECT ip, COUNT(*) c FROM dashboard_events WHERE ip IS NOT NULL GROUP BY ip ORDER BY c DESC LIMIT 20"
            ).fetchall()
        ]

    return {
        "mcp": {
            "total": mcp_total,
            "errors": mcp_errors,
            "unique_ips": mcp_ips,
            "top_tools": top_tools,
            "top_ips": top_mcp_ips,
        },
        "dashboard": {
            "total": dash_total,
            "unique_ips": dash_ips,
            "top_paths": top_dash_paths,
            "top_ips": top_dash_ips,
        },
    }
