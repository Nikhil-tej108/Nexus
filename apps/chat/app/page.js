"use client";

import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

function getCsrf() {
  const m = document.cookie.match(/(?:^|; )nexus_csrf=([^;]*)/);
  return m ? decodeURIComponent(m[1]) : "";
}

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}), "X-CSRF-Token": getCsrf() };
  if (opts.json) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.json);
  }
  const res = await fetch(`/api${path}`, { ...opts, headers, credentials: "include" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.message || res.statusText);
  return data;
}

function Markdown({ text }) {
  if (!text) return null;
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        p: ({ children }) => <p style={mdP}>{children}</p>,
        h1: ({ children }) => <h3 style={mdHeading(1)}>{children}</h3>,
        h2: ({ children }) => <h4 style={mdHeading(2)}>{children}</h4>,
        h3: ({ children }) => <h5 style={mdHeading(3)}>{children}</h5>,
        h4: ({ children }) => <h6 style={mdHeading(4)}>{children}</h6>,
        ul: ({ children }) => <ul style={mdUl}>{children}</ul>,
        ol: ({ children }) => <ol style={mdOl}>{children}</ol>,
        li: ({ children }) => <li style={mdLi}>{children}</li>,
        blockquote: ({ children }) => <blockquote style={mdQuote}>{children}</blockquote>,
        pre: ({ children }) => <pre style={mdPre}>{children}</pre>,
        code: ({ inline, children }) =>
          inline ? <code style={mdInlineCode}>{children}</code> : <code>{children}</code>,
        a: ({ href, children }) => (
          <a href={href} target="_blank" rel="noreferrer" style={mdLink}>
            {children}
          </a>
        ),
        hr: () => <hr style={mdHr} />,
        table: ({ children }) => (
          <div style={mdTableWrap} className="md-table-wrap">
            <table style={mdTable} className="md-table">{children}</table>
          </div>
        ),
        thead: ({ children }) => <thead style={mdThead}>{children}</thead>,
        th: ({ children }) => <th style={mdTh}>{children}</th>,
        td: ({ children }) => <td style={mdTd}>{children}</td>,
      }}
    >
      {text}
    </ReactMarkdown>
  );
}

/* ---------------------------------------------------------------- */

