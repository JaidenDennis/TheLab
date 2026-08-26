import Link from "next/link";
import { supabaseAdmin } from "@/lib/supabase/admin";
import { cell, checklistTradePairs } from "@/lib/insights";

export const dynamic = "force-dynamic";

export default async function RulesInsight() {
  const db = supabaseAdmin();
  const pairs = await checklistTradePairs(db);

  const byRule = new Map<string, number>();
  for (const p of pairs) {
    for (const v of p.entry.rule_violations ?? []) {
      byRule.set(v, (byRule.get(v) ?? 0) + 1);
    }
    if (p.entry.htf_bias_overridden) byRule.set("htf_bias_override", (byRule.get("htf_bias_override") ?? 0) + 1);
  }

  const withPnl = pairs.filter((p) => p.net !== null);
  const clean = cell(withPnl.filter((p) => (p.entry.rule_violations ?? []).length === 0 && !p.entry.htf_bias_overridden).map((p) => p.net!));
  const violated = cell(withPnl.filter((p) => (p.entry.rule_violations ?? []).length > 0 || p.entry.htf_bias_overridden).map((p) => p.net!));

  const convictions = pairs.map((p) => p.entry.conviction as number);
  const convDist = Array.from({ length: 10 }, (_, i) => convictions.filter((c) => c === i + 1).length);

  return (
    <>
      <h1>Rule adherence</h1>
      <div className="card">
        <h2>Violations logged ({pairs.length} checklist entries)</h2>
        {byRule.size === 0 && <p className="muted">None yet.</p>}
        <ul className="plain">
          {[...byRule.entries()].map(([rule, n]) => (
            <li key={rule}>
              {rule}: <b>{n}</b>
            </li>
          ))}
        </ul>
      </div>
      <div className="card">
        <h2>Outcome: clean vs violated</h2>
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
            {[
              ["clean", clean],
              ["violated", violated],
            ].map(([name, c]) => {
              const k = c as ReturnType<typeof cell>;
              return (
                <tr key={String(name)}>
                  <td>{String(name)}</td>
                  <td className="num">{k.n}</td>
                  <td className={"num " + ((k.net ?? 0) >= 0 ? "pos" : "neg")}>{k.net}</td>
                  <td className="num">{k.insufficient ? "insufficient sample" : k.winRate}</td>
                  <td className="num">{k.insufficient ? "—" : k.expectancy}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <p className="muted">Checklist↔trade pairing is positional within each day (1st entry ↔ 1st trade).</p>
      </div>
      <div className="card">
        <h2>Conviction distribution (pre-trade, honest)</h2>
        <table>
          <thead>
            <tr>{convDist.map((_, i) => <th className="num" key={i}>{i + 1}</th>)}</tr>
          </thead>
          <tbody>
            <tr>{convDist.map((n, i) => <td className="num" key={i}>{n || "·"}</td>)}</tr>
          </tbody>
        </table>
      </div>
      <p>
        <Link href="/journal/insights">← Insight</Link>
      </p>
    </>
  );
}
