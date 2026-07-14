// Pure, browser-free retry policy for GET /usage (consumed by lib/useUsage.ts, thrown by lib/api.ts).
// Kept dependency-free (no React/fetch/timers — they're injected) so it unit-tests in the node runner,
// mirroring lib/uploadResilience.ts.
//
// THE BUG THIS EXISTS FOR (observed on prod 2026-07-14): `quip-api` runs scale-to-zero on Cloud Run,
// and `containerConcurrency` was lowered 80 → 8, so containers spin up far more often. The FIRST
// request after idle can die at the infrastructure layer (the LB answers before the container is up,
// so the response carries no CORS headers) — the browser can only report that as
// "No 'Access-Control-Allow-Origin' header" + a rejected fetch (TypeError). A reload right after is
// clean, and `curl -X OPTIONS` returns 200 with the right origin: CORS is fine, the CONTAINER was cold.
//
// One dead request must not decide a paying customer's plan. Historically /usage failure silently
// rendered the FREE plan (a Pro user "lost" their subscription on screen); that's fixed (useUsage has
// explicit loading/ok/error), but a single cold-start miss still pinned the user on the error card.
// So: retry the transient class, and only ever show a plan we actually fetched.

import { isConnectivityError } from "./connectionError";

/** A non-OK HTTP answer from GET /usage. Carries the status so the retry policy can tell a cold
 *  container (502/503 → retry) from a genuine "you are not signed in" (401 → don't retry). Thrown by
 *  `getUsage` in lib/api.ts; a plain `Error("getUsage failed: 503")` couldn't be classified. */
export class UsageHttpError extends Error {
  readonly status: number;
  constructor(status: number) {
    super(`getUsage failed: ${status}`);
    this.name = "UsageHttpError";
    this.status = status;
  }
}

/** `transient` = the request never reached a healthy container (retry it). `fatal` = the server
 *  answered and its answer won't change on a retry (surface an honest error). */
export type UsageErrorKind = "transient" | "fatal";

/**
 * Classify a failed /usage fetch. PURE — `online` is passed in (read from `navigator.onLine` at the
 * call site), like `isConnectivityError`, which this reuses for the connectivity class:
 *   - browser offline, or `fetch` rejecting outright (`TypeError` — this is what a cold-start CORS
 *     failure looks like to JS), or our `fetchWithTimeout` cap firing (`AbortError`/`TimeoutError`);
 *   - plus HTTP statuses that mean "the app never really answered": any 5xx (502/503 = the cold or
 *     overloaded container), 408 (request timeout), 429 (rate-limited).
 *
 * Everything else is fatal: 401/403 (not signed in / not yours) and any other 4xx won't heal by
 * retrying — hammering them just delays an honest message. An unknown throw is fatal too: we refuse
 * to retry blindly on something we don't understand (and we NEVER downgrade it to "Free").
 */
export function classifyUsageError(err: unknown, online: boolean): UsageErrorKind {
  if (isConnectivityError(err, online)) return "transient";
  if (!(err instanceof UsageHttpError)) return "fatal";
  if (err.status >= 500) return "transient"; // cold / crashed / overloaded container
  if (err.status === 408 || err.status === 429) return "transient";
  return "fatal"; // 401 / 403 / other 4xx — a real answer, retrying can't change it
}

/** Backoff ladder between attempts, in ms. Same shape as useJob's reconnect ladder (2.5s → 5s → 10s,
 *  capped): 4 attempts total (1 + 3 retries) spanning ~17.5s — comfortably longer than a Cloud Run
 *  cold start, short enough that a truly-down API reaches the honest error state quickly. */
export const USAGE_RETRY_DELAYS_MS: readonly number[] = [2_500, 5_000, 10_000];

export type UsageRetryDeps<T> = {
  /** One attempt (i.e. `getUsage`). Injected so the policy stays pure/testable. */
  fetchOnce: () => Promise<T>;
  /** Injected timer — tests pass a no-op, the hook passes setTimeout. */
  sleep: (ms: number) => Promise<void>;
  /** `navigator.onLine` at call time (injected: keeps this node-testable). */
  isOnline: () => boolean;
  /** Unmounted / superseded by a newer attempt → stop retrying and stop touching React state. */
  isCancelled?: () => boolean;
  /** Fired before each backoff wait — the UI uses it to say "taking longer than usual". */
  onRetry?: (info: { attempt: number; delayMs: number; error: unknown }) => void;
  /** Override the ladder (tests). */
  delays?: readonly number[];
};

/**
 * Run `fetchOnce`, retrying ONLY the transient class with the backoff ladder. Resolves with the
 * value on any success; REJECTS with the last error once the ladder is exhausted (or immediately on
 * a fatal one). It never invents a value — the caller must render "unknown", never a guessed plan.
 * Generic in T so this file needs no import from api.ts (no cycle).
 */
export async function retryUsageFetch<T>(deps: UsageRetryDeps<T>): Promise<T> {
  const delays = deps.delays ?? USAGE_RETRY_DELAYS_MS;
  for (let attempt = 0; ; attempt++) {
    try {
      return await deps.fetchOnce();
    } catch (err) {
      // A cancelled caller (unmount / reload()) must not keep retrying in the background.
      if (deps.isCancelled?.()) throw err;
      if (attempt >= delays.length) throw err; // ladder exhausted → honest failure
      if (classifyUsageError(err, deps.isOnline()) === "fatal") throw err;
      const delayMs = delays[attempt];
      deps.onRetry?.({ attempt: attempt + 1, delayMs, error: err });
      await deps.sleep(delayMs);
      if (deps.isCancelled?.()) throw err;
    }
  }
}
