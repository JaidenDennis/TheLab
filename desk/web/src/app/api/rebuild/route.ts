// Internal endpoint for desk-brain's fill auto-pull: after upserting fills it
// asks the web side to regenerate trades, so reconstruction logic exists in
// exactly one place. Authenticated by the shared secret, not a user session.
import { NextResponse, type NextRequest } from "next/server";
import { timingSafeEqual } from "node:crypto";
import { supabaseAdmin } from "@/lib/supabase/admin";
import { rebuildGroups, type Group } from "@/lib/ingest/rebuild";

function secretOk(request: NextRequest): boolean {
  const expected = process.env.BRAIN_SHARED_SECRET;
  const got = request.headers.get("x-brain-secret");
  if (!expected || !got) return false;
  const a = Buffer.from(expected);
  const b = Buffer.from(got);
  return a.length === b.length && timingSafeEqual(a, b);
}

export async function POST(request: NextRequest) {
  if (!secretOk(request)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  const body = await request.json().catch(() => null);
  const groups: Group[] = Array.isArray(body?.groups)
    ? body.groups.filter((g: Group) => typeof g?.account === "string" && typeof g?.contract === "string")
    : [];
  if (groups.length === 0) {
    return NextResponse.json({ error: "no groups" }, { status: 400 });
  }
  const summary = await rebuildGroups(supabaseAdmin(), groups.slice(0, 50));
  return NextResponse.json(summary, { status: summary.errors.length === 0 ? 200 : 500 });
}
