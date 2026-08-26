"use client";

// The buddy (spec §5): streaming replies, tool calls collapsed under each
// answer, stale warnings visible, commands passed straight through.
// Turns persist to localStorage so the conversation survives reloads;
// the component itself stays mounted across tabs (see ChatDock).

import { useEffect, useRef, useState } from "react";

interface ToolCall {
  name: string;
  ok: boolean;
  stale: boolean;
}

interface Turn {
  role: "user" | "assistant";
  text: string;
  tools?: ToolCall[];
  streaming?: boolean;
  stale?: boolean;
  opinion?: string | null;
}

const COMMANDS = "/watch /unwatch /mute /remember /confirm /note /status";
const STORE_KEY = "desk-chat-turns";
const STORE_MAX = 200;

export function ChatPanel() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const hydrated = useRef(false);

  useEffect(() => {
    try {
      const saved = localStorage.getItem(STORE_KEY);
      if (saved) setTurns(JSON.parse(saved));
    } catch {
      /* fresh start */
    }
    hydrated.current = true;
  }, []);

  useEffect(() => {
    if (!hydrated.current) return;
    try {
      const done = turns.slice(-STORE_MAX).map((t) => ({ ...t, streaming: false }));
      localStorage.setItem(STORE_KEY, JSON.stringify(done));
    } catch {
      /* storage full or unavailable */
    }
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns]);

  const send = async () => {
    const message = input.trim();
    if (!message || busy) return;
    setInput("");
    setBusy(true);
    setTurns((t) => [...t, { role: "user", text: message }, { role: "assistant", text: "", tools: [], streaming: true }]);

    const update = (fn: (last: Turn) => Turn) =>
      setTurns((t) => {
        const copy = [...t];
        copy[copy.length - 1] = fn(copy[copy.length - 1]);
        return copy;
      });

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message }),
      });
      if (!res.ok || !res.body) {
        const detail = res.status === 503 || res.status === 502 ? "buddy offline" : `error ${res.status}`;
        update((l) => ({ ...l, text: detail, streaming: false }));
        return;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split("\n\n");
        buffer = events.pop() ?? "";
        for (const raw of events) {
          const line = raw.split("\n").find((l) => l.startsWith("data: "));
          if (!line) continue;
          let event: Record<string, unknown>;
          try {
            event = JSON.parse(line.slice(6));
          } catch {
            continue;
          }
          if (event.kind === "delta") {
            update((l) => ({ ...l, text: l.text + (event.text as string) }));
          } else if (event.kind === "tool") {
            update((l) => ({
              ...l,
              tools: [...(l.tools ?? []), { name: event.name as string, ok: !!event.ok, stale: !!event.stale }],
            }));
          } else if (event.kind === "final") {
            update((l) => ({
              ...l,
              text: event.text as string,
              streaming: false,
              stale: !!event.stale,
              opinion: (event.opinion as string) ?? null,
            }));
          }
        }
      }
      update((l) => ({ ...l, streaming: false }));
    } catch {
      update((l) => ({ ...l, text: l.text || "connection lost", streaming: false }));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="chat">
      <div className="chat-scroll" ref={scroller}>
        {turns.length === 0 && (
          <p className="muted">
            Ask anything grounded in the desk&rsquo;s data. Commands: <span className="mono">{COMMANDS}</span>
          </p>
        )}
        {turns.map((t, i) => (
          <div key={i} className={"chat-turn " + t.role}>
            <div className="chat-who">
              {t.role === "user" ? "you" : "buddy"}
              {t.opinion && <span className="pill" style={{ marginLeft: 6 }}>opinion: {t.opinion} · logged</span>}
            </div>
            <div className="chat-body">{t.text || (t.streaming ? "…" : "")}</div>
            {t.tools && t.tools.length > 0 && (
              <details style={{ marginTop: 4 }}>
                <summary className="muted" style={{ cursor: "pointer", fontSize: "0.78rem" }}>
                  {t.tools.length} tool call{t.tools.length === 1 ? "" : "s"}
                </summary>
                <div style={{ marginTop: 4 }}>
                  {t.tools.map((tc, j) => (
                    <span key={j} className="pill" style={{ marginRight: 4 }}>
                      {tc.name}
                      {!tc.ok && " ✗"}
                      {tc.stale && " ⚠"}
                    </span>
                  ))}
                </div>
              </details>
            )}
          </div>
        ))}
      </div>
      <div className="chat-input">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
          placeholder={busy ? "thinking…" : "message the buddy"}
        />
        <button onClick={() => void send()} disabled={busy || !input.trim()}>
          Send
        </button>
      </div>
    </div>
  );
}
