import { redirect } from "next/navigation";
import { NextIntlClientProvider } from "next-intl";
import { getLocale, getMessages } from "next-intl/server";
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
  // The root layout no longer mounts a provider (that would force every route dynamic),
  // so the app shell's many client components (dashboard, AppHeader, ClipGrid, UsageMeter,
  // ExportMenu, …) get their provider here. This route group is auth-gated and already
  // dynamic (reads the session above), so we ship the full catalog rather than enumerate
  // ~11 namespaces and risk a runtime miss.
  const locale = await getLocale();
  const messages = await getMessages();
  return (
    <NextIntlClientProvider locale={locale} messages={messages}>
      {/* Client sentinel: purges cross-account localStorage caches on sign-out / account switch. */}
      <AuthStateSync />
      {children}
    </NextIntlClientProvider>
  );
}
