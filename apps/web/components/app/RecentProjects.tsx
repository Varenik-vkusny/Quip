"use client";

import { Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge } from "@/components/ui/Badge";
import { Eyebrow } from "@/components/ui/Eyebrow";
import { Numeral } from "@/components/ui/Numeral";
import { getMyJobs, type MyJob } from "@/lib/api";
import {
  getOptimisticRecents,
  isReviewed,
  pruneOptimistic,
  subscribeRecentsRefetch,
} from "@/lib/recent";
import { createSupabaseBrowserClient } from "@/lib/supabase/client";
import { isSupabaseConfigured } from "@/lib/supabase/config";

// Live processing stages that have a localized label under `recentProjects.stage.*`.
const STAGE_KEYS = ["queued", "downloading", "transcribing", "selecting", "rendering"] as const;
const TERMINAL = new Set(["done", "failed", "cancelled"]);
const POLL_MS = 2500; // cap cadence at ~2.5s like useJob
const HIDDEN_POLL_MS = 10_000; // backgrounded-tab cadence — same idea as useJob's HIDDEN_POLL_MS

/** A single ledger row: a server job or a transient optimistic entry (status undefined). */
type RecentRow = {
  id: string;
  label: string;
  at: number; // ms epoch (for relTime)
  status?: string;
  nclips?: number;
  reviewed: boolean;
};

function isTerminal(status?: string): boolean {
  return status !== undefined && TERMINAL.has(status);
}

/** GET /jobs `created_at` is `number | string` — normalize to ms. Epoch SECONDS (< 1e12) are
 *  scaled up; ms pass through; ISO strings are parsed; anything unparseable falls back to now. */
function toMillis(v: number | string): number {
  if (typeof v === "number") return v < 1e12 ? v * 1000 : v;
  const p = Date.parse(v);
  return Number.isNaN(p) ? Date.now() : p;
}

/** Compact ledger timestamp: "now" / "12m" / "3h" / "5d". Mono-rendered by the caller.
 *  Only the sub-minute "now" is a word, so it's passed in already localized. */
function relTime(at: number, nowLabel: string): string {
  const s = Math.max(0, Math.floor((Date.now() - at) / 1000));
  if (s < 60) return nowLabel;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
}

/** Right-edge status reading for a ledger row: live state or the "New" cue for unopened results. */
function StatusCell({
  status,
  nclips,
  reviewed,
}: {
  status?: string;
  nclips?: number;
  reviewed?: boolean;
}) {
  const t = useTranslations("recentProjects");
  if (status === "done") {
    const clips = nclips != null && nclips > 0 ? t("clipCount", { count: nclips }) : t("ready");
    if (!reviewed) {
      return (
        <Badge tone="accent" dot>
          {t("newWithClips", { clips })}
        </Badge>
      );
    }
    return (
      <Badge tone="ok" dot>
        {clips}
      </Badge>
    );
  }
  if (status === "failed") {
    return (
      <Badge tone="bad" dot>
        {t("failed")}
      </Badge>
    );
  }
  if (status === "cancelled") {
    return (
      <Badge tone="neutral" dot>
        {t("stopped")}
      </Badge>
    );
  }
  // processing (or unknown-yet / optimistic) → animated cue
  const stageKey = status && (STAGE_KEYS as readonly string[]).includes(status) ? status : null;
  return (
    <span className="inline-flex shrink-0 items-center gap-1.5 font-mono text-eyebrow uppercase text-muted">
      <Loader2 className="size-3 animate-spin" />
      {stageKey ? t(`stage.${stageKey}`) : t("processing")}…
    </span>
  );
}

