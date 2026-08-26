import Link from "next/link";
import { supabaseAdmin } from "@/lib/supabase/admin";

export const dynamic = "force-dynamic";

export default async function SessionsInsight() {
  const db = supabaseAdmin();
  const [{ data: sessions }, { data: trades }] = await Promise.all([
    db.from("sessions").select("*").order("session_date", { ascending: false }).limit(30),
    db.from("trades").select("session_date, direction, net_pnl, size, contract"),
  ]);
  const byDay = new Map<string, NonNullable<typeof trades>>();
  for (const t of trades ?? []) {
    byDay.set(t.session_date, [...(byDay.get(t.session_date) ?? []), t]);
  }

  return (
    <>
      <h1>Session review</h1>
      {(sessions ?? []).length === 0 && (
        <div className="card">
          <p className="muted">No session plans yet.</p>
        </div>
      )}
      {(sessions ?? []).map((s) => {
        const dayTrades = byDay.get(s.session_date) ?? [];
        const net = dayTrades.reduce((sum, t) => sum + Number(t.net_pnl), 0);
        const dayRead = s.day_read_json as { lean?: string } | null;
        const counterPlan = dayTrades.filter(
          (t) => s.htf_bias !== "neutral" && ((s.htf_bias === "bullish" && t.direction === "short") || (s.htf_bias === "bearish" && t.direction === "long"))
        ).length;
        return (
          <div className="card" key={s.id}>
            <h2>
              {s.session_date} <span className={net >= 0 ? "pos" : "neg"}>{(net >= 0 ? "+" : "−") + Math.abs(net).toFixed(2)}</span>
            </h2>
            <p>
              Plan: <b>{s.htf_bias}</b> · hunting {s.hunting}
              {dayRead?.lean && (
                <>
                  {" "}
                  · frozen day read: <b>{dayRead.lean}</b>
                </>
              )}
            </p>
            <p className="muted">Invalidation: {s.invalidation}</p>
            <p>
              Traded: {dayTrades.length === 0 ? "nothing" : dayTrades.map((t) => `${t.direction} ${t.size} ${t.contract} (${Number(t.net_pnl) >= 0 ? "+" : ""}${Number(t.net_pnl).toFixed(0)})`).join(" · ")}
              {counterPlan > 0 && <span className="warn"> · {counterPlan} counter-plan</span>}
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
