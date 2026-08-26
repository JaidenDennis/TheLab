// Tradovate CSV -> NormalizedFill[]. All knowledge of the broker's export
// format lives here (spec §7, §23: format drift is isolated to Ingest).
//
// Column matching is alias-based and case/space-insensitive so minor export
// changes are absorbed here, in one place. A missing required column fails
// the whole file with a message naming it — never a silent partial import.

import Papa from "papaparse";
import { productRoot } from "./contracts";
import { parseFillTime } from "./time";
import type { NormalizedFill, ParseResult } from "./types";

type Field = "account" | "orderId" | "execId" | "contract" | "product" | "side" | "qty" | "price" | "filledAt" | "fees";

const ALIASES: Record<Field, string[]> = {
  account: ["account", "accountname", "accountid"],
  orderId: ["orderid", "order", "ordernumber"],
  execId: ["execid", "fillid", "id", "executionid"],
  contract: ["contract", "symbol", "contractname"],
  product: ["product"],
  side: ["bs", "side", "buysell", "action"],
  qty: ["qty", "filledqty", "fillqty", "quantity"],
  price: ["price", "fillprice", "avgprice", "avgfillprice"],
  filledAt: ["filltime", "timestamp", "time", "datetime", "date"],
  fees: ["commission", "commissions", "fees", "fee", "comm"],
};

const REQUIRED: Field[] = ["account", "orderId", "contract", "side", "qty", "price", "filledAt"];

const norm = (h: string) => h.toLowerCase().replace(/[^a-z0-9]/g, "");

function mapHeaders(headers: string[]): { map: Partial<Record<Field, string>>; missing: Field[] } {
  const map: Partial<Record<Field, string>> = {};
  for (const field of Object.keys(ALIASES) as Field[]) {
    for (const alias of ALIASES[field]) {
      const hit = headers.find((h) => norm(h) === alias);
      if (hit !== undefined) {
        map[field] = hit;
        break;
      }
    }
  }
  return { map, missing: REQUIRED.filter((f) => map[f] === undefined) };
}

function parseSide(text: string): "buy" | "sell" | null {
  const t = text.trim().toLowerCase();
  if (["b", "buy", "bot", "bought"].includes(t)) return "buy";
  if (["s", "sell", "sld", "sold"].includes(t)) return "sell";
  return null;
}

function parseNum(text: string): number | null {
  const t = text.replace(/[$,\s]/g, "").replace(/^\((.*)\)$/, "-$1");
  if (t === "") return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}

export function parseTradovateCsv(csv: string): ParseResult {
  const warnings: string[] = [];
  const errors: string[] = [];

  const parsed = Papa.parse<Record<string, string>>(csv.replace(/^﻿/, ""), {
    header: true,
    skipEmptyLines: true,
  });
  for (const e of parsed.errors) {
    if (e.code !== "TooFewFields" && e.code !== "TooManyFields") {
      errors.push(`CSV parse error on row ${e.row}: ${e.message}`);
    }
  }
  const headers = parsed.meta.fields ?? [];
  const { map, missing } = mapHeaders(headers);
  if (missing.length > 0) {
    errors.push(
      `Unrecognized export format — missing column(s): ${missing.join(", ")}. ` +
        `Found headers: ${headers.join(", ") || "(none)"}. Fix the alias map in desk/web/src/lib/ingest/parse.ts.`
    );
    return { fills: [], warnings, errors };
  }

  const get = (row: Record<string, string>, f: Field) => (map[f] !== undefined ? (row[map[f]!] ?? "").trim() : "");

  const fills: NormalizedFill[] = [];
  // For synthesizing exec ids when the export has none: occurrence counter per
  // fill signature, stable across re-exports of overlapping ranges.
  const sigCount = new Map<string, number>();

  parsed.data.forEach((row, i) => {
    const rowNo = i + 2; // 1-based + header row
    const account = get(row, "account");
    const orderId = get(row, "orderId");
    const contract = get(row, "contract").toUpperCase();
    const side = parseSide(get(row, "side"));
    const qty = parseNum(get(row, "qty"));
    const price = parseNum(get(row, "price"));
    const filledAt = parseFillTime(get(row, "filledAt"));

    const problems: string[] = [];
    if (!account) problems.push("account");
    if (!orderId) problems.push("order id");
    if (!contract) problems.push("contract");
    if (!side) problems.push("side");
    if (qty === null || qty <= 0 || !Number.isInteger(qty)) problems.push("qty");
    if (price === null || price <= 0) problems.push("price");
    if (!filledAt) problems.push("fill time");
    if (problems.length > 0) {
      errors.push(`Row ${rowNo}: bad or missing ${problems.join(", ")}`);
      return;
    }

    let execId = get(row, "execId");
    if (!execId) {
      const sig = `${account}|${orderId}|${filledAt}|${price}|${qty}|${side}`;
      const n = (sigCount.get(sig) ?? 0) + 1;
      sigCount.set(sig, n);
      execId = `synth:${sig}#${n}`;
      if (i === 0) warnings.push("Export has no exec-id column; synthesizing stable ids from fill signatures.");
    }

    const feesRaw = get(row, "fees");
    let fees = 0;
    if (feesRaw) {
      const f = parseNum(feesRaw);
      if (f === null) warnings.push(`Row ${rowNo}: unparseable fee "${feesRaw}", using 0`);
      else fees = Math.abs(f);
    }

    const product = get(row, "product").toUpperCase() || productRoot(contract);

    fills.push({
      account,
      orderId,
      execId,
      contract,
      product,
      side: side!,
      qty: qty!,
      price: price!,
      fees,
      filledAt: filledAt!,
      raw: row,
    });
  });

  return { fills, warnings, errors };
}
