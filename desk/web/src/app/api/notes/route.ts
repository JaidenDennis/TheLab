import { NextResponse, type NextRequest } from "next/server";
import { supabaseAdmin } from "@/lib/supabase/admin";

export async function POST(request: NextRequest) {
  const { body, captured_at } = await request.json().catch(() => ({}));
  if (!body || typeof body !== "string" || !body.trim()) {
    return NextResponse.json({ ok: false, error: "empty note" }, { status: 400 });
  }
  const capturedAt = captured_at && !Number.isNaN(Date.parse(captured_at)) ? new Date(captured_at).toISOString() : new Date().toISOString();
  const ua = request.headers.get("user-agent") ?? "";
  const source = /iPhone|iPad|Android|Mobile/i.test(ua) ? "phone" : "desktop";

  const db = supabaseAdmin();
  const { data, error } = await db
    .from("notes")
    .insert({ body: body.trim(), captured_at: capturedAt, source })
    .select("id")
    .single();
  if (error) return NextResponse.json({ ok: false, error: error.message }, { status: 500 });
  return NextResponse.json({ ok: true, id: data.id });
}

export async function GET() {
  const db = supabaseAdmin();
  const since = new Date(Date.now() - 24 * 3600 * 1000).toISOString();
  const { data, error } = await db
    .from("notes")
    .select("id, body, captured_at, source, matched_trade_id")
    .gte("captured_at", since)
    .order("captured_at", { ascending: false })
    .limit(30);
  if (error) return NextResponse.json({ ok: false, error: error.message }, { status: 500 });
  return NextResponse.json({ ok: true, notes: data });
}
