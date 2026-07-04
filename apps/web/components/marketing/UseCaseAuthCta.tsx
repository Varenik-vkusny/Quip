"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { buttonVariants } from "@/components/ui/Button";
import { createSupabaseBrowserClient } from "@/lib/supabase/client";
import { isSupabaseConfigured } from "@/lib/supabase/config";

/**
 * Client-resolved auth state for the programmatic SEO use-case pages. These pages MUST
 * be statically generated (they are the EN-Google / RU-Yandex SEO surface), so the
 * shared renderer can no longer call getOptionalUser() (a cookie read = Dynamic API that
 * deopted the whole route). The auth-dependent bits (primary CTA label/href + the
 * "no card required" line) resolve on the client instead; the entire content body
 * (hero copy, steps, benefits, FAQ, JSON-LD) stays static and crawlable.
 *
 * Default = logged-out — correct for crawlers and anonymous visitors (the SEO audience).
 */
function useAuthed(): boolean {
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
  return authed;
}

/** Primary CTA button. Logged-out → /signup ("Try it free"); logged-in → /dashboard
 *  ("Open app"). Labels are passed in already localized by the server renderer. */
export function UseCaseCta({ dashLabel, tryLabel }: { dashLabel: string; tryLabel: string }) {
  const authed = useAuthed();
  return (
    <Link
      href={authed ? "/dashboard" : "/signup"}
      className={buttonVariants({ variant: "primary", size: "lg" })}
    >
      {authed ? dashLabel : tryLabel}
      <span aria-hidden>→</span>
    </Link>
  );
}

/** "No card required. N free videos every month." — shown only to logged-out visitors. */
export function UseCaseNoCard({ emphasis, rest }: { emphasis: string; rest: string }) {
  const authed = useAuthed();
  if (authed) return null;
  return (
    <p className="mt-6 text-sm text-faint">
      <span className="font-medium text-muted">{emphasis}</span> {rest}
    </p>
  );
}
