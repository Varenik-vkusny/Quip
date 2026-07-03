"use client";

import { useEffect } from "react";
import { LAST_USER_ID_KEY, shouldPurge } from "@/lib/authReset";
import { purgeClientCaches } from "@/lib/recent";
import { createSupabaseBrowserClient } from "@/lib/supabase/client";
import { isSupabaseConfigured } from "@/lib/supabase/config";

/**
 * Invisible client sentinel that reconciles per-device localStorage caches with the ACTIVE Supabase
 * identity. Defense-in-depth for the IDOR / cross-account-leak fix (the worker's ownership gate is
 * the real boundary): it stops account B, on a shared device, from seeing account A's client
 * leftovers when a session change bypasses the Sign out button — e.g. an OAuth redirect landing or
 * a multi-tab account switch.
 *
 * On SIGNED_OUT, or on any session whose user id differs from the stored `quip:lastUserId`, it
 * purges the cross-account caches (legacy global recents, editor inspector width, every user's
 * reviewed set) and then records the new id. Mounted once in the gated app shell.
 */
export function AuthStateSync() {
  useEffect(() => {
    if (!isSupabaseConfigured) return;
    const supabase = createSupabaseBrowserClient();

    const reconcile = (currentUserId: string | null) => {
      let lastUserId: string | null = null;
      try {
        lastUserId = window.localStorage.getItem(LAST_USER_ID_KEY);
      } catch {
        /* storage unavailable → treat as no prior id (purge path is safe) */
      }
      if (shouldPurge(lastUserId, currentUserId)) {
        purgeClientCaches(); // also removes LAST_USER_ID_KEY; re-set below for the new user
      }
      try {
        if (currentUserId) window.localStorage.setItem(LAST_USER_ID_KEY, currentUserId);
        else window.localStorage.removeItem(LAST_USER_ID_KEY);
      } catch {
        /* best-effort marker; nothing actionable on failure */
      }
    };

    // Reconcile against the current session immediately (covers an OAuth redirect that lands here
    // without ever firing SIGNED_OUT).
    void supabase.auth
      .getSession()
      .then(({ data }) => reconcile(data.session?.user?.id ?? null))
      .catch(() => {
        /* no session readable → leave caches; a later auth event will reconcile */
      });

    const { data: sub } = supabase.auth.onAuthStateChange((event, session) => {
      if (event === "SIGNED_OUT") {
        reconcile(null);
        return;
      }
      reconcile(session?.user?.id ?? null);
    });

    return () => sub.subscription.unsubscribe();
  }, []);

  return null;
}
