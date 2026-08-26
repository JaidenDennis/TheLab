"use client";

// Post-session facet tagging (spec §11): closed lists, all taps.

import { useTransition } from "react";
import { toggleTag } from "@/app/journal/[id]/actions";

export interface TagOption {
  id: string;
  facet: string;
  label: string;
}

const FACET_ORDER = ["location", "context", "trigger", "management"];

export function FacetTagger({ tradeId, tags, selected }: { tradeId: string; tags: TagOption[]; selected: string[] }) {
  const [pending, start] = useTransition();
  const byFacet = new Map<string, TagOption[]>();
  for (const t of tags) {
    byFacet.set(t.facet, [...(byFacet.get(t.facet) ?? []), t]);
  }
  return (
    <div className="card" style={pending ? { opacity: 0.6 } : undefined}>
      <h2>Facets</h2>
      {FACET_ORDER.filter((f) => byFacet.has(f)).map((facet) => (
        <div key={facet}>
          <label style={{ textTransform: "capitalize" }}>{facet}</label>
          <div>
            {byFacet.get(facet)!.map((t) => {
              const on = selected.includes(t.id);
              return (
                <button
                  key={t.id}
                  className={on ? "" : "secondary"}
                  style={{ marginRight: 6, marginBottom: 6, padding: "6px 10px", fontSize: "0.85rem" }}
                  onClick={() => start(() => toggleTag(tradeId, t.id, !on))}
                >
                  {t.label}
                </button>
              );
            })}
          </div>
        </div>
      ))}
      <p className="muted">New options are added in Settings only — never inline.</p>
    </div>
  );
}
