"use client";

import Link from "next/link";
import { useRef, useState } from "react";
import type { ImportSummary } from "@/lib/ingest/types";

export function ImportDrop() {
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ImportSummary | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const send = async (file: File) => {
    setBusy(true);
    setResult(null);
    try {
      const res = await fetch("/api/import", { method: "POST", body: await file.text() });
      setResult(await res.json());
    } catch {
      setResult({ ok: false, errors: ["Upload failed — are you online?"] } as ImportSummary);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div
        className={"dropzone" + (over ? " over" : "")}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          const f = e.dataTransfer.files[0];
          if (f) void send(f);
        }}
        onClick={() => fileInput.current?.click()}
      >
        {busy ? "Importing…" : "Drop CSV here, or tap to choose"}
        <input
          ref={fileInput}
          type="file"
          accept=".csv,text/csv"
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) void send(f);
            e.target.value = "";
          }}
        />
      </div>

      {result && (
        <div className="card">
          {result.ok ? (
            <>
              <p className="pos">
                <b>Imported.</b> {result.fillsNew} new fill{result.fillsNew === 1 ? "" : "s"} of {result.fillsInCsv} in file ·{" "}
                {result.tradesRebuilt} trade{result.tradesRebuilt === 1 ? "" : "s"} rebuilt
                {result.notesJoined > 0 && <> · {result.notesJoined} note(s) joined</>}
              </p>
              {result.openPositions.length > 0 && (
                <p className="warn">
                  Open position not journaled:{" "}
                  {result.openPositions.map((p) => `${p.direction} ${p.size} ${p.contract} @ ${p.avgEntry}`).join("; ")}
                </p>
              )}
              <p>
                <Link href="/journal">View journal →</Link>
              </p>
            </>
          ) : (
            <>
              <p className="neg">
                <b>Import failed.</b> Nothing was changed.
              </p>
              <ul className="plain">
                {result.errors?.map((e, i) => (
                  <li key={i} className="neg">
                    {e}
                  </li>
                ))}
              </ul>
            </>
          )}
          {result.warnings?.length > 0 && (
            <ul className="plain">
              {result.warnings.map((w, i) => (
                <li key={i} className="warn">
                  {w}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </>
  );
}
