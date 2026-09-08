# Handoff: Metra MCP Server — landing page + Copilot redesign

## Overview

Two screens of `metra.remote-mcp.dev` get a visual redesign onto the **Modernist** design system (flat, architectural, Archivo, single red accent, 2px rules, zero corner radius):

1. **`/` — the landing/docs page.** Goal: get a developer connected to the MCP server in under a minute, and make the live-data capability obvious. Currently `src/metra_mcp/web/docs.html`.
2. **`/copilot` — the chat UI.** Same conversation product, restyled and restructured: a full-height thread with a visible tool-call trace and structured result blocks. Currently `src/metra_mcp/web/index.html` + `app.jsx` (built to `app.js`).

No backend changes are required. `/api/chat`, the MCP tool set, and the GTFS plumbing are untouched.

## About the design files

The files in this bundle are **design references written as HTML** — prototypes of intended look and behavior, not production code to paste in. `Metra MCP - Landing.dc.html` and `Metra Copilot.dc.html` are streaming Design Components (a template plus a small logic class, mounted by `support.js`). **Do not ship them.** Recreate them in the repo's existing environment:

- **Landing** → static HTML/CSS in `src/metra_mcp/web/docs.html`. It is hand-written HTML today; keep it that way. The one behavior it needs is the copy-to-clipboard buttons and the client-snippet picker — a ~30-line inline `<script>`, no framework.
- **Copilot** → the existing React app in `src/metra_mcp/web/app.jsx`, rebuilt to `app.js` per the README's "Frontend build" step. Keep the current architecture: same-origin vendored React/DOMPurify, no CDN, `sanitize()` on every model-produced fragment, the SSE streaming reader, and the 120s stall abort. Only the presentation layer changes.

### One decision to make first: Tailwind vs. tokens

`app.jsx` styles with inline style objects derived from a `PALETTES` object, while the model's HTML fragments are prompted to use Tailwind classes (`bg-red-50 dark:bg-red-950 …`). The redesign is token-based (CSS custom properties). Recommended path:

1. Add the Modernist token block (see **Design tokens**) to a real stylesheet served from the origin — `src/metra_mcp/web/modernist.css` — and link it from both `docs.html` and `index.html`.
2. Replace `PALETTES` / `cssVars` in `app.jsx` with `var(--…)` references in the inline styles.
3. **Update the server-side system prompt in `/api/chat`** so model-generated fragments emit the same vocabulary. Either (a) tell the model to use only `var(--color-*)` custom properties and no Tailwind, or (b) keep Tailwind and add a `tailwind.config` theme extension mapping `metra-*` colors to the tokens. Option (a) is simpler and removes the 451KB Tailwind vendor script from `/copilot` entirely. Either way the prompt must be revised in the same change, or model output will look like the old design inside the new one.

## Fidelity

**High fidelity.** Colors, type, spacing, dividers and copy in the prototypes are final. Recreate pixel-for-pixel using the tokens below. The only intentionally fake parts are the data: the landing's eleven-line board and the Copilot's replies are illustrative samples (see **Mock data**).

---

## Screen 1 — Landing (`/`)

**Purpose:** a developer lands here, copies an endpoint, pastes it into their client. Secondary: understand what data is available; tertiary: try the Copilot.

**Page frame:** single column, full-bleed, no max-width container. Background `var(--color-bg)`. Every major section is separated by a **2px** `var(--color-divider)` rule; within sections rows are **1px** of the same. Horizontal padding is `32px` throughout. Everything flush left — no centered text anywhere.

### 1.1 Header (sticky)

- `position: sticky; top: 0; z-index: 10`, background `var(--color-bg)`, `padding: 14px 32px`, `border-bottom: 2px solid var(--color-divider)`, `display: flex; align-items: center; gap: 24px; flex-wrap: wrap`.
- Brand, `margin-right: auto`: `METRA MCP` — Archivo 800, 18px, `letter-spacing: -0.01em`; then `SERVER` — 11px, `letter-spacing: 0.14em`, uppercase, `color-mix(in srgb, var(--color-text) 50%, transparent)`.
- Nav: `CONNECT`, `TOOLS`, `LINES` — 13px, uppercase, `letter-spacing: 0.04em`, `color: inherit`, no underline, anchors to `#connect` / `#tools` / `#lines`. Then a primary button `LAUNCH COPILOT` → `/copilot`.

