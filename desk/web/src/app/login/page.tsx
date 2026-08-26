import { supabaseServer } from "@/lib/supabase/server";
import { redirect } from "next/navigation";

async function sendLink(formData: FormData) {
  "use server";
  const email = String(formData.get("email") ?? "").trim().toLowerCase();
  // Allowlist enforced before any email is sent, not just at session check.
  if (email !== process.env.ALLOWED_EMAIL?.toLowerCase()) {
    redirect("/login?sent=1"); // indistinguishable from success on purpose
  }
  const supabase = await supabaseServer();
  const { error } = await supabase.auth.signInWithOtp({
    email,
    options: { shouldCreateUser: true },
  });
  redirect(error ? "/login?error=1" : "/login?sent=1");
}

export default async function LoginPage({ searchParams }: { searchParams: Promise<{ sent?: string; error?: string }> }) {
  const sp = await searchParams;
  return (
    <div className="card" style={{ marginTop: 48 }}>
      <h1>Trading Desk</h1>
      {sp.sent ? (
        <p>If that address is allowed, a sign-in link is on its way. Open it on this device.</p>
      ) : (
        <form action={sendLink}>
          <label htmlFor="email">Email</label>
          <input id="email" name="email" type="email" required autoComplete="email" />
          {sp.error && <p className="neg">Could not send the link. Try again.</p>}
          <button type="submit">Send sign-in link</button>
        </form>
      )}
    </div>
  );
}
