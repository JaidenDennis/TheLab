"use client";

// Ordered, gated (spec §10): each step unlocks the next. Conviction below 8 is
// permitted and stamped as a rule violation — never blocked, never nudged.

import { useRouter } from "next/navigation";
import { useState } from "react";

const BIASES = [
  { v: "bullish", label: "Bullish" },
  { v: "bearish", label: "Bearish" },
  { v: "neutral", label: "Neutral" },
];
const PHASES = [
  { v: "accumulation", label: "Accumulation" },
  { v: "manipulation", label: "Manipulation" },
  { v: "distribution", label: "Distribution" },
  { v: "unclear", label: "Unclear" },
];

export function ChecklistForm({ nextTradeNumber, sessionBias }: { nextTradeNumber: 1 | 2; sessionBias: string | null }) {
  const router = useRouter();
  const [tradeNumber, setTradeNumber] = useState<number | null>(null);
  const [bias, setBias] = useState<string | null>(null);
  const [phase, setPhase] = useState<string | null>(null);
  const [conviction, setConviction] = useState<number | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<{ violations: string[]; overridden: boolean } | null>(null);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/checklist", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          trade_number: tradeNumber,
          htf_bias: bias,
          amd_phase: phase,
          conviction,
          entry_confirmation: confirmation,
        }),
      });
      const json = await res.json();
      if (!json.ok) {
        setError(json.error ?? "save failed");
        return;
      }
      setSaved({ violations: json.violations, overridden: json.htf_bias_overridden });
    } catch {
      setError("offline — checklist requires a connection");
    } finally {
      setBusy(false);
    }
  };

  if (saved) {
    return (
      <div className="card">
        <p className="pos">
          <b>Logged.</b> Go take the trade.
        </p>
        {saved.overridden && <p className="warn">HTF bias override flagged (differs from session plan).</p>}
        {saved.violations.includes("conviction") && <p className="warn">Stamped rule_violation: conviction.</p>}
        <button onClick={() => router.push("/today")}>Back to Today</button>
      </div>
    );
  }

  return (
    <div className="card">
      <label>1 · Trade # of day</label>
      <div>
        {[1, 2].map((n) => (
          <button
            key={n}
            className={tradeNumber === n ? "" : "secondary"}
            style={{ marginRight: 8 }}
            onClick={() => setTradeNumber(n)}
          >
            {n === 1 ? "1st" : "2nd"}
          </button>
        ))}
        {nextTradeNumber === 2 && <span className="muted"> one already logged today</span>}
      </div>

      {tradeNumber !== null && (
        <>
          <label>2 · HTF bias {sessionBias && <span className="muted">(plan: {sessionBias})</span>}</label>
          <div>
            {BIASES.map((b) => (
              <button
                key={b.v}
                className={bias === b.v ? "" : "secondary"}
                style={{ marginRight: 8 }}
                onClick={() => setBias(b.v)}
              >
                {b.label}
              </button>
            ))}
          </div>
          {bias && sessionBias && bias !== sessionBias && <p className="warn">Override of session plan — will be flagged.</p>}
        </>
      )}

      {bias !== null && (
        <>
          <label>3 · AMD phase</label>
          <div>
            {PHASES.map((p) => (
              <button
                key={p.v}
                className={phase === p.v ? "" : "secondary"}
                style={{ marginRight: 8, marginBottom: 6 }}
                onClick={() => setPhase(p.v)}
              >
                {p.label}
              </button>
            ))}
          </div>
        </>
      )}

      {phase !== null && (
        <>
          <label>4 · Conviction (1–10)</label>
          <div>
            {Array.from({ length: 10 }, (_, i) => i + 1).map((n) => (
              <button
                key={n}
                className={conviction === n ? "" : "secondary"}
                style={{ marginRight: 4, marginBottom: 4, padding: "8px 12px" }}
                onClick={() => setConviction(n)}
              >
                {n}
              </button>
            ))}
          </div>
        </>
      )}

      {conviction !== null && (
        <>
          <label htmlFor="confirmation">5 · Entry point confirmed</label>
          <input
            id="confirmation"
            type="text"
            value={confirmation}
            onChange={(e) => setConfirmation(e.target.value)}
            placeholder="What confirms this entry, right now?"
          />
          <button onClick={submit} disabled={busy || !confirmation.trim()}>
            {busy ? "Saving…" : "Log it"}
          </button>
        </>
      )}

      {error && <p className="neg">{error}</p>}
    </div>
  );
}
