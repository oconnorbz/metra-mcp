const $ = (id) => document.getElementById(id);

// Visit /stats?token=... to see client IPs / user-agents; without a valid
// METRA_STATS_TOKEN the API redacts them and the columns show "hidden".
const STATS_TOKEN = new URLSearchParams(location.search).get("token");
const api = (path) => fetch(path, STATS_TOKEN ? { headers: { "X-Stats-Token": STATS_TOKEN } } : undefined);

const ROW_LIMIT = 300;
const REFRESH_SEC = 30;

function esc(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function fmtTs(ts) {
  if (!ts) return "";
  return ts.replace("T", " ").replace("+00:00", "").replace("Z", "");
}

// ── Top-list panels ────────────────────────────────────────────────────────
// Each row carries a 3px bar proportional to the panel's own top row, so the
// four panels are read individually rather than against each other.
function renderPanel(el, rows, opts) {
  if (!rows.length) {
    el.innerHTML = `<div class="m-empty-note">${
      opts.redacted
        ? "Requires the stats token &mdash; open /stats?token=&hellip; to reveal."
        : "No data yet."
    }</div>`;
    return;
  }
  const max = rows.reduce((n, r) => Math.max(n, r.count), 0) || 1;
  el.innerHTML = rows.map(r => {
    const pct = (r.count / max * 100).toFixed(1);
    return `<div class="m-rank">
      <div class="m-rank-row">
        <span class="m-rank-name" title="${esc(r.name)}">${esc(r.name)}</span>
        ${r.tag ? `<span class="m-rank-tag">${esc(r.tag)}</span>` : ""}
        <span class="m-rank-count">${r.count.toLocaleString()}</span>
      </div>
      <div class="m-bar"><div class="m-bar-fill" style="width: ${pct}%"></div></div>
    </div>`;
  }).join("");
}

async function loadSummary() {
  const r = await api("/api/stats/summary");
  const s = await r.json();
  const m = s.mcp, d = s.dashboard;
  // The API omits IP lists entirely (not just empties them) when no valid
  // stats token was presented — say so once, at the top, rather than
  // repeating "hidden" inside every panel.
  const redacted = !("top_ips" in m);
  $("redaction-notice").hidden = !redacted;
  m.top_ips = m.top_ips || [];
  d.top_ips = d.top_ips || [];

  $("mcp-total").textContent = m.total.toLocaleString();
  $("mcp-errors").textContent = m.errors.toLocaleString();
  $("mcp-errpct").textContent = m.total > 0 ? `${(m.errors / m.total * 100).toFixed(1)}% error rate` : "";
  $("mcp-ips").textContent = m.unique_ips.toLocaleString();
  $("dash-total").textContent = d.total.toLocaleString();
  $("dash-ips").textContent = d.unique_ips.toLocaleString();

  renderPanel($("top-tools"), m.top_tools.map(t => ({ name: t.tool_name, count: t.count })), {});
  renderPanel($("top-mcp-ips"), m.top_ips.map(i => ({ name: i.ip, count: i.count })), { redacted });
  renderPanel($("top-dash-paths"), d.top_paths.map(p => ({ name: p.path, tag: p.event_type, count: p.count })), {});
  renderPanel($("top-dash-ips"), d.top_ips.map(i => ({ name: i.ip, count: i.count })), { redacted });
}

let mcpRows = [], dashRows = [];

async function loadMcpCalls() {
  const r = await api(`/api/stats/mcp?limit=${ROW_LIMIT}`);
  const j = await r.json();
  mcpRows = j.calls;
  renderMcp();
}

async function loadDashEvents() {
  const r = await api(`/api/stats/dashboard?limit=${ROW_LIMIT}`);
  const j = await r.json();
  dashRows = j.events;
  renderDash();
}

// "LAST 42 OF 300" — what the table shows over what was fetched. The
// denominator is the loaded count, not the limit, so it stays honest before
// the database has ROW_LIMIT rows in it.
const countNote = (shown, loaded) => `Last ${shown.toLocaleString()} of ${loaded.toLocaleString()}`;

const ipCell = (r) => "ip" in r
  ? `<td class="m-cell-ip">${esc(r.ip || "—")}</td>`
  : `<td class="m-cell-ip m-cell-hidden">hidden</td>`;

const uaCell = (r) => "user_agent" in r
  ? `<td class="m-cell-ua" title="${esc(r.user_agent || "")}">${esc(r.user_agent || "—")}</td>`
  : `<td class="m-cell-ua m-cell-hidden">hidden</td>`;

function renderMcp() {
  const q = $("mcp-filter").value.toLowerCase();
  const rows = mcpRows.filter(r => !q || [r.tool_name, r.ip, r.arguments, r.user_agent, r.error].some(v => (v || "").toLowerCase().includes(q)));
  $("mcp-count-note").textContent = countNote(rows.length, mcpRows.length);
  $("mcp-rows").innerHTML = rows.map(r => `
      <tr>
        <td class="m-cell-ts">${esc(fmtTs(r.ts))}</td>
        ${ipCell(r)}
        <td class="m-cell-tool">${esc(r.tool_name)}</td>
        <td class="m-cell-blob">${esc(r.arguments || "{}")}</td>
        <td>${r.success
          ? '<span class="tag tag-neutral m-stats-tag">OK</span>'
          : `<span class="tag tag-accent m-stats-tag">ERR</span><span class="m-cell-err" title="${esc(r.error || "")}">${esc(r.error || "")}</span>`}</td>
        <td class="m-cell-num">${r.duration_ms != null ? r.duration_ms + "ms" : "—"}</td>
        ${uaCell(r)}
      </tr>
    `).join("");
  const empty = $("mcp-empty");
  empty.hidden = rows.length > 0;
  empty.textContent = q ? "No tool calls match that filter." : "No tool calls yet.";
}

function renderDash() {
  const q = $("dash-filter").value.toLowerCase();
  const rows = dashRows.filter(r => !q || [r.path, r.ip, r.event_type, r.details, r.user_agent].some(v => (v || "").toLowerCase().includes(q)));
  $("dash-count-note").textContent = countNote(rows.length, dashRows.length);
  $("dash-rows").innerHTML = rows.map(r => `
      <tr>
        <td class="m-cell-ts">${esc(fmtTs(r.ts))}</td>
        ${ipCell(r)}
        <td class="m-cell-path">${esc(r.path || "—")}</td>
        <td><span class="tag ${r.event_type === "chat_query" ? "tag-outline" : "tag-neutral"} m-stats-tag">${esc(r.event_type)}</span></td>
        <td class="m-cell-blob m-cell-detail">${esc(r.details || "")}</td>
        ${uaCell(r)}
      </tr>
    `).join("");
  const empty = $("dash-empty");
  empty.hidden = rows.length > 0;
  empty.textContent = q ? "No events match that filter." : "No dashboard events yet.";
}

// ── Refresh loop ───────────────────────────────────────────────────────────
// One 1s timer drives both the header countdown and the reload, so the label
// can't drift away from when the fetch actually happens.
let remaining = REFRESH_SEC, inFlight = false;

function paintCountdown() {
  $("live-label").textContent = `Auto-refresh · ${remaining}s`;
}

async function refresh() {
  if (inFlight) return;
  inFlight = true;
  remaining = REFRESH_SEC;
  paintCountdown();
  try {
    await Promise.all([loadSummary(), loadMcpCalls(), loadDashEvents()]);
  } finally {
    inFlight = false;
  }
}

setInterval(() => {
  if (inFlight) return;
  remaining -= 1;
  if (remaining <= 0) refresh();
  else paintCountdown();
}, 1000);

$("mcp-filter").addEventListener("input", renderMcp);
$("dash-filter").addEventListener("input", renderDash);
$("mcp-clear").addEventListener("click", () => { $("mcp-filter").value = ""; renderMcp(); });
$("dash-clear").addEventListener("click", () => { $("dash-filter").value = ""; renderDash(); });
$("refresh-btn").addEventListener("click", refresh);
// Expand/collapse long argument and detail cells via delegation (no inline
// handlers, so the page can run under a script-src 'self' CSP).
document.addEventListener("click", (e) => {
  const td = e.target.closest("td.m-cell-blob");
  if (td) td.classList.toggle("expanded");
});

paintCountdown();
refresh();
