// Shared types for the Ingest module. Only this module knows broker formats (spec §7).

export interface NormalizedFill {
  account: string;
  orderId: string;
  execId: string;
  contract: string; // e.g. MNQU6
  product: string; // e.g. MNQ
  side: "buy" | "sell";
  qty: number;
  price: number;
  fees: number;
  filledAt: string; // ISO 8601 UTC
  raw: Record<string, string>;
}

export interface ParseResult {
  fills: NormalizedFill[];
  warnings: string[];
  /** Fatal problems — nothing should be imported when non-empty. */
  errors: string[];
}

export interface ReconstructedTrade {
  account: string;
  contract: string;
  product: string;
  direction: "long" | "short";
  entryAt: string;
  exitAt: string;
  avgEntry: number;
  avgExit: number;
  /** Peak absolute position during the trade. */
  size: number;
  /** Total contracts entered (scale-ins summed). */
  qtyTraded: number;
  grossPnl: number; // dollars
  fees: number;
  netPnl: number;
  /** exec ids of every fill that participated, in time order. */
  fillExecIds: string[];
  /** ET calendar date of the entry fill, YYYY-MM-DD. */
  sessionDate: string;
}

export interface OpenPosition {
  account: string;
  contract: string;
  direction: "long" | "short";
  size: number;
  avgEntry: number;
  entryAt: string;
}

export interface ImportSummary {
  ok: boolean;
  errors: string[];
  warnings: string[];
  fillsInCsv: number;
  fillsNew: number;
  tradesRebuilt: number;
  tradesDeleted: number;
  notesJoined: number;
  openPositions: OpenPosition[];
}

export interface ReconstructionResult {
  trades: ReconstructedTrade[];
  /** Positions still open at the end of the fill stream — not journaled as trades. */
  openPositions: OpenPosition[];
  warnings: string[];
}
