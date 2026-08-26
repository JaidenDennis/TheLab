import Link from "next/link";
import { supabaseAdmin } from "@/lib/supabase/admin";
import { etDateOf } from "@/lib/ingest/time";
import { PnlCalendar, type CalDay } from "@/components/PnlCalendar";

export const dynamic = "force-dynamic";

const fmt = (n: number) => (n >= 0 ? "+" : "−") + Math.abs(n).toFixed(2);
const time = (iso: string) =>
  new Date(iso).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hour12: false });

export default async function JournalPage({ searchParams }: { searchParams: Promise<{ m?: string }> }) {
  const sp = await searchParams;
  const today = etDateOf(new Date().toISOString());
  const month = /^\d{4}-(0[1-9]|1[0-2])$/.test(sp.m ?? "") ? sp.m! : today.slice(0, 7);

  const db = supabaseAdmin();
  const [y, mo] = month.split("-").map(Number);
  const monthEnd = `${month}-${String(new Date(Date.UTC(y, mo, 0)).getUTCDate()).padStart(2, "0")}`;

  const [{ data: monthTrades }, { data: trades }] = await Promise.all([
    db.from("trades").select("session_date, net_pnl").gte("session_date", `${month}-01`).lte("session_date", monthEnd),
    db
      .from("trades")
      .select("id, session_date, account, contract, direction, size, avg_entry, avg_exit, net_pnl, entry_at")
      .order("entry_at", { ascending: false })
      .limit(200),
  ]);

  const calDays: Record<string, CalDay> = {};
  for (const t of monthTrades ?? []) {
    const d = (calDays[t.session_date] ??= { net: 0, count: 0 });
    d.net += Number(t.net_pnl);
    d.count += 1;
  }

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

      <PnlCalendar month={month} days={calDays} today={today} />

      {byDate.size === 0 && (
        <div className="card">
          <p className="muted">
            No trades yet. Import a Tradovate fills export, or add one by hand with the + on any calendar day.
          </p>
        </div>
      )}
      {[...byDate.entries()].map(([date, rows]) => {
        const dayNet = rows.reduce((s, t) => s + Number(t.net_pnl), 0);
        return (
          <div className="card" key={date} id={`d-${date}`}>
            <h2>
              <span className="mono">{date}</span>{" "}
              <span className={"mono " + (dayNet >= 0 ? "pos" : "neg")}>{fmt(dayNet)}</span>
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
                    <td className="mono">
                      <Link href={`/journal/${t.id}`}>{time(t.entry_at)}</Link>
                      {t.account === "manual" && <span className="pill" style={{ marginLeft: 6 }}>manual</span>}
                    </td>
                    <td className="mono">{t.contract}</td>
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
