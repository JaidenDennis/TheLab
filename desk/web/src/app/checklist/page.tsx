import { supabaseAdmin } from "@/lib/supabase/admin";
import { etDateOf } from "@/lib/ingest/time";
import { ChecklistForm } from "@/components/ChecklistForm";

export const dynamic = "force-dynamic";

export default async function ChecklistPage() {
  const db = supabaseAdmin();
  const today = etDateOf(new Date().toISOString());
  const [{ count }, { data: session }] = await Promise.all([
    db.from("checklist_entries").select("*", { count: "exact", head: true }).eq("session_date", today),
    db.from("sessions").select("htf_bias").eq("session_date", today).maybeSingle(),
  ]);
  const done = count ?? 0;

  return (
    <>
      <h1>Pre-trade checklist</h1>
      {done >= 2 ? (
        <div className="card">
          <p className="neg">
            <b>Hard block.</b> Two trades logged today. Done.
          </p>
        </div>
      ) : (
        <ChecklistForm nextTradeNumber={(done + 1) as 1 | 2} sessionBias={session?.htf_bias ?? null} />
      )}
    </>
  );
}