export default function Page() {
  const [me, setMe] = useState(null);
  const [mode, setMode] = useState("external");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState([]);
  const [busy, setBusy] = useState(false);
  const scroller = useRef(null);

  useEffect(() => {
    api("/auth/me")
      .then((d) => setMe(d.user))
      .catch(() => setMe(null));
  }, []);

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  async function startExternal(e) {
    e.preventDefault();
    setError("");
    try {
      const d = await api("/auth/external", { method: "POST", json: { email } });
      setMe(d.user);
    } catch (err) {
      setError(err.message);
    }
  }

  async function loginInternal(e) {
    e.preventDefault();
    setError("");
    try {
      const d = await api("/auth/login", { method: "POST", json: { email, password } });
      setMe(d.user);
    } catch (err) {
      setError(err.message);
    }
  }

  async function send(e) {
    e.preventDefault();
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    setMessages((m) => [...m, { role: "user", text }]);
    setBusy(true);
    let answer = "";
    setMessages((m) => [...m, { role: "assistant", text: "", citations: [] }]);
    try {
      const res = await fetch("/api/chat/stream", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrf() },
        body: JSON.stringify({ message: text }),
      });
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const parts = buf.split("\n\n");
        buf = parts.pop() || "";
        for (const part of parts) {
          const line = part.replace(/^data:\s*/, "");
          if (!line) continue;
          const ev = JSON.parse(line);
          if (ev.type === "token") {
            answer += ev.text;
            setMessages((m) => {
              const copy = [...m];
              copy[copy.length - 1] = { ...copy[copy.length - 1], text: answer };
              return copy;
            });
          }
          if (ev.type === "done") {
            setMessages((m) => {
              const copy = [...m];
              copy[copy.length - 1] = {
                ...copy[copy.length - 1],
                text: answer,
                citations: ev.citations || [],
                refused: ev.refused,
              };
              return copy;
            });
          }
        }
      }
    } catch (err) {
      setMessages((m) => {
        const copy = [...m];
        copy[copy.length - 1] = { role: "assistant", text: err.message || "Request failed" };
        return copy;
      });
    } finally {
      setBusy(false);
    }
  }

  if (!me) {
    return (
      <main style={wrap}>
        <header style={hero}>
          <div style={mark}>N</div>
          <div>
            <h1 style={h1}>Nexus</h1>
            <p style={sub}>Local knowledge. Intranet only.</p>
          </div>
        </header>
        <div style={card}>
          <div style={tabs}>
            <button style={tabBtn(mode === "external")} onClick={() => setMode("external")}>
              Visitor
            </button>
            <button style={tabBtn(mode === "internal")} onClick={() => setMode("internal")}>
              Internal
            </button>
          </div>
          {mode === "external" ? (
            <form onSubmit={startExternal} style={form}>
              <p style={hint}>Enter your email to start a generic-only session. Individual records are never disclosed.</p>
              <input style={inputEl} type="email" required placeholder="you@example.edu" value={email} onChange={(e) => setEmail(e.target.value)} />
              <button style={primary} type="submit">Begin chat</button>
            </form>
          ) : (
            <form onSubmit={loginInternal} style={form}>
              <p style={hint}>Accounts are created by an administrator.</p>
              <input style={inputEl} type="email" required placeholder="work email" value={email} onChange={(e) => setEmail(e.target.value)} />
              <input style={inputEl} type="password" required placeholder="password" value={password} onChange={(e) => setPassword(e.target.value)} />
              <button style={primary} type="submit">Sign in</button>
            </form>
          )}
          {error && <p style={err}>{error}</p>}
        </div>
        <p style={footNote}>A private index of institutional documents, papers, and records.</p>
      </main>
    );
  }

  const internal = me.role === "internal";

  return (
    <main style={chatShell}>
      <header style={topbar}>
        <div style={brandRow}>
          <div style={markSmall}>N</div>
          <strong style={brandWord}>Nexus</strong>
        </div>
        <span style={statusPill(internal)}>
          {internal ? "Internal · citations on" : "Visitor · generic sources only"}
        </span>
        <span style={{ marginLeft: "auto", fontSize: 13, color: "#dce5ea" }}>{me.email}</span>
      </header>
      <div ref={scroller} style={thread}>
        {messages.length === 0 && (
          <div style={empty}>
            <h2 style={emptyTitle}>Ask the knowledge base</h2>
            <p style={{ color: "var(--muted)", margin: 0, maxWidth: 440, marginInline: "auto" }}>
              {internal
                ? "Answers include citations from ingested documents."
                : "You will only receive general information. Personal student or faculty records are blocked."}
            </p>
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} style={row(m.role)}>
            <div style={avatar(m.role, m.refused)}>{m.role === "user" ? "U" : "N"}</div>
            <article style={bubble(m.role, m.refused)}>
              {m.role === "assistant" ? (
                m.text ? (
                  <Markdown text={m.text} />
                ) : busy && i === messages.length - 1 ? (
                  <span style={typing}>
                    <span style={dot(0)} />
                    <span style={dot(1)} />
                    <span style={dot(2)} />
                  </span>
                ) : null
              ) : (
                <div style={{ whiteSpace: "pre-wrap", lineHeight: 1.55 }}>{m.text}</div>
              )}
              {internal && m.citations?.length > 0 && (
                <div style={cites}>
                  {m.citations.map((c, j) => (
                    <span key={j} style={chip}>
                      {c.title}
                      {c.page ? ` · p.${c.page}` : ""}
                      {c.sheet ? ` · ${c.sheet}` : ""}
                    </span>
                  ))}
                </div>
              )}
            </article>
          </div>
        ))}
      </div>
      <form onSubmit={send} style={composer}>
        <input
          style={{ ...inputEl, flex: 1 }}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask Nexus…"
          disabled={busy}
        />
        <button style={{ ...primary, opacity: busy || !input.trim() ? 0.55 : 1 }} disabled={busy || !input.trim()} type="submit">
          Send
        </button>
      </form>
    </main>
  );
}

/* ---------------------------- layout / chrome styles ---------------------------- */

