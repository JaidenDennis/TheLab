// Minimal service worker: makes the app installable and exempts storage from
// Safari eviction (spec §19). Network-first — the capture queue must never be
// served a stale shell that drops notes. Web push lands here later.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {
  // pass-through; presence of a fetch handler is what makes iOS treat this as installable
});
