# Handoff addendum: `/stats` — usage dashboard

Third screen, same change as the landing and Copilot: the dark amber/zinc dashboard (`Inter` + `JetBrains Mono`, `#09090b`, 10px radii, colored semantic text) moves onto Modernist. `src/metra_mcp/web/modernist.css` and the `.m-*` application layer already exist in the repo from the first pass — **extend that file**, don't start a new one.

Design reference: `Metra MCP - Stats.dc.html` (a prototype, not shippable code — recreate it in `stats.html` + `stats.js`).

## What changes

- Delete the entire `<style>` block in `stats.html`. Link `/modernist.css` and the Archivo `<link>` pair exactly as `docs.html` does; drop the Google Fonts `@import` (it blocks, and JetBrains Mono/Inter are gone).
- Drop `.container`'s `max-width: 1200px; margin: 0 auto`. The page is **full-bleed and flush left** like the other two: `32px` horizontal padding, 2px rules between major sections, 1px between rows.
- Remove the locomotive SVG logo tile and the glowing green `.status-dot`. Brand is type: `METRA MCP STATS` + `USAGE`, and the live indicator is the accent square on `metraBlink`.
- Every semantic color goes: no `#f59e0b` values, no blue IPs (`#93c5fd`), no green success (`#4ade80`), no red errors (`#fca5a5`). Ink on ground, with `--color-accent` reserved for errors, the live square, and the bar fills.
- No corner radius anywhere — the stat cards, tables, inputs and badges all go square.

`stats.js` keeps its structure: same three fetches, same 30s `setInterval`, same `esc()`, same `fmtTs()`, same filter/`renderMcp`/`renderDash` split, same `td.args` expand-on-click delegation, same `X-Stats-Token` header and `redacted` detection. Only the HTML strings it emits change.

## Layout, top to bottom

### Header — reuse `.m-head`

Add `position: sticky; top: 0; z-index: 10`. Contents, in order:
- `.m-brand` linking to `/`: `.m-brand-name` `METRA MCP STATS` + `.m-brand-sub` `Usage`.
- Live indicator: `<span class="m-sq m-sq-live">` + an 11px uppercase `letter-spacing: 0.14em` `var(--color-accent-700)` label reading `AUTO-REFRESH · 22S` — count down from 30 with the existing interval and rewrite the label each second (a `setInterval` tick you already pay for). Replaces "Live usage — auto-refreshes every 30s".
- `.m-nav-link` × 2: `DOCS` → `/`, `COPILOT` → `/copilot`.
- `REFRESH` — `.btn.btn-secondary`, 11px uppercase `letter-spacing: 0.1em`. Keep `#refresh-btn`'s handler; it should also reset the countdown.

### Redaction notice (new — only when the API omits IPs)

The current page buries "requires the stats token" inside two table bodies. Hoist it: when `loadSummary()` computes `redacted`, render one band directly under the header, `padding: 11px 32px`, `border-bottom: 2px solid var(--color-divider)`, `background: var(--color-accent-100)`:
- `DETAIL REDACTED` — Archivo 800, 11px, uppercase, `letter-spacing: 0.12em`, `var(--color-accent-800)`.
- Then 13px `var(--color-accent-900)`: "Client IPs and user-agents are hidden. Open `/stats?token=…` with a valid stats token to see them."

Hide the band entirely when a valid token is present. Add as `.m-notice` / `.m-notice-label` in the application layer.

### Headline band — four cells, replacing the two `.stat-card` grids

One ruled band, not two boxed groups: **wrapping flex, not a grid** — `display: flex; flex-wrap: wrap; border-bottom: 2px solid var(--color-divider)` on the container, and `flex: 1 1 200px` on each cell (`padding: 22px 24px 20px`, 1px right and bottom rules, `min-width: 0`) as `.m-kpi`.

