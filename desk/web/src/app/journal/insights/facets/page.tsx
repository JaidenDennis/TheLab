import Link from "next/link";
import { supabaseAdmin } from "@/lib/supabase/admin";
import { cell } from "@/lib/insights";

export const dynamic = "force-dynamic";

const FACETS = ["location", "context", "trigger", "management"];

export default async function FacetsInsight() {
  const db = supabaseAdmin();
  const { data: trades } = await db.from("trades").select("id, net_pnl, trade_tags(tags(facet, label))");

  const byOption = new Map<string, number[]>();
  for (const t of trades ?? []) {
    for (const tt of t.trade_tags ?? []) {
      const tag = tt.tags as unknown as { facet: string; label: string } | null;
      if (!tag) continue;
      const key = `${tag.facet}|${tag.label}`;
      byOption.set(key, [...(byOption.get(key) ?? []), Number(t.net_pnl)]);
    }
  }

  return (
    <>
      <h1>Facet explorer</h1>
      {FACETS.map((facet) => {
        const rows = [...byOption.entries()]
          .filter(([k]) => k.startsWith(facet + "|"))
          .map(([k, pnls]) => ({ label: k.split("|")[1], c: cell(pnls) }))
          .sort((a, b) => b.c.n - a.c.n);
        return (
          <div className="card" key={facet}>
            <h2 style={{ textTransform: "capitalize" }}>{facet}</h2>
            {rows.length === 0 && <p className="muted">No tagged trades yet.</p>}
            {rows.length > 0 && (
              <table>
                <thead>
                  <tr>
                    <th></th>
                    <th className="num">n</th>
                    <th className="num">net</th>
                    <th className="num">win rate</th>
                    <th className="num">expectancy</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.label}>
                      <td>{r.label}</td>
                      <td className="num">{r.c.n}</td>
                      <td className={"num " + (r.c.net >= 0 ? "pos" : "neg")}>{r.c.net}</td>
                      <td className="num">{r.c.insufficient ? "insufficient sample" : r.c.winRate}</td>
                      <td className="num">{r.c.insufficient ? "—" : r.c.expectancy}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        );
      })}
      <p className="muted">
        Single-facet cuts only — 4 facets × ~5 options fragments {`n`} fast; combinations unlock when samples justify them.
      </p>
      <p>
        <Link href="/journal/insights">← Insight</Link>
      </p>
    </>
  );
}
