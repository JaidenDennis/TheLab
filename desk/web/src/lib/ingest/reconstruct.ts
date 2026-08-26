// Fill stream -> round-trip trades (spec §8). Pure and deterministic:
// the same fills always produce the same trades, so `trades` is regenerable.
//
// Method: per (account, contract), walk fills in time order holding a signed
// position and a weighted-average entry cost. A trade opens when the position
// leaves zero and closes when it returns to zero. A fill that crosses through
// zero (direction flip) is split: the closing portion ends the old trade, the
// remainder opens a new one, with fees allocated pro-rata.

import { pointValue } from "./contracts";
import { etDateOf } from "./time";
import type { NormalizedFill, OpenPosition, ReconstructedTrade, ReconstructionResult } from "./types";

interface OpenState {
  direction: "long" | "short";
  position: number; // absolute contracts currently open
  avgEntry: number; // weighted average entry price of the open position
  entryAt: string;
  peak: number;
  qtyTraded: number; // total contracts entered
  entryNotional: number; // sum(entry qty x price), for avg_entry of the whole trade
  exitQty: number;
  exitNotional: number;
  grossPnl: number; // dollars, accumulated per closing fill
  fees: number;
  fillExecIds: string[];
  lastFillAt: string;
}

function emit(s: OpenState, account: string, contract: string, product: string, warnings: string[]): ReconstructedTrade {
  return {
    account,
    contract,
    product,
    direction: s.direction,
    entryAt: s.entryAt,
    exitAt: s.lastFillAt,
    avgEntry: s.entryNotional / s.qtyTraded,
    avgExit: s.exitNotional / s.exitQty,
    size: s.peak,
    qtyTraded: s.qtyTraded,
    grossPnl: s.grossPnl,
    fees: s.fees,
    netPnl: s.grossPnl - s.fees,
    fillExecIds: s.fillExecIds,
    sessionDate: etDateOf(s.entryAt),
  };
}

export function reconstructTrades(fills: NormalizedFill[]): ReconstructionResult {
  const trades: ReconstructedTrade[] = [];
  const openPositions: OpenPosition[] = [];
  const warnings: string[] = [];

  // Never merge across accounts or contracts (spec §8).
  const groups = new Map<string, NormalizedFill[]>();
  for (const f of fills) {
    const key = `${f.account}|${f.contract}`;
    let g = groups.get(key);
    if (!g) groups.set(key, (g = []));
    g.push(f);
  }

  for (const group of groups.values()) {
    const sorted = [...group].sort(
      (a, b) => a.filledAt.localeCompare(b.filledAt) || a.orderId.localeCompare(b.orderId) || a.execId.localeCompare(b.execId)
    );
    const { account, contract, product } = sorted[0];
    const pv = pointValue(product);
    if (pv === null) {
      warnings.push(`Unknown product "${product}" (${contract}): using $1/point — add it to contracts.ts for real P&L.`);
    }
    const dollarsPerPoint = pv ?? 1;

    let s: OpenState | null = null;

    const open = (f: NormalizedFill, qty: number, fees: number) => {
      s = {
        direction: f.side === "buy" ? "long" : "short",
        position: qty,
        avgEntry: f.price,
        entryAt: f.filledAt,
        peak: qty,
        qtyTraded: qty,
        entryNotional: qty * f.price,
        exitQty: 0,
        exitNotional: 0,
        grossPnl: 0,
        fees,
        fillExecIds: [f.execId],
        lastFillAt: f.filledAt,
      };
    };

    for (const f of sorted) {
      if (s === null) {
        open(f, f.qty, f.fees);
        continue;
      }
      const st: OpenState = s;
      st.lastFillAt = f.filledAt;
      const fillDir = f.side === "buy" ? "long" : "short";

      if (fillDir === st.direction) {
        // Scale-in.
        st.entryNotional += f.qty * f.price;
        st.avgEntry = (st.avgEntry * st.position + f.price * f.qty) / (st.position + f.qty);
        st.position += f.qty;
        st.qtyTraded += f.qty;
        st.peak = Math.max(st.peak, st.position);
        st.fees += f.fees;
        st.fillExecIds.push(f.execId);
        continue;
      }

      // Opposing fill: close up to the open position; any excess flips.
      const closeQty = Math.min(f.qty, st.position);
      const flipQty = f.qty - closeQty;
      const closeFees = f.qty > 0 ? (f.fees * closeQty) / f.qty : 0;
      const sign = st.direction === "long" ? 1 : -1;

      st.grossPnl += (f.price - st.avgEntry) * sign * closeQty * dollarsPerPoint;
      st.exitQty += closeQty;
      st.exitNotional += closeQty * f.price;
      st.position -= closeQty;
      st.fees += closeFees;
      st.fillExecIds.push(f.execId);

      if (st.position === 0) {
        trades.push(emit(st, account, contract, product, warnings));
        s = null;
        if (flipQty > 0) open(f, flipQty, f.fees - closeFees);
      }
    }

    if (s !== null) {
      const st: OpenState = s;
      openPositions.push({
        account,
        contract,
        direction: st.direction,
        size: st.position,
        avgEntry: st.avgEntry,
        entryAt: st.entryAt,
      });
    }
  }

  trades.sort((a, b) => a.entryAt.localeCompare(b.entryAt));
  return { trades, openPositions, warnings };
}
