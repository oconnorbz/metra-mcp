"""Stats tracking for MCP tool calls and dashboard events.

Stores events in a SQLite DB so the /stats page can render historical usage.
Request context (IP, user-agent) is carried from ASGI handlers into the MCP
tool-call handler via a ContextVar.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DB_PATH = Path(os.environ.get("METRA_STATS_DB", "/var/lib/metra-mcp/stats.db"))
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


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
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False, isolation_level=None)
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


def record_mcp_call(
    tool_name: str,
    arguments: dict[str, Any] | None,
    success: bool,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    ctx = get_ctx()
    try:
        with _lock:
            conn = _ensure_db()
            conn.execute(
                "INSERT INTO mcp_calls (ts, ip, user_agent, tool_name, arguments, success, error, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _now(),
                    ctx.ip if ctx else None,
                    _truncate(ctx.user_agent if ctx else None, 500),
                    tool_name,
                    _truncate(json.dumps(arguments or {}, default=str)),
                    1 if success else 0,
                    _truncate(error, 1000),
                    duration_ms,
                ),
            )
    except Exception:
        pass


def record_dashboard_event(
    event_type: str,
    details: dict[str, Any] | None = None,
) -> None:
    ctx = get_ctx()
    try:
        with _lock:
            conn = _ensure_db()
            conn.execute(
                "INSERT INTO dashboard_events (ts, ip, user_agent, path, event_type, details) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    _now(),
                    ctx.ip if ctx else None,
                    _truncate(ctx.user_agent if ctx else None, 500),
                    ctx.path if ctx else None,
                    event_type,
                    _truncate(json.dumps(details or {}, default=str)),
                ),
            )
    except Exception:
        pass


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
