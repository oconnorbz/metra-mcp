const {
  useState,
  useRef,
  useEffect
} = React;

// The system prompt is pinned server-side (/api/chat builds it); the client
// only sends the resolved theme so the model styles for the active mode.

// Model HTML is sanitized before it touches the DOM — it is built from live
// upstream feed data, which we don't trust to be markup-safe.
const sanitize = html => DOMPurify.sanitize(html, {
  USE_PROFILES: {
    html: true
  },
  FORBID_TAGS: ["form", "input", "textarea", "select", "button", "style", "iframe", "object", "embed"],
  FORBID_ATTR: ["style", "formaction", "action"]
});
const escHtml = s => String(s).replace(/[&<>"']/g, ch => ({
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;"
})[ch]);
const QUICK_ACTIONS = [{
  icon: "⚠",
  label: "Service Alerts",
  query: "Show all current Metra service alerts"
}, {
  icon: "🚂",
  label: "BNSF Line",
  query: "Show BNSF line status and next departing trains"
}, {
  icon: "📋",
  label: "All Lines Status",
  query: "Show status overview for all Metra lines"
}, {
  icon: "📍",
  label: "UP-NW Trains",
  query: "Next trains on Union Pacific Northwest line"
}, {
  icon: "🗺",
  label: "ME Line",
  query: "Metra Electric line next trains and status"
}, {
  icon: "🔴",
  label: "MD-W Status",
  query: "Milwaukee District West line status and departures"
}];

// ── Theme management ──
function useTheme() {
  const [theme, setTheme] = useState(() => localStorage.getItem("metra-theme") || "system");
  const [resolved, setResolved] = useState(() => {
    const stored = localStorage.getItem("metra-theme") || "system";
    if (stored === "system") return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    return stored;
  });
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const update = () => {
      const r = theme === "system" ? mq.matches ? "dark" : "light" : theme;
      setResolved(r);
      document.documentElement.classList.toggle("dark", r === "dark");
    };
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, [theme]);
  useEffect(() => {
    localStorage.setItem("metra-theme", theme);
  }, [theme]);
  return [theme, setTheme, resolved];
}
const PALETTES = {
  dark: {
    bg: "#09090b",
    surface: "#111113",
    surfaceAlt: "#18181b",
    border: "#27272a",
    borderAlt: "#3f3f46",
    text: "#e4e4e7",
    textMuted: "#a1a1aa",
    textDim: "#71717a",
    textFaint: "#52525b",
    accent: "#f59e0b",
    accentHover: "#d97706",
    userBg: "#1e3a5f",
    userBorder: "#1d4ed8",
    userText: "#bfdbfe",
    scrollbar: "#3f3f46"
  },
  light: {
    bg: "#fafafa",
    surface: "#ffffff",
    surfaceAlt: "#f4f4f5",
    border: "#e4e4e7",
    borderAlt: "#d4d4d8",
    text: "#18181b",
    textMuted: "#52525b",
    textDim: "#71717a",
    textFaint: "#a1a1aa",
    accent: "#d97706",
    accentHover: "#b45309",
    userBg: "#dbeafe",
    userBorder: "#2563eb",
    userText: "#1e3a8a",
    scrollbar: "#a1a1aa"
  }
};
function ThemeToggle({
  theme,
  setTheme
}) {
  const modes = [{
    id: "light",
    icon: "☀",
    label: "Light"
  }, {
    id: "dark",
    icon: "🌙",
    label: "Dark"
  }, {
    id: "system",
    icon: "💻",
    label: "System"
  }];
  const current = modes.find(m => m.id === theme) || modes[2];
  const cycle = () => {
    const idx = modes.findIndex(m => m.id === theme);
    setTheme(modes[(idx + 1) % modes.length].id);
  };
  return /*#__PURE__*/React.createElement("button", {
    onClick: cycle,
    title: `Theme: ${current.label} (click to cycle)`,
    style: {
      background: "transparent",
      border: "1px solid var(--border)",
      color: "var(--text-muted)",
      width: "28px",
      height: "28px",
      borderRadius: "8px",
      fontSize: "14px",
      cursor: "pointer",
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      transition: "all 0.15s"
    },
    onMouseEnter: e => {
      e.currentTarget.style.borderColor = "var(--accent)";
    },
    onMouseLeave: e => {
      e.currentTarget.style.borderColor = "var(--border)";
    }
  }, current.icon);
}