const wrap = { maxWidth: 480, margin: "12vh auto 0", padding: 24 };
const hero = { display: "flex", gap: 16, alignItems: "center", marginBottom: 28 };
const mark = {
  width: 52,
  height: 52,
  borderRadius: 10,
  border: "1px solid var(--line-strong)",
  background: "linear-gradient(160deg, var(--primary-cyan), var(--primary-cyan-deep))",
  display: "grid",
  placeItems: "center",
  color: "#fff",
  fontFamily: "var(--font-serif)",
  fontSize: 24,
  boxShadow: "0 6px 16px rgba(2, 107, 135, 0.22)",
};
const h1 = { margin: 0, fontFamily: "var(--font-serif)", fontWeight: 600, fontSize: 32, color: "var(--text)" };
const sub = { margin: "4px 0 0", color: "var(--muted)", fontSize: 14 };
const card = {
  background: "var(--surface)",
  border: "1px solid var(--line)",
  borderRadius: 12,
  padding: 24,
  boxShadow: "0 1px 2px rgba(20, 32, 51, 0.04), 0 12px 32px rgba(20, 32, 51, 0.06)",
};
const tabs = { display: "flex", gap: 6, marginBottom: 18, background: "var(--surface-2)", padding: 4, borderRadius: 10 };
const tabBtn = (on) => ({
  flex: 1,
  padding: "9px 10px",
  borderRadius: 8,
  border: "none",
  background: on ? "var(--surface)" : "transparent",
  color: on ? "var(--primary-cyan-deep)" : "var(--muted)",
  fontWeight: on ? 600 : 500,
  fontSize: 14,
  boxShadow: on ? "0 1px 3px rgba(20,32,51,0.10)" : "none",
  cursor: "pointer",
  transition: "background 150ms ease, color 150ms ease",
});
const form = { display: "flex", flexDirection: "column", gap: 12 };
const hint = { color: "var(--muted)", fontSize: 13.5, lineHeight: 1.5, margin: "0 0 2px" };
const inputEl = {
  padding: "12px 14px",
  borderRadius: 10,
  border: "1px solid var(--line-strong)",
  background: "var(--surface)",
  color: "var(--text)",
  outline: "none",
  fontSize: 14.5,
  transition: "border-color 150ms ease, box-shadow 150ms ease",
};
const primary = {
  padding: "12px 16px",
  borderRadius: 10,
  border: "none",
  background: "linear-gradient(180deg, var(--primary-cyan), var(--primary-cyan-deep))",
  color: "#fff",
  fontWeight: 600,
  fontSize: 14.5,
  cursor: "pointer",
  transition: "opacity 150ms ease, transform 150ms ease",
};
const err = { color: "var(--accent-coral)", fontSize: 13, margin: 0 };
const footNote = { textAlign: "center", color: "var(--muted)", fontSize: 12.5, marginTop: 18 };

const chatShell = { height: "100vh", display: "flex", flexDirection: "column", background: "var(--bg-light)" };
const topbar = {
  display: "flex",
  gap: 16,
  alignItems: "center",
  padding: "12px 22px",
  borderBottom: "1px solid var(--line)",
  background: "var(--surface-charcoal)",
};
const brandRow = { display: "flex", alignItems: "center", gap: 10 };
const markSmall = {
  width: 30,
  height: 30,
  borderRadius: 10,
  background: "linear-gradient(160deg, var(--primary-cyan), var(--primary-cyan-deep))",
  color: "#fff",
  display: "grid",
  placeItems: "center",
  fontFamily: "var(--font-serif)",
  fontSize: 15,
};
const brandWord = { fontFamily: "var(--font-serif)", fontSize: 17, color: "#f6f9fb", fontWeight: 600 };
const statusPill = (internal) => ({
  fontSize: 12.5,
  color: internal ? "var(--primary-cyan)" : "#dce5ea",
  background: internal ? "rgba(53, 199, 224, 0.14)" : "rgba(255, 255, 255, 0.08)",
  border: `1px solid ${internal ? "rgba(53, 199, 224, 0.30)" : "rgba(255,255,255,0.15)"}`,
  borderRadius: 999,
  padding: "4px 10px",
});

const thread = { flex: 1, overflow: "auto", padding: "24px 20px 8px", maxWidth: 860, width: "100%", margin: "0 auto" };
const empty = { textAlign: "center", marginTop: "16vh" };
const emptyTitle = { fontFamily: "var(--font-serif)", fontWeight: 600, fontSize: 22, margin: "0 0 8px", color: "var(--text)" };

