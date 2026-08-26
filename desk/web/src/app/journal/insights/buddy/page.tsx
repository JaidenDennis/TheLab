import Link from "next/link";
import { supabaseAdmin } from "@/lib/supabase/admin";

export const dynamic = "force-dynamic";

interface Op {
  ts: string;
  type: string;
  outcome: string | null;
  score: number | null;
  factors_json: { validated_cited?: string[] } | null;
}

function weekOf(iso: string): string {
  const d = new Date(iso);
  const day = (d.getUTCDay() + 6) % 7; // Monday=0
  const monday = new Date(d);
  monday.setUTCDate(d.getUTCDate() - day);
  return monday.toISOString().slice(0, 10);
}

export default async function BuddyInsight() {
  const db = supabaseAdmin();
  const { data } = await db.from("opinions").select("ts, type, outcome, score, factors_json").order("ts", { ascending: false }).limit(1000);
  const ops = (data ?? []) as Op[];

  const weeks = new Map<string, Op[]>();
  for (const o of ops) {
    const w = weekOf(o.ts);
    weeks.set(w, [...(weeks.get(w) ?? []), o]);
  }

  return (
    <>
      <h1>Buddy scorecard</h1>
      {weeks.size === 0 && (
        <div className="card">
          <p className="muted">No opinions logged yet. They accumulate automatically as you chat.</p>
        </div>
      )}
      {[...weeks.entries()].map(([week, rows]) => {
        const graded = rows.filter((o) => o.outcome?.startsWith("hit") || o.outcome?.startsWith("miss"));
        const hits = graded.filter((o) => o.outcome!.startsWith("hit"));
        const briers = graded.map((o) => o.score).filter((s): s is number => s !== null);
        const val = graded.filter((o) => (o.factors_json?.validated_cited?.length ?? 0) > 0);
        const mixed = graded.filter((o) => (o.factors_json?.validated_cited?.length ?? 0) === 0);
        const byType = new Map<string, { n: number; hit: number }>();
        for (const o of graded) {
          const c = byType.get(o.type) ?? { n: 0, hit: 0 };
          c.n += 1;
          if (o.outcome!.startsWith("hit")) c.hit += 1;
          byType.set(o.type, c);
        }
        const rate = (h: number, n: number) => (n ? `${h}/${n} (${Math.round((100 * h) / n)}%)` : "—");
        return (
          <div className="card" key={week}>
            <h2>Week of {week}</h2>
            <p>
              {rows.length} opinion(s), {graded.length} graded · hit {rate(hits.length, graded.length)}
              {briers.length > 0 && <> · Brier {(briers.reduce((s, b) => s + b, 0) / briers.length).toFixed(3)} (n={briers.length})</>}
            </p>
            <p className="muted">
              {[...byType.entries()].map(([t, c]) => `${t}: ${rate(c.hit, c.n)}`).join(" · ") || "nothing graded yet"}
            </p>
            <p className="muted">
              cited-validated {rate(val.filter((o) => o.outcome!.startsWith("hit")).length, val.length)} vs none-cited{" "}
              {rate(mixed.filter((o) => o.outcome!.startsWith("hit")).length, mixed.length)}
              {graded.length < 40 && " · below 40 samples, read nothing into this split"}
            </p>
          </div>
        );
      })}
      <p>
        <Link href="/journal/insights">← Insight</Link>
      </p>
    </>
  );
}