**Four cells, not five** — the count is load-bearing. Five is prime, so no wrap-based track definition can balance it: on an `auto-fit` grid the fifth cell orphans beside dead tracks, and under `flex-grow` it stretches to the full page width as a lone banner. Both happen below ~1150px, i.e. most real browser windows. Four wraps cleanly 4-up, then 2+2, then 1-up, filling every row at every width. Keep it at four, and do not substitute `repeat(auto-fit, minmax(…))` for the flex.

Cell internals: a `7×7px` square + an `.m-label` (`margin-bottom: 12px`), then the value — Archivo 800, `clamp(30px, 3.4vw, 44px)`, `line-height: 1`, `letter-spacing: -0.03em`, tabular nums — then an 11px uppercase `letter-spacing: 0.08em` 45%-ink sub-line.

| Label | Value | Sub | Square | Value color |
| --- | --- | --- | --- | --- |
| MCP tool calls | `m.total` | All time | accent | ink |
| Errors | `m.errors` | `X.X% error rate` (blank if total is 0) | accent | `--color-accent-700` |
| MCP clients | `m.unique_ips` | Unique IPs | `--color-neutral-400` | ink |
| Dashboard events | `d.total` | `1,188 unique visitors` (`d.unique_ips`) | `--color-neutral-400` | ink |

Keep `toLocaleString()` on every count. The existing element ids (`#mcp-total`, `#mcp-errors`, `#mcp-errpct`, `#mcp-ips`, `#dash-total`, `#dash-ips`) can stay — only their wrappers change; `#dash-ips` moves into the dashboard cell's sub-line rather than getting a cell of its own.

### Four top-lists — ruled lists with bars, replacing the four mini tables

Same wrapping-flex treatment as the KPI band, for the same orphan reason: `display: flex; flex-wrap: wrap` on the container with `flex: 1 1 340px` per panel, 1px right/bottom rules per panel, 2px rule closing the band. Two-up at typical desktop widths, one-up on mobile, never a dangling empty track. Each panel (`.m-panel`):
- Head, `padding: 16px 24px 12px`: title (Archivo 800, 13px, uppercase, `letter-spacing: 0.08em`) + right-aligned 11px uppercase 45%-ink unit note (`CALLS` / `HITS`).
- Rows, `padding: 9px 24px`, `border-bottom: 1px solid var(--color-divider)`, `border-top` on the first: name (13px, monospace, ellipsis-truncated) · optional middle tag (11px uppercase 45% ink — the event type on the paths panel) · count pushed right (Archivo 800, 14px, tabular nums).
- Under each row, a **3px proportional bar**: track `color-mix(in srgb, var(--color-text) 10%, transparent)`, fill `var(--color-accent)` at `count / max * 100%` where `max` is the panel's top row. Flat, square, no label. This is the one new data graphic — it replaces the old right-aligned amber numerals as the thing your eye scans.

Panels: **Top tools** (`m.top_tools`, calls) · **Top MCP clients** (`m.top_ips`, calls) · **Top paths** (`d.top_paths`, hits, with `event_type` as the middle tag) · **Top dashboard clients** (`d.top_ips`, hits).

Empty and redacted states are plain 13px 45%-ink text at `padding: 14px 24px 20px` — not a `colspan` row: "No data yet." / "Requires the stats token — open /stats?token=… to reveal." Drop `.muted`'s italics and centering.

### Recent tool calls

Section head at `padding: 36px 32px 16px`: `<h2>Recent tool calls</h2>` (32px, `letter-spacing: -0.02em`) + an `.m-sec-note` reading `LAST 42 OF 300` — filtered count over the fetch limit; recompute on every filter keystroke.

Controls: the existing `#mcp-filter` as `.input` (`min-height: 38px`, `background: var(--color-bg)`, `max-width: 560px` on the row, placeholder "Filter by tool, IP, arguments") plus a `CLEAR` `.btn.btn-secondary` that empties the field and re-renders. The old amber `Refresh` button moves to the header.

