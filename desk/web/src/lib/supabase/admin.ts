// Service-role client — bypasses RLS. Server-side only; the key never reaches
// the browser. All table reads/writes go through this after the middleware has
// authenticated the (single) user.
import "server-only";
import { createClient } from "@supabase/supabase-js";

export function supabaseAdmin() {
  return createClient(process.env.NEXT_PUBLIC_SUPABASE_URL!, process.env.SUPABASE_SERVICE_ROLE_KEY!, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
}
