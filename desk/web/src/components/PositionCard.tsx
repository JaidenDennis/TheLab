"use client";

// Live position card (spec §5): read-only, with the engine heartbeat visible.

import { useEffect, useState } from "react";

interface Status {
  offline: boolean;
  reason?: string;
  engine?: string;
  heartbeat_age_s?: number | null;
  tradovate_connected?: boolean;
  last?: number | null;
  positions?: { contract: string; side: string; size: number; avg_price: number; unrealized: number | null }[];
  working_orders?: { contract: string; action: string; type: string; qty: number; price: number | null; status: string }[];
}

export function PositionCard() {
  const [status, setStatus] = useState<Status | null>(null);

  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const res = await fetch("/api/brain-status", { cache: "no-store" });
        if (alive) setStatus(await res.json());
      } catch {
        if (alive) setStatus({ offline: true, reason: "unreachable" });
      }
    };
    void poll();
    const id = setInterval(poll, 5000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  const dot = (color: string) => (
    <span style={{ display: "inline-block", width: 10, height: 10, borderRadius: 5, background: color, marginRight: 6 }} />
  );

  const engineColor =
    status?.engine === "ok" ? "var(--green)" : status?.engine === "stale" ? "var(--warn)" : "var(--red)";

  return (
    <div className="card">
      <h2>
        Position{" "}
        <span className="muted" style={{ fontWeight: 400, fontSize: "0.8rem" }}>
          {dot(status ? engineColor : "var(--muted)")}
          {status?.offline ? "brain offline" : `engine ${status?.engine ?? "…"}`}
          {status?.last != null && <> · last {status.last}</>}
        </span>
      </h2>
      {status?.offline && <p className="muted">Waiting for desk-brain ({status.reason}).</p>}
      {!status?.offline && !status?.tradovate_connected && <p className="muted">Tradovate sync not connected.</p>}
      {!status?.offline && status?.tradovate_connected && (status.positions?.length ?? 0) === 0 && <p>Flat.</p>}
      {(status?.positions ?? []).map((p, i) => (
        <p key={i}>
          <b>
            {p.side} {p.size} {p.contract}
          </b>{" "}
          from {p.avg_price}
          {p.unrealized != null && (
            <span className={p.unrealized >= 0 ? "pos" : "neg"}> {p.unrealized >= 0 ? "+" : "−"}${Math.abs(p.unrealized).toFixed(2)}</span>
          )}
        </p>
      ))}
      {(status?.working_orders ?? []).length > 0 && (
        <p className="muted">
          Working:{" "}
          {(status?.working_orders ?? [])
            .map((o) => `${o.action} ${o.qty} ${o.contract} ${o.type}${o.price ? ` @ ${o.price}` : ""}`)
            .join(" · ")}
        </p>
      )}
    </div>
  );
}
