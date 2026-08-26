// Magic-link landing: exchanges the token_hash for a session cookie.
// Configure the Supabase email template to link to
//   {{ .SiteURL }}/auth/confirm?token_hash={{ .TokenHash }}&type=email
import { supabaseServer } from "@/lib/supabase/server";
import { NextResponse, type NextRequest } from "next/server";

export async function GET(request: NextRequest) {
  const url = request.nextUrl;
  const token_hash = url.searchParams.get("token_hash");
  const type = url.searchParams.get("type");

  if (token_hash && type === "email") {
    const supabase = await supabaseServer();
    const { error } = await supabase.auth.verifyOtp({ type: "email", token_hash });
    if (!error) {
      return NextResponse.redirect(new URL("/today", url.origin));
    }
  }
  return NextResponse.redirect(new URL("/login?error=1", url.origin));
}
