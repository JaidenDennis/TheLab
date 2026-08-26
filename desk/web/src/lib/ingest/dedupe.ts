import type { NormalizedFill } from "./types";

/** Natural key matching the fills table unique constraint (spec §6). */
export function fillKey(f: Pick<NormalizedFill, "account" | "orderId" | "execId">): string {
  return `${f.account}|${f.orderId}|${f.execId}`;
}

/** Drop duplicate fills by natural key, keeping the first occurrence. */
export function dedupeFills(fills: NormalizedFill[]): NormalizedFill[] {
  const seen = new Set<string>();
  return fills.filter((f) => {
    const k = fillKey(f);
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}