export function RecentProjects() {
  const t = useTranslations("recentProjects");
  const [jobs, setJobs] = useState<MyJob[]>([]);
  const [optimistic, setOptimistic] = useState(() => getOptimisticRecents());
  const [loaded, setLoaded] = useState(false);
  const [userId, setUserId] = useState<string | null>(null);

  // Resolve the current user id once for per-user "reviewed" namespacing. null in dev/no-auth →
  // every done job shows the "New" badge until opened (acceptable; the flag is per-user).
  useEffect(() => {
    if (!isSupabaseConfigured) return;
    let active = true;
    createSupabaseBrowserClient()
      .auth.getUser()
      .then(({ data }) => {
        if (active) setUserId(data.user?.id ?? null);
      })
      .catch(() => {
        /* keep null → New badges only; not fatal */
      });
    return () => {
      active = false;
    };
  }, []);

  // Refetch is driven by a nonce: bumping it re-runs the single fetch effect below. Keeping the
  // setState calls inside the effect's promise `.then` (not a synchronous effect-body call) avoids
  // the react-hooks/set-state-in-effect cascade lint.
  const [reloadNonce, setReloadNonce] = useState(0);
  const triggerRefetch = useCallback(() => setReloadNonce((n) => n + 1), []);

  // Fetch the server-authoritative list on mount and on every refetch trigger. getMyJobs never
  // throws on a non-OK (404 deploy window / 401) → [] , so this only guards genuine network/timeout
  // errors (keep the last-known list). setState lives inside the promise callback → lint-clean.
  useEffect(() => {
    let active = true;
    getMyJobs()
      .then((list) => {
        if (!active) return;
        setJobs(list);
        setOptimistic(pruneOptimistic(new Set(list.map((j) => j.id))));
      })
      .catch(() => {
        /* transient network error → keep last-known list, retry next tick */
      })
      .finally(() => {
        if (active) setLoaded(true);
      });
    return () => {
      active = false;
    };
  }, [reloadNonce]);

  // Refetch whenever the dashboard signals a new job was created (also seeds the optimistic row).
  useEffect(
    () =>
      subscribeRecentsRefetch(() => {
        setOptimistic(getOptimisticRecents());
        triggerRefetch();
      }),
    [triggerRefetch],
  );

  // Merge optimistic entries (not yet in the server list) ahead of the server jobs.
  const rows = useMemo<RecentRow[]>(() => {
    const known = new Set(jobs.map((j) => j.id));
    const optRows: RecentRow[] = optimistic
      .filter((o) => !known.has(o.id))
      .map((o) => ({ id: o.id, label: o.label, at: o.at, reviewed: false }));
    const serverRows: RecentRow[] = jobs.map((j) => ({
      id: j.id,
      label: j.label,
      at: toMillis(j.created_at),
      status: j.status,
      nclips: j.nclips,
      reviewed: isReviewed(userId, j.id),
    }));
    return [...optRows, ...serverRows];
  }, [jobs, optimistic, userId]);

  // Poll the server list while ANY row is non-terminal so "Processing… → Ready" updates live
  // even if a job finished while the user was away. Stops once every row is terminal.
  //
  // Backgrounded tabs poll at HIDDEN_POLL_MS instead of POLL_MS — no point hammering the API
  // every 2.5s for a tab the user isn't looking at. The instant the tab becomes visible again
  // we fire an immediate refetch and resume the full cadence. Mirrors useJob's visibility pattern.
  const hasPending = rows.some((r) => !isTerminal(r.status));
  useEffect(() => {
    if (!hasPending) return;
    let id = setInterval(triggerRefetch, document.hidden ? HIDDEN_POLL_MS : POLL_MS);

    const onVisibilityChange = () => {
      clearInterval(id);
      if (document.hidden) {
        id = setInterval(triggerRefetch, HIDDEN_POLL_MS);
      } else {
        triggerRefetch(); // catch up immediately on return to foreground
        id = setInterval(triggerRefetch, POLL_MS);
      }
    };
    document.addEventListener("visibilitychange", onVisibilityChange);

    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [hasPending, triggerRefetch]);

  return (
    <section className="rounded-lg border border-line bg-surface p-5">
      <Eyebrow tone="muted" as="h2">
        {t("heading")}
      </Eyebrow>
      {!loaded && rows.length === 0 ? (
        <p className="mt-3 inline-flex items-center gap-2 text-sm text-muted">
          <Loader2 className="size-3.5 animate-spin" />
          {t("loading")}
        </p>
      ) : rows.length === 0 ? (
        <p className="mt-3 text-sm leading-relaxed text-muted">{t("empty")}</p>
      ) : (
        <ul className="mt-3 -mx-2 divide-y divide-line">
          {rows.map((p) => (
            <li key={p.id} className="group relative flex items-center">
              <Link
                href={`/dashboard?job=${p.id}`}
                className="flex min-w-0 flex-1 items-center gap-3 rounded-md px-2 py-2.5 transition-colors hover:bg-surface-2"
              >
                <Numeral className="w-8 shrink-0 text-eyebrow text-faint">
                  {relTime(p.at, t("now"))}
                </Numeral>
                <span className="min-w-0 flex-1 truncate text-sm text-muted group-hover:text-ink">
                  {p.label}
                </span>
                <StatusCell status={p.status} nclips={p.nclips} reviewed={p.reviewed} />
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
