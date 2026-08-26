// Proxies the pings SSE feed from desk-brain.
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

export async function GET() {
  const brainUrl = process.env.BRAIN_URL;
  if (!brainUrl) {
    return NextResponse.json({ error: "brain not configured" }, { status: 503 });
  }
  let upstream: Response;
  try {
    upstream = await fetch(`${brainUrl.replace(/\/$/, "")}/pings`, {
      headers: { "x-brain-secret": process.env.BRAIN_SHARED_SECRET ?? "" },
    });
  } catch {
    return NextResponse.json({ error: "buddy offline" }, { status: 502 });
  }
  return new Response(upstream.body, {
    status: upstream.status,
    headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive" },
  });
}
