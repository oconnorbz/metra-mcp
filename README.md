# Metra MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server that exposes
real-time GTFS data for Chicago's Metra commuter rail system. AI assistants
like Claude can call its tools to answer questions like "next BNSF train from
Union Station" or "any UP-N delays right now?"

A public hosted instance is available at **https://metra.remote-mcp.dev** —
add it to any MCP-compatible client without running anything locally.

## Features

- 10 tools covering routes, stops, schedules, live positions, trip updates,
  service alerts, and a combined per-line status view
- Multiple transports: stdio (for desktop clients), SSE, and streamable HTTP
- Built-in web UI at `/copilot` (Claude chat with tools wired up) and a
  documentation page at `/`
- Per-request stats dashboard at `/stats` (SQLite-backed; tracks tool calls,
  source IPs, queries, dashboard usage)

## Tools

| Tool | Description |
| --- | --- |
| `get_routes` | List all 11 Metra lines |
| `get_stops` | List stations, optionally filtered by route |
| `search_stops` | Find stops by name (case-insensitive) |
| `get_schedule` | Scheduled trips for a route, stop, or direction |
| `get_next_trains` | Next departures from a stop with countdown |
| `get_train_positions` | Real-time GPS positions of active trains |
| `get_trip_updates` | Real-time arrival/departure predictions |
| `get_alerts` | Active service alerts |
| `get_train_status` | Combined view: positions + delays + alerts |
| `refresh_schedule` | Force re-download of GTFS static data |

## Quick start — use the hosted instance

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "metra": {
      "command": "npx",
      "args": ["mcp-remote", "https://metra.remote-mcp.dev/mcp"]
    }
  }
}
```

**Claude Code:**
```bash
claude mcp add --transport http metra https://metra.remote-mcp.dev/mcp
```

**Claude.ai:** add a custom remote MCP integration pointing at
`https://metra.remote-mcp.dev/mcp`.

## Run your own

You'll need a Metra GTFS API token —
[apply here](https://metra.com/metra-gtfs-api) (it's free).

```bash
git clone https://github.com/oconnorbz/metra-mcp
cd metra-mcp
python -m venv venv && source venv/bin/activate
pip install -e .

export METRA_API_TOKEN=...        # required
metra-mcp                         # stdio mode (for Claude Desktop)
metra-mcp --http                  # HTTP/SSE mode for remote use
```

When `--http` is set the server binds `0.0.0.0:8080` (override with
`MCP_HOST` / `MCP_PORT`) and exposes:

- `GET /` — documentation page
- `GET /copilot` — chat UI (requires `ANTHROPIC_API_KEY`)
- `GET /stats` — usage dashboard
- `GET /health` — liveness/readiness JSON
- `POST /mcp` — streamable HTTP transport
- `GET /sse` + `POST /messages/` — legacy SSE transport (opt-in via
  `METRA_ENABLE_LEGACY_SSE=1`; deprecated upstream)
- `POST /api/chat` — proxy used by `/copilot`

See [`.env.example`](.env.example) for all environment variables.

## Production deployment

A sample systemd unit lives in [`deploy/metra-mcp.service`](deploy/metra-mcp.service).
Behind a reverse proxy (Caddy, nginx, Cloudflare Tunnel, etc.), make sure
`X-Forwarded-Proto` and `Host` are forwarded so source IPs in `/stats` and the
auto-detected public MCP URL work correctly.

The chat proxy at `/api/chat` configures the Anthropic API to call your MCP
server back at `request.scheme://request.host/mcp` by default. Set
`METRA_PUBLIC_MCP_URL` explicitly in production, and set `METRA_ALLOWED_HOSTS`
so the MCP endpoints reject requests for hostnames you don't serve.

The proxy spends your Anthropic key. Besides the built-in per-IP and global
daily limits (`METRA_CHAT_RATE_MAX`, `METRA_CHAT_GLOBAL_DAILY_MAX`), set a
spend limit on the key in the Anthropic Console.

## Frontend build

`/copilot` is a small React app. The JSX source is `src/metra_mcp/web/app.jsx`;
the served file is the prebuilt `app.js` next to it (no in-browser Babel).
After editing `app.jsx`, rebuild with:

```bash
npx -y -p @babel/core -p @babel/cli -p @babel/preset-react \
  babel --presets @babel/preset-react src/metra_mcp/web/app.jsx -o src/metra_mcp/web/app.js
```

React, ReactDOM, DOMPurify and the Tailwind runtime are vendored under
`src/metra_mcp/web/vendor/` and served from this origin, so the pages run under
a `script-src 'self'` Content-Security-Policy with no third-party CDN in the
trust chain.

## Data sources

- **Realtime feeds** — `gtfspublic.metrarr.com` (positions, trip updates, alerts)
- **Static schedule** — `schedules.metrarail.com/gtfs/schedule.zip` (cached daily)

This project is not affiliated with Metra.

## License

MIT — see [LICENSE](LICENSE).
