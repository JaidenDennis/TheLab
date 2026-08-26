// Proxies the chat SSE stream from desk-brain, attaching the shared secret.
// The browser never learns the secret or the brain's address.
import { NextResponse, type NextRequest } from "next/server";

export const dynamic = "force-dynamic";

export async function POST(request: NextRequest) {
  const brainUrl = process.env.BRAIN_URL;
  if (!brainUrl) {
    return NextResponse.json({ error: "brain not configured" }, { status: 503 });
  }
  const body = await request.text();
  let upstream: Response;
  try {
    upstream = await fetch(`${brainUrl.replace(/\/$/, "")}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-brain-secret": process.env.BRAIN_SHARED_SECRET ?? "" },
      body,
      // @ts-expect-error duplex is required by node fetch for streaming bodies
      duplex: "half",
    });
  } catch {
    return NextResponse.json({ error: "buddy offline" }, { status: 502 });
  }
  return new Response(upstream.body, {
    status: upstream.status,
    headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive" },
  });
}
