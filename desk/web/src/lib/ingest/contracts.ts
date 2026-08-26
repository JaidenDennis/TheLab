// Futures contract metadata: dollars per point, and product-root extraction
// from a Tradovate contract code like "MNQU6" or "NQZ25".

const POINT_VALUES: Record<string, number> = {
  NQ: 20,
  MNQ: 2,
  ES: 50,
  MES: 5,
  YM: 5,
  MYM: 0.5,
  RTY: 50,
  M2K: 5,
  CL: 1000,
  MCL: 100,
  GC: 100,
  MGC: 10,
};

const MONTH_CODES = new Set(["F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"]);

/** "MNQU6" -> "MNQ"; "NQZ25" -> "NQ". Falls back to the alpha prefix. */
export function productRoot(contract: string): string {
  const code = contract.trim().toUpperCase();
  const m = code.match(/^([A-Z0-9]+?)([FGHJKMNQUVXZ])\d{1,2}$/);
  if (m && MONTH_CODES.has(m[2])) return m[1];
  const alpha = code.match(/^[A-Z]+/);
  return alpha ? alpha[0] : code;
}

/** Dollars per point, or null when the product is unknown to us. */
export function pointValue(product: string): number | null {
  return POINT_VALUES[product.toUpperCase()] ?? null;
}
