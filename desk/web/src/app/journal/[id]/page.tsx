import Link from "next/link";
import { notFound } from "next/navigation";
import { revalidatePath } from "next/cache";
import { supabaseAdmin } from "@/lib/supabase/admin";
import { FacetTagger, type TagOption } from "@/components/FacetTagger";
import { uploadScreenshot, deleteAttachment } from "./actions";

const TV_SYMBOLS: Record<string, string> = {
  MNQ: "CME_MINI:MNQ1!",
  NQ: "CME_MINI:NQ1!",
  MES: "CME_MINI:MES1!",
  ES: "CME_MINI:ES1!",
};

export const dynamic = "force-dynamic";

const et = (iso: string) =>
  new Date(iso).toLocaleString("en-US", {
    timeZone: "America/New_York",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });

async function saveNarrative(formData: FormData) {
  "use server";
  const id = String(formData.get("id"));
  const narrative = String(formData.get("narrative") ?? "").trim();
  const db = supabaseAdmin();
  await db.from("trades").update({ narrative: narrative || null }).eq("id", id);
  revalidatePath(`/journal/${id}`);
}

export default async function TradePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const db = supabaseAdmin();
  const { data: trade } = await db.from("trades").select("*").eq("id", id).maybeSingle();
  if (!trade) notFound();

  const [{ data: fills }, { data: notes }, { data: session }, { data: opinions }, { data: allTags }, { data: tradeTags }, { data: attachments }] =
    await Promise.all([
      db.from("fills").select("side, qty, price, filled_at, fees").in("id", trade.fill_ids ?? []).order("filled_at"),
      db.from("notes").select("id, body, captured_at, source").eq("matched_trade_id", id).order("captured_at"),
      db.from("sessions").select("htf_bias, key_levels, hunting, invalidation").eq("session_date", trade.session_date).maybeSingle(),
      db.from("opinions").select("ts, type, verdict, confidence").eq("trade_id", id).order("ts"),
      db.from("tags").select("id, facet, label").eq("active", true),
      db.from("trade_tags").select("tag_id").eq("trade_id", id),
      db.from("attachments").select("id, storage_path, caption").eq("trade_id", id).order("created_at"),
    ]);

  const signed = await Promise.all(
    (attachments ?? []).map(async (a) => {
      const { data } = await db.storage.from("attachments").createSignedUrl(a.storage_path, 3600);
      return { ...a, url: data?.signedUrl ?? null };
    })
  );
  const tvSymbol = TV_SYMBOLS[trade.product] ?? null;

  const net = Number(trade.net_pnl);

  return (
    <>
      <h1>
        {trade.session_date} · {trade.contract} {trade.direction} ·{" "}
        <span className={net >= 0 ? "pos" : "neg"}>{(net >= 0 ? "+" : "−") + Math.abs(net).toFixed(2)}</span>
      </h1>
      <p className="muted">
        {trade.size} lot peak · {Number(trade.avg_entry).toFixed(2)} → {Number(trade.avg_exit).toFixed(2)} · {et(trade.entry_at)}
        {" – "}
        {et(trade.exit_at)} ET
      </p>

      <div className="card">
        <h2>Fills</h2>
        <table>
          <thead>
            <tr>
              <th>Time (ET)</th>
              <th>Side</th>
              <th className="num">Qty</th>
              <th className="num">Price</th>
              <th className="num">Fees</th>
            </tr>
          </thead>
          <tbody>
            {(fills ?? []).map((f, i) => (
              <tr key={i}>
                <td>{et(f.filled_at)}</td>
                <td>{f.side}</td>
                <td className="num">{f.qty}</td>
                <td className="num">{Number(f.price).toFixed(2)}</td>
                <td className="num">{Number(f.fees).toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {session && (
        <div className="card">
          <h2>That morning&rsquo;s plan</h2>
          <p>
            Bias <b>{session.htf_bias}</b> · Hunting: {session.hunting}
          </p>
          <p className="muted">Invalidation: {session.invalidation}</p>
        </div>
      )}

      <div className="card">
        <h2>Notes during trade</h2>
        {(notes ?? []).length === 0 && <p className="muted">None joined. Notes within ±10 min of the trade auto-attach on import.</p>}
        <ul className="plain">
          {(notes ?? []).map((n) => (
            <li key={n.id}>
              <span className="pill">{n.source}</span> {n.body} <span className="muted">{et(n.captured_at)}</span>
            </li>
          ))}
        </ul>
      </div>

      {(opinions ?? []).length > 0 && (
        <div className="card">
          <h2>Buddy opinions while open</h2>
          <ul className="plain">
            {(opinions ?? []).map((o, i) => (
              <li key={i}>
                <span className="pill">{o.type}</span> {o.verdict} <span className="muted">{et(o.ts)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <form className="card" action={saveNarrative}>
        <h2>Full thoughts</h2>
        <input type="hidden" name="id" value={trade.id} />
        <textarea name="narrative" defaultValue={trade.narrative ?? ""} placeholder="The story of this trade — write it with the plan and notes above in view." style={{ minHeight: 140 }} />
        <button type="submit">Save narrative</button>
      </form>

      <FacetTagger tradeId={trade.id} tags={(allTags ?? []) as TagOption[]} selected={(tradeTags ?? []).map((t) => t.tag_id)} />

      <div className="card">
        <h2>Screenshots</h2>
        {signed.length === 0 && <p className="muted">The primary review artifact — attach the chart as you saw it.</p>}
        {signed.map((a) => (
          <div key={a.id} style={{ marginBottom: 10 }}>
            {a.url ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={a.url} alt={a.caption ?? "screenshot"} style={{ maxWidth: "100%", borderRadius: 8 }} />
            ) : (
              <p className="neg">could not sign {a.storage_path}</p>
            )}
            <form action={deleteAttachment.bind(null, a.id, trade.id)}>
              <button className="secondary" style={{ marginTop: 4, padding: "4px 10px", fontSize: "0.8rem" }}>
                remove
              </button>
            </form>
          </div>
        ))}
        <form action={uploadScreenshot}>
          <input type="hidden" name="trade_id" value={trade.id} />
          <input type="file" name="file" accept="image/*" />
          <button type="submit">Upload</button>
        </form>
      </div>

      {tvSymbol && (
        <div className="card">
          <h2>Chart</h2>
          <div style={{ position: "relative", paddingBottom: "62%", height: 0 }}>
            <iframe
              title="TradingView"
              src={`https://www.tradingview.com/widgetembed/?symbol=${encodeURIComponent(tvSymbol)}&interval=1&theme=dark&hidetoptoolbar=0&saveimage=0`}
              style={{ position: "absolute", inset: 0, width: "100%", height: "100%", border: 0, borderRadius: 8 }}
              allowFullScreen
            />
          </div>
          <p className="muted">
            Best-effort embed seeded to {tvSymbol}; navigate to {trade.session_date}. Futures entitlements on the free
            widget may be delayed — the screenshot above is the record.
          </p>
        </div>
      )}

      <p>
        <Link href="/journal">← Journal</Link>
      </p>
    </>
  );
}