const row = (role) => ({
  display: "flex",
  gap: 10,
  flexDirection: role === "user" ? "row-reverse" : "row",
  marginBottom: 16,
});
const avatar = (role, refused) => ({
  flexShrink: 0,
  width: 30,
  height: 30,
  borderRadius: 10,
  display: "grid",
  placeItems: "center",
  fontSize: 12.5,
  fontWeight: 700,
  marginTop: 2,
  color: role === "user" ? "var(--primary-cyan-deep)" : "#fff",
  background: role === "user" ? "var(--primary-cyan-tint)" : refused ? "var(--accent-coral)" : "var(--primary-cyan)",
});
const bubble = (role, refused) => ({
  maxWidth: "78%",
  padding: "13px 16px",
  borderRadius: role === "user" ? "12px 12px 8px 12px" : "12px 12px 12px 8px",
  background: role === "user" ? "var(--primary-cyan-tint)" : "var(--surface)",
  border: `1px solid ${refused ? "rgba(226,83,76,0.35)" : role === "user" ? "rgba(53, 199, 224, 0.30)" : "var(--line)"}`,
  fontFamily: "var(--font-serif)",
  fontSize: 15.5,
  color: "var(--text)",
  boxShadow: role === "assistant" ? "0 1px 2px rgba(20,32,51,0.04)" : "none",
});

const typing = { display: "inline-flex", gap: 4, padding: "4px 2px" };
const dot = (idx) => ({
  width: 6,
  height: 6,
  borderRadius: "50%",
  background: "var(--primary-cyan)",
  display: "inline-block",
  animation: `nexus-bounce 1.1s ${idx * 0.15}s infinite ease-in-out`,
});

const cites = { display: "flex", flexWrap: "wrap", gap: 6, marginTop: 10 };
const chip = {
  fontFamily: "var(--font-sans)",
  fontSize: 11.5,
  color: "var(--primary-cyan-deep)",
  background: "var(--primary-cyan-tint)",
  border: "1px solid rgba(53, 199, 224, 0.28)",
  borderRadius: 999,
  padding: "3px 9px",
};

const composer = {
  display: "flex",
  gap: 10,
  padding: 14,
  borderTop: "1px solid var(--line)",
  background: "var(--bg-muted)",
  maxWidth: 820,
  width: "100%",
  margin: "0 auto 14px",
};

/* ---------------------------- markdown styles ---------------------------- */

const mdP = { margin: "0 0 10px", lineHeight: 1.65, overflowWrap: "anywhere" };
const mdHeading = (level) => ({
  fontFamily: "var(--font-serif)",
  fontWeight: 600,
  color: "var(--text)",
  margin: level <= 1 ? "4px 0 8px" : "10px 0 6px",
  lineHeight: 1.35,
});
const mdUl = { margin: "0 0 10px", paddingLeft: 22 };
const mdOl = { margin: "0 0 10px", paddingLeft: 22 };
const mdLi = { marginBottom: 4, lineHeight: 1.6 };
const mdQuote = {
  margin: "0 0 10px",
  padding: "4px 14px",
  borderLeft: "3px solid var(--primary-cyan)",
  color: "var(--muted)",
  fontStyle: "italic",
};
const mdPre = {
  margin: "0 0 10px",
  padding: "12px 14px",
  borderRadius: 10,
  background: "var(--surface-2)",
  border: "1px solid var(--line)",
  overflowX: "auto",
  fontFamily: "var(--font-mono)",
  fontSize: 13,
  lineHeight: 1.55,
};
const mdInlineCode = {
  fontFamily: "var(--font-mono)",
  fontSize: "0.88em",
  background: "var(--surface-2)",
  border: "1px solid var(--line)",
  borderRadius: 4,
  padding: "1px 5px",
};
const mdLink = { color: "var(--primary-cyan-deep)", textDecoration: "underline", textUnderlineOffset: 2 };
const mdHr = { border: 0, borderTop: "1px solid var(--line)", margin: "10px 0 14px" };
const mdTableWrap = { overflowX: "auto", margin: "0 0 10px" };
const mdTable = { width: "100%", borderCollapse: "collapse", fontSize: 14, minWidth: 320 };
const mdThead = { background: "var(--bg-muted)" };
const mdTh = { border: "1px solid var(--line)", textAlign: "left", padding: "8px 10px", fontWeight: 600 };
const mdTd = { border: "1px solid var(--line)", padding: "8px 10px", background: "rgba(248, 249, 250, 0.55)" };