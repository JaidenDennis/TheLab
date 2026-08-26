import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

export async function GET() {
  const brainUrl = process.env.BRAIN_URL;
  if (!brainUrl) {
    return NextResponse.json({ offline: true, reason: "brain not configured" });
  }
  try {
    const res = await fetch(`${brainUrl.replace(/\/$/, "")}/status`, {
      headers: { "x-brain-secret": process.env.BRAIN_SHARED_SECRET ?? "" },
      cache: "no-store",
    });
    if (!res.ok) return NextResponse.json({ offline: true, reason: `brain ${res.status}` });
    return NextResponse.json({ offline: false, ...(await res.json()) });
  } catch {
    return NextResponse.json({ offline: true, reason: "unreachable" });
  }
}
