"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { MobileMenu } from "@/components/marketing/MobileMenu";
import { buttonVariants } from "@/components/ui/Button";
import { createSupabaseBrowserClient } from "@/lib/supabase/client";
import { isSupabaseConfigured } from "@/lib/supabase/config";

/**
 * Auth-aware nav CTAs, resolved on the CLIENT so the (marketing) layout stays
 * statically generated. MarketingNav used to be an async RSC calling
 * getOptionalUser() (a cookie read) — a Dynamic API in the shared layout that
 * opted every marketing/SEO page (/pricing, /terms, /privacy, /use-case) out of
 * static generation. Moving the auth read here keeps the nav shell (Logo, links)
 * server-static; only this island hydrates.
 *
 * Default = logged-out ("Sign in" / "Try it free") — correct for crawlers and the
 * overwhelmingly-anonymous marketing traffic (no hydration mismatch). A logged-in
 * visitor briefly sees the logged-out CTA before it swaps to "Dashboard" after
 * hydration — an accepted tradeoff for edge-cached static SEO pages.
 */
export function MarketingNavAuth({ links }: { links: { href: string; label: string }[] }) {
  const [authed, setAuthed] = useState(false);

  useEffect(() => {
    if (!isSupabaseConfigured) return;
    let active = true;
    createSupabaseBrowserClient()
      .auth.getUser()
      .then(({ data }) => {
        if (active) setAuthed(Boolean(data.user));
      })
      .catch(() => {
        // No session / transient error → keep the logged-out CTA (honest default).
      });
    return () => {
      active = false;
    };
  }, []);

  return (
    <div className="flex items-center gap-2">
      {authed ? (
        <Link href="/dashboard" className={buttonVariants({ variant: "primary", size: "sm" })}>
          Dashboard
        </Link>
      ) : (
        <>
          <Link
            href="/login"
            className="hidden rounded-md px-3 py-2 text-sm text-muted transition-colors hover:text-ink sm:inline-flex"
          >
            Sign in
          </Link>
          <Link href="/signup" className={buttonVariants({ variant: "primary", size: "sm" })}>
            Try it free
          </Link>
        </>
      )}

      <MobileMenu links={links} authed={authed} />
    </div>
  );
}