### 1.2 Hero (two columns)

`display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(260px, 1fr)`. Left cell has `border-right: 2px solid var(--color-divider)` and `padding: 56px 32px 40px`.

Left:
- Eyebrow row, `margin-bottom: 28px`: an `8×8px` solid `var(--color-accent)` square, then `MODEL CONTEXT PROTOCOL · CHICAGO` — 11px, uppercase, `letter-spacing: 0.16em`, `color: var(--color-accent-700)`.
- H1: **"Live Metra data, wired straight into your assistant."** — Archivo 800, `font-size: clamp(40px, 6vw, 76px)`, `line-height: 0.98`, `letter-spacing: -0.03em`, `max-width: 15ch`, `text-wrap: balance`.
- Body: **"A remote MCP server over Metra's public GTFS feeds. Train positions, arrival predictions, service alerts and the full schedule for all eleven lines — exposed as ten tools any MCP client can call."** — 17px/1.5, `max-width: 54ch`, `color-mix(in srgb, var(--color-text) 78%, transparent)`, `text-wrap: pretty`.
- Buttons, `gap: 10px`: primary `LAUNCH COPILOT →` (`/copilot`) and secondary `CONNECTION DETAILS` (`#connect`). Both 15px, uppercase, `letter-spacing: 0.06em`, `padding: 12px 20px`.

Right — a stat stack, each row `padding: 16px 24px`, `border-bottom: 1px solid var(--color-divider)`, label left / value right (`justify-content: space-between; align-items: baseline`):
- Header row (`padding: 18px 24px`): a `9×9px` accent square + `OPERATIONAL` (Archivo 800, 13px, uppercase, `letter-spacing: 0.12em`).
- `LINES COVERED / 11`, `MCP TOOLS / 10`, `TRANSPORTS / 2`, `AUTH REQUIRED / None`. Labels 11px uppercase `letter-spacing: 0.12em` at 55% ink; values Archivo 800, 26px, `letter-spacing: -0.02em`, `font-variant-numeric: tabular-nums`.
- Footnote, 12px, 55% ink: "Static schedule refreshes daily. Realtime feeds are queried per tool call."

### 1.3 Connection (`#connect`)

Section head: `padding: 40px 32px 8px`, H2 **"Connection"** (32px, `letter-spacing: -0.02em`) with a 12px uppercase `letter-spacing: 0.1em` 55%-ink note **"Two transports · no auth"** on the same baseline.

**Endpoint rows** — `display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); padding: 24px 32px 40px`, each cell `padding-right: 32px`:
- `.tag.tag-outline` badge (`HTTP` / `SSE`, uppercase, `letter-spacing: 0.1em`) + 13px 65%-ink label ("Streamable HTTP — recommended" / "Server-Sent Events — legacy clients").
- Below: a `1px solid var(--color-divider)` box, `display: flex`. A `<code>` fills it — `padding: 12px 14px`, 13px, monospace, `background: var(--color-surface)`, `white-space: nowrap; overflow-x: auto` — holding `https://metra.remote-mcp.dev/mcp` and `…/sse`. On the right a `COPY` button, `min-width: 84px`, 11px uppercase `letter-spacing: 0.1em`, `border-left: 1px solid var(--color-divider)`, no other border.
- Click → `navigator.clipboard.writeText(url)`, label swaps to `COPIED` for **1600ms**, then reverts. Failures are swallowed silently.

**Client picker** — a row above `border-top: 1px solid var(--color-divider)`, `display: grid; grid-template-columns: minmax(200px, 0.6fr) minmax(0, 2fr)`, left cell `border-right: 1px solid var(--color-divider)`:
- Left: label `ADD IT TO` (11px uppercase, `letter-spacing: 0.12em`, 55% ink, `padding: 0 32px 12px`), then three full-width left-aligned buttons — **Claude Desktop**, **Claude Code**, **Claude.ai** — Archivo 800, 14px, `letter-spacing: 0.04em`, `padding: 12px 28px`, `border-left: 4px solid transparent`. Selected: `border-left-color: var(--color-accent)`, text `var(--color-accent-700)`, background `color-mix(in srgb, var(--color-accent) 10%, transparent)`. Unselected hover: `color-mix(in srgb, var(--color-text) 6%, transparent)`. Default selection: **Claude Desktop**.
- Right (`padding: 24px 32px`): a 12px/1.5 70%-ink note (`max-width: 62ch`), then a `<pre>` — `background: var(--color-neutral-900)`, `color: var(--color-neutral-100)`, `padding: 18px 20px`, 13px/1.6 monospace, `overflow-x: auto`, no radius.

