"use client";

// Pings feed on Today (spec §17): SSE, newest first.

import { useEffect, useState } from "react";

interface Ping {
  id: string;
  ts: string;
  trigger: string;
  body: string;
}

const etTime = (iso: string) =>
  new Date(iso).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hour12: false });

export function PingsFeed() {
  const [pings, setPings] = useState<Ping[]>([]);
  const [offline, setOffline] = useState(false);

  useEffect(() => {
    const es = new EventSource("/api/pings");
    es.onmessage = (e) => {
      try {
        const event = JSON.parse(e.data);
        const ping: Ping = event.ping;
        if (!ping) return;
        setOffline(false);
        setPings((p) => (p.some((x) => x.id === ping.id) ? p : [ping, ...p].slice(0, 50)));
      } catch {
        /* keepalive */
      }
    };
    es.onerror = () => setOffline(true);
    return () => es.close();
  }, []);

  return (
    <div className="card">
      <h2>
        Pings {offline && <span className="warn" style={{ fontSize: "0.8rem", fontWeight: 400 }}>feed disconnected</span>}
      </h2>
      {pings.length === 0 && <p className="muted">Quiet so far.</p>}
      <ul className="plain">
        {pings.map((p) => (
          <li key={p.id}>
            <span className="pill">{p.trigger}</span> <span className="muted">{etTime(p.ts)}</span>
            <div style={{ whiteSpace: "pre-wrap" }}>{p.body}</div>
          </li>
        ))}
      </ul>
    </div>
  );
}
