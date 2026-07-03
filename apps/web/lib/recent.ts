/**
 * Client-side helpers for the "recent projects" list.
 *
 * The list itself is now SERVER-authoritative (GET /jobs via `getMyJobs`) — the old global
 * localStorage store (`quip:recent-projects`) was NOT per-user and never cleared, so account B on
 * a shared device saw account A's projects (the cross-account leak). That store is gone.
 *
 * What stays client-side here:
 *   1. The "New / reviewed" badge — a PER-USER-NAMESPACED set of job ids the user has opened
 *      (`quip:reviewed:<userId>`). Booleans only, not a leak.
 *   2. An OPTIMISTIC-entry + refetch-signal bus so a just-created job shows instantly (and the
 *      list refetches) before the server list catches up.
 *   3. `purgeClientCaches` — the sign-out / account-switch localStorage sweep (decision lives in
 *      the pure `authReset` module).
 */

import { keysToPurge, LAST_USER_ID_KEY, REVIEWED_KEY_PREFIX } from "./authReset";

// ── Per-user "reviewed" set (drops the "New" badge once results are opened) ──

const REVIEWED_MAX = 300; // cap the id list so it can't grow unbounded

function reviewedKey(userId: string): string {
  return `${REVIEWED_KEY_PREFIX}${userId}`;
}

/** Has this user already opened this job's results? False in dev / no-auth (userId null). */
export function isReviewed(userId: string | null, jobId: string): boolean {
  if (!userId || typeof window === "undefined") return false;
  try {
    const raw = window.localStorage.getItem(reviewedKey(userId));
    if (!raw) return false;
    const arr: unknown = JSON.parse(raw);
    return Array.isArray(arr) && arr.includes(jobId);
  } catch {
    // Corrupt storage — the badge is cosmetic, treat as not-reviewed.
    return false;
  }
}

/** Mark a job's results as seen for THIS user → clears the "New" badge. No-op without a userId. */
export function markReviewed(userId: string | null, jobId: string): void {
  if (!userId || typeof window === "undefined") return;
  try {
    const raw = window.localStorage.getItem(reviewedKey(userId));
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    const arr: string[] = Array.isArray(parsed) ? (parsed as string[]) : [];
    if (arr.includes(jobId)) return; // already recorded → skip the write
    const next = [...arr, jobId].slice(-REVIEWED_MAX);
    window.localStorage.setItem(reviewedKey(userId), JSON.stringify(next));
  } catch {
    /* full/corrupt storage — the New badge is cosmetic, don't crash */
  }
}

// ── Optimistic entries + refetch signal (dashboard → RecentProjects) ──

/** A transient, in-memory recents entry shown the instant a job is created, before GET /jobs
 *  includes it. Pruned once the server list returns the id. */
export type OptimisticRecent = { id: string; label: string; at: number };

const OPTIMISTIC_MAX = 12;
let optimistic: OptimisticRecent[] = [];
const refetchListeners = new Set<() => void>();

/** Subscribe to "recents changed" pings (a new job was created). Returns an unsubscribe fn. */
export function subscribeRecentsRefetch(cb: () => void): () => void {
  refetchListeners.add(cb);
  return () => {
    refetchListeners.delete(cb);
  };
}

/** Dashboard calls this right after createJob/createUploadJob so the new project shows instantly
 *  (optimistic row) and any mounted RecentProjects refetches the server list. */
export function noteRecentCreated(entry: OptimisticRecent): void {
  optimistic = [entry, ...optimistic.filter((o) => o.id !== entry.id)].slice(0, OPTIMISTIC_MAX);
  for (const cb of refetchListeners) cb();
}

/** Current optimistic entries (module-level so they survive a RecentProjects unmount/remount). */
export function getOptimisticRecents(): OptimisticRecent[] {
  return optimistic;
}

/** Drop optimistic entries the server list now includes; returns the surviving entries. */
export function pruneOptimistic(knownIds: Set<string>): OptimisticRecent[] {
  const next = optimistic.filter((o) => !knownIds.has(o.id));
  if (next.length !== optimistic.length) optimistic = next;
  return optimistic;
}

// ── Sign-out / account-switch cache purge (defense in depth) ──

/** Impure: wipe cross-account client caches from localStorage — the legacy global recents blob,
 *  the editor inspector width, EVERY user's reviewed set, and the last-user marker. Used by
 *  SignOutButton and AuthStateSync. The pure decision of WHICH keys lives in `authReset.keysToPurge`. */
export function purgeClientCaches(): void {
  if (typeof window === "undefined") return;
  try {
    const all: string[] = [];
    for (let i = 0; i < window.localStorage.length; i++) {
      const k = window.localStorage.key(i);
      if (k) all.push(k);
    }
    for (const k of keysToPurge(all)) window.localStorage.removeItem(k);
    window.localStorage.removeItem(LAST_USER_ID_KEY);
  } catch {
    /* storage unavailable — nothing to purge */
  }
}