Notes and code per client:

| Client | Note | Code |
| --- | --- | --- |
| Claude Desktop | Add the server to your claude_desktop_config.json, then restart the app. | the `mcpServers` JSON block with `npx mcp-remote https://metra.remote-mcp.dev/mcp` |
| Claude Code | One command in your terminal — the transport is HTTP streamable. | `claude mcp add --transport http metra \` / `  https://metra.remote-mcp.dev/mcp` |
| Claude.ai | Open the integrations menu, add a custom remote MCP server, and paste the URL below. | `https://metra.remote-mcp.dev/mcp` |

### 1.4 Tools (`#tools`)

Head: H2 **"Ten tools"** + note `SCHEDULE · REALTIME · OPS`, `padding: 40px 32px 24px`.

Grid: `repeat(auto-fill, minmax(280px, 1fr))`, `border-top: 1px solid var(--color-divider)`; each cell `padding: 18px 24px`, `border-bottom` and `border-right` 1px divider, `display: flex; gap: 14px; align-items: flex-start`.
- Index: `01`…`10`, Archivo 800, 12px, `letter-spacing: 0.06em`, `var(--color-accent)`, tabular nums, `padding-top: 2px`.
- Name: monospace 14px/600. Description: 13px/1.45 at 68% ink, `text-wrap: pretty`.

