import { NextResponse, type NextRequest } from "next/server";
import { supabaseAdmin } from "@/lib/supabase/admin";
import { importCsv } from "@/lib/ingest/importCsv";

export async function POST(request: NextRequest) {
  const csv = await request.text();
  if (!csv.trim()) {
    return NextResponse.json({ ok: false, errors: ["Empty file."] }, { status: 400 });
  }
  const summary = await importCsv(supabaseAdmin(), csv);
  return NextResponse.json(summary, { status: summary.ok ? 200 : 400 });
}
