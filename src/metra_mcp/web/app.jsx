const { useState, useRef, useEffect, useCallback } = React;

// The system prompt is pinned server-side (/api/chat builds it). It tells the
// model to build fragments out of the .mc-* classes in modernist.css and
// nothing else, which is why this page ships no Tailwind and no inline styles
// from the model.
//
// Model HTML is sanitized before it touches the DOM — it is built from live
// upstream feed data (alert text etc.), which we don't trust to be markup-safe.
// Beyond DOMPurify's defaults we also drop form controls and <style>, so a
// prompt-injected response can't render a credential-looking form or restyle
// the page; the design system only needs divs/spans/lists/links.
const sanitize = (html) => DOMPurify.sanitize(html, {
  USE_PROFILES: { html: true },
  FORBID_TAGS: ["form", "input", "textarea", "select", "button", "style", "iframe", "object", "embed"],
  FORBID_ATTR: ["style", "formaction", "action"],
});
const escHtml = (s) => String(s).replace(/[&<>"']/g, ch => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[ch]));

const STARTERS = [
  { kicker: "Alerts", label: "Any service alerts right now?" },
  { kicker: "Departures", label: "Next trains from Ogilvie to Kenosha" },
  { kicker: "Status", label: "Status overview for all eleven lines" },
  { kicker: "Schedule", label: "UP-N schedule for tomorrow morning" },
];

// Code + terminal only — the rail is a launcher, not a data table. Live
// service state comes from /api/board and only colors the status square.
const LINES = [
  ["BNSF", "Aurora"], ["HC", "Joliet"], ["MD-N", "Fox Lake"],
  ["MD-W", "Elgin"], ["ME", "University Park"], ["NCS", "Antioch"],
  ["RI", "Joliet"], ["SWS", "Manhattan"], ["UP-N", "Kenosha"],
  ["UP-NW", "Harvard"], ["UP-W", "Elburn"],
];

const stamp = () => new Date().toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });

// Some models wrap the fragment in a ```html fence — strip it so the markup
// renders instead of displaying as literal text.
const stripFences = (s) => s.trim().replace(/^```(?:html)?\s*/i, "").replace(/```\s*$/, "");

// "stop_id=OTC · limit=4" from a tool's JSON input. Long values are clipped:
// this is a trace line, not a payload dump.
const summarizeArgs = (raw) => {
  if (!raw) return "";
  let obj;
  try { obj = JSON.parse(raw); } catch { return ""; }
  if (!obj || typeof obj !== "object") return "";
  return Object.keys(obj).slice(0, 4).map(k => {
    const v = obj[k];
    const s = typeof v === "object" ? JSON.stringify(v) : String(v);
    return `${k}=${s.length > 32 ? s.slice(0, 32) + "…" : s}`;
  }).join(" · ");
};

function Meta({ who, accent, at }) {
  return (
    <div className="m-meta">
      <span className={accent ? "m-meta-sq m-meta-sq-accent" : "m-meta-sq"} />
      <span className={accent ? "m-role m-role-accent" : "m-role"}>{who}</span>
      <span className="m-stamp">{at}</span>
    </div>
  );
}

function Row({ msg }) {
  if (msg.role === "user") {
    return (
      <div className="m-msg m-msg-user">
        <Meta who="You" at={msg.at} />
        <div className="m-ask">{msg.content}</div>
      </div>
    );
  }
  if (msg.role === "tool") {
    return (
      <div className="m-msg">
        <Meta who="Tool call" at="MCP" />
        <div className="m-toolcall">
          <code>{msg.name}</code>
          {msg.args ? <span className="m-toolcall-args">{msg.args}</span> : null}
        </div>
      </div>
    );
  }
  return (
    <div className="m-msg">
      <Meta who="Copilot" accent at={msg.at} />
      <div className="m-answer">
        <div className="mc" dangerouslySetInnerHTML={{ __html: sanitize(msg.html) }} />
        {msg.source ? <div className="mc-note" style={{ marginTop: "14px" }}>{msg.source}</div> : null}
      </div>
    </div>
  );
}

