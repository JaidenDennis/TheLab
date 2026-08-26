"use server";

// Manual trade entry (calendar "+"). Inserted under account "manual" so the
// fill-driven rebuild can never prune it: rebuildGroups only touches accounts
// that appear in imported fills, and "manual" never will.

import { revalidatePath } from "next/cache";
import { supabaseAdmin } from "@/lib/supabase/admin";
import { pointValue, productRoot } from "@/lib/ingest/contracts";
import { etToUtc } from "@/lib/ingest/time";

export interface AddTradeResult {
  error: string | null;
}

function etStamp(date: string, time: string): string | null {
  const dm = date.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  const tm = time.match(/^(\d{1,2}):(\d{2})$/);
  if (!dm || !tm) return null;
  return etToUtc(+dm[1], +dm[2], +dm[3], +tm[1], +tm[2], 0).toISOString();
}

export async function addManualTrade(_prev: AddTradeResult, formData: FormData): Promise<AddTradeResult> {
  const date = String(formData.get("date") ?? "");
  const contract = String(formData.get("contract") ?? "").trim().toUpperCase();
  const direction = String(formData.get("direction") ?? "");
  const size = Number(formData.get("size"));
  const entryPrice = Number(formData.get("entry_price"));
  const exitPrice = Number(formData.get("exit_price"));
  const entryTime = String(formData.get("entry_time") ?? "");
  const exitTime = String(formData.get("exit_time") ?? "");
  const fees = Number(formData.get("fees") || 0);

  if (!contract) return { error: "Contract is required." };
  if (direction !== "long" && direction !== "short") return { error: "Pick a direction." };
  if (!Number.isInteger(size) || size < 1) return { error: "Size must be a whole number of contracts." };
  if (!Number.isFinite(entryPrice) || !Number.isFinite(exitPrice)) return { error: "Entry and exit prices are required." };
  if (!Number.isFinite(fees) || fees < 0) return { error: "Fees can't be negative." };

  const entryAt = etStamp(date, entryTime);
  const exitAt = etStamp(date, exitTime);
  if (!entryAt || !exitAt) return { error: "Times must be HH:MM (ET)." };
  if (exitAt < entryAt) return { error: "Exit time is before entry time." };

  const product = productRoot(contract);
  const pv = pointValue(product);
  if (pv == null) return { error: `Unknown product "${product}" — no point value on file.` };

  const points = (exitPrice - entryPrice) * (direction === "long" ? 1 : -1);
  const gross = points * pv * size;

  const db = supabaseAdmin();
  const { error } = await db.from("trades").upsert(
    {
      account: "manual",
      contract,
      product,
      direction,
      entry_at: entryAt,
      exit_at: exitAt,
      avg_entry: entryPrice,
      avg_exit: exitPrice,
      size,
      qty_traded: size,
      gross_pnl: gross,
      fees,
      net_pnl: gross - fees,
      fill_ids: [],
      session_date: date,
      rebuilt_at: new Date().toISOString(),
    },
    { onConflict: "account,contract,direction,entry_at" },
  );
  if (error) return { error: `Saving failed: ${error.message}` };

  revalidatePath("/journal");
  return { error: null };
}
