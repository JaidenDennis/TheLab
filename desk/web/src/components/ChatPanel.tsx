"use client";

// The buddy (spec §5): streaming replies, tool calls collapsed under each
// answer, stale warnings visible, commands passed straight through.

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

export function ChatPanel() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
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
    <div style={{ display: "flex", flexDirection: "column", minHeight: "70vh" }}>
      <div style={{ flex: 1 }}>
        {turns.length === 0 && (
          <p className="muted">
            Ask anything grounded in the desk&rsquo;s data. Commands: {COMMANDS}
          </p>
        )}
        {turns.map((t, i) => (
          <div key={i} className="card" style={t.role === "user" ? { background: "transparent" } : undefined}>
            <div className="muted" style={{ fontSize: "0.75rem" }}>
              {t.role === "user" ? "you" : "buddy"}
              {t.opinion && <span className="pill" style={{ marginLeft: 6 }}>opinion: {t.opinion} · logged</span>}
            </div>
            <div style={{ whiteSpace: "pre-wrap" }}>{t.text || (t.streaming ? "…" : "")}</div>
            {t.tools && t.tools.length > 0 && (
              <details style={{ marginTop: 6 }}>
                <summary className="muted" style={{ cursor: "pointer", fontSize: "0.8rem" }}>
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
        <div ref={bottom} />
      </div>
      <div style={{ position: "sticky", bottom: 0, background: "var(--bg)", paddingTop: 8 }}>
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
          style={{ minHeight: 48 }}
        />
        <button onClick={() => void send()} disabled={busy || !input.trim()}>
          Send
        </button>
      </div>
    </div>
  );
}