function MetraCopilot() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [alerted, setAlerted] = useState({});
  const threadRef = useRef(null);
  const inputRef = useRef(null);

  // Keep the thread pinned to the bottom. scrollIntoView fights the fixed
  // 100vh layout, so scroll the container itself — except on narrow screens,
  // where the pane isn't the scroller and the document is.
  useEffect(() => {
    const el = threadRef.current;
    if (!el) return;
    if (el.scrollHeight > el.clientHeight) el.scrollTop = el.scrollHeight;
    else window.scrollTo(0, document.body.scrollHeight);
  }, [messages, loading]);

  // Which lines currently have an alert or delay — colors the rail squares.
  useEffect(() => {
    let cancelled = false;
    const load = () => {
      fetch("/api/board", { headers: { Accept: "application/json" } })
        .then(r => (r.ok ? r.json() : null))
        .then(data => {
          if (cancelled || !data || !Array.isArray(data.lines)) return;
          const map = {};
          data.lines.forEach(l => { map[l.code] = l.status !== "On time"; });
          setAlerted(map);
        })
        .catch(() => {});
    };
    load();
    const t = setInterval(load, 60000);
    return () => { cancelled = true; clearInterval(t); };
  }, []);

  const send = useCallback(async (text) => {
    if (!text.trim() || loading) return;

    const userMsg = { role: "user", content: text, at: stamp() };
    const history = [...messages, userMsg];
    setMessages(history);
    setInput("");
    setLoading(true);

    // Rows produced by this turn, appended to `history` on every update.
    const live = [];
    const paint = () => setMessages([...history, ...live]);

    // Abort if the stream goes quiet for 2 minutes (upstream stall) rather
    // than leaving the spinner running forever.
    const controller = new AbortController();
    let stallTimer;
    const armStallTimer = () => {
      clearTimeout(stallTimer);
      stallTimer = setTimeout(() => controller.abort(), 120000);
    };

    try {
      armStallTimer();
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          // Tool rows are local trace UI; the model never sees them back.
          messages: history
            .filter(m => m.role === "user" || m.role === "assistant")
            .map(m => ({ role: m.role, content: m.content || m.html || "" })),
        })
      });

      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        throw new Error(errData.error?.message || errData.error || `HTTP ${response.status}`);
      }

      const ctype = response.headers.get("content-type") || "";

      if (ctype.includes("text/event-stream")) {
        // The proxy streams Anthropic SSE through verbatim. Content blocks
        // arrive in order — text, MCP tool calls, more text — so rendering
        // one row per block as it opens gives a real tool trace rather than
        // a fabricated one.
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buf = "", upstreamErr = null;
        let textRow = null, textRaw = "", toolRow = null, toolJson = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          armStallTimer();
          buf += decoder.decode(value, { stream: true });
          const events = buf.split("\n\n");
          buf = events.pop();
          let dirty = false;

          for (const evt of events) {
            const lines = evt.split("\n");
            const evtName = (lines.find(l => l.startsWith("event:")) || "").slice(6).trim();
            const data = lines.filter(l => l.startsWith("data:")).map(l => l.slice(5).trim()).join("");
            if (!data) continue;
            let obj;
            try { obj = JSON.parse(data); } catch { continue; }

            if (obj.type === "content_block_start") {
              const block = obj.content_block || {};
              if (block.type === "text") {
                textRow = { role: "assistant", html: "", at: stamp() };
                textRaw = "";
                live.push(textRow);
                dirty = true;
              } else if (block.type === "tool_use" || block.type === "mcp_tool_use") {
                toolRow = { role: "tool", name: block.name || "tool", args: summarizeArgs(JSON.stringify(block.input || null)) };
                toolJson = "";
                live.push(toolRow);
                dirty = true;
              } else {
                // tool results and anything else stay out of the trace.
                textRow = null;
                toolRow = null;
              }
            } else if (obj.type === "content_block_delta" && obj.delta) {
              if (obj.delta.type === "text_delta" && textRow) {
                textRaw += obj.delta.text;
                textRow.html = stripFences(textRaw);
                dirty = true;
              } else if (obj.delta.type === "input_json_delta" && toolRow) {
                toolJson += obj.delta.partial_json || "";
              }
            } else if (obj.type === "content_block_stop") {
              if (toolRow && toolJson) {
                toolRow.args = summarizeArgs(toolJson);
                dirty = true;
              }
              textRow = null;
              toolRow = null;
            } else if (evtName === "error" || obj.type === "error" || obj.error) {
              // The proxy wraps any upstream refusal in `event: error`. When
              // an LLM gateway sits in front of Anthropic it answers in its
              // own shape, so don't assume {error:{message}} — keying off the
              // event name is what keeps a gateway 401 from rendering as an
              // empty answer.
              upstreamErr = (obj.error && obj.error.message)
                || obj.err_msg
                || JSON.stringify(obj.error || obj);
            }
          }
          if (dirty) paint();
        }
        if (upstreamErr) throw new Error(upstreamErr);
      } else {
        const data = await response.json();
        (data.content || []).forEach(b => {
          if (b.type === "text") {
            live.push({ role: "assistant", html: stripFences(b.text || ""), at: stamp() });
          } else if (b.type === "tool_use" || b.type === "mcp_tool_use") {
            live.push({ role: "tool", name: b.name || "tool", args: summarizeArgs(JSON.stringify(b.input || null)) });
          }
        });
      }

      // Provenance: which tools actually produced this answer, on the last
      // answer row. Derived from the stream — never invented.
      const used = [...new Set(live.filter(m => m.role === "tool").map(m => m.name))];
      const answers = live.filter(m => m.role === "assistant");
      if (used.length && answers.length) {
        answers[answers.length - 1].source = `${used.join(" + ")} · ${used.length === 1 ? "1 tool call" : used.length + " tools"}`;
      }
      if (!live.some(m => m.role === "assistant" && m.html.trim())) {
        live.push({ role: "assistant", html: "<p>No answer came back from the model. Try asking again.</p>", at: stamp() });
      }
      paint();
    } catch (err) {
      const friendly = err.name === "AbortError"
        ? "The request stalled — live data feeds may be slow. Please try again."
        : err.message;
      live.push({
        role: "assistant",
        at: stamp(),
        html: `<div class="mc-alert"><div class="mc-alert-title">System error</div>` +
              `<div class="mc-alert-body">${escHtml(friendly)}</div></div>`,
      });
      paint();
    } finally {
      clearTimeout(stallTimer);
      setLoading(false);
      setTimeout(() => inputRef.current?.focus(), 100);
    }
  }, [messages, loading]);

  return (
    <div className="m-app">

      <div className="m-head">
        <a href="/" className="m-brand">
          <span className="m-brand-name">METRA COPILOT</span>
          <span className="m-brand-sub">Chicago commuter rail</span>
        </a>
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          <span className="m-sq m-sq-live" />
          <span style={{ fontSize: "11px", letterSpacing: "0.14em", textTransform: "uppercase", color: "var(--color-accent-700)" }}>Live feed</span>
        </div>
        <button
          type="button"
          className="btn btn-secondary"
          style={{ fontSize: "11px", letterSpacing: "0.1em", textTransform: "uppercase" }}
          onClick={() => { setMessages([]); setInput(""); setLoading(false); }}>
          New thread
        </button>
      </div>

      <div className="m-body">

        <div className="m-thread-col">
          <div className="m-thread" ref={threadRef}>
            {messages.length === 0 && (
              <div className="m-empty">
                <div style={{ fontSize: "11px", letterSpacing: "0.16em", textTransform: "uppercase", color: "var(--color-accent-700)", marginBottom: "20px" }}>
                  Ten tools · eleven lines · realtime GTFS
                </div>
                <h1>Ask about any train on the system.</h1>
                <p style={{ fontSize: "16px", lineHeight: 1.5, maxWidth: "50ch", margin: "0 0 32px", color: "color-mix(in srgb, var(--color-text) 72%, transparent)" }}>
                  Positions, delays, alerts and schedules — pulled live from Metra's public feeds through the MCP server.
                </p>
                <div className="hr" style={{ margin: "0 0 24px" }} />
                <div className="m-label" style={{ letterSpacing: "0.14em", marginBottom: "12px" }}>Start here</div>
                <div className="m-starters">
                  {STARTERS.map((s, i) => (
                    <button key={i} type="button" className="m-starter" onClick={() => send(s.label)}>
                      <span className="m-starter-kicker">{s.kicker}</span>
                      <span>{s.label}</span>
                    </button>
                  ))}
                </div>
              </div>
            )}

            {messages.map((m, i) => <Row key={i} msg={m} />)}

            {loading && (
              <div className="m-loading">
                <span className="m-loading-label">Fetching live data</span>
                <span style={{ display: "flex", gap: "4px" }}>
                  {[0, 0.2, 0.4].map((d, i) => (
                    <span key={i} className="m-loading-sq" style={{ animation: `metraBlink 1.2s ease-in-out ${d}s infinite` }} />
                  ))}
                </span>
              </div>
            )}
          </div>

          <div className="m-composer">
            <div className="m-composer-row">
              <input
                ref={inputRef}
                className="input"
                value={input}
                onChange={e => setInput(e.target.value)}
                onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input); } }}
                placeholder="Ask about trains, schedules, alerts, stops"
              />
              <button
                type="button"
                className="btn btn-primary"
                style={{ minWidth: "108px", letterSpacing: "0.08em", textTransform: "uppercase" }}
                onClick={() => send(input)}
                disabled={loading || !input.trim()}>
                Send
              </button>
            </div>
            <div className="m-composer-hints">
              <span>Enter to send</span>
              <span>Responses are generated — verify before you board</span>
            </div>
          </div>
        </div>

        <div className="m-rail">
          <div className="m-label" style={{ letterSpacing: "0.14em", padding: "14px 18px", borderBottom: "1px solid var(--color-divider)" }}>Lines</div>
          {LINES.map(([code, terminal]) => (
            <button key={code} type="button" className="m-rail-line"
              onClick={() => send(`${code} line status and next departures`)}>
              <span className="m-rail-code">{code}</span>
              <span className="m-rail-term">{terminal}</span>
              <span className={alerted[code] ? "m-rail-sq m-rail-sq-alert" : "m-rail-sq"} />
            </button>
          ))}
          <div className="m-rail-note">Unofficial. Data from Metra's public GTFS feeds via the Metra MCP server.</div>
        </div>

      </div>
    </div>
  );
}

const root = ReactDOM.createRoot(document.getElementById("root"));
root.render(<MetraCopilot />);
