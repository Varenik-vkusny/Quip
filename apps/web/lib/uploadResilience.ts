// Pure, browser-free helpers for the resilient multipart upload path (consumed by lib/api.ts).
// Kept dependency-free (no XHR/IndexedDB) so they can be unit-tested in the node vitest runner.
// The retry/refresh/resume machinery in api.ts is built on top of these decisions.

/** The reason a single part transfer failed at the transport layer (no HTTP status available),
 *  OR `http` when the server answered with a non-2xx status (classified via that status). */
export type PartErrorKind = "timeout" | "stall" | "network" | "http";

/** What to do about a failed part: try the same URL again after a backoff, mint a FRESH presigned
 *  URL first (the old one expired → 403), or give up (a genuine client error). */
export type PartErrorAction = "retry" | "refresh" | "fatal";

// Backoff envelope. Exponential base with full-ish jitter, hard-capped so a long outage doesn't
// stretch a single wait into minutes. Bands for consecutive attempts do not overlap (each attempt's
// minimum >= the previous attempt's maximum) so `backoffDelay(n)` is monotonic in `n` for ANY jitter.
const BACKOFF_BASE_MS = 500;
export const BACKOFF_CAP_MS = 20_000;

/** Stable identity for a picked File — resume keys the IndexedDB checkpoint on this. Distinct
 *  whenever the bytes could differ (size or mtime change), so we never resume onto a different file
 *  under the same name. PURE. */
export function fileFingerprint(file: { name: string; size: number; lastModified: number }): string {
  // size + lastModified guard against a same-named but different file; name is a human hint.
  return `up:${file.size}:${file.lastModified}:${file.name}`;
}

/** The part numbers still to upload = `all` minus the ones already completed. Order preserved. PURE. */
export function remainingParts(all: number[], completed: Set<number>): number[] {
  return all.filter((n) => !completed.has(n));
}

/** Backoff (ms) before retry `attempt` (0-indexed). Exponential + jitter, capped at BACKOFF_CAP_MS.
 *  `rand` is injectable (defaults to Math.random) so tests are deterministic. The result lies in
 *  `[exp/2, exp]` where `exp = min(cap, base·2^attempt)`, so it is monotonic in `attempt`. PURE. */
export function backoffDelay(attempt: number, rand: () => number = Math.random): number {
  const exp = Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** attempt);
  const jittered = exp * 0.5 + rand() * (exp * 0.5); // in [exp/2, exp]
  return Math.round(Math.min(BACKOFF_CAP_MS, jittered));
}

/** Decide how to handle a failed part. Transport failures (timeout/stall/network) are always
 *  transient → retry. For an HTTP status: 403 = expired presigned URL → refresh; 5xx / 408 / 429 =
 *  transient → retry; any other 4xx = a real client error → fatal. A null status with kind `http`
 *  is treated leniently (retry) rather than crashing the upload. PURE. */
export function classifyPartError(status: number | null, kind: PartErrorKind): PartErrorAction {
  if (kind !== "http") return "retry"; // timeout / stall / network — no status, always transient
  if (status === 403) return "refresh"; // presigned URL expired → mint a fresh one and retry
  if (status === null) return "retry";
  if (status >= 500) return "retry"; // server-side hiccup
  if (status === 408 || status === 429) return "retry"; // request-timeout / rate-limit
  if (status >= 400) return "fatal"; // other 4xx: a genuine client error, retrying won't help
  return "retry"; // unexpected non-error status reached the error path — be lenient
}

/** What a failed RESUME `/upload-parts` probe means for the interrupted transfer:
 *  - `auth`      — 401/403: the session token expired or was momentarily rejected. The multipart is
 *                  FINE (its parts still sit in R2). Refresh the token and retry; NEVER drop the checkpoint.
 *  - `gone`      — an explicit 404: the multipart genuinely no longer exists → the ONLY case where we
 *                  discard the checkpoint and legitimately restart from a fresh multipart.
 *  - `transient` — everything else (network/null, 5xx, 408/429, any other 4xx): a server/network hiccup
 *                  or an ambiguous error → retry with backoff, but KEEP the checkpoint. */
export type ResumeErrorAction = "auth" | "gone" | "transient";

/** Classify a failed resume-probe by HTTP status (null = transport-level failure). The guiding rule:
 *  the checkpoint is destroyed (→ full re-upload from 0%) ONLY on a definitive 404. An auth blip or a
 *  transient server/network error must never silently restart a multi-GB transfer — that data loss is
 *  exactly the "login drops mid-long-upload → upload starts over" bug. PURE. */
export function classifyResumeError(status: number | null): ResumeErrorAction {
  if (status === 401 || status === 403) return "auth";
  if (status === 404) return "gone";
  return "transient"; // null/network, 5xx, 408/429, and any other 4xx — keep the parts, don't restart
}

/** Thrown by the resume path once a probe failure has been classified. `kind` drives the caller:
 *  only `gone` may discard the checkpoint; `auth`/`transient` keep it so the user can resume (after a
 *  re-login / reconnect) instead of re-uploading everything. */
export class ResumeError extends Error {
  readonly kind: ResumeErrorAction;
  readonly status: number | null;
  constructor(kind: ResumeErrorAction, status: number | null, message: string) {
    super(message);
    this.name = "ResumeError";
    this.kind = kind;
    this.status = status;
  }
}
