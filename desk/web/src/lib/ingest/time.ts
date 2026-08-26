// Eastern-time helpers. Tradovate web exports stamp fills in the account's
// display timezone with no offset; Jay's account is set to America/New_York.

const ET = "America/New_York";

const etFormatter = new Intl.DateTimeFormat("en-US", {
  timeZone: ET,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

function etWallClock(utc: Date): { y: number; mo: number; d: number; h: number; mi: number; s: number } {
  const parts: Record<string, number> = {};
  for (const p of etFormatter.formatToParts(utc)) {
    if (p.type !== "literal") parts[p.type] = Number(p.value);
  }
  return { y: parts.year, mo: parts.month, d: parts.day, h: parts.hour % 24, mi: parts.minute, s: parts.second };
}

/**
 * Interpret a wall-clock time as America/New_York and return the UTC Date.
 * Two-pass fixed-point: guess UTC, see what ET wall clock that lands on,
 * correct by the difference. Exact except inside the 1h DST-fallback overlap,
 * where it resolves to the earlier (EDT) instant.
 */
export function etToUtc(y: number, mo: number, d: number, h: number, mi: number, s: number): Date {
  let guess = Date.UTC(y, mo - 1, d, h, mi, s);
  for (let i = 0; i < 2; i++) {
    const wc = etWallClock(new Date(guess));
    const seen = Date.UTC(wc.y, wc.mo - 1, wc.d, wc.h, wc.mi, wc.s);
    const want = Date.UTC(y, mo - 1, d, h, mi, s);
    guess += want - seen;
  }
  return new Date(guess);
}

/** ET calendar date (YYYY-MM-DD) of a UTC instant. */
export function etDateOf(utcIso: string): string {
  const wc = etWallClock(new Date(utcIso));
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${wc.y}-${pad(wc.mo)}-${pad(wc.d)}`;
}

/**
 * Parse a fill timestamp. Accepts ISO 8601 (with offset or Z) verbatim;
 * otherwise expects Tradovate's "MM/DD/YYYY HH:mm:ss" (optionally with AM/PM)
 * and interprets it as America/New_York. Returns ISO UTC, or null.
 */
export function parseFillTime(text: string): string | null {
  const t = text.trim();
  if (!t) return null;

  if (/^\d{4}-\d{2}-\d{2}T/.test(t)) {
    const d = new Date(t);
    if (!Number.isNaN(d.getTime())) {
      // Bare ISO with no offset is interpreted as ET, matching Tradovate exports.
      if (!/(Z|[+-]\d{2}:?\d{2})$/.test(t)) {
        const m = t.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/);
        if (!m) return null;
        return etToUtc(+m[1], +m[2], +m[3], +m[4], +m[5], +(m[6] ?? 0)).toISOString();
      }
      return d.toISOString();
    }
    return null;
  }

  const m = t.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})[ ,]+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?$/i);
  if (!m) return null;
  let h = +m[4];
  const ampm = m[7]?.toUpperCase();
  if (ampm === "PM" && h < 12) h += 12;
  if (ampm === "AM" && h === 12) h = 0;
  return etToUtc(+m[3], +m[1], +m[2], h, +m[5], +(m[6] ?? 0)).toISOString();
}
