"use client";

// The buddy lives here, mounted once in the root layout so the conversation
// survives tab switches. Desktop (≥1120px): a permanent right rail. Smaller
// screens: a floating button opening a full-height sheet. The ChatPanel
// instance is shared between the two presentations — CSS decides which shows.

import { useState } from "react";
import { usePathname } from "next/navigation";
import { ChatPanel } from "./ChatPanel";

export function ChatDock() {
  const path = usePathname();
  const [open, setOpen] = useState(false);
  if (path === "/login" || path.startsWith("/auth")) return null;

  return (
    <>
      <aside className={open ? "chatdock-sheet" : "chatdock-rail"} aria-label="Buddy chat">
        <div className="chat-rail-head">
          <span className="eyebrow">Buddy</span>
          {open && (
            <button className="ghost" onClick={() => setOpen(false)} aria-label="Close chat">
              ✕
            </button>
          )}
        </div>
        <ChatPanel />
      </aside>
      {!open && (
        <button className="chatdock-fab" onClick={() => setOpen(true)} aria-label="Open buddy chat">
          ✦
        </button>
      )}
    </>
  );
}
