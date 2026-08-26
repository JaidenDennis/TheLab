import Link from "next/link";

const VIEWS = [
  { href: "/journal/insights/rules", title: "Rule adherence", blurb: "Violations by rule, correlated with outcome. Valid at low n — the primary view." },
  { href: "/journal/insights/facets", title: "Facet explorer", blurb: "P&L and expectancy by facet, every cell with its n." },
  { href: "/journal/insights/sessions", title: "Session review", blurb: "Plan vs what was actually traded, frozen day read alongside." },
  { href: "/journal/insights/buddy", title: "Buddy scorecard", blurb: "Graded opinions by week: hit rate, Brier, validated-only vs mixed." },
];

export default function InsightsPage() {
  return (
    <>
      <h1>Insight</h1>
      {VIEWS.map((v) => (
        <div className="card" key={v.href}>
          <Link href={v.href}>
            <b>{v.title} →</b>
          </Link>
          <p className="muted">{v.blurb}</p>
        </div>
      ))}
      <p className="muted">No metric renders without its sample size. Below n=10 a cell is labeled insufficient.</p>
    </>
  );
}
