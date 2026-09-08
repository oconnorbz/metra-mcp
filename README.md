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
- `GET /api/board` — per-line active train counts and service state (public,
  cached 60s; feeds the landing page's line board and the Copilot's line rail)

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

### Routing the copilot through an LLM gateway

`/api/chat` talks to `https://api.anthropic.com` by default. To put a gateway
in front of it — for inspection, DLP, or model policy — set:

```bash
METRA_ANTHROPIC_BASE_URL=https://ai-gw.example.com
METRA_ANTHROPIC_EXTRA_HEADERS="x-ns-aig-slug: claude, x-ns-aig-apikey: <jwt>"
```

(Those two header names are Netskope AI Gateway's: the slug selects the route,
`x-ns-aig-apikey` carries the tenant JWT. `ANTHROPIC_API_KEY` is still sent as
`x-api-key` for the gateway to forward upstream.)

The proxy then POSTs to `<base>/v1/messages` and merges those headers over its
own (so a gateway can also supply an auth header). Two requirements the
gateway has to meet:

- **Streaming.** The proxy always sets `"stream": true` and passes the SSE
  bytes through untouched; a gateway that buffers the response will trip
  Cloudflare's first-byte timeout on tool-heavy queries.
- **The MCP connector beta.** Requests carry
  `anthropic-beta: mcp-client-2025-04-04` and an `mcp_servers` block, and
  Anthropic calls this server's `/mcp` back directly — that callback does not
  traverse the gateway, so only the model traffic is inspected.

Anything the gateway rejects comes back to the browser as an SSE `error`
event; the frontend keys off the event name rather than the payload shape, so
a gateway's own error JSON still renders instead of silently producing an
empty answer.

## Frontend

Both pages run on the **Modernist** design system: flat, architectural, Archivo,
a single red accent, 2px rules, zero corner radius. The tokens and component
classes live in `src/metra_mcp/web/modernist.css`, served at `/modernist.css`.
That file has three layers:

1. the design system itself (tokens, `.btn`, `.input`, `.tag`, `.table`, …),
2. the app chrome (`.m-*`) used by the landing page and the Copilot,
3. the **Copilot response vocabulary** (`.mc-*`) — the only classes the model
   is allowed to style its answers with.

Layer 3 is load-bearing for security as well as looks: DOMPurify strips `style`
attributes from model output, so a fragment can only ever render as the design
system. The list is spelled out in `_CHAT_SYSTEM_PROMPT` in `server.py`; change
the stylesheet and the prompt in the same commit or model output will render
unstyled.

`/` is hand-written HTML (`web/docs.html`) plus `web/docs.js` for the clipboard
buttons, the client-snippet picker and the live line board. `/stats` is the
same arrangement (`web/stats.html` + `web/stats.js`): a KPI band, four ranked
top-lists with proportional bars, and the two event tables, all fed by
`/api/stats/*` and re-fetched on a 30s countdown. `/copilot` is a
small React app: the JSX source is `web/app.jsx` and the served file is the
prebuilt `app.js` next to it (no in-browser Babel). After editing `app.jsx`,
rebuild with:

```bash
echo '{ "presets": [["@babel/preset-react", { "runtime": "classic" }]] }' > /tmp/babel.json
npx -y -p @babel/core -p @babel/cli -p @babel/preset-react \
  babel --config-file /tmp/babel.json \
  src/metra_mcp/web/app.jsx -o src/metra_mcp/web/app.js
```

The `classic` runtime is not optional: it emits `React.createElement` against
the global `React`. Recent Babel defaults to the automatic runtime, which emits
`import … from "react/jsx-runtime"` — there is no bundler and no module loader
on the page, so that build silently renders nothing.

React, ReactDOM and DOMPurify are vendored under `src/metra_mcp/web/vendor/`
and served from this origin, so the pages run under a `script-src 'self'`
Content-Security-Policy with no third-party CDN in the trust chain and no
inline scripts. The only third party is Google Fonts, and only for the Archivo
stylesheet and font files, which cannot execute script.

## Data sources

- **Realtime feeds** — `gtfspublic.metrarr.com` (positions, trip updates, alerts)
- **Static schedule** — `schedules.metrarail.com/gtfs/schedule.zip` (cached daily)

This project is not affiliated with Metra.

## License

MIT — see [LICENSE](LICENSE).
