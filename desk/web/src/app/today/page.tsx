import Link from "next/link";
import { revalidatePath } from "next/cache";
import { supabaseAdmin } from "@/lib/supabase/admin";
import { etDateOf } from "@/lib/ingest/time";
import { QuickNote } from "@/components/QuickNote";
import { PositionCard } from "@/components/PositionCard";
import { PingsFeed } from "@/components/PingsFeed";

export const dynamic = "force-dynamic";

async function savePlan(formData: FormData) {
  "use server";
  const db = supabaseAdmin();
  const row = {
    session_date: String(formData.get("session_date")),
    htf_bias: String(formData.get("htf_bias")),
    key_levels: String(formData.get("key_levels") ?? "").trim(),
    hunting: String(formData.get("hunting") ?? "").trim(),
    invalidation: String(formData.get("invalidation") ?? "").trim(),
  };
  await db.from("sessions").upsert(row, { onConflict: "session_date" });
  revalidatePath("/today");
}

export default async function TodayPage({ searchParams }: { searchParams: Promise<{ edit?: string }> }) {
  const sp = await searchParams;
  const db = supabaseAdmin();
  const today = etDateOf(new Date().toISOString());
  const { data: session } = await db.from("sessions").select("*").eq("session_date", today).maybeSingle();
  const showForm = !session || sp.edit === "1";

  return (
    <>
      <h1>Today · {today}</h1>

      <PositionCard />

      {showForm ? (
        <form className="card" action={savePlan}>
          <h2>{session ? "Edit session plan" : "Session plan"}</h2>
          <input type="hidden" name="session_date" value={today} />
          <label htmlFor="htf_bias">HTF bias</label>
          <select id="htf_bias" name="htf_bias" defaultValue={session?.htf_bias ?? "neutral"}>
            <option value="bullish">Bullish</option>
            <option value="bearish">Bearish</option>
            <option value="neutral">Neutral</option>
          </select>
          <label htmlFor="key_levels">Key levels</label>
          <textarea id="key_levels" name="key_levels" required defaultValue={session?.key_levels ?? ""} />
          <label htmlFor="hunting">What am I hunting?</label>
          <textarea id="hunting" name="hunting" required defaultValue={session?.hunting ?? ""} />
          <label htmlFor="invalidation">Invalidation</label>
          <textarea id="invalidation" name="invalidation" required defaultValue={session?.invalidation ?? ""} />
          <button type="submit">Save plan</button>
        </form>
      ) : (
        <div className="card">
          <h2>
            Session plan <Link href="/today?edit=1" className="muted" style={{ fontWeight: 400 }}>edit</Link>
          </h2>
          <p>
            Bias: <b>{session.htf_bias}</b>
          </p>
          <p>
            <span className="muted">Levels:</span> {session.key_levels}
          </p>
          <p>
            <span className="muted">Hunting:</span> {session.hunting}
          </p>
          <p>
            <span className="muted">Invalidation:</span> {session.invalidation}
          </p>
          {session.day_read_json == null && (
            <p className="muted">No frozen day read yet — posted by the buddy at 09:20 ET once the brain is live.</p>
          )}
        </div>
      )}

      <QuickNote />

      <div className="card">
        <Link href="/checklist">
          <b>Pre-trade checklist →</b>
        </Link>
        <p className="muted">Fill before entry. Five taps.</p>
      </div>

      <PingsFeed />
    </>
  );
}