Exact copy (order matters — it mirrors the server's tool registration order):

1. `get_routes` — List all eleven Metra lines with their identifiers.
2. `get_stops` — List stations, optionally filtered by route.
3. `search_stops` — Find stops by name, case-insensitive.
4. `get_schedule` — Scheduled trips for a route, stop or direction.
5. `get_next_trains` — Next departures from a stop, with countdown.
6. `refresh_schedule` — Force a re-download of the GTFS static data.
7. `get_train_positions` — Realtime GPS positions of active trains.
8. `get_trip_updates` — Realtime arrival and departure predictions.
9. `get_alerts` — Active service alerts — delays and cancellations.
10. `get_train_status` — Combined view: positions, delays and alerts.

### 1.5 Lines (`#lines`)

Head: H2 **"Eleven lines"** + a stamp note (`SAMPLE BOARD · 5:41 PM CT`). Table uses `.table` from the design system, `min-width: 640px` inside an `overflow-x: auto` wrapper, `padding: 0 32px 40px`.

Columns: `Code` (86px), `Line`, `Terminal`, `Trains` (120px), `Service` (160px). Code cells Archivo 800 13px `letter-spacing: 0.06em`; terminal 13px 65% ink; trains tabular nums; service a `.tag` — `.tag-neutral` for "On time", `.tag-accent` for anything else, uppercase `letter-spacing: 0.08em`.

Rows: BNSF/Burlington Northern Santa Fe/Aurora/18/On time · HC/Heritage Corridor/Joliet/2/On time · MD-N/Milwaukee District North/Fox Lake/9/On time · MD-W/Milwaukee District West/Elgin/11/Minor delays · ME/Metra Electric/University Park/14/On time · NCS/North Central Service/Antioch/4/On time · RI/Rock Island/Joliet/12/On time · SWS/Southwest Service/Manhattan/3/On time · UP-N/Union Pacific North/Kenosha/13/Minor delays · UP-NW/Union Pacific Northwest/Harvard / McHenry/15/On time · UP-W/Union Pacific West/Elburn/12/On time.

Caption below, 11px uppercase `letter-spacing: 0.08em` at 45% ink: "Illustrative board — the live values come from get_train_status."

> **If you wire this up for real:** call `get_train_status` server-side on page render (or a small `/api/board` endpoint polled every 30s), map each route to its active vehicle count and worst alert severity, and replace the stamp with the real feed timestamp. Keep the illustrative caption only while the data is fake.

### 1.6 Try-it + Data source (two columns)

`repeat(auto-fit, minmax(300px, 1fr))`, left cell `border-right: 1px solid var(--color-divider)`, both `padding: 40px 32px`, section closes with a 2px rule.

Left — H3 **"Ask it in plain English"** (25px), 14px 68%-ink lede "The Copilot is the same server behind a chat window. Nothing to install.", then four stacked links to `/copilot` (`gap: 8px`), each `1px solid var(--color-divider)`, `padding: 11px 14px`, 14px, undecorated; hover → `border-color: var(--color-accent)`, `color: var(--color-accent-700)`:
- "What's the status of the BNSF line right now?"
- "Are there any service alerts?"
- "Next trains from Ogilvie heading to Kenosha"
- "Show me the UP-N schedule for tomorrow morning"

Right — H3 **"Data source"**, then: "Realtime GTFS feeds come straight from Metra's public API. Static schedule data is fetched from the published GTFS zip and cached; the realtime feeds are read on every tool call." Below, a two-row definition list bounded by 1px rules: `REALTIME / gtfspublic.metrarr.com`, `SCHEDULE / schedules.metrarail.com` (label 12px uppercase `letter-spacing: 0.1em` 55% ink; value monospace, right-aligned). Then a secondary button `METRA GTFS API DOCS` → `https://metra.com/metra-gtfs-api`, `margin-top: 20px`.

### 1.7 Poster close

The **one** place red runs as a field. `background: var(--color-accent)`, `color: var(--color-bg)`, `padding: 56px 32px`.
- Kicker: `ONE ENDPOINT. TEN TOOLS. ELEVEN LINES.` — 11px, uppercase, `letter-spacing: 0.18em`, `opacity: 0.85`.
- Statement: `metra.remote-mcp.dev/mcp` — Archivo 800, `clamp(24px, 4.6vw, 54px)`, `line-height: 1.05`, `letter-spacing: -0.03em`, `word-break: break-all`.
- Buttons (`margin-top: 28px`): inverted primary `LAUNCH COPILOT` (bg `var(--color-bg)`, text `var(--color-text)`) and an outlined `COPY ENDPOINT` (border `color-mix(in srgb, #f3f2f2 60%, transparent)`, text `var(--color-bg)`) with the same 1600ms `COPIED` behavior.

### 1.8 Footer

`padding: 20px 32px`, 12px, 55% ink, wrapping flex `gap: 16px`: "Metra MCP Server" · [Model Context Protocol](https://modelcontextprotocol.io) · [Data from Metra](https://metra.com), then right-aligned (`margin-left: auto`) `UNOFFICIAL · NOT AFFILIATED WITH METRA` (uppercase, `letter-spacing: 0.1em`).

---

## Screen 2 — Copilot (`/copilot`)

**Purpose:** ask about trains in plain English and see the answer, plus which MCP tool produced it.

**Frame change from today:** the current app is a 720px centered rounded card. The redesign is **full viewport**: `height: 100vh; display: flex; flex-direction: column`, no radius, no outer card, no centering. Update `index.html`'s `#root` CSS accordingly (`display: block; padding: 0`, and drop `.app-container`'s `max-width`).

**Also removed:** the light/dark theme toggle and the `useTheme` hook. Modernist is a single light ground. `localStorage["metra-theme"]` and the `theme` field in the `/api/chat` body can go — but check the server prompt builder before deleting the field, and keep the request shape backward-compatible if anything else reads it.

### 2.1 Header

`padding: 12px 24px`, `border-bottom: 2px solid var(--color-divider)`, flex, wrap, `gap: 20px`.
- Brand block links to `/`, `margin-right: auto`: `METRA COPILOT` (Archivo 800, 17px, `letter-spacing: -0.01em`) + `CHICAGO COMMUTER RAIL` (11px uppercase `letter-spacing: 0.14em`, 50% ink).
- Status: an `8×8px` accent square animating `metraBlink` (`@keyframes metraBlink { 0%,100% { opacity: .25 } 50% { opacity: 1 } }`, `2s ease-in-out infinite`) + `LIVE FEED` (11px uppercase `letter-spacing: 0.14em`, `var(--color-accent-700)`). Replaces the old green dot — green is not in the palette.
- `NEW THREAD` secondary button, 11px uppercase `letter-spacing: 0.1em`; clears the thread, draft and loading flag.

### 2.2 Body grid

`flex: 1; min-height: 0; display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 240px)`. Left = thread + composer (`border-right: 1px solid var(--color-divider)`); right = line rail.

### 2.3 Empty state (thread length 0)

`padding: 48px 32px`, `max-width: 720px`, flush left:
- Eyebrow `TEN TOOLS · ELEVEN LINES · REALTIME GTFS` — 11px uppercase `letter-spacing: 0.16em`, `var(--color-accent-700)`.
- H1 **"Ask about any train on the system."** — Archivo 800, `clamp(32px, 5vw, 52px)`, `line-height: 1.0`, `letter-spacing: -0.03em`, `max-width: 18ch`.
- Lede: "Positions, delays, alerts and schedules — pulled live from Metra's public feeds through the MCP server." — 16px/1.5, `max-width: 50ch`, 72% ink.
- A `.hr` (2px), then `START HERE` (11px uppercase `letter-spacing: 0.14em`, 55% ink).
- Starter grid: `repeat(auto-fill, minmax(240px, 1fr))`, `gap: 8px`. Each is a left-aligned button, `1px solid var(--color-divider)`, `padding: 13px 15px`, hover `border-color: var(--color-accent)`; inside, an accent kicker (Archivo 800, 11px, uppercase, `letter-spacing: 0.1em`) over a 14px label:
  - `ALERTS` / "Any service alerts right now?"
  - `DEPARTURES` / "Next trains from Ogilvie to Kenosha"
  - `STATUS` / "Status overview for all eleven lines"
  - `SCHEDULE` / "UP-N schedule for tomorrow morning"
  Clicking a starter sends its label as the user message.

### 2.4 Thread

Messages are full-width rows, `padding: 20px 32px`, `border-bottom: 1px solid var(--color-divider)` — **no bubbles, no alternating alignment**. Each row opens with a meta line: a `7×7px` square, a role label (Archivo 800, 11px, uppercase, `letter-spacing: 0.14em`), and a right-aligned timestamp (11px, `letter-spacing: 0.08em`, 45% ink).

Three row types:

| Type | Label | Square | Label color | Row background | Content |
| --- | --- | --- | --- | --- | --- |
| user | `YOU` | `var(--color-neutral-400)` | 55% ink | `color-mix(in srgb, var(--color-text) 4%, transparent)` | the question, Archivo 600, 17px/1.4, `letter-spacing: -0.01em`, `max-width: 60ch` |
| tool call | `TOOL CALL` (stamp reads `MCP`) | `var(--color-neutral-400)` | 55% ink | transparent | a monospace `<code>` chip — `background: var(--color-surface)`, `1px solid var(--color-divider)`, `padding: 4px 9px`, 13px — plus a 55%-ink argument summary |
| answer | `COPILOT` | `var(--color-accent)` | `var(--color-accent-700)` | transparent | prose + optional result blocks + provenance line, `max-width: 68ch` |

Answer internals:
- Prose paragraph, 15px/1.55, `margin-bottom: 16px`.
- **Departure board block** (optional): `1px solid var(--color-divider)`. Head `padding: 10px 14px`, `border-bottom: 2px solid var(--color-divider)`: title (Archivo 800, 13px, uppercase, `letter-spacing: 0.08em`) + right-aligned stamp (11px uppercase `letter-spacing: 0.1em`, 50% ink). Rows: `grid-template-columns: 68px minmax(0, 1fr) auto`, `gap: 12px`, `padding: 10px 14px`, `border-bottom: 1px solid var(--color-divider)` — time (Archivo 800, 15px, tabular nums), destination (14px), status `.tag` uppercase `letter-spacing: 0.08em`.
- **Alert block** (optional): `border-left: 4px solid var(--color-accent)`, `background: var(--color-accent-100)`, `padding: 12px 16px`. Title Archivo 800, 11px, uppercase, `letter-spacing: 0.12em`, `var(--color-accent-800)`; body 14px/1.5, `var(--color-accent-900)`.
- **Provenance line**: 11px uppercase `letter-spacing: 0.1em`, 45% ink — e.g. `get_next_trains + get_trip_updates · 4 predictions`. Populate from the real tool-use blocks in the API response.

**Loading row:** `padding: 20px 32px` — `FETCHING LIVE DATA` (11px uppercase `letter-spacing: 0.14em`, `var(--color-accent-700)`) and three `5×5px` accent squares (not circles) blinking on `metraBlink 1.2s ease-in-out infinite` with `0s / 0.2s / 0.4s` delays.

**Error row:** render as an answer row whose content is the alert block, title `⚠ SYSTEM ERROR` → restyled to `SYSTEM ERROR` with no emoji, body = the escaped message. Keep the existing friendly copy for the abort case ("The request stalled — live data feeds may be slow. Please try again.").

**Autoscroll:** the prototype sets `parent.scrollTop = parent.scrollHeight` on update. Keep that approach — do **not** use `scrollIntoView` on a `100vh` layout; it fights the sticky header.

### 2.5 Composer

Pinned below the thread: `border-top: 2px solid var(--color-divider)`, `padding: 14px 24px`.
- `.input` (flex: 1), `min-height: 46px`, 15px, `background: var(--color-bg)`, `1px` divider border, placeholder "Ask about trains, schedules, alerts, stops" (no ellipsis). Focus is the design system's `:focus-visible` accent border — drop the old `box-shadow` glow.
- `SEND` primary button, `min-width: 108px`, uppercase, `letter-spacing: 0.08em`. Disabled while loading or when the draft is empty (`.btn:disabled` → 45% opacity).
- Enter (without Shift) sends. Below, two 11px uppercase `letter-spacing: 0.08em` 45%-ink hints, `gap: 14px`: "Enter to send" and "Responses are generated — verify before you board".
- Refocus the input ~100ms after a response completes (existing behavior — keep it).

### 2.6 Line rail (right column)

`overflow-y: auto`. Header `LINES` (11px uppercase `letter-spacing: 0.14em`, 55% ink, `padding: 14px 18px`, 1px bottom rule). Then eleven full-width buttons, `padding: 11px 18px`, `border-bottom: 1px solid var(--color-divider)`, hover `color-mix(in srgb, var(--color-text) 5%, transparent)`:
- code (Archivo 800, 12px, `letter-spacing: 0.06em`, `min-width: 46px`), terminal (12px, 60% ink, truncated with ellipsis), and a `7×7px` status square pushed right — `var(--color-neutral-400)` when normal, `var(--color-accent)` when the line has an alert or delay.
- Click sends `"<CODE> line status and next departures"`.
- Order and terminals: BNSF/Aurora, HC/Joliet, MD-N/Fox Lake, MD-W/Elgin, ME/University Park, NCS/Antioch, RI/Joliet, SWS/Manhattan, UP-N/Kenosha, UP-NW/Harvard, UP-W/Elburn. Wire the squares to `get_alerts` when available; the prototype flags MD-W and UP-N.
- Footer note, 11px/1.5, 45% ink: "Unofficial. Data from Metra's public GTFS feeds via the Metra MCP server."

**Responsive:** below ~860px, collapse the grid to one column and move the line rail below the composer (or behind a `LINES` disclosure in the header). The header, starter grid, endpoint grid and tools grid already wrap. Mobile must work; desktop is the primary target.

---

## Interactions & behavior summary

**Landing** — anchor scrolling; two clipboard copies with a 1600ms `COPIED` label; client picker swapping note + code. No fetches, no animation beyond CSS hovers. All transitions ≤150ms or none.

**Copilot** — real behavior is unchanged from today; only rendering differs:
1. `send(text)`: no-op on empty or while loading. Append the user message, clear the draft, set loading.
2. `POST /api/chat` with the full message history. Keep the AbortController with the **120000ms** stall timer, re-armed on every chunk.
3. If `content-type` is `text/event-stream`, read the body, accumulate `content_block_delta` / `text_delta` text, strip a leading ```` ```html ```` fence and trailing fence, and re-render progressively. Otherwise parse JSON and join the text blocks.
4. `sanitize()` every fragment through DOMPurify with the existing `FORBID_TAGS` / `FORBID_ATTR` lists. **Do not relax this** — fragments contain upstream alert text.
5. On error, append the alert-styled row and surface the friendly message.

**New, optional:** the tool-call rows. The current `/api/chat` proxies Anthropic SSE through verbatim, so `content_block_start` events for `tool_use` blocks are already in the stream — render one tool row per block as it opens (name from `block.name`, argument summary from the input deltas). If that's more than you want, drop the tool rows and the provenance line; everything else stands. Do **not** fabricate them client-side.

## State

**Landing:** `activeClient: "desktop" | "code" | "web"`, `copied: string | null` (with a 1600ms timer). No routing state.

**Copilot:** `messages: Array<{role: "user" | "tool" | "assistant", …}>` — the `tool` role is new and must be filtered out of the history sent to `/api/chat`; `input: string`; `loading: boolean`; `error: string | null`. Remove `theme` / `resolved`. Refs: input element, thread scroll container.

## Design tokens

From `design_system/styles.css` (bundled — serve it as-is or inline the `:root` block). Never hard-code these values inline; reference the variables.

```
--color-bg: #f3f2f2        --color-surface: #eae9e9
--color-text: #201e1d      --color-accent: #ec3013
--color-divider: color-mix(in srgb, #201e1d 40%, transparent)

neutral 100→900: #f8f4f4 #eae7e7 #d7d3d3 #bab6b6 #9b9797 #7d7979 #605d5d #444141 #2d2b2b
accent  100→900: #fff2ef #ffe0d9 #ffc4b8 #ff9783 #ff563c #dd2b0f #ae1800 #7c1405 #4d170e

--font-heading / --font-body: "Archivo", system-ui, sans-serif   (weights 400, 600, 800)
--space-1..8: 4 8 12 16 24 32 px
--radius-sm/md/lg: 0px   ← every corner, no exceptions
--shadow-sm/md/lg: (unused in these two screens — both are flat)
```

Type scale in use: 76/52/42 display · 32 h2 · 25 h3 · 17 lede · 15 body · 14 UI · 13 dense · 12 meta · 11 label. Uppercase labels carry `letter-spacing` 0.08–0.18em; display type carries `-0.02em` to `-0.03em`. Monospace (code only): `ui-monospace, "SFMono-Regular", Menlo, monospace` — the design system has no mono token, so this stack is the intended fallback. Body copy in accent color must use `--color-accent-700` or darker; `--color-accent` itself is for fills, chrome and large type only (3:1).

Rule weights are load-bearing: **2px** between major sections and above the composer, **1px** between rows inside a section. Do not soften them to hairlines.

## Assets

None. No images, no icon font, no SVG illustrations. The old locomotive `TrainIcon` SVG and all emoji (`🚂 ⚠ 📋 📍 🗺 🔴 ☀ 🌙 💻 ↗`) are **removed** — squares, rules and type do the work. If you later want icons, the design system specifies [Lucide](https://lucide.dev) as inline SVG on `currentColor`.

The existing favicons (`favicon.ico`, `favicon-square.png`, `Logo_Metra.png`) are untouched.

## Mock data

Sample values in the prototypes, so nothing here gets mistaken for real feed output: the landing's per-line train counts and service statuses, its `5:41 PM CT` stamp, and every Copilot reply (the OTC→Kenosha board with trains 349/351/353/355, the Ravenswood signal-work alert, and the "113 active trains · 9/11 on time" system summary). The Copilot prototype keyword-matches the input to pick one of three canned replies with 550ms/1400ms fake latency — replace all of it with the real `/api/chat` call.

## Files

In this bundle:
- `Metra MCP - Landing.dc.html` — landing design reference
- `Metra Copilot.dc.html` — Copilot design reference
- `support.js` — runtime that mounts the two references (open either file in a browser; not for production)
- `design_system/styles.css` — the Modernist token + component stylesheet
- `design_system/readme.md` — the design system's own rules (read the Do/Don't list before improvising)
- `github.md` — repo association and screen map

In `oconnorbz/metra-mcp` (targets):
- `src/metra_mcp/web/docs.html` → screen 1
- `src/metra_mcp/web/index.html` → Copilot page shell (drop the centered card, link the new stylesheet, consider dropping the Tailwind vendor script)
- `src/metra_mcp/web/app.jsx` → screen 2; rebuild `app.js` per the README's Frontend build step
- the `/api/chat` system prompt in `src/metra_mcp/server.py` → **must** be updated so model-generated fragments use the new token vocabulary
