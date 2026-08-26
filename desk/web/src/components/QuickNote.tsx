"use client";

// Quick capture (spec §12): timestamp at the moment of typing, write to a
// local send queue, fire immediately, retry on reconnect. localStorage is a
// queue, never a store — entries are removed the moment the server accepts.

import { useCallback, useEffect, useRef, useState } from "react";

const QUEUE_KEY = "desk.noteQueue.v1";

interface QueuedNote {
  body: string;
  captured_at: string;
}

function readQueue(): QueuedNote[] {
  try {
    return JSON.parse(localStorage.getItem(QUEUE_KEY) ?? "[]");
  } catch {
    return [];
  }
}

function writeQueue(q: QueuedNote[]) {
  try {
    localStorage.setItem(QUEUE_KEY, JSON.stringify(q));
  } catch {
    // storage unavailable: the in-flight fetch is the only path
  }
}

export function QuickNote() {
  const [text, setText] = useState("");
  const [pending, setPending] = useState(0);
  const [savedFlash, setSavedFlash] = useState(false);
  const flushing = useRef(false);

  const flush = useCallback(async () => {
    if (flushing.current) return;
    flushing.current = true;
    try {
      let queue = readQueue();
      while (queue.length > 0) {
        const head = queue[0];
        const res = await fetch("/api/notes", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(head),
        });
        if (!res.ok && res.status !== 400) throw new Error(String(res.status));
        queue = queue.slice(1); // 400 = rejected as malformed; drop rather than loop
        writeQueue(queue);
        setPending(queue.length);
      }
    } catch {
      // offline or server down — queue stays, retried on reconnect/next note
    } finally {
      flushing.current = false;
      setPending(readQueue().length);
    }
  }, []);

  useEffect(() => {
    setPending(readQueue().length);
    void flush();
    const onOnline = () => void flush();
    window.addEventListener("online", onOnline);
    return () => window.removeEventListener("online", onOnline);
  }, [flush]);

  const save = () => {
    const body = text.trim();
    if (!body) return;
    const queue = readQueue();
    queue.push({ body, captured_at: new Date().toISOString() });
    writeQueue(queue);
    setPending(queue.length);
    setText("");
    setSavedFlash(true);
    setTimeout(() => setSavedFlash(false), 1200);
    void flush();
  };

  return (
    <div className="card">
      <label htmlFor="quicknote">Quick note</label>
      <textarea
        id="quicknote"
        autoFocus
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) save();
        }}
        placeholder="What are you seeing / feeling right now?"
      />
      <button onClick={save} disabled={!text.trim()}>
        Save note
      </button>{" "}
      {savedFlash && <span className="pos">saved</span>}
      {pending > 0 && <span className="warn"> {pending} queued offline</span>}
    </div>
  );
}
