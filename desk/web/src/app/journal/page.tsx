import Link from "next/link";
import { supabaseAdmin } from "@/lib/supabase/admin";

export const dynamic = "force-dynamic";

const fmt = (n: number) => (n >= 0 ? "+" : "−") + Math.abs(n).toFixed(2);
const time = (iso: string) =>
  new Date(iso).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hour12: false });

export default async function JournalPage() {
  const db = supabaseAdmin();
  const { data: trades } = await db
    .from("trades")
    .select("id, session_date, contract, direction, size, avg_entry, avg_exit, net_pnl, entry_at")
    .order("entry_at", { ascending: false })
    .limit(200);

  const byDate = new Map<string, NonNullable<typeof trades>>();
  for (const t of trades ?? []) {
    const g = byDate.get(t.session_date) ?? [];
    g.push(t);
    byDate.set(t.session_date, g);
  }

  return (
    <>
      <h1>Journal</h1>
      <p>
        <Link href="/journal/import">Import CSV</Link> · <Link href="/journal/insights">Insight</Link> ·{" "}
        <Link href="/settings">Settings</Link>
      </p>
      {byDate.size === 0 && (
        <div className="card">
          <p className="muted">No trades yet. Import a Tradovate fills export to get started.</p>
        </div>
      )}
      {[...byDate.entries()].map(([date, rows]) => {
        const dayNet = rows.reduce((s, t) => s + Number(t.net_pnl), 0);
        return (
          <div className="card" key={date}>
            <h2>
              {date} <span className={dayNet >= 0 ? "pos" : "neg"}>{fmt(dayNet)}</span>
            </h2>
            <table>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Contract</th>
                  <th>Dir</th>
                  <th className="num">Size</th>
                  <th className="num">Entry → Exit</th>
                  <th className="num">Net</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((t) => (
                  <tr key={t.id}>
                    <td>
                      <Link href={`/journal/${t.id}`}>{time(t.entry_at)}</Link>
                    </td>
                    <td>{t.contract}</td>
                    <td>{t.direction === "long" ? "L" : "S"}</td>
                    <td className="num">{t.size}</td>
                    <td className="num">
                      {Number(t.avg_entry).toFixed(2)} → {Number(t.avg_exit).toFixed(2)}
                    </td>
                    <td className={"num " + (Number(t.net_pnl) >= 0 ? "pos" : "neg")}>{fmt(Number(t.net_pnl))}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      })}
    </>
  );
}
