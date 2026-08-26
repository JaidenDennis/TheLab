// Server-side import pipeline: CSV text -> fills upserted -> trades rebuilt.
// Rebuild (shared with desk-brain's fill auto-pull) always reads ALL fills for
// the touched (account, contract) groups so a partial re-export still
// reconstructs complete round-trips.
import "server-only";
import type { SupabaseClient } from "@supabase/supabase-js";
import { parseTradovateCsv } from "./parse";
import { dedupeFills } from "./dedupe";
import { rebuildGroups, type Group } from "./rebuild";
import type { ImportSummary } from "./types";

export async function importCsv(db: SupabaseClient, csv: string): Promise<ImportSummary> {
  const summary: ImportSummary = {
    ok: false,
    errors: [],
    warnings: [],
    fillsInCsv: 0,
    fillsNew: 0,
    tradesRebuilt: 0,
    tradesDeleted: 0,
    notesJoined: 0,
    openPositions: [],
  };

  const parsed = parseTradovateCsv(csv);
  summary.warnings.push(...parsed.warnings);
  if (parsed.errors.length > 0) {
    summary.errors = parsed.errors;
    return summary;
  }
  const fills = dedupeFills(parsed.fills);
  summary.fillsInCsv = fills.length;
  if (fills.length === 0) {
    summary.errors.push("No fills in file.");
    return summary;
  }

  // 1. Upsert raw fills; duplicates are ignored (idempotent re-import).
  const { count: before } = await db.from("fills").select("*", { count: "exact", head: true });
  const { error: upErr } = await db.from("fills").upsert(
    fills.map((f) => ({
      account: f.account,
      order_id: f.orderId,
      exec_id: f.execId,
      contract: f.contract,
      product: f.product,
      side: f.side,
      qty: f.qty,
      price: f.price,
      fees: f.fees,
      filled_at: f.filledAt,
      raw_json: f.raw,
    })),
    { onConflict: "account,order_id,exec_id", ignoreDuplicates: true }
  );
  if (upErr) {
    summary.errors.push(`Storing fills failed: ${upErr.message}`);
    return summary;
  }
  const { count: after } = await db.from("fills").select("*", { count: "exact", head: true });
  summary.fillsNew = (after ?? 0) - (before ?? 0);

  // 2. Rebuild trades for every (account, contract) group touched by this file.
  const groups: Group[] = [...new Set(fills.map((f) => JSON.stringify([f.account, f.contract])))].map((k) => {
    const [account, contract] = JSON.parse(k) as [string, string];
    return { account, contract };
  });
  const rebuild = await rebuildGroups(db, groups);

  summary.errors.push(...rebuild.errors);
  summary.warnings.push(...rebuild.warnings);
  summary.tradesRebuilt = rebuild.tradesRebuilt;
  summary.tradesDeleted = rebuild.tradesDeleted;
  summary.notesJoined = rebuild.notesJoined;
  summary.openPositions = rebuild.openPositions;

  summary.ok = summary.errors.length === 0;
  return summary;
}
