// Bulk "Download all" — the pure decisions behind it. (I/O lives in ClipGrid.)
//
// WHY THIS EXISTS (prod bug, 2026-07-14). «Скачать (N)» fetched `/export/captioned.mp4` per clip
// with an Authorization header. For an already-baked clip that endpoint **302-redirects to
// cdn.quip.ink** — and per the Fetch spec, a CORS request that redirects CROSS-ORIGIN gets its
// origin rewritten to an opaque one, so the browser sends `Origin: null` to the CDN. The R2 bucket
// echoes CORS only for explicit origins, NOT for `null` (verified live: `Origin: null` → 200 with
// NO Access-Control-Allow-Origin). So the blob fetch died with a CORS TypeError on EVERY baked clip,
// and the catch-block fell back to a bare <a download> that could send neither a header NOR a `?mt=`
// ticket → the API answered `401 {"detail":"missing media ticket"}` and the browser SAVED THAT JSON.
// The user saw the error of the fallback, never the real one.
//
// THE FIX: don't make the browser follow that redirect at all. A DIRECT fetch to the CDN sends
// `Origin: https://quip.ink`, which the bucket DOES allow — so we go straight to the baked object
// and skip the API (and its on-the-fly render) entirely. The API stays as the fallback for clips
// that were never baked.

/** Amazon-style signature params. A presigned URL's path is part of what is signed → rewriting it
 *  produces a 403. We must detect and refuse, rather than "cleverly" building a broken link. */
const SIGNED_PARAM_PREFIX = "x-amz-";

/**
 * PURE. The CDN URL of a clip's BAKED captioned object, derived from its clean `video_url`.
 * `null` when it cannot be derived safely — the caller must then fall back to the API endpoint.
 *
 * The pipeline uploads `<job>/<clip>.mp4` (clean) and `<job>/<clip>_captioned.mp4` (burned) to the
 * same bucket, both with `Content-Disposition: attachment` (verified live). So the captioned object
 * is exactly the clean URL with `_captioned` appended to the filename — query string preserved
 * (`?v=` is a CDN cache-buster and must ride along).
 *
 * Refuses (→ null) when:
 *   • the URL is PRESIGNED (any `X-Amz-*` param) — the path is signed; rewriting it breaks the
 *     signature and yields a 403. This is the shape when R2_PUBLIC_URL is unset (local/dev).
 *   • the last path segment is not exactly `<clipId>.mp4` — we refuse to guess.
 */
export function captionedCdnUrl(cleanUrl: string | null | undefined, clipId: string): string | null {
  if (!cleanUrl) return null;
  let u: URL;
  try {
    u = new URL(cleanUrl, typeof window === "undefined" ? "https://x.invalid" : window.location.href);
  } catch {
    return null;
  }
  for (const key of u.searchParams.keys()) {
    if (key.toLowerCase().startsWith(SIGNED_PARAM_PREFIX)) return null; // presigned → do not touch
  }
  const tail = `/${clipId}.mp4`;
  if (!u.pathname.endsWith(tail)) return null;
  u.pathname = `${u.pathname.slice(0, -tail.length)}/${clipId}_captioned.mp4`;
  return u.toString();
}

/** Ceiling on how long we keep re-trying ONE clip against a busy server before giving up. */
export const BUSY_MAX_ATTEMPTS = 6;

/**
 * PURE. How long to wait before retrying a clip the server said it was BUSY for.
 *
 * `/export/*.mp4` answers **503 + `Retry-After`** when its heavy-work semaphore is full (the API box
 * renders at most N clips at once — it is a web service, not a render farm). 503 is the server
 * asking us to WAIT, not an error: the previous code treated it as a hard failure and fell through
 * to a broken fallback, which is why the user was shown `clip export busy` as a downloaded "file"
 * instead of simply getting their clip a few seconds later.
 *
 * Honours `Retry-After` (seconds) when the server sends it; otherwise backs off 2s → 4s → 8s …
 * capped at 20s. `attempt` is 1-based.
 */
export function busyRetryDelayMs(attempt: number, retryAfterHeader?: string | null): number {
  const fromHeader = Number(retryAfterHeader);
  if (Number.isFinite(fromHeader) && fromHeader > 0) return Math.min(fromHeader * 1000, 30_000);
  return Math.min(2000 * 2 ** (attempt - 1), 20_000);
}

/** PURE. Is this HTTP status the server saying "I'm busy, come back" (vs a real failure)? */
export function isBusyStatus(status: number): boolean {
  return status === 503 || status === 429;
}

/** A non-OK download response, carrying what the caller needs to DECIDE: the status (busy vs fatal)
 *  and the server's own `Retry-After`. The old code threw `new Error("HTTP 503")` — a string — so a
 *  "busy, come back in 5s" was indistinguishable from a real failure, and the caller gave up. */
export class DownloadHttpError extends Error {
  constructor(
    readonly status: number,
    readonly retryAfter: string | null = null,
  ) {
    super(`HTTP ${status}`);
    this.name = "DownloadHttpError";
  }
}
