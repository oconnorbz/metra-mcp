const $ = (id) => document.getElementById(id);

// Visit /stats?token=... to see client IPs / user-agents; without a valid
// METRA_STATS_TOKEN the API redacts them and the columns show "—".
const STATS_TOKEN = new URLSearchParams(location.search).get("token");
const api = (path) => fetch(path, STATS_TOKEN ? { headers: { "X-Stats-Token": STATS_TOKEN } } : undefined);

function esc(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function fmtTs(ts) {
  if (!ts) return "";
  return ts.replace("T", " ").replace("+00:00", "").replace("Z", "");
}

async function loadSummary() {
  const r = await api("/api/stats/summary");
  const s = await r.json();
  const m = s.mcp, d = s.dashboard;
  // The API omits IP lists entirely (not just empties them) when no valid
  // stats token was presented — tell the viewer that, rather than "no data".
  const redacted = !("top_ips" in m);
  const hiddenRow = (cols) => `<tr><td colspan="${cols}" class="muted">hidden &mdash; IPs require the stats token (open /stats?token=&hellip;)</td></tr>`;
  m.top_ips = m.top_ips || [];
  d.top_ips = d.top_ips || [];
  $("mcp-total").textContent = m.total.toLocaleString();
  $("mcp-errors").textContent = m.errors.toLocaleString();
  $("mcp-errpct").textContent = m.total > 0 ? `${(m.errors / m.total * 100).toFixed(1)}% error rate` : "";
  $("mcp-ips").textContent = m.unique_ips.toLocaleString();
  $("dash-total").textContent = d.total.toLocaleString();
  $("dash-ips").textContent = d.unique_ips.toLocaleString();

  $("top-tools").innerHTML = m.top_tools.length
    ? m.top_tools.map(t => `<tr><td class="tool">${esc(t.tool_name)}</td><td class="num">${t.count}</td></tr>`).join("")
    : `<tr><td colspan="2" class="muted">no data yet</td></tr>`;
  $("top-mcp-ips").innerHTML = m.top_ips.length
    ? m.top_ips.map(i => `<tr><td class="ip">${esc(i.ip)}</td><td class="num">${i.count}</td></tr>`).join("")
    : redacted ? hiddenRow(2) : `<tr><td colspan="2" class="muted">no data yet</td></tr>`;
  $("top-dash-paths").innerHTML = d.top_paths.length
    ? d.top_paths.map(p => `<tr><td>${esc(p.path)}</td><td>${esc(p.event_type)}</td><td class="num">${p.count}</td></tr>`).join("")
    : `<tr><td colspan="3" class="muted">no data yet</td></tr>`;
  $("top-dash-ips").innerHTML = d.top_ips.length
    ? d.top_ips.map(i => `<tr><td class="ip">${esc(i.ip)}</td><td class="num">${i.count}</td></tr>`).join("")
    : redacted ? hiddenRow(2) : `<tr><td colspan="2" class="muted">no data yet</td></tr>`;
}

let mcpRows = [], dashRows = [];

async function loadMcpCalls() {
  const r = await api("/api/stats/mcp?limit=300");
  const j = await r.json();
  mcpRows = j.calls;
  renderMcp();
}

async function loadDashEvents() {
  const r = await api("/api/stats/dashboard?limit=300");
  const j = await r.json();
  dashRows = j.events;
  renderDash();
}

function renderMcp() {
  const q = $("mcp-filter").value.toLowerCase();
  const rows = mcpRows.filter(r => !q || [r.tool_name, r.ip, r.arguments, r.user_agent, r.error].some(v => (v || "").toLowerCase().includes(q)));
  $("mcp-rows").innerHTML = rows.length
    ? rows.map(r => `
      <tr>
        <td class="ts">${esc(fmtTs(r.ts))}</td>
        <td class="ip">${"ip" in r ? esc(r.ip || "—") : "hidden"}</td>
        <td class="tool">${esc(r.tool_name)}</td>
        <td class="args">${esc(r.arguments || "{}")}</td>
        <td>${r.success ? '<span class="badge badge-ok">OK</span>' : `<span class="badge badge-err">ERR</span> <span class="error">${esc(r.error || "")}</span>`}</td>
        <td class="num">${r.duration_ms != null ? r.duration_ms + "ms" : "—"}</td>
        <td class="ua" title="${esc(r.user_agent || "")}">${"user_agent" in r ? esc(r.user_agent || "—") : "hidden"}</td>
      </tr>
    `).join("")
    : `<tr><td colspan="7" class="muted">no MCP calls yet</td></tr>`;
}

function renderDash() {
  const q = $("dash-filter").value.toLowerCase();
  const rows = dashRows.filter(r => !q || [r.path, r.ip, r.event_type, r.details, r.user_agent].some(v => (v || "").toLowerCase().includes(q)));
  $("dash-rows").innerHTML = rows.length
    ? rows.map(r => `
      <tr>
        <td class="ts">${esc(fmtTs(r.ts))}</td>
        <td class="ip">${"ip" in r ? esc(r.ip || "—") : "hidden"}</td>
        <td>${esc(r.path || "—")}</td>
        <td><span class="badge ${r.event_type === 'chat_query' ? 'badge-chat' : 'badge-pv'}">${esc(r.event_type)}</span></td>
        <td class="args">${esc(r.details || "")}</td>
        <td class="ua" title="${esc(r.user_agent || "")}">${"user_agent" in r ? esc(r.user_agent || "—") : "hidden"}</td>
      </tr>
    `).join("")
    : `<tr><td colspan="6" class="muted">no dashboard events yet</td></tr>`;
}

async function refresh() {
  await Promise.all([loadSummary(), loadMcpCalls(), loadDashEvents()]);
}

$("mcp-filter").addEventListener("input", renderMcp);
$("dash-filter").addEventListener("input", renderDash);
$("refresh-btn").addEventListener("click", refresh);
// Expand/collapse long argument cells via delegation (no inline handlers, so
// the page can run under a script-src 'self' CSP).
document.addEventListener("click", (e) => {
  const td = e.target.closest("td.args");
  if (td) td.classList.toggle("expanded");
});

refresh();
setInterval(refresh, 30000);
