// Auth gate (spec §4): every page, API route, and stream requires the session,
// and the session email must equal the single allow-listed address.
import { createServerClient, type CookieOptions } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

type CookieToSet = { name: string; value: string; options?: CookieOptions };

// /api/rebuild is session-exempt: it authenticates desk-brain by shared secret in-route.
const PUBLIC_PATHS = ["/login", "/auth/confirm", "/api/rebuild", "/manifest.webmanifest", "/icon.svg", "/apple-icon.png", "/sw.js"];

export async function middleware(request: NextRequest) {
  let response = NextResponse.next({ request });

  const supabase = createServerClient(process.env.NEXT_PUBLIC_SUPABASE_URL!, process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!, {
    cookies: {
      getAll: () => request.cookies.getAll(),
      setAll: (all: CookieToSet[]) => {
        all.forEach(({ name, value }) => request.cookies.set(name, value));
        response = NextResponse.next({ request });
        all.forEach(({ name, value, options }) => response.cookies.set(name, value, options));
      },
    },
  });

  const {
    data: { user },
  } = await supabase.auth.getUser();

  const path = request.nextUrl.pathname;
  const isPublic = PUBLIC_PATHS.some((p) => path === p || path.startsWith(p + "/"));

  const allowed = user?.email && user.email.toLowerCase() === process.env.ALLOWED_EMAIL?.toLowerCase();

  if (!allowed) {
    // A signed-in but non-allow-listed user is treated as unauthenticated.
    if (user) await supabase.auth.signOut();
    if (!isPublic) {
      if (path.startsWith("/api/")) {
        return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
      }
      const url = request.nextUrl.clone();
      url.pathname = "/login";
      url.search = "";
      return NextResponse.redirect(url);
    }
  }

  return response;
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
