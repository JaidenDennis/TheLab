import { revalidatePath } from "next/cache";
import { supabaseAdmin } from "@/lib/supabase/admin";

export const dynamic = "force-dynamic";

const FACETS = ["location", "context", "trigger", "management"] as const;

async function addTag(formData: FormData) {
  "use server";
  const facet = String(formData.get("facet"));
  const label = String(formData.get("label") ?? "").trim();
  if (!label || !FACETS.includes(facet as (typeof FACETS)[number])) return;
  const db = supabaseAdmin();
  await db.from("tags").upsert({ facet, label, active: true }, { onConflict: "facet,label" });
  revalidatePath("/settings");
}

async function toggleTagActive(formData: FormData) {
  "use server";
  const id = String(formData.get("id"));
  const active = String(formData.get("active")) === "true";
  const db = supabaseAdmin();
  await db.from("tags").update({ active: !active }).eq("id", id);
  revalidatePath("/settings");
}

async function addCalendar(formData: FormData) {
  "use server";
  const raw = String(formData.get("events") ?? "");
  const db = supabaseAdmin();
  const rows = [];
  for (const line of raw.split("\n")) {
    // "2026-08-28 08:30 Core PCE high"
    const m = line.trim().match(/^(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})\s+(.+?)\s+(low|medium|high)$/i);
    if (m) rows.push({ event_date: m[1], event_time_et: m[2], name: m[3], impact: m[4].toLowerCase() });
  }
  if (rows.length > 0) await db.from("calendar_events").insert(rows);
  revalidatePath("/settings");
}

export default async function SettingsPage() {
  const db = supabaseAdmin();
  const [{ data: tags }, { data: rules }, { data: events }] = await Promise.all([
    db.from("tags").select("*").order("facet").order("label"),
    db.from("rule_versions").select("*").order("version", { ascending: false }),
    db.from("calendar_events").select("*").gte("event_date", new Date().toISOString().slice(0, 10)).order("event_date").order("event_time_et").limit(20),
  ]);

  return (
    <>
      <h1>Settings</h1>

      <div className="card">
        <h2>Facet vocabulary</h2>
        {FACETS.map((facet) => (
          <div key={facet}>
            <label style={{ textTransform: "capitalize" }}>{facet}</label>
            <div>
              {(tags ?? [])
                .filter((t) => t.facet === facet)
                .map((t) => (
                  <form key={t.id} action={toggleTagActive} style={{ display: "inline" }}>
                    <input type="hidden" name="id" value={t.id} />
                    <input type="hidden" name="active" value={String(t.active)} />
                    <button
                      className="secondary"
                      style={{ marginRight: 6, marginBottom: 6, padding: "5px 10px", fontSize: "0.85rem", opacity: t.active ? 1 : 0.4 }}
                      title={t.active ? "tap to deactivate" : "tap to reactivate"}
                    >
                      {t.label}
                    </button>
                  </form>
                ))}
            </div>
          </div>
        ))}
        <form action={addTag}>
          <label>Add option</label>
          <select name="facet" defaultValue="location">
            {FACETS.map((f) => (
              <option key={f} value={f}>
                {f}
              </option>
            ))}
          </select>
          <input type="text" name="label" placeholder="new option label" style={{ marginTop: 6 }} />
          <button type="submit">Add</button>
        </form>
        <p className="muted">A taxonomy where every option is legitimate cannot detect a bad trade — keep the honest ones.</p>
      </div>

      <div className="card">
        <h2>Rule versions</h2>
        {(rules ?? []).map((r) => (
          <p key={r.id}>
            <b>v{r.version}</b> <span className="muted">{JSON.stringify(r.rules_json)}</span>
          </p>
        ))}
        <p className="muted">
          New versions are added by inserting into rule_versions (SQL editor) — deliberately not a button. Statistics never
          mix trades judged under different rules.
        </p>
      </div>

      <div className="card">
        <h2>Econ calendar</h2>
        {(events ?? []).length > 0 && (
          <ul className="plain">
            {(events ?? []).map((e) => (
              <li key={e.id}>
                {e.event_date} {String(e.event_time_et).slice(0, 5)} ET — {e.name} <span className="pill">{e.impact}</span>
              </li>
            ))}
          </ul>
        )}
        <form action={addCalendar}>
          <label>Paste events, one per line: YYYY-MM-DD HH:MM Name impact</label>
          <textarea name="events" placeholder={"2026-08-28 08:30 Core PCE high\n2026-09-05 08:30 NFP high"} />
          <button type="submit">Add events</button>
        </form>
        <p className="muted">The buddy&rsquo;s calendar tool and the 5-minute event warning read from here.</p>
      </div>

      <div className="card">
        <h2>AMD labels</h2>
        <p className="muted">
          Checklist dropdown: Accumulation / Manipulation / Distribution / Unclear. Open item from the spec — if these
          don&rsquo;t match the words you actually use, say so in chat and we rename them in one place.
        </p>
      </div>
    </>
  );
}
