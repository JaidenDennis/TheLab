import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";
import { parseTradovateCsv } from "../parse";
import { reconstructTrades } from "../reconstruct";
import { dedupeFills } from "../dedupe";

const fixture = (name: string) => readFileSync(path.join(__dirname, "fixtures", name), "utf8");

function reconstructFixture(name: string) {
  const parsed = parseTradovateCsv(fixture(name));
  expect(parsed.errors).toEqual([]);
  return reconstructTrades(parsed.fills);
}

describe("simple round trip", () => {
  it("reconstructs one long trade with correct P&L and session date", () => {
    const { trades, openPositions } = reconstructFixture("simple.csv");
    expect(openPositions).toEqual([]);
    expect(trades).toHaveLength(1);
    const t = trades[0];
    expect(t.direction).toBe("long");
    expect(t.size).toBe(4);
    expect(t.qtyTraded).toBe(4);
    expect(t.avgEntry).toBe(20100);
    expect(t.avgExit).toBe(20110);
    // 10 pts x 4 MNQ x $2/pt
    expect(t.grossPnl).toBeCloseTo(80, 6);
    expect(t.fees).toBeCloseTo(2.96, 6);
    expect(t.netPnl).toBeCloseTo(77.04, 6);
    expect(t.sessionDate).toBe("2026-08-24");
    // 09:31:05 ET in August is 13:31:05 UTC (EDT)
    expect(t.entryAt).toBe("2026-08-24T13:31:05.000Z");
  });
});

describe("scale-in", () => {
  it("multiple entry fills, one position, weighted average entry", () => {
    const { trades } = reconstructFixture("scale_in.csv");
    expect(trades).toHaveLength(1);
    const t = trades[0];
    expect(t.avgEntry).toBe(20105);
    expect(t.avgExit).toBe(20115);
    expect(t.size).toBe(4);
    expect(t.qtyTraded).toBe(4);
    // (20115 - 20105) x 4 x $2
    expect(t.grossPnl).toBeCloseTo(80, 6);
  });
});

describe("partial exit", () => {
  it("weighted average exit, realized against average entry", () => {
    const { trades } = reconstructFixture("partial_exit.csv");
    expect(trades).toHaveLength(1);
    const t = trades[0];
    expect(t.avgEntry).toBe(20100);
    expect(t.avgExit).toBe(20105);
    // +20 pts x 2 and -10 pts x 2, at $2/pt
    expect(t.grossPnl).toBeCloseTo(40, 6);
    expect(t.exitAt).toBe("2026-08-24T15:25:00.000Z");
  });
});

describe("direction flip", () => {
  it("splits the crossing fill into a close and a new opposite trade", () => {
    const { trades, openPositions } = reconstructFixture("flip.csv");
    expect(openPositions).toEqual([]);
    expect(trades).toHaveLength(2);

    const [long, short] = trades;
    expect(long.direction).toBe("long");
    expect(long.size).toBe(2);
    expect(long.avgEntry).toBe(20100);
    expect(long.avgExit).toBe(20110);
    expect(long.grossPnl).toBeCloseTo(40, 6); // +10 pts x 2 x $2
    // 2/5 of the crossing fill's $1.85 fee
    expect(long.fees).toBeCloseTo(0.74 + 0.74, 6);

    expect(short.direction).toBe("short");
    expect(short.size).toBe(3);
    expect(short.avgEntry).toBe(20110);
    expect(short.avgExit).toBe(20095);
    expect(short.grossPnl).toBeCloseTo(90, 6); // +15 pts x 3 x $2
    // 3/5 of $1.85 plus its own exit fill fee
    expect(short.fees).toBeCloseTo(1.11 + 1.11, 6);
    // Both trades share the crossing fill
    expect(long.fillExecIds).toContain("E2");
    expect(short.fillExecIds).toContain("E2");
  });
});

describe("multiple accounts", () => {
  it("same contract in different accounts is never merged", () => {
    const { trades } = reconstructFixture("multi_account.csv");
    expect(trades).toHaveLength(2);
    const demo = trades.find((t) => t.account === "DEMO123")!;
    const live = trades.find((t) => t.account === "LIVE456")!;
    expect(demo.grossPnl).toBeCloseTo(20, 6); // +5 pts x 2 x $2
    expect(live.grossPnl).toBeCloseTo(-12, 6); // -6 pts x 1 x $2
  });
});

describe("duplicate import", () => {
  it("importing the same CSV twice produces no change", () => {
    const parsed = parseTradovateCsv(fixture("simple.csv"));
    const once = reconstructTrades(dedupeFills(parsed.fills));
    const twice = reconstructTrades(dedupeFills([...parsed.fills, ...parsed.fills]));
    expect(twice).toEqual(once);
  });
});

describe("open position at end of stream", () => {
  it("emits no trade, reports the open position", () => {
    const csv = `Account,Order ID,Exec ID,B/S,Contract,Product,Avg Price,Filled Qty,Fill Time,Commission
DEMO123,6001,E1,Buy,MNQU6,MNQ,20100.00,2,08/24/2026 15:00:00,0.74
DEMO123,6002,E2,Sell,MNQU6,MNQ,20110.00,1,08/24/2026 15:10:00,0.37
`;
    const parsed = parseTradovateCsv(csv);
    expect(parsed.errors).toEqual([]);
    const { trades, openPositions } = reconstructTrades(parsed.fills);
    expect(trades).toEqual([]);
    expect(openPositions).toHaveLength(1);
    expect(openPositions[0]).toMatchObject({ direction: "long", size: 1, avgEntry: 20100 });
  });
});

describe("unknown product", () => {
  it("warns and falls back to $1/point instead of guessing silently", () => {
    const csv = `Account,Order ID,Exec ID,B/S,Contract,Product,Avg Price,Filled Qty,Fill Time
DEMO123,7001,E1,Buy,ZZQU6,ZZQ,100.00,1,08/24/2026 15:00:00
DEMO123,7002,E2,Sell,ZZQU6,ZZQ,110.00,1,08/24/2026 15:10:00
`;
    const parsed = parseTradovateCsv(csv);
    const { trades, warnings } = reconstructTrades(parsed.fills);
    expect(trades[0].grossPnl).toBeCloseTo(10, 6);
    expect(warnings.join(" ")).toMatch(/Unknown product "ZZQ"/);
  });
});
