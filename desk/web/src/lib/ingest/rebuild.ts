// Trade rebuild for a set of (account, contract) groups, from the full fill
// history in the database. The single home of reconstruction persistence —
// used by the CSV importer and by desk-brain's fill auto-pull (POST /api/rebuild).
// Upserts on the trades natural key so enrichment survives; prunes vanished
// keys; auto-joins unmatched notes by timestamp window.
import "server-only";
import type { SupabaseClient } from "@supabase/supabase-js";
import { reconstructTrades } from "./reconstruct";
import type { NormalizedFill, OpenPosition } from "./types";

const NOTE_JOIN_WINDOW_MS = 10 * 60 * 1000;

export interface RebuildSummary {
  errors: string[];
  warnings: string[];
  tradesRebuilt: number;
  tradesDeleted: number;
  notesJoined: number;
  openPositions: OpenPosition[];
}

export interface Group {
  account: string;
  contract: string;
}

export async function rebuildGroups(db: SupabaseClient, groups: Group[]): Promise<RebuildSummary> {
  const summary: RebuildSummary = {
    errors: [],
    warnings: [],
    tradesRebuilt: 0,
    tradesDeleted: 0,
    notesJoined: 0,
    openPositions: [],
  };

  for (const { account, contract } of groups) {
    const { data: rows, error } = await db
      .from("fills")
      .select("id, account, order_id, exec_id, contract, product, side, qty, price, fees, filled_at")
      .eq("account", account)
      .eq("contract", contract)
      .order("filled_at");
    if (error || !rows) {
      summary.errors.push(`Reading fills for ${account} ${contract} failed: ${error?.message}`);
      return summary;
    }

    const dbFills: NormalizedFill[] = rows.map((r) => ({
      account: r.account,
      orderId: r.order_id,
      execId: r.exec_id,
      contract: r.contract,
      product: r.product,
      side: r.side,
      qty: r.qty,
      price: Number(r.price),
      fees: Number(r.fees),
      filledAt: new Date(r.filled_at).toISOString(),
      raw: {},
    }));
    const idByExec = new Map(rows.map((r) => [r.exec_id, r.id as string]));

    const recon = reconstructTrades(dbFills);
    summary.warnings.push(...recon.warnings);
    summary.openPositions.push(...recon.openPositions);

    const tradeRows = recon.trades.map((t) => ({
      account: t.account,
      contract: t.contract,
      product: t.product,
      direction: t.direction,
      entry_at: t.entryAt,
      exit_at: t.exitAt,
      avg_entry: t.avgEntry,
      avg_exit: t.avgExit,
      size: t.size,
      qty_traded: t.qtyTraded,
      gross_pnl: t.grossPnl,
      fees: t.fees,
      net_pnl: t.netPnl,
      fill_ids: t.fillExecIds.map((e) => idByExec.get(e)).filter(Boolean),
      session_date: t.sessionDate,
      rebuilt_at: new Date().toISOString(),
    }));

    if (tradeRows.length > 0) {
      const { error: tErr } = await db
        .from("trades")
        .upsert(tradeRows, { onConflict: "account,contract,direction,entry_at" });
      if (tErr) {
        summary.errors.push(`Writing trades for ${account} ${contract} failed: ${tErr.message}`);
        return summary;
      }
    }
    summary.tradesRebuilt += tradeRows.length;

    // Prune trades whose natural key no longer exists (e.g. a reconstruction
    // bug fix regrouped fills). Enrichment on surviving keys is untouched.
    const { data: existing } = await db
      .from("trades")
      .select("id, direction, entry_at")
      .eq("account", account)
      .eq("contract", contract);
    const liveKeys = new Set(recon.trades.map((t) => `${t.direction}|${t.entryAt}`));
    const stale = (existing ?? []).filter((t) => !liveKeys.has(`${t.direction}|${new Date(t.entry_at).toISOString()}`));
    if (stale.length > 0) {
      const { error: dErr } = await db.from("trades").delete().in("id", stale.map((t) => t.id));
      if (dErr) summary.warnings.push(`Pruning ${stale.length} stale trade(s) failed: ${dErr.message}`);
      else summary.tradesDeleted += stale.length;
    }

    // Auto-join unmatched notes by timestamp window (manually reassignable later).
    for (const t of recon.trades) {
      const from = new Date(new Date(t.entryAt).getTime() - NOTE_JOIN_WINDOW_MS).toISOString();
      const to = new Date(new Date(t.exitAt).getTime() + NOTE_JOIN_WINDOW_MS).toISOString();
      const { data: trade } = await db
        .from("trades")
        .select("id")
        .eq("account", t.account)
        .eq("contract", t.contract)
        .eq("direction", t.direction)
        .eq("entry_at", t.entryAt)
        .single();
      if (!trade) continue;
      const { data: joined } = await db
        .from("notes")
        .update({ matched_trade_id: trade.id })
        .is("matched_trade_id", null)
        .gte("captured_at", from)
        .lte("captured_at", to)
        .select("id");
      summary.notesJoined += joined?.length ?? 0;
    }
  }

  return summary;
}
