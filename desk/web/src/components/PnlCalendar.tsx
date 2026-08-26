"use client";

// The month tape: one tile per day, net P&L in mono, green/red wash by
// outcome, amber ring on today. "+" on any day opens the manual trade form.

import { useActionState, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { addManualTrade, type AddTradeResult } from "@/app/journal/actions";

export interface CalDay {
  net: number;
  count: number;
}

const DOW = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];

const fmtPnl = (n: number) => {
  const sign = n >= 0 ? "+" : "−";
  const a = Math.abs(n);
  return sign + (a >= 1000 ? (a / 1000).toFixed(1) + "k" : a.toFixed(0));
};

function monthShift(month: string, by: number): string {
  const [y, m] = month.split("-").map(Number);
  const d = new Date(Date.UTC(y, m - 1 + by, 1));
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
}

export function PnlCalendar({
  month,
  days,
  today,
}: {
  month: string; // YYYY-MM
  days: Record<string, CalDay>;
  today: string; // YYYY-MM-DD (ET)
}) {
  const [adding, setAdding] = useState<string | null>(null);
  const [y, m] = month.split("-").map(Number);
  const firstDow = new Date(Date.UTC(y, m - 1, 1)).getUTCDay();
  const daysInMonth = new Date(Date.UTC(y, m, 0)).getUTCDate();
  const title = new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString("en-US", {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  });

  const cells: (number | null)[] = [
    ...Array.from({ length: firstDow }, () => null),
    ...Array.from({ length: daysInMonth }, (_, i) => i + 1),
  ];

  return (
    <div className="cal">
      <div className="cal-head">
        <span className="cal-title">{title}</span>
        <span className="cal-nav">
          <Link href={`/journal?m=${monthShift(month, -1)}`} aria-label="Previous month">←</Link>
          <Link href={`/journal?m=${monthShift(month, 1)}`} aria-label="Next month">→</Link>
        </span>
      </div>
      <div className="cal-grid">
        {DOW.map((d) => (
          <div key={d} className="cal-dow">{d}</div>
        ))}
        {cells.map((day, i) => {
          if (day === null) return <div key={`x${i}`} className="cal-day out" />;
          const date = `${month}-${String(day).padStart(2, "0")}`;
          const info = days[date];
          const cls =
            "cal-day" +
            (date === today ? " today" : "") +
            (info ? (info.net >= 0 ? " win" : " loss") : "");
          return (
            <div key={date} className={cls}>
              <span className="cal-num">{day}</span>
              <button className="cal-add" onClick={() => setAdding(date)} aria-label={`Add trade on ${date}`}>
                +
              </button>
              {info && (
                <a href={`#d-${date}`} className={"cal-pnl " + (info.net >= 0 ? "pos" : "neg")}>
                  {fmtPnl(info.net)}
                </a>
              )}
            </div>
          );
        })}
      </div>
      {adding && <AddTradeModal date={adding} onClose={() => setAdding(null)} />}
    </div>
  );
}

const INITIAL: AddTradeResult = { error: null };

function AddTradeModal({ date, onClose }: { date: string; onClose: () => void }) {
  const [state, action, pending] = useActionState(addManualTrade, INITIAL);
  const submitted = useRef(false);

  useEffect(() => {
    if (submitted.current && !pending && state.error === null) onClose();
  }, [state, pending, onClose]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="modal-back" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label="Add trade">
        <h2>
          Add trade <span className="mono muted">{date}</span>
        </h2>
        <form action={action} onSubmit={() => (submitted.current = true)}>
          <input type="hidden" name="date" value={date} />
          <div className="form-row">
            <div>
              <label htmlFor="mt-contract">Contract</label>
              <input id="mt-contract" name="contract" type="text" defaultValue="MNQ" required />
            </div>
            <div>
              <label htmlFor="mt-direction">Direction</label>
              <select id="mt-direction" name="direction" defaultValue="long">
                <option value="long">Long</option>
                <option value="short">Short</option>
              </select>
            </div>
          </div>
          <div className="form-row">
            <div>
              <label htmlFor="mt-size">Size</label>
              <input id="mt-size" name="size" type="number" min={1} step={1} defaultValue={1} required />
            </div>
            <div>
              <label htmlFor="mt-fees">Fees ($)</label>
              <input id="mt-fees" name="fees" type="number" min={0} step="0.01" defaultValue={0} />
            </div>
          </div>
          <div className="form-row">
            <div>
              <label htmlFor="mt-entry-time">Entry time (ET)</label>
              <input id="mt-entry-time" name="entry_time" type="time" required />
            </div>
            <div>
              <label htmlFor="mt-exit-time">Exit time (ET)</label>
              <input id="mt-exit-time" name="exit_time" type="time" required />
            </div>
          </div>
          <div className="form-row">
            <div>
              <label htmlFor="mt-entry-price">Entry price</label>
              <input id="mt-entry-price" name="entry_price" type="number" step="0.25" required />
            </div>
            <div>
              <label htmlFor="mt-exit-price">Exit price</label>
              <input id="mt-exit-price" name="exit_price" type="number" step="0.25" required />
            </div>
          </div>
          {state.error && <p className="neg" style={{ marginBottom: 0 }}>{state.error}</p>}
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button type="button" className="secondary" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" disabled={pending}>
              {pending ? "Saving…" : "Save trade"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
