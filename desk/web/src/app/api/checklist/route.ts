import { NextResponse, type NextRequest } from "next/server";
import { supabaseAdmin } from "@/lib/supabase/admin";
import { etDateOf } from "@/lib/ingest/time";

const BIASES = ["bullish", "bearish", "neutral"];
const PHASES = ["accumulation", "manipulation", "distribution", "unclear"];

export async function POST(request: NextRequest) {
  const b = await request.json().catch(() => ({}));
  const db = supabaseAdmin();
  const today = etDateOf(new Date().toISOString());

  const tradeNumber = Number(b.trade_number);
  const conviction = Number(b.conviction);
  if (![1, 2].includes(tradeNumber) || !BIASES.includes(b.htf_bias) || !PHASES.includes(b.amd_phase))
    return NextResponse.json({ ok: false, error: "invalid entry" }, { status: 400 });
  if (!Number.isInteger(conviction) || conviction < 1 || conviction > 10)
    return NextResponse.json({ ok: false, error: "invalid conviction" }, { status: 400 });
  if (typeof b.entry_confirmation !== "string" || !b.entry_confirmation.trim())
    return NextResponse.json({ ok: false, error: "entry confirmation required" }, { status: 400 });

  // Hard block at 2 attempted trades per day (spec §10), enforced server-side too.
  const { count } = await db
    .from("checklist_entries")
    .select("*", { count: "exact", head: true })
    .eq("session_date", today);
  if ((count ?? 0) >= 2) {
    return NextResponse.json({ ok: false, error: "hard block: 2 trades already logged today" }, { status: 409 });
  }

  const { data: session } = await db.from("sessions").select("htf_bias").eq("session_date", today).maybeSingle();
  const overridden = !!session && session.htf_bias !== b.htf_bias;

  const violations: string[] = [];
  if (conviction < 8) violations.push("conviction");

  const { data: rv } = await db
    .from("rule_versions")
    .select("version")
    .order("version", { ascending: false })
    .limit(1)
    .single();

  const { data, error } = await db
    .from("checklist_entries")
    .insert({
      session_date: today,
      trade_number: tradeNumber,
      htf_bias: b.htf_bias,
      htf_bias_overridden: overridden,
      amd_phase: b.amd_phase,
      conviction,
      entry_confirmation: b.entry_confirmation.trim(),
      rule_version: rv?.version ?? 1,
      rule_violations: violations,
    })
    .select("id")
    .single();
  if (error) return NextResponse.json({ ok: false, error: error.message }, { status: 500 });
  return NextResponse.json({ ok: true, id: data.id, violations, htf_bias_overridden: overridden });
}
