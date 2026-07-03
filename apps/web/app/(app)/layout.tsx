import { redirect } from "next/navigation";
import { AuthStateSync } from "@/components/auth/AuthStateSync";
import { isSupabaseConfigured } from "@/lib/supabase/config";
import { createSupabaseServerClient } from "@/lib/supabase/server";

/** Authoritative auth gate for the app (dashboard + editor). Validates the JWT
 *  with getUser() (never getSession). Open in dev until Supabase is connected. */
export default async function AppLayout({ children }: { children: React.ReactNode }) {
  if (isSupabaseConfigured) {
    const supabase = await createSupabaseServerClient();
    const {
      data: { user },
    } = await supabase.auth.getUser();
    if (!user) redirect("/login");
  }
  return (
    <>
      {/* Client sentinel: purges cross-account localStorage caches on sign-out / account switch. */}
      <AuthStateSync />
      {children}
    </>
  );
}
