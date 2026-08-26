import { describe, it, expect } from "vitest";
import { parseTradovateCsv } from "../parse";
import { parseFillTime, etDateOf } from "../time";
import { productRoot, pointValue } from "../contracts";

describe("header aliasing", () => {
  it("accepts renamed columns case/space-insensitively", () => {
    const csv = `account,order id,ID,Side,Symbol,Fill Price,Quantity,Timestamp
DEMO,1,X1,BUY,NQZ5,20100,1,2026-08-24T13:31:05Z
`;
    const { fills, errors } = parseTradovateCsv(csv);
    expect(errors).toEqual([]);
    expect(fills).toHaveLength(1);
    expect(fills[0]).toMatchObject({ contract: "NQZ5", product: "NQ", side: "buy", qty: 1, price: 20100 });
  });

  it("fails the whole file naming the missing column", () => {
    const csv = `Account,Order ID,B/S,Contract,Avg Price,Fill Time
DEMO,1,Buy,MNQU6,20100,08/24/2026 09:31:05
`;
    const { fills, errors } = parseTradovateCsv(csv);
    expect(fills).toEqual([]);
    expect(errors[0]).toMatch(/missing column\(s\): qty/);
  });

  it("rejects bad rows with row numbers, keeps good ones out of the result", () => {
    const csv = `Account,Order ID,Exec ID,B/S,Contract,Product,Avg Price,Filled Qty,Fill Time
DEMO,1,X1,Buy,MNQU6,MNQ,20100,1,08/24/2026 09:31:05
DEMO,2,X2,Hold,MNQU6,MNQ,20100,0,notatime
`;
    const { fills, errors } = parseTradovateCsv(csv);
    expect(fills).toHaveLength(1);
    expect(errors).toHaveLength(1);
    expect(errors[0]).toMatch(/Row 3: bad or missing side, qty, fill time/);
  });

  it("synthesizes stable exec ids when the export has none", () => {
    const csv = `Account,Order ID,B/S,Contract,Product,Avg Price,Filled Qty,Fill Time
DEMO,1,Buy,MNQU6,MNQ,20100,1,08/24/2026 09:31:05
DEMO,1,Buy,MNQU6,MNQ,20100,1,08/24/2026 09:31:05
`;
    const a = parseTradovateCsv(csv);
    const b = parseTradovateCsv(csv);
    expect(a.fills).toHaveLength(2);
    // identical rows get distinct ids, and re-parsing reproduces them exactly
    expect(a.fills[0].execId).not.toBe(a.fills[1].execId);
    expect(b.fills.map((f) => f.execId)).toEqual(a.fills.map((f) => f.execId));
  });
});

describe("timestamps", () => {
  it("parses Tradovate US-style ET timestamps to UTC (EDT)", () => {
    expect(parseFillTime("08/24/2026 09:31:05")).toBe("2026-08-24T13:31:05.000Z");
  });
  it("parses winter timestamps as EST", () => {
    expect(parseFillTime("01/15/2026 09:31:05")).toBe("2026-01-15T14:31:05.000Z");
  });
  it("parses 12h clock", () => {
    expect(parseFillTime("08/24/2026 1:31:05 PM")).toBe("2026-08-24T17:31:05.000Z");
  });
  it("passes ISO with offset through", () => {
    expect(parseFillTime("2026-08-24T13:31:05.000Z")).toBe("2026-08-24T13:31:05.000Z");
  });
  it("session date is the ET calendar date", () => {
    expect(etDateOf("2026-08-25T01:30:00.000Z")).toBe("2026-08-24"); // 21:30 ET prior evening
    expect(etDateOf("2026-08-24T13:31:05.000Z")).toBe("2026-08-24");
  });
});

describe("contracts", () => {
  it("extracts product roots", () => {
    expect(productRoot("MNQU6")).toBe("MNQ");
    expect(productRoot("NQZ25")).toBe("NQ");
    expect(productRoot("MESH6")).toBe("MES");
  });
  it("knows point values", () => {
    expect(pointValue("NQ")).toBe(20);
    expect(pointValue("MNQ")).toBe(2);
    expect(pointValue("ZZQ")).toBeNull();
  });
});
