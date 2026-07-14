"use client";

import { useCallback, useEffect, useState } from "react";
import { getUsage, type UsageInfo } from "./api";
import { isSupabaseConfigured } from "./supabase/config";
import { retryUsageFetch } from "./usageRetry";

/** Free defaults — used ONLY in dual-mode dev (Supabase not configured), where the app
 *  runs open and there's no real plan to fetch. In production a fetch failure is surfaced
 *  as an error (status="error"), NOT silently shown as Free (что и пряталось раньше). */
export const FREE_DEFAULT: UsageInfo = {
  plan: "free",
  plan_name: "Free",
  monthly_videos: 1,
  monthly_minutes: 60,
  used_minutes: 0,
  remaining_minutes: 60,
  remaining_videos: 1,
  payg_videos: 0,
  payg_minutes: 0,
};

export type UsageStatus = "loading" | "ok" | "error";

export type UsageState = {
  status: UsageStatus;
  usage: UsageInfo | null;
  /** A first attempt failed transiently and we're backing off. Still `status:"loading"` — the plan
   *  is UNKNOWN, so the UI must keep showing a loading state (never a guessed plan); this only lets
   *  it explain the wait ("taking longer than usual"). */
  retrying: boolean;
  reload: () => void;
};

/**
 * Fetches the live plan/limits with explicit loading/ok/error states (no silent Free fallback).
 * Shared by the dashboard meter, the header pill and the Video-Map gate so the rule "no silent
 * fallbacks" holds in ONE place. `reload()` re-runs the fetch (retry button).
 *
 * COLD-START RESILIENCE (prod, 2026-07-14): `quip-api` is scale-to-zero and now runs
 * containerConcurrency=8, so containers spin up often. The first request after idle can fail at the
 * infrastructure level (no container yet → the response carries no CORS headers → the browser calls
 * it a CORS error and `fetch` rejects). That single dead request used to pin the user on the error
 * card — and, before c60ccc8, on the FREE plan. So a transient failure is now RETRIED on the same
 * backoff ladder useJob reconnects with (2.5s → 5s → 10s), and we re-fetch the moment the browser
 * fires "online". Only after the whole ladder fails do we show the honest error.
 *
 * What we never do: invent a plan. While the answer is unknown the state is `loading`/`error` with
 * `usage === null` — a paying customer is never told they're on Free.
 */
export function useUsage(): UsageState {
  // Dual-mode dev: no Supabase → worker is open, no plan to load → start in Free/ok (valid
  // state, not an error). Configured (prod) → start loading and fetch; failure → error.
  // `isSupabaseConfigured` is a stable module constant, so it's safe as initial state.
  const [status, setStatus] = useState<UsageStatus>(isSupabaseConfigured ? "loading" : "ok");
  const [usage, setUsage] = useState<UsageInfo | null>(isSupabaseConfigured ? null : FREE_DEFAULT);
  const [retrying, setRetrying] = useState(false);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (!isSupabaseConfigured) return; // dev: state already Free/ok, nothing to fetch
    let cancelled = false;
    retryUsageFetch({
      fetchOnce: getUsage,
      sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
      isOnline: () => (typeof navigator === "undefined" ? true : navigator.onLine),
      isCancelled: () => cancelled,
      onRetry: () => {
        if (!cancelled) setRetrying(true); // "still loading" — NOT a plan, the answer is still unknown
      },
    })
      .then((u) => {
        if (cancelled) return;
        setUsage(u);
        setStatus("ok");
        setRetrying(false);
      })
      .catch(() => {
        if (cancelled) return;
        // Every attempt failed (or the server gave a fatal answer) → say so. NOT Free.
        setStatus("error");
        setRetrying(false);
      });
    return () => {
      cancelled = true;
    };
  }, [nonce]);

  // Retry: flip to loading (in this handler, not the effect) then re-run the fetch.
  const reload = useCallback(() => {
    setStatus("loading");
    setRetrying(false);
    setNonce((n) => n + 1);
  }, []);

  // Connectivity came back → re-fetch immediately instead of leaving the user on a stale error (or
  // sitting out a backoff we now know is pointless). Mirrors useJob's "online" listener. We only
  // subscribe while the plan is still UNKNOWN — once it's loaded there's nothing for "online" to fix,
  // so this unsubscribes itself on success (and `status` has 3 values, so re-subscribing is cheap).
  useEffect(() => {
    if (!isSupabaseConfigured || status === "ok") return;
    const onOnline = () => reload();
    window.addEventListener("online", onOnline);
    return () => window.removeEventListener("online", onOnline);
  }, [status, reload]);

  return { status, usage, retrying, reload };
}

export interface UsageView {
  planId: string;
  planName: string;
  /** Total videos available right now = monthly remaining + never-expiring PAYG. */
  videosLeft: number;
  /** Total minutes available right now (rounded). */
  minutesLeft: number;
  /** Monthly plan pool, this month (the bar): used / total, in whole "videos". */
  monthlyUsedVideos: number;
  monthlyTotalVideos: number;
  /** Monthly pool used, 0–100 (the orange bar's width). */
  usedPct: number;
  /** Monthly pool ≥80% used → bar goes amber. */
  near: boolean;
  /** Nothing left to create with (monthly + PAYG exhausted) → highlight red. */
  out: boolean;
  paygVideos: number;
  paygMinutes: number;
}

/** Pure: UsageInfo → display view. The bar is the MONTHLY pool only (resets monthly);
 *  the hero number is the TOTAL available (monthly remaining + PAYG). Keeping them
 *  distinct is what makes "how much is left" unambiguous. */
export function deriveUsage(u: UsageInfo): UsageView {
  const videosLeft = u.remaining_videos + u.payg_videos;
  const usedPct =
    u.monthly_minutes > 0
      ? Math.min(100, Math.max(0, (u.used_minutes / u.monthly_minutes) * 100))
      : 0;
  return {
    planId: u.plan,
    planName: u.plan === "free" ? "Free" : u.plan_name,
    videosLeft,
    minutesLeft: Math.round(u.remaining_minutes + u.payg_minutes),
    monthlyUsedVideos: u.used_minutes / 60,
    monthlyTotalVideos: u.monthly_videos,
    usedPct,
    near: usedPct >= 80,
    out: videosLeft < 1,
    paygVideos: u.payg_videos,
    paygMinutes: u.payg_minutes,
  };
}

/** "1,026" for big counts (instant read), "1.8" for small fractional, "2" for whole. */
export function fmtVideos(v: number): string {
  if (v >= 100) return Math.round(v).toLocaleString("en-US");
  if (Number.isInteger(v)) return String(v);
  return v.toFixed(1);
}

/** Whole minutes with thousands separators ("61,605"). */
export function fmtMinutes(m: number): string {
  return Math.round(m).toLocaleString("en-US");
}