// Top-down "map view" locomotive — the engine on a track, seen from above.
// Reads as a transit-data mark, fits the live-positions theme of the app.
function TrainIcon({
  size = 24,
  color = "currentColor",
  strokeWidth = 1.6
}) {
  return /*#__PURE__*/React.createElement("svg", {
    width: size,
    height: size,
    viewBox: "0 0 32 32",
    fill: "none",
    xmlns: "http://www.w3.org/2000/svg",
    style: {
      display: "block"
    }
  }, /*#__PURE__*/React.createElement("line", {
    x1: "11",
    y1: "2",
    x2: "11",
    y2: "30",
    stroke: color,
    strokeWidth: strokeWidth,
    strokeLinecap: "round",
    strokeOpacity: "0.5"
  }), /*#__PURE__*/React.createElement("line", {
    x1: "21",
    y1: "2",
    x2: "21",
    y2: "30",
    stroke: color,
    strokeWidth: strokeWidth,
    strokeLinecap: "round",
    strokeOpacity: "0.5"
  }), [4, 9, 14, 19, 24, 29].map(y => /*#__PURE__*/React.createElement("line", {
    key: y,
    x1: "9",
    y1: y,
    x2: "23",
    y2: y,
    stroke: color,
    strokeWidth: strokeWidth * 0.8,
    strokeLinecap: "round",
    strokeOpacity: "0.3"
  })), /*#__PURE__*/React.createElement("path", {
    d: "M13 8 H19 L20 11 V22 H12 V11 Z",
    fill: color,
    fillOpacity: "0.18",
    stroke: color,
    strokeWidth: strokeWidth,
    strokeLinejoin: "round"
  }), /*#__PURE__*/React.createElement("rect", {
    x: "14",
    y: "13",
    width: "4",
    height: "6",
    rx: "0.5",
    fill: color,
    fillOpacity: "0.55",
    stroke: color,
    strokeWidth: strokeWidth
  }), /*#__PURE__*/React.createElement("path", {
    d: "M14 8 L16 6 L18 8 Z",
    fill: color
  }));
}
function LoadingDots({
  c
}) {
  return /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      alignItems: "center",
      gap: "12px",
      padding: "16px"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      background: c.surfaceAlt,
      border: `1px solid ${c.borderAlt}`,
      borderRadius: "12px",
      padding: "12px 16px",
      display: "flex",
      alignItems: "center",
      gap: "10px"
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      color: c.accent,
      fontSize: "11px",
      letterSpacing: "0.15em",
      fontFamily: "monospace"
    }
  }, "FETCHING LIVE DATA"), /*#__PURE__*/React.createElement("span", {
    style: {
      display: "flex",
      gap: "4px"
    }
  }, [0, 1, 2].map(i => /*#__PURE__*/React.createElement("span", {
    key: i,
    style: {
      display: "inline-block",
      width: "5px",
      height: "5px",
      background: c.accent,
      borderRadius: "50%",
      animation: `blink 1.2s ease-in-out ${i * 0.2}s infinite`
    }
  })))));
}
function WelcomeScreen({
  onAction,
  c
}) {
  return /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      justifyContent: "center",
      minHeight: "380px",
      padding: "32px 20px",
      textAlign: "center"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      marginBottom: "16px",
      color: c.accent,
      display: "inline-flex"
    }
  }, /*#__PURE__*/React.createElement(TrainIcon, {
    size: 72
  })), /*#__PURE__*/React.createElement("div", {
    style: {
      color: c.accent,
      fontSize: "18px",
      fontWeight: "800",
      letterSpacing: "0.12em",
      fontFamily: "monospace",
      marginBottom: "4px"
    }
  }, "METRA COPILOT"), /*#__PURE__*/React.createElement("div", {
    style: {
      color: c.textFaint,
      fontSize: "11px",
      letterSpacing: "0.08em",
      marginBottom: "28px",
      fontFamily: "monospace"
    }
  }, "CHICAGO COMMUTER RAIL \u2014 LIVE INTELLIGENCE"), /*#__PURE__*/React.createElement("div", {
    style: {
      width: "40px",
      height: "2px",
      background: c.accent,
      marginBottom: "28px"
    }
  }), /*#__PURE__*/React.createElement("div", {
    style: {
      color: c.textMuted,
      fontSize: "12px",
      marginBottom: "20px"
    }
  }, "Quick access \u2014 tap a line or ask anything"), /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      flexWrap: "wrap",
      gap: "8px",
      justifyContent: "center",
      maxWidth: "480px"
    }
  }, QUICK_ACTIONS.map((a, i) => /*#__PURE__*/React.createElement("button", {
    key: i,
    onClick: () => onAction(a.query),
    style: {
      background: c.surfaceAlt,
      border: `1px solid ${c.borderAlt}`,
      color: c.text,
      padding: "8px 14px",
      borderRadius: "20px",
      fontSize: "12px",
      cursor: "pointer",
      fontFamily: "monospace",
      transition: "all 0.15s",
      display: "flex",
      alignItems: "center",
      gap: "6px"
    },
    onMouseEnter: e => {
      e.currentTarget.style.borderColor = c.accent;
      e.currentTarget.style.color = c.accent;
    },
    onMouseLeave: e => {
      e.currentTarget.style.borderColor = c.borderAlt;
      e.currentTarget.style.color = c.text;
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      fontSize: "13px"
    }
  }, a.icon), /*#__PURE__*/React.createElement("span", null, a.label)))));
}
function MetraCopilot() {
  const [theme, setTheme, resolved] = useTheme();
  const c = PALETTES[resolved];
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const messagesEndRef = useRef(null);
  const inputRef = useRef(null);
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({
      behavior: "smooth"
    });
  }, [messages, loading]);
  const sendMessage = async text => {
    if (!text.trim() || loading) return;
    setError(null);
    const userMsg = {
      role: "user",
      content: text
    };
    const history = [...messages, userMsg];
    setMessages(history);
    setInput("");
    setLoading(true);

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
        headers: {
          "Content-Type": "application/json"
        },
        signal: controller.signal,
        body: JSON.stringify({
          theme: resolved,
          messages: history.map(m => ({
            role: m.role,
            content: m.content || m.html || ""
          }))
        })
      });
      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        throw new Error(errData.error?.message || errData.error || `HTTP ${response.status}`);
      }

      // Haiku sometimes wraps the fragment in a ```html fence — strip it so
      // the markup renders instead of displaying as literal text.
      const stripFences = s => s.trim().replace(/^```(?:html)?\s*/i, "").replace(/```\s*$/, "");
      const ctype = response.headers.get("content-type") || "";
      let htmlContent = "";
      if (ctype.includes("text/event-stream")) {
        // The proxy streams Anthropic SSE through verbatim; accumulate
        // text deltas and render progressively.
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buf = "",
          raw = "",
          upstreamErr = null;
        while (true) {
          const {
            done,
            value
          } = await reader.read();
          if (done) break;
          armStallTimer();
          buf += decoder.decode(value, {
            stream: true
          });
          const events = buf.split("\n\n");
          buf = events.pop();
          let sawText = false;
          for (const evt of events) {
            const data = evt.split("\n").filter(l => l.startsWith("data:")).map(l => l.slice(5).trim()).join("");
            if (!data) continue;
            let obj;
            try {
              obj = JSON.parse(data);
            } catch {
              continue;
            }
            if (obj.type === "content_block_delta" && obj.delta && obj.delta.type === "text_delta") {
              raw += obj.delta.text;
              sawText = true;
            } else if (obj.type === "error" || obj.error) {
              upstreamErr = obj.error && obj.error.message || JSON.stringify(obj.error || obj);
            }
          }
          if (sawText) {
            const partial = stripFences(raw);
            if (partial) setMessages([...history, {
              role: "assistant",
              html: partial
            }]);
          }
        }
        if (upstreamErr) throw new Error(upstreamErr);
        htmlContent = stripFences(raw);
      } else {
        const data = await response.json();
        htmlContent = stripFences((data.content || []).filter(b => b.type === "text").map(b => b.text).join("\n"));
      }
      setMessages([...history, {
        role: "assistant",
        html: htmlContent
      }]);
    } catch (err) {
      const friendly = err.name === "AbortError" ? "The request stalled — live data feeds may be slow. Please try again." : err.message;
      setError(friendly);
      err = {
        message: friendly
      };
      setMessages([...history, {
        role: "assistant",
        html: `<div class="bg-red-50 dark:bg-red-950 border border-red-300 dark:border-red-800 rounded-xl p-4 font-mono">
          <div class="text-red-700 dark:text-red-400 font-bold text-xs tracking-widest mb-2">⚠ SYSTEM ERROR</div>
          <div class="text-red-600 dark:text-red-300 text-xs">${escHtml(err.message)}</div>
        </div>`
      }]);
    } finally {
      clearTimeout(stallTimer);
      setLoading(false);
      setTimeout(() => inputRef.current?.focus(), 100);
    }
  };

  // Inject CSS variables for theme-aware styling
  const cssVars = {
    "--bg": c.bg,
    "--surface": c.surface,
    "--surface-alt": c.surfaceAlt,
    "--border": c.border,
    "--border-alt": c.borderAlt,
    "--text": c.text,
    "--text-muted": c.textMuted,
    "--accent": c.accent
  };
  return /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement("style", null, `
        @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700;800&display=swap');
        @keyframes blink { 0%,100%{opacity:.25;transform:scale(.7)} 50%{opacity:1;transform:scale(1)} }
        @keyframes fadeIn { from{opacity:0;transform:translateY(6px)} to{opacity:1;transform:translateY(0)} }
        .metra-msg { animation: fadeIn 0.25s ease-out; }
        .metra-input:focus { outline: none; border-color: ${c.accent} !important; box-shadow: 0 0 0 1px ${c.accent}40; }
        .metra-send:hover:not(:disabled) { background: ${c.accentHover} !important; }
        .metra-send:disabled { opacity: 0.4; cursor: not-allowed; }
        .chip:hover { border-color: ${c.accent} !important; color: ${c.accent} !important; }
        ::-webkit-scrollbar { width: 4px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: ${c.scrollbar}; border-radius: 4px; }
      `), /*#__PURE__*/React.createElement("div", {
    style: {
      ...cssVars,
      fontFamily: "'JetBrains Mono', 'Courier New', monospace",
      background: c.bg,
      borderRadius: "16px",
      border: `1px solid ${c.border}`,
      overflow: "hidden",
      display: "flex",
      flexDirection: "column",
      minHeight: "640px",
      transition: "background 0.15s, border-color 0.15s"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      background: c.surface,
      borderBottom: `1px solid ${c.border}`,
      padding: "14px 20px",
      display: "flex",
      alignItems: "center",
      gap: "12px",
      transition: "background 0.15s, border-color 0.15s"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      width: "32px",
      height: "32px",
      background: c.accent,
      borderRadius: "8px",
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      color: resolved === "light" ? "#ffffff" : "#000"
    }
  }, /*#__PURE__*/React.createElement(TrainIcon, {
    size: 22
  })), /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("div", {
    style: {
      color: c.accent,
      fontWeight: "800",
      fontSize: "13px",
      letterSpacing: "0.12em"
    }
  }, "METRA COPILOT"), /*#__PURE__*/React.createElement("div", {
    style: {
      color: c.textFaint,
      fontSize: "10px",
      letterSpacing: "0.06em"
    }
  }, "CHICAGO COMMUTER RAIL")), /*#__PURE__*/React.createElement("div", {
    style: {
      marginLeft: "auto",
      display: "flex",
      alignItems: "center",
      gap: "10px"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      alignItems: "center",
      gap: "6px"
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      width: "7px",
      height: "7px",
      background: "#22c55e",
      borderRadius: "50%",
      display: "inline-block",
      boxShadow: "0 0 6px #22c55e80"
    }
  }), /*#__PURE__*/React.createElement("span", {
    style: {
      color: "#22c55e",
      fontSize: "10px",
      letterSpacing: "0.08em"
    }
  }, "LIVE")), /*#__PURE__*/React.createElement(ThemeToggle, {
    theme: theme,
    setTheme: setTheme
  }))), /*#__PURE__*/React.createElement("div", {
    style: {
      flex: 1,
      overflowY: "auto",
      padding: "16px",
      background: c.bg,
      transition: "background 0.15s"
    }
  }, messages.length === 0 ? /*#__PURE__*/React.createElement(WelcomeScreen, {
    onAction: sendMessage,
    c: c
  }) : /*#__PURE__*/React.createElement(React.Fragment, null, messages.map((msg, i) => /*#__PURE__*/React.createElement("div", {
    key: i,
    className: "metra-msg",
    style: {
      marginBottom: "14px",
      display: "flex",
      justifyContent: msg.role === "user" ? "flex-end" : "flex-start"
    }
  }, msg.role === "user" ? /*#__PURE__*/React.createElement("div", {
    style: {
      background: c.userBg,
      border: `1px solid ${c.userBorder}`,
      color: c.userText,
      padding: "10px 16px",
      borderRadius: "16px 16px 4px 16px",
      fontSize: "12px",
      maxWidth: "75%",
      lineHeight: "1.5"
    }
  }, msg.content) : /*#__PURE__*/React.createElement("div", {
    dangerouslySetInnerHTML: {
      __html: sanitize(msg.html)
    },
    style: {
      width: "100%"
    }
  }))), loading && /*#__PURE__*/React.createElement(LoadingDots, {
    c: c
  }), /*#__PURE__*/React.createElement("div", {
    ref: messagesEndRef
  }))), messages.length > 0 && /*#__PURE__*/React.createElement("div", {
    style: {
      padding: "8px 16px",
      borderTop: `1px solid ${c.border}`,
      background: c.bg,
      display: "flex",
      gap: "6px",
      overflowX: "auto"
    }
  }, QUICK_ACTIONS.map((a, i) => /*#__PURE__*/React.createElement("button", {
    key: i,
    className: "chip",
    onClick: () => sendMessage(a.query),
    disabled: loading,
    style: {
      background: "transparent",
      border: `1px solid ${c.borderAlt}`,
      color: c.textMuted,
      padding: "5px 11px",
      borderRadius: "14px",
      fontSize: "10px",
      cursor: "pointer",
      whiteSpace: "nowrap",
      flexShrink: 0,
      fontFamily: "inherit",
      letterSpacing: "0.05em",
      transition: "border-color 0.15s, color 0.15s"
    }
  }, a.icon, " ", a.label))), /*#__PURE__*/React.createElement("div", {
    style: {
      padding: "12px 16px",
      borderTop: `1px solid ${c.border}`,
      background: c.surface,
      display: "flex",
      gap: "8px",
      alignItems: "center",
      transition: "background 0.15s, border-color 0.15s"
    }
  }, /*#__PURE__*/React.createElement("input", {
    ref: inputRef,
    className: "metra-input",
    value: input,
    onChange: e => setInput(e.target.value),
    onKeyDown: e => e.key === "Enter" && !e.shiftKey && sendMessage(input),
    placeholder: "Ask about trains, schedules, alerts, stops...",
    disabled: loading,
    style: {
      flex: 1,
      background: c.bg,
      border: `1px solid ${c.borderAlt}`,
      color: c.text,
      padding: "10px 14px",
      borderRadius: "10px",
      fontSize: "12px",
      fontFamily: "inherit",
      transition: "border-color 0.15s, background 0.15s, color 0.15s"
    }
  }), /*#__PURE__*/React.createElement("button", {
    className: "metra-send",
    onClick: () => sendMessage(input),
    disabled: loading || !input.trim(),
    style: {
      background: c.accent,
      color: resolved === "light" ? "#ffffff" : "#000",
      border: "none",
      padding: "10px 16px",
      borderRadius: "10px",
      fontSize: "14px",
      fontWeight: "800",
      cursor: "pointer",
      transition: "background 0.15s"
    }
  }, "\u2197"))));
}
const root = ReactDOM.createRoot(document.querySelector("#root .app-container"));
root.render(/*#__PURE__*/React.createElement(MetraCopilot, null));