Table: `.table` from the design system inside `overflow-x: auto`, `min-width: 1040px`, `border-top: 1px solid var(--color-divider)`. First and last cells take `padding-left: 32px` / `padding-right: 32px` so the columns align with the page edge. Columns and treatments:

| Column | Width | Treatment |
| --- | --- | --- |
| Time (UTC) | 150px | monospace 12px, `nowrap`, 55% ink |
| IP | 128px | monospace 12px, ink — 35% ink when the value is `hidden` |
| Tool | 172px | monospace 13px/600, **ink not accent** |
| Arguments | flex | monospace 12px, 72% ink, ellipsis; keep the click-to-expand (`white-space: normal; word-break: break-all`) |
| Status | 130px | `.tag.tag-neutral` `OK` / `.tag.tag-accent` `ERR`, uppercase `letter-spacing: 0.1em`, `font-weight: 600`; on failure the message follows in 11px `var(--color-accent-700)`, truncated |
| Duration | 92px | right-aligned, tabular nums, 13px, `—` when null |
| User-agent | 190px | 12px, 50% ink, ellipsis, `title` attr for the full string |

Row hover is `.table`'s built-in 4% ink tint — remove `tr:hover td { background: #18181b }`.

Empty state: 13px 45%-ink line below the table, "No tool calls match that filter." (or "No tool calls yet." when unfiltered).

### Recent dashboard events

Same section pattern, `min-width: 900px`. Columns: Time (150px) · IP (128px) · Path (150px, monospace 13px) · Event (140px) · Details (flex, 13px, 72% ink, ellipsis + click-to-expand) · User-agent (190px).

Event badge: `.tag.tag-outline` for `chat_query`, `.tag.tag-neutral` for everything else, uppercase `letter-spacing: 0.1em`, `font-weight: 600`. The four old `.badge-*` colors (green/red/blue/purple) all collapse into this two-way outline/neutral distinction — `.tag-accent` stays reserved for errors so a red cell always means something went wrong.

### Footer — reuse `.m-foot`

"Metra MCP Server" · `/` "Docs" · `/copilot` "Copilot", then right-aligned `EVENTS RETAINED IN SQLITE · TIMES IN UTC`.

## Responsive

Below 860px: the headline band and both panel groups collapse on their own via `flex-wrap` — no grid-template overrides needed. Add to the existing `@media (max-width: 860px)` block — `.m-kpi`, `.m-panel` and the section heads drop to `padding-left: 20px; padding-right: 20px`, and the two big tables stay horizontally scrollable (do not stack them into cards).

## Mock data

Everything in the prototype is invented — the 18,412 calls, 1.2% error rate, 63 clients, the tool/IP/path rankings, and every row in both tables (including the "upstream feed timeout after 8s" and "invalid route_id: 'UPN'" errors). All of it comes from `/api/stats/*` in the real page; nothing needs porting.

## New classes to add to `modernist.css`

Append to the `.m-*` application layer, tokens only: `.m-notice`, `.m-notice-label`, `.m-kpi`, `.m-kpi-val`, `.m-kpi-sub`, `.m-panel`, `.m-panel-head`, `.m-panel-title`, `.m-panel-note`, `.m-rank`, `.m-rank-name`, `.m-rank-count`, `.m-bar`, `.m-bar-fill`, `.m-filter-row`, `.m-empty-note`. Bar widths are the one inline style the page needs (`style="width: 42%"`) — that's fine here; unlike the Copilot, `/stats` renders no model output, so nothing on this page passes through DOMPurify.

## Files

- `Metra MCP - Stats.dc.html` — this screen's design reference (in this bundle)
- `src/metra_mcp/web/stats.html` → the markup rewrite
- `src/metra_mcp/web/stats.js` → same logic, new HTML strings, plus the countdown label and the hoisted redaction band
- `src/metra_mcp/web/modernist.css` → append the classes above
