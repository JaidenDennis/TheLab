// Shared aggregate helpers for the Insight views. Read-only (spec §7).
import "server-only";
import type { SupabaseClient } from "@supabase/supabase-js";

export const MIN_N = 10;

export interface Cell {
  n: number;
  net: number;
  winRate: number | null;
  expectancy: number | null;
  insufficient: boolean;
}

export function cell(pnls: number[]): Cell {
  const n = pnls.length;
  return {
    n,
    net: round2(pnls.reduce((s, p) => s + p, 0)),
    winRate: n ? round2(pnls.filter((p) => p > 0).length / n) : null,
    expectancy: n ? round2(pnls.reduce((s, p) => s + p, 0) / n) : null,
    insufficient: n < MIN_N,
  };
}

const round2 = (x: number) => Math.round(x * 100) / 100;

/** Pair each day's checklist entries with that day's trades by order. The join
 * is positional (1st entry ~ 1st trade) — honest about being approximate. */
export async function checklistTradePairs(db: SupabaseClient) {
  const [{ data: entries }, { data: trades }] = await Promise.all([
    db.from("checklist_entries").select("*").order("created_at"),
    db.from("trades").select("session_date, entry_at, net_pnl").order("entry_at"),
  ]);
  const tradesByDay = new Map<string, { net_pnl: number }[]>();
  for (const t of trades ?? []) {
    tradesByDay.set(t.session_date, [...(tradesByDay.get(t.session_date) ?? []), t]);
  }
  const seen = new Map<string, number>();
  return (entries ?? []).map((e) => {
    const idx = seen.get(e.session_date) ?? 0;
    seen.set(e.session_date, idx + 1);
    const trade = (tradesByDay.get(e.session_date) ?? [])[idx] ?? null;
    return { entry: e, net: trade ? Number(trade.net_pnl) : null };
  });
}
