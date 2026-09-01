// Landing-page behavior: clipboard copies, the client-snippet picker, and the
// live line board. Served from this origin because the page runs under a
// script-src 'self' CSP — there are no inline scripts anywhere in docs.html.
(function () {
  "use strict";

  // ── Copy buttons ──
  // Any [data-copy] element copies its value and flashes COPIED for 1.6s.
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    var original = btn.textContent;
    var timer = null;
    btn.addEventListener("click", function () {
      var done = function () {
        btn.textContent = "Copied";
        clearTimeout(timer);
        timer = setTimeout(function () { btn.textContent = original; }, 1600);
      };
      // navigator.clipboard is undefined on insecure origins; fail quietly
      // rather than surfacing a browser-specific error to the visitor.
      try {
        navigator.clipboard.writeText(btn.dataset.copy).then(done, function () {});
      } catch (e) { /* no clipboard access */ }
    });
  });

  // ── Client snippet picker ──
  var CLIENTS = {
    desktop: {
      note: "Add the server to your claude_desktop_config.json, then restart the app.",
      code: '{\n  "mcpServers": {\n    "metra": {\n      "command": "npx",\n      "args": ["mcp-remote", "https://metra.remote-mcp.dev/mcp"]\n    }\n  }\n}',
    },
    code: {
      note: "One command in your terminal — the transport is HTTP streamable.",
      code: "claude mcp add --transport http metra \\\n  https://metra.remote-mcp.dev/mcp",
    },
    web: {
      note: "Open the integrations menu, add a custom remote MCP server, and paste the URL below.",
      code: "https://metra.remote-mcp.dev/mcp",
    },
  };
  var noteEl = document.getElementById("client-note");
  var codeEl = document.getElementById("client-code");
  var picks = document.querySelectorAll(".m-client-pick");
  picks.forEach(function (btn) {
    btn.addEventListener("click", function () {
      var client = CLIENTS[btn.dataset.client];
      if (!client) return;
      picks.forEach(function (b) { b.setAttribute("aria-selected", String(b === btn)); });
      // textContent, not innerHTML: these are static strings, but the snippet
      // pane must never become an HTML injection point.
      noteEl.textContent = client.note;
      codeEl.textContent = client.code;
    });
  });

  // ── Live line board ──
  // /api/board is the real feed (positions + trip updates + alerts, bucketed
  // per route and cached server-side). On failure the table says so rather
  // than showing invented numbers.
  var LINES = [
    ["BNSF", "Burlington Northern Santa Fe", "Aurora"],
    ["HC", "Heritage Corridor", "Joliet"],
    ["MD-N", "Milwaukee District North", "Fox Lake"],
    ["MD-W", "Milwaukee District West", "Elgin"],
    ["ME", "Metra Electric", "University Park"],
    ["NCS", "North Central Service", "Antioch"],
    ["RI", "Rock Island", "Joliet"],
    ["SWS", "Southwest Service", "Manhattan"],
    ["UP-N", "Union Pacific North", "Kenosha"],
    ["UP-NW", "Union Pacific Northwest", "Harvard / McHenry"],
    ["UP-W", "Union Pacific West", "Elburn"],
  ];
  var body = document.getElementById("board-body");
  var stamp = document.getElementById("board-stamp");
  var caption = document.getElementById("board-caption");

  var esc = function (s) {
    return String(s).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  };

  var render = function (rows) {
    body.innerHTML = rows.map(function (r) {
      var onTime = r.status === "On time";
      return '<tr>' +
        '<td><span style="font-family: var(--font-heading); font-weight: 800; font-size: 13px; letter-spacing: 0.06em;">' + esc(r.code) + '</span></td>' +
        '<td style="font-size: 14px;">' + esc(r.name) + '</td>' +
        '<td style="font-size: 13px; color: color-mix(in srgb, var(--color-text) 65%, transparent);">' + esc(r.terminal) + '</td>' +
        '<td class="tnum" style="font-size: 14px;">' + esc(r.active) + '</td>' +
        '<td><span class="tag ' + (onTime ? "tag-neutral" : "tag-accent") + '" style="letter-spacing: 0.08em; text-transform: uppercase;">' + esc(r.status) + '</span></td>' +
        '</tr>';
    }).join("");
  };

  fetch("/api/board", { headers: { Accept: "application/json" } })
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (data) {
      if (!data || !Array.isArray(data.lines) || !data.lines.length) throw new Error("empty board");
      render(data.lines);
      stamp.textContent = "Live board · " + (data.stamp || "");
    })
    .catch(function () {
      // Names and terminals are static facts about the system, so the table
      // still renders; the live columns say "unavailable" instead of guessing.
      render(LINES.map(function (l) {
        return { code: l[0], name: l[1], terminal: l[2], active: "—", status: "Unavailable" };
      }));
      stamp.textContent = "Live board unavailable";
      caption.textContent = "Train counts and service state could not be fetched — the realtime feed is unreachable right now.";
    });
})();
