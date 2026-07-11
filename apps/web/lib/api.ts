import { createSupabaseBrowserClient } from "./supabase/client";
import { isSupabaseConfigured } from "./supabase/config";
import type {
  AgentRun,
  CaptionPreset,
  CaptionStyle,
  CaptionTrack,
  ChaptersData,
  ClipEdit,
  ClipOut,
  HighlightStyle,
  Job,
  TimelineData,
  VideoMap,
  Word,
} from "./types";
import {
  clearCheckpoint,
  loadCheckpoint,
  saveCheckpoint,
  type UploadCheckpoint,
  updatePart,
} from "./uploadCheckpoint";
import { backoffDelay, classifyPartError, fileFingerprint } from "./uploadResilience";

// База воркера: реальный worker через env, иначе встроенный мок (/api/mock).
const BASE = process.env.NEXT_PUBLIC_WORKER_URL ?? "/api/mock";

/** PURE. Обновлять ли access_token ПЕРЕД использованием? True, если он истекает в пределах `skewMs`
 *  (или уже просрочен). Multi-GB заливка (минуты-часы) переживает ~1ч токен → финализация
 *  (`upload-complete`) со снимком токена со старта ловит 401. Обновляем проактивно у границы. */
export function shouldRefreshToken(
  expiresAtSec: number | undefined | null,
  nowMs: number,
  skewMs = 120_000,
): boolean {
  if (!expiresAtSec) return false;
  return expiresAtSec * 1000 - nowMs <= skewMs;
}

/** Текущий Supabase access_token (когда auth настроен), иначе null (dev / auth не подключён).
 *  Единый источник токена: используется и для Authorization-заголовка, и для `?token=` на
 *  медиа-URL (`<video src>` / нативные скачивания не могут выставить заголовок).
 *  У границы истечения — проактивный `refreshSession` (иначе долгая заливка финалит протухшим → 401);
 *  падение refresh → отдаём текущий токен (никогда не роняем авторизацию в null из-за рефреша). */
export async function getAccessToken(): Promise<string | null> {
  if (!isSupabaseConfigured) return null;
  try {
    const client = createSupabaseBrowserClient();
    const { data } = await client.auth.getSession();
    const session = data.session;
    if (!session) return null;
    if (shouldRefreshToken(session.expires_at, Date.now())) {
      try {
        const { data: r } = await client.auth.refreshSession();
        return r.session?.access_token ?? session.access_token;
      } catch {
        return session.access_token;
      }
    }
    return session.access_token;
  } catch {
    return null;
  }
}

/** Заголовок авторизации для воркера: текущий Supabase access_token (когда auth настроен).
 *  Воркер валидирует JWT (JWKS проекта) и достаёт user_id. Без auth → пустой объект (dev). */
async function authHeaders(): Promise<Record<string, string>> {
  const token = await getAccessToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/** Append a SCOPED media ticket to a WORKER media URL for surfaces that can't send an
 *  Authorization header — `<video src>` (editor preview / PendingThumb) and native downloads.
 *  The ticket is an opaque, short-lived, job-scoped HMAC (see `getMediaToken`) — NOT the session
 *  JWT — so it's safe to leak into logs/Referer. Query key is `mt` by default. Worker media
 *  endpoints accept EITHER the Authorization header OR this `?mt=` param. PURE; no-op when the
 *  token is absent (dev/open mode) or the url is empty. Preserves any existing query string. */
export function withToken(url: string, token: string | null | undefined, key = "mt"): string {
  if (!token || !url) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}${key}=${encodeURIComponent(token)}`;
}

/** Result of an on-demand clip extraction (POST /jobs/{id}/clips). The worker is the authority on
 *  the gate: a click always calls it and reacts to the response, so the UI can't drift from
 *  billing.py. `reason` maps the worker's 402/403/409 to a human message. */
export type ExtractResult =
  | { ok: true; clip: ClipOut }
  | {
      ok: false;
      reason: "gated" | "paywall" | "expired" | "auth" | "error";
      priceUsd?: number;
      message: string;
    };

/** Cut a NEW clip from an arbitrary source range [start,end] (an un-clipped moment or a timeline
 *  drag-select). Returns the pending ClipOut (empty video_url = still rendering, streams in like any
 *  pending clip), or a typed failure for the gate/paywall/expiry the worker enforces. */
export async function createClipFromRange(
  jobId: string,
  range: { start: number; end: number },
): Promise<ExtractResult> {
  const res = await fetch(`${BASE}/jobs/${jobId}/clips`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify(range),
  });
  if (res.ok) return { ok: true, clip: (await res.json()) as ClipOut };

  // FastAPI HTTPException nests our payload under `detail`.
  const body = (await res.json().catch(() => null)) as {
    error?: string;
    price_usd?: number;
    detail?: { error?: string; price_usd?: number } | string;
  } | null;
  const detail = typeof body?.detail === "object" ? body?.detail : undefined;
  const err = body?.error ?? detail?.error;
  const priceUsd = body?.price_usd ?? detail?.price_usd;

  if (res.status === 403 || err === "upgrade_required")
    return { ok: false, reason: "gated", message: "Extracting clips is a Pro feature." };
  if (res.status === 402 || err === "payment_required")
    return {
      ok: false,
      reason: "paywall",
      priceUsd,
      message: `That's past your 30 free clips — $${priceUsd ?? 1} to pull this one.`,
    };
  if (res.status === 409 || err === "source_expired")
    return {
      ok: false,
      reason: "expired",
      message: "This video's source is no longer stored — re-upload to extract more.",
    };
  if (res.status === 401)
    return { ok: false, reason: "auth", message: "Sign in to extract clips." };
  return { ok: false, reason: "error", message: `Couldn't extract this clip (${res.status}).` };
}

// ── Scoped media tickets (opaque, ~5min TTL, per job) ──
// A media ticket authorizes ONLY this job's media/export endpoints for a short window — it is not
// a full-account credential, so putting it in a URL is safe (unlike the session JWT). Cached per
// jobId with its TTL so N PendingThumbs / the editor preview share ONE fetch; refetched ~30s before
// expiry. Never throws (non-OK → null → caller falls back to header-only / a 401'd load that retries).
type MediaTicket = { token: string; expEpochMs: number };
const mediaTicketCache = new Map<string, MediaTicket>();
const MEDIA_TOKEN_SKEW_MS = 30_000; // refetch this long BEFORE the ticket actually expires

export async function getMediaToken(jobId: string): Promise<string | null> {
  const now = Date.now();
  const cached = mediaTicketCache.get(jobId);
  if (cached && cached.expEpochMs - MEDIA_TOKEN_SKEW_MS > now) return cached.token;
  const res = await fetchWithTimeout(`${BASE}/jobs/${jobId}/media-token`, {
    cache: "no-store",
    headers: await authHeaders(),
  });
  if (!res.ok) return null; // 403 (not your job) / 401 / 404 (deploy window) → no ticket
  const data = (await res.json().catch(() => null)) as
    | { token?: string; expires_in?: number }
    | null;
  const token = data?.token;
  if (!token) return null;
  const ttlSec = typeof data?.expires_in === "number" && data.expires_in > 0 ? data.expires_in : 300;
  mediaTicketCache.set(jobId, { token, expEpochMs: now + ttlSec * 1000 });
  return token;
}

/** Drop a cached media ticket so the next `getMediaToken(jobId)` refetches — call after a media
 *  load 401s (ticket rotated/expired) to recover without a full reload. */
export function invalidateMediaToken(jobId: string): void {
  mediaTicketCache.delete(jobId);
}

/** A row in the user's own "recent projects" list (server-authoritative, from GET /jobs).
 *  Local UI type — the recents list has always been a local shape with no shared-codegen dep. */
export type MyJob = {
  id: string;
  label: string;
  status: string;
  nclips: number;
  created_at: number | string;
  source_kind: string;
};

/** The caller's OWN jobs (newest first) — the server-backed recents list that replaces the old
 *  global-localStorage store (the cross-account leak). Auth via bearer header.
 *
 *  Graceful during the worker DEPLOY WINDOW: the route may briefly be missing (old worker → 404)
 *  or the token may not be accepted yet — ANY non-OK response resolves to `[]` (empty list), never
 *  throws and never spams the console, so the dashboard can't crash while the route rolls out.
 *  A genuine network error / timeout still rejects (fetchWithTimeout) so the poller can retry. */
export async function getMyJobs(): Promise<MyJob[]> {
  const res = await fetchWithTimeout(`${BASE}/jobs`, {
    cache: "no-store",
    headers: await authHeaders(),
  });
  if (!res.ok) return []; // 404 (deploy window) / 401 / 5xx → empty, no crash, no console spam
  const data = (await res.json().catch(() => null)) as { jobs?: MyJob[] } | null;
  return data?.jobs ?? [];
}

export type CreateJobInput = {
  source_type: "youtube";
  source_ref: string;
  max_clips?: number;
  clip_min_sec?: number;
  clip_max_sec?: number;
};

export interface UsageInfo {
  plan: string;
  plan_name: string;
  monthly_videos: number; // план: видео/мес (free=1)
  monthly_minutes: number; // = monthly_videos × 60 (free=60)
  used_minutes: number; // израсходовано в этом месяце (дробно)
  remaining_minutes: number; // осталось в месячном пуле (без PAYG)
  remaining_videos: number; // = remaining_minutes / 60 (напр. 8.7)
  payg_videos: number; // не сгорающий баланс (видео)
  payg_minutes: number; // = payg_videos × 60
}

export async function createJob(input: CreateJobInput): Promise<{ id: string }> {
  const res = await fetch(`${BASE}/jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify(input),
  });
  await throwOnAuthOrQuota(res);
  if (!res.ok) throw new Error(`createJob failed: ${res.status}`);
  return res.json();
}

/** 401/402 от воркера → понятная ошибка (серверная причина квоты сохраняется). */
async function throwOnAuthOrQuota(res: Response): Promise<void> {
  if (res.status === 401) throw new Error("Sign in to create clips.");
  if (res.status === 402) {
    const body = (await res.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(body?.detail ?? "Monthly limit reached. Upgrade your plan on the pricing page.");
  }
}

/** Rich upload-progress reading passed to the UI so it can render a live rate + ETA (not just a %).
 *  `elapsedSec` is measured at each progress EVENT (client-side, since this transfer's bytes began
 *  moving), so the UI derives speed = loadedBytes / elapsedSec and an ETA purely from these — the
 *  presigned-PUT contract to R2 is untouched (pure client math). Measuring elapsed at event time (not
 *  in render) keeps the consuming component pure: no Date.now() during render. `loadedBytes`/
 *  `totalBytes` are the summed bytes across all in-flight parts for a multipart upload. */
export type UploadProgressInfo = {
  pct: number;
  loadedBytes: number;
  totalBytes: number;
  elapsedSec: number;
  // Narrates the resilient multipart path so the UI can distinguish "bytes moving" from a transient
  // recovery. `uploading` = normal; `retrying` = a part failed and we're backing off before another
  // try; `stalled` = a part stopped sending bytes and the watchdog aborted it (also retried). During
  // retry/stall, `part`/`attempt` identify what's being re-tried (1-indexed attempt). Optional so the
  // single-PUT / local paths and the pre-flight `{pct:0}` seed stay valid without a phase.
  phase?: "uploading" | "retrying" | "stalled";
  part?: number;
  attempt?: number;
};

/** XHR с upload-progress + abort. Резолвит {status, responseText}. (fetch не даёт upload-progress.) */
function xhrRequest(
  method: string,
  url: string,
  body: XMLHttpRequestBodyInit | null,
  opts: {
    headers?: Record<string, string>;
    onProgress?: (p: UploadProgressInfo) => void;
    signal?: AbortSignal;
  } = {},
): Promise<{ status: number; responseText: string }> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const startTime = Date.now(); // anchor for the client-side rate/ETA math
    xhr.open(method, url);
    for (const [k, v] of Object.entries(opts.headers ?? {})) xhr.setRequestHeader(k, v);
    const { signal } = opts;
    if (signal) {
      if (signal.aborted) {
        xhr.abort();
        return reject(new DOMException("Upload aborted", "AbortError"));
      }
      signal.addEventListener(
        "abort",
        () => {
          xhr.abort();
          reject(new DOMException("Upload aborted", "AbortError"));
        },
        { once: true },
      );
    }
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && opts.onProgress) {
        opts.onProgress({
          pct: Math.round((e.loaded / e.total) * 100),
          loadedBytes: e.loaded,
          totalBytes: e.total,
          elapsedSec: (Date.now() - startTime) / 1000,
        });
      }
    };
    xhr.onload = () => resolve({ status: xhr.status, responseText: xhr.responseText });
    xhr.onerror = () => reject(new Error("Upload failed — check your connection and try again"));
    xhr.send(body);
  });
}

// ── Resilient multipart tuning ──
// Hard per-part ceiling: even with a live socket a single part shouldn't take longer than this
// (a 5 GB source with ~8 MB parts on a 2 Mbps floor uplinks one part in ~30s; 120s is generous).
const PART_TIMEOUT_MS = 120_000;
// Stall watchdog: no upload-progress bytes for this long on a flaky socket that fired neither
// onload nor onerror → abort + retry (this is the "frozen at 99% forever" bug, killed).
const STALL_MS = 30_000;
const STALL_TICK_MS = 2_000; // how often the watchdog checks for silence
const PART_MAX_ATTEMPTS = 6; // total tries per part before a hard failure (then resume takes over)

/** A part transfer that failed at the transport layer (no HTTP status). `kind` feeds
 *  classifyPartError so the retry loop can decide retry vs refresh vs fatal. */
class UploadPartError extends Error {
  kind: "timeout" | "stall" | "network";
  constructor(kind: "timeout" | "stall" | "network") {
    super(`upload part ${kind}`);
    this.name = "UploadPartError";
    this.kind = kind;
  }
}

/** Abortable delay used between retries. Rejects with AbortError if `signal` fires first so a
 *  cancelled / fatally-failed upload doesn't sit in a backoff sleep. */
function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      clearTimeout(timer);
      reject(new DOMException("Upload aborted", "AbortError"));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    if (signal) {
      if (signal.aborted) {
        clearTimeout(timer);
        reject(new DOMException("Upload aborted", "AbortError"));
        return;
      }
      signal.addEventListener("abort", onAbort, { once: true });
    }
  });
}

/** PUT одной части в R2 по presigned URL; резолвит её ETag (нужен для сборки объекта).
 *  ETag читается из заголовка ответа — требует CORS `ExposeHeaders: ["ETag"]` на бакете.
 *  Adds a hard `timeout` + a stall watchdog: a socket that stops sending bytes (but fires no
 *  onload/onerror) is aborted and rejected with a retryable `stall`/`timeout`/`network` kind, so a
 *  single hung part can never freeze the whole upload. A non-2xx HTTP answer RESOLVES with its
 *  status (the retry loop classifies it); only transport failures reject. */
function putPart(
  url: string,
  blob: Blob,
  onProgress: (loaded: number) => void,
  signal?: AbortSignal,
): Promise<{ status: number; etag: string | null }> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    let settled = false;
    let lastProgressTs = Date.now();
    let watchdog: ReturnType<typeof setInterval> | null = null;

    const cleanup = () => {
      if (watchdog !== null) {
        clearInterval(watchdog);
        watchdog = null;
      }
      signal?.removeEventListener("abort", onAbort);
    };
    const settleResolve = (v: { status: number; etag: string | null }) => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(v);
    };
    const settleReject = (e: unknown) => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(e);
    };
    function onAbort() {
      try {
        xhr.abort();
      } catch {
        /* already gone */
      }
      settleReject(new DOMException("Upload aborted", "AbortError"));
    }

    xhr.open("PUT", url);
    xhr.timeout = PART_TIMEOUT_MS;
    if (signal) {
      if (signal.aborted) {
        settleReject(new DOMException("Upload aborted", "AbortError"));
        return;
      }
      signal.addEventListener("abort", onAbort, { once: true });
    }
    xhr.upload.onprogress = (e) => {
      lastProgressTs = Date.now();
      if (e.lengthComputable) onProgress(e.loaded);
    };
    xhr.ontimeout = () => settleReject(new UploadPartError("timeout"));
    xhr.onerror = () => settleReject(new UploadPartError("network"));
    xhr.onload = () => settleResolve({ status: xhr.status, etag: xhr.getResponseHeader("ETag") });
    watchdog = setInterval(() => {
      if (Date.now() - lastProgressTs > STALL_MS) {
        try {
          xhr.abort();
        } catch {
          /* already gone */
        }
        settleReject(new UploadPartError("stall"));
      }
    }, STALL_TICK_MS);
    xhr.send(blob);
  });
}

// Parallel R2 part uploads in flight at once. Raised 3→6 in tandem with the worker shrinking
// `part_size` (smaller parts + more of them) so a fat pipe stays saturated and big uploads finish
// sooner. The client never assumes a part size: each chunk's byte range is derived from the
// server's `part_size` and the server's `parts` plan (see uploadParts), so this stays correct
// whatever size the worker picks.
const UPLOAD_PART_CONCURRENCY = 6;

/** Bytes contributed by a single part number given the plan's part_size and the file total (the
 *  last part is short). Used to seed the progress baseline from already-completed (resumed) parts. */
function partBytes(partNumber: number, partSize: number, total: number): number {
  const start = (partNumber - 1) * partSize;
  return Math.max(0, Math.min(partSize, total - start));
}

type ResilientUploadOpts = {
  total: number; // file.size — the progress denominator
  preCompleted: { part_number: number; etag: string }[]; // already in R2 (resume) — progress baseline
  jobId: string; // for the upload-parts URL-refresh endpoint
  uploadId: string;
  headers: Record<string, string>;
  fingerprint: string; // resume-checkpoint key — each landed ETag is persisted here
  onProgress?: (p: UploadProgressInfo) => void;
  signal?: AbortSignal; // external (user) cancel
  rand?: () => number; // jitter seed (tests)
};

/** Upload `parts` (the REMAINING part plan) to R2 with per-part timeout+watchdog+retry+URL-refresh,
 *  persisting each ETag to the resume checkpoint as it lands. Progress = (resumed bytes + in-flight
 *  bytes) / total, carrying a `phase` so the UI can narrate retries. An internal AbortController fans
 *  a fatal part out to its siblings (blast-radius containment); the ORIGINAL error is surfaced (not
 *  the sibling AbortErrors it triggers). Returns the newly-completed [{part_number, etag}]. */
async function uploadPartsResilient(
  file: File,
  parts: { part_number: number; url: string }[],
  partSize: number,
  opts: ResilientUploadOpts,
): Promise<{ part_number: number; etag: string }[]> {
  const rand = opts.rand ?? Math.random;
  const startTime = Date.now(); // anchor for the client-side rate/ETA math (all parts share it)
  const loaded = new Array<number>(parts.length).fill(0);
  const results = new Array<{ part_number: number; etag: string }>(parts.length);
  const preCompletedBytes = opts.preCompleted.reduce(
    (sum, p) => sum + partBytes(p.part_number, partSize, opts.total),
    0,
  );

  // Retry-phase state, shared across the concurrent workers → drives the progress `phase`.
  let retryDepth = 0; // how many parts are currently backing off
  let lastRetry: { part: number; attempt: number } | null = null;
  let lastStalled = false;
  // Monotonic guard: a retried part resets its byte contribution (backoff sets loaded[i]=0), which
  // would otherwise make the displayed % visibly roll BACKWARDS. Clamp the reported total to its
  // running max so the bar freezes during a re-upload instead of regressing (confusing to users).
  let reportedSum = 0;

  const report = () => {
    if (!opts.onProgress) return;
    const raw = preCompletedBytes + loaded.reduce((a, b) => a + b, 0);
    const sum = Math.max(reportedSum, raw);
    reportedSum = sum;
    const active = retryDepth > 0;
    opts.onProgress({
      pct: Math.min(100, Math.round((sum / opts.total) * 100)),
      loadedBytes: Math.min(opts.total, sum),
      totalBytes: opts.total,
      elapsedSec: (Date.now() - startTime) / 1000,
      phase: active ? (lastStalled ? "stalled" : "retrying") : "uploading",
      part: active ? lastRetry?.part : undefined,
      attempt: active ? lastRetry?.attempt : undefined,
    });
  };

  // A fatal part aborts its siblings via this controller; it's also chained off the external
  // (user-cancel) signal so a cancel tears everything down.
  const internal = new AbortController();
  const onExtAbort = () => internal.abort();
  if (opts.signal) {
    if (opts.signal.aborted) internal.abort();
    else opts.signal.addEventListener("abort", onExtAbort, { once: true });
  }

  const refreshPartUrl = async (n: number): Promise<string> => {
    const r = await fetch(`${BASE}/jobs/${opts.jobId}/upload-parts`, {
      method: "POST",
      // Fresh auth: on a very long upload the start-of-upload token can expire before a URL renewal.
      headers: { ...opts.headers, ...(await authHeaders()), "Content-Type": "application/json" },
      body: JSON.stringify({ upload_id: opts.uploadId, part_numbers: [n] }),
      signal: internal.signal,
    });
    if (!r.ok) throw new Error(`Couldn’t refresh upload URL for part ${n}: ${r.status}`);
    const d = (await r.json()) as { parts?: { part_number: number; url: string }[] };
    const url = d.parts?.find((p) => p.part_number === n)?.url;
    if (!url) throw new Error(`Upload URL refresh for part ${n} returned no URL`);
    return url;
  };

  // Enter a backoff wait: reset this part's byte contribution (the failed attempt didn't land),
  // flip the phase, sleep, then restore the phase. retryDepth counts concurrently-retrying parts.
  const backoff = async (i: number, part: number, attempt: number, stalled: boolean) => {
    loaded[i] = 0;
    retryDepth += 1;
    lastRetry = { part, attempt: attempt + 1 };
    lastStalled = stalled;
    report();
    try {
      await sleep(backoffDelay(attempt, rand), internal.signal);
    } finally {
      retryDepth -= 1;
      if (retryDepth === 0) {
        lastRetry = null;
        lastStalled = false;
      }
      report();
    }
  };

  const uploadOne = async (i: number): Promise<void> => {
    const p = parts[i];
    const start = (p.part_number - 1) * partSize;
    const chunk = file.slice(start, Math.min(start + partSize, opts.total));
    let url = p.url;
    for (let attempt = 0; ; attempt++) {
      if (internal.signal.aborted) throw new DOMException("Upload aborted", "AbortError");
      try {
        const { status, etag } = await putPart(
          url,
          chunk,
          (l) => {
            loaded[i] = l;
            report();
          },
          internal.signal,
        );
        if (status >= 200 && status < 300) {
          if (!etag) throw new Error("Upload part missing ETag (R2 CORS must expose the ETag header)");
          loaded[i] = chunk.size;
          report();
          // ETag верстаем как вернул R2 (в кавычках) — complete_multipart_upload ждёт ровно его.
          results[i] = { part_number: p.part_number, etag };
          void updatePart(opts.fingerprint, { part_number: p.part_number, etag }); // persist for resume
          return;
        }
        // Non-2xx HTTP → classify by status (403 → refresh URL, 5xx/408/429 → retry, other 4xx → fatal).
        const action = classifyPartError(status, "http");
        if (action === "fatal") throw new Error(`Upload part ${p.part_number} failed: ${status}`);
        if (attempt + 1 >= PART_MAX_ATTEMPTS) {
          throw new Error(`Upload part ${p.part_number} failed after ${PART_MAX_ATTEMPTS} attempts: ${status}`);
        }
        if (action === "refresh") url = await refreshPartUrl(p.part_number);
        await backoff(i, p.part_number, attempt, false);
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") throw e;
        if (e instanceof UploadPartError) {
          if (attempt + 1 >= PART_MAX_ATTEMPTS) {
            throw new Error(`Upload part ${p.part_number} failed after ${PART_MAX_ATTEMPTS} attempts (${e.kind})`);
          }
          await backoff(i, p.part_number, attempt, e.kind === "stall");
          continue;
        }
        throw e; // missing ETag / URL-refresh failure / anything else → fatal
      }
    }
  };

  // Bounded concurrency. Workers catch their own error (recording the FIRST fatal one) and abort
  // internally so a sibling failure stops the rest deterministically — we then throw the real error.
  let nextIdx = 0;
  let fatal: unknown = null;
  const worker = async (): Promise<void> => {
    for (let i = nextIdx++; i < parts.length; i = nextIdx++) {
      if (internal.signal.aborted) return;
      try {
        await uploadOne(i);
      } catch (e) {
        if (!(e instanceof DOMException && e.name === "AbortError") && fatal === null) fatal = e;
        internal.abort();
        return;
      }
    }
  };

  try {
    report(); // initial paint incl. any resumed baseline
    await Promise.all(Array.from({ length: Math.min(UPLOAD_PART_CONCURRENCY, parts.length) }, worker));
  } finally {
    opts.signal?.removeEventListener("abort", onExtAbort);
  }

  if (fatal !== null) throw fatal; // the real transfer error wins over the AbortErrors it triggered
  if (opts.signal?.aborted) throw new DOMException("Upload aborted", "AbortError");
  return results;
}

/** POST /jobs/{id}/upload-complete — assemble the R2 object from the collected part ETags + spawn
 *  the job. Shared by the fresh and resume multipart paths. */
async function completeMultipart(
  id: string,
  uploadId: string,
  parts: { part_number: number; etag: string }[],
  file: File,
  maxClips: number | undefined,
  language: string | null | undefined,
  headers: Record<string, string>,
  signal?: AbortSignal,
  clipMinSec?: number,
  clipMaxSec?: number,
): Promise<void> {
  const done = await fetch(`${BASE}/jobs/${id}/upload-complete`, {
    method: "POST",
    // Re-fetch auth FRESH here: this runs at the very END of a possibly multi-GB upload, long after
    // `headers` was snapshotted at upload start — the snapshot's ~1h token may have expired → 401.
    // Fresh authHeaders() overrides; `...headers` stays as the dev/no-auth fallback ({}).
    headers: { ...headers, ...(await authHeaders()), "Content-Type": "application/json" },
    body: JSON.stringify({
      filename: file.name,
      max_clips: maxClips ?? null,
      language: language ?? null,
      clip_min_sec: clipMinSec ?? null,
      clip_max_sec: clipMaxSec ?? null,
      upload_id: uploadId,
      parts,
    }),
    signal,
  });
  if (!done.ok) throw new Error(`Couldn’t finalize upload: ${done.status}`);
}

/** Resume an interrupted multipart upload from its IndexedDB checkpoint. The parts already sit in R2
 *  under the checkpoint's upload_id (the job row doesn't exist until upload-complete), so we ask the
 *  server (ListParts) what's really there, reconcile with the local checkpoint, upload only the
 *  remainder, then finalize. Throws if the checkpoint is unusable (expired multipart) — the caller
 *  clears it and restarts fresh. */
async function resumeMultipart(
  file: File,
  cp: UploadCheckpoint,
  fingerprint: string,
  maxClips: number | undefined,
  language: string | null | undefined,
  headers: Record<string, string>,
  onProgress?: (p: UploadProgressInfo) => void,
  signal?: AbortSignal,
  clipMinSec?: number,
  clipMaxSec?: number,
  rand?: () => number,
): Promise<{ id: string }> {
  const res = await fetch(`${BASE}/jobs/${cp.job_id}/upload-parts`, {
    method: "POST",
    headers: { ...headers, ...(await authHeaders()), "Content-Type": "application/json" },
    // No part_numbers ⇒ server returns fresh URLs for every not-yet-completed part (the resume case).
    body: JSON.stringify({ upload_id: cp.upload_id, size: file.size }),
    signal,
  });
  if (!res.ok) throw new Error(`resume upload-parts failed: ${res.status}`);
  const data = (await res.json()) as {
    part_size?: number;
    completed?: { part_number: number; etag: string }[];
    parts?: { part_number: number; url: string }[];
  };
  const partSize = data.part_size || cp.part_size;
  // Reconcile: server ListParts is authoritative; union with the local checkpoint (belt + braces).
  const completedMap = new Map<number, string>();
  for (const p of cp.completed) completedMap.set(p.part_number, p.etag);
  for (const p of data.completed ?? []) completedMap.set(p.part_number, p.etag);
  const preCompleted = [...completedMap.entries()]
    .map(([part_number, etag]) => ({ part_number, etag }))
    .sort((a, b) => a.part_number - b.part_number);
  const toUpload = (data.parts ?? []).filter((p) => !completedMap.has(p.part_number));

  // Rewrite the checkpoint with the reconciled state (part_size may have been echoed back).
  await saveCheckpoint(fingerprint, {
    job_id: cp.job_id,
    upload_id: cp.upload_id,
    part_size: partSize,
    completed: preCompleted,
  });

  const newly = await uploadPartsResilient(file, toUpload, partSize, {
    total: file.size,
    preCompleted,
    jobId: cp.job_id,
    uploadId: cp.upload_id,
    headers,
    fingerprint,
    onProgress,
    signal,
    rand,
  });
  const all = [...preCompleted, ...newly].sort((a, b) => a.part_number - b.part_number);
  await completeMultipart(cp.job_id, cp.upload_id, all, file, maxClips, language, headers, signal, clipMinSec, clipMaxSec);
  await clearCheckpoint(fingerprint);
  return { id: cp.job_id };
}

/** Drop any resume checkpoint for this file — call on an explicit "Start over" so the next upload of
 *  the same file begins fresh instead of resuming from the interrupted transfer. Best-effort. */
export function clearUploadCheckpoint(file: {
  name: string;
  size: number;
  lastModified: number;
}): Promise<void> {
  return clearCheckpoint(
    fileFingerprint({ name: file.name, size: file.size, lastModified: file.lastModified }),
  );
}

export async function createUploadJob(
  file: File,
  maxClips?: number,
  onProgress?: (p: UploadProgressInfo) => void,
  signal?: AbortSignal,
  language?: string | null,
  clipMinSec?: number,
  clipMaxSec?: number,
  rand?: () => number,
): Promise<{ id: string }> {
  const headers = await authHeaders();
  const fingerprint = fileFingerprint({
    name: file.name,
    size: file.size,
    lastModified: file.lastModified,
  });

  // ── RESUME: a checkpoint from a previous interrupted multipart upload of THIS exact file. The
  // parts already sit in R2 under the checkpoint's upload_id, so resume that — DON'T mint a new one.
  // If the checkpoint is unusable (R2 multipart expired / aborted → upload-parts errors), clear it
  // and fall through to a fresh upload: an honest restart, not a swallowed error (rule #8). ──
  const checkpoint = await loadCheckpoint(fingerprint);
  if (checkpoint?.job_id && checkpoint.upload_id && checkpoint.part_size > 0) {
    try {
      return await resumeMultipart(
        file,
        checkpoint,
        fingerprint,
        maxClips,
        language,
        headers,
        onProgress,
        signal,
        clipMinSec,
        clipMaxSec,
        rand,
      );
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") throw e;
      await clearCheckpoint(fingerprint); // stale/unusable checkpoint → start clean below
    }
  }

  // 1. Спросить у воркера URL для ПРЯМОЙ загрузки браузер→R2 (cloud) или сигнал local-fallback.
  // Большие видео через ОДИН долгий POST на Modal web рвались (truncated multipart → 400 + CORS).
  // В облаке браузер PUT'ит файл ПРЯМО в R2 (Cloudflare edge, надёжно), минуя Modal web-функцию.
  const initRes = await fetch(`${BASE}/jobs/upload-url`, {
    method: "POST",
    headers: { ...headers, "Content-Type": "application/json" },
    // size → воркер решает: один PUT (мелкие) или multipart-план (крупные, >100 МБ).
    body: JSON.stringify({ filename: file.name, max_clips: maxClips ?? null, size: file.size, clip_min_sec: clipMinSec ?? null, clip_max_sec: clipMaxSec ?? null }),
    signal,
  });
  if (initRes.status === 401) throw new Error("Sign in to create clips.");
  if (initRes.status === 402) {
    const b = (await initRes.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(b?.detail ?? "Monthly limit reached. Upgrade your plan on the pricing page.");
  }
  if (!initRes.ok) throw new Error(`upload init failed: ${initRes.status}`);
  const init = (await initRes.json()) as {
    id?: string;
    put_url?: string;
    local?: boolean;
    upload_id?: string;
    part_size?: number;
    parts?: { part_number: number; url: string }[];
  };

  // Multipart (большой файл): части грузятся ПАРАЛЛЕЛЬНО прямо в R2 (с таймаутом/watchdog/ретраями/
  // обновлением URL), затем upload-complete собирает объект из их ETag'ов.
  if (init.id && init.upload_id && init.parts && init.part_size) {
    const { id, upload_id, parts, part_size } = init;
    // Record the checkpoint up front so an interrupted transfer (crash / reload / offline) can resume.
    await saveCheckpoint(fingerprint, { job_id: id, upload_id, part_size, completed: [] });
    try {
      const completed = await uploadPartsResilient(file, parts, part_size, {
        total: file.size,
        preCompleted: [],
        jobId: id,
        uploadId: upload_id,
        headers,
        fingerprint,
        onProgress,
        signal,
        rand,
      });
      await completeMultipart(id, upload_id, completed, file, maxClips, language, headers, signal, clipMinSec, clipMaxSec);
      await clearCheckpoint(fingerprint); // fully uploaded → no resume state to keep
      return { id };
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") {
        // Deliberate cancel (reset / new submit / unmount) → clean up R2 + drop the checkpoint.
        void fetch(`${BASE}/jobs/${id}/upload-abort`, {
          method: "POST",
          headers: { ...headers, "Content-Type": "application/json" },
          body: JSON.stringify({ upload_id }),
          keepalive: true,
        }).catch(() => {});
        void clearCheckpoint(fingerprint);
        throw e;
      }
      // Terminal transfer error after retries → KEEP the uploaded parts + checkpoint so the user can
      // Resume (do NOT abort the multipart — that would delete the parts). Surface the error (rule #8).
      throw e;
    }
  }

  if (init.put_url && init.id) {
    // 2. PUT файла ПРЯМО в R2 (XHR ради progress+abort). Content-Type в presigned URL не подписан
    //    → можно слать любой; ставим тип файла (R2 хранит). Нужен CORS на бакете (фаундер в дашборде).
    const put = await xhrRequest("PUT", init.put_url, file, {
      headers: { "Content-Type": file.type || "video/mp4" },
      onProgress,
      signal,
    });
    if (put.status < 200 || put.status >= 300) {
      throw new Error(`Upload to storage failed: ${put.status}`);
    }
    // 3. Финализация → воркер создаёт джоб + спавнит обработку.
    const done = await fetch(`${BASE}/jobs/${init.id}/upload-complete`, {
      method: "POST",
      headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify({ filename: file.name, max_clips: maxClips ?? null, language: language ?? null, clip_min_sec: clipMinSec ?? null, clip_max_sec: clipMaxSec ?? null }),
      signal,
    });
    if (!done.ok) throw new Error(`Couldn’t start processing: ${done.status}`);
    return { id: init.id };
  }

  // Local dev (нет R2): стримим через воркер — на localhost ок (старый multipart-путь).
  const form = new FormData();
  form.append("file", file);
  if (maxClips != null) form.append("max_clips", String(maxClips));
  if (language != null) form.append("language", language);
  if (clipMinSec != null) form.append("clip_min_sec", String(clipMinSec));
  if (clipMaxSec != null) form.append("clip_max_sec", String(clipMaxSec));
  const res = await xhrRequest("POST", `${BASE}/jobs/upload`, form, { headers, onProgress, signal });
  if (res.status === 401) throw new Error("Sign in to create clips.");
  if (res.status === 402) {
    let detail: string | undefined;
    try {
      detail = (JSON.parse(res.responseText) as { detail?: string }).detail;
    } catch {
      detail = undefined;
    }
    throw new Error(detail ?? "Monthly limit reached. Upgrade your plan on the pricing page.");
  }
  if (res.status < 200 || res.status >= 300) {
    throw new Error(`createUploadJob failed: ${res.status}`);
  }
  try {
    return JSON.parse(res.responseText) as { id: string };
  } catch {
    throw new Error("createUploadJob: invalid server response");
  }
}

/** Живой расход (план + остаток кредитов) для UsageMeter. Бросает при недоступности —
 *  UsageMeter откатывается на дефолт free (dual-mode без воркера/auth). */
export async function getUsage(): Promise<UsageInfo> {
  const res = await fetch(`${BASE}/usage`, { cache: "no-store", headers: await authHeaders() });
  if (!res.ok) throw new Error(`getUsage failed: ${res.status}`);
  return res.json();
}

/** fetch с таймаутом: висящий ответ (TCP открыт, воркер молчит) иначе НИКОГДА не реджектит →
 *  поллинг useJob тихо стопорится, MAX_FAILS не растёт, юзер на «tracking» навсегда.
 *  Таймаут → AbortError → throw → засчитывается как сбой опроса. */
async function fetchWithTimeout(input: string, init: RequestInit = {}, ms = 15000): Promise<Response> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ms);
  try {
    return await fetch(input, { ...init, signal: ctrl.signal });
  } finally {
    clearTimeout(timer);
  }
}

export async function getJob(id: string): Promise<Job> {
  const res = await fetchWithTimeout(`${BASE}/jobs/${id}`, {
    cache: "no-store",
    headers: await authHeaders(),
  });
  // IDOR surface: the dashboard deep-links jobs by id (?job=). The worker's ownership gate returns
  // 403 for someone else's job → show a clean message (mirrors applyStyleToAll) instead of a raw
  // "getJob failed: 403". 401 → not signed in. (useJob surfaces these via the error panel.)
  if (res.status === 403) throw new Error("This isn’t your project");
  if (res.status === 401) throw new Error("Sign in to view this project.");
  if (!res.ok) throw new Error(`getJob failed: ${res.status}`);
  return res.json();
}

/** Stop-кнопка: отменить джоб во FREE-фазе (до транскрипции). 409 = уже вошёл в платную
 *  стадию → дружелюбная ошибка. Идемпотентно (done/failed/cancelled → cancelled:false). */
export async function cancelJob(id: string): Promise<{ status: string; cancelled: boolean }> {
  const res = await fetch(`${BASE}/jobs/${id}/cancel`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: "{}",
  });
  if (res.status === 409) throw new Error("This video is already processing and can’t be stopped.");
  if (!res.ok) throw new Error(`cancelJob failed: ${res.status}`);
  return res.json();
}

export async function getTimeline(jobId: string): Promise<TimelineData> {
  const res = await fetch(`${BASE}/jobs/${jobId}/timeline`, {
    cache: "no-store",
    headers: await authHeaders(),
  });
  if (!res.ok) throw new Error(`getTimeline failed: ${res.status}`);
  return res.json();
}

export async function getChapters(jobId: string, retry = false): Promise<ChaptersData> {
  // AI-карта видео (главы). Первый вызов стартует генерацию (status=pending) → поллить.
  // retry=true перезапускает генерацию, если предыдущая упала (квота Gemini free-tier).
  const url = `${BASE}/jobs/${jobId}/chapters${retry ? "?retry=true" : ""}`;
  const res = await fetch(url, { cache: "no-store", headers: await authHeaders() });
  if (!res.ok) throw new Error(`getChapters failed: ${res.status}`);
  return res.json();
}

export async function getVideoMap(jobId: string, retry = false): Promise<VideoMap> {
  // Карта видео (нарратив + главы + цветные моменты). Первый вызов стартует генерацию
  // (status=pending) → поллить. retry=true перезапускает упавшую генерацию.
  const url = `${BASE}/jobs/${jobId}/video-map${retry ? "?retry=true" : ""}`;
  const res = await fetch(url, { cache: "no-store", headers: await authHeaders() });
  if (!res.ok) throw new Error(`getVideoMap failed: ${res.status}`);
  return res.json();
}

export type PreviewMoment = {
  t: number;
  kind: "question" | "stat" | "emphasis" | "beat";
  intensity: number;
  text?: string;
};

/** Cosmetic co-watch markers shown DURING processing (Part 4). ⚠️ Never influence clip
 *  selection — purely the "the AI is reading your video" visual. Empty until the worker has
 *  detected any (and [] on local dev / old jobs). fetchWithTimeout so a hung poll can't stick. */
export async function getPreviewMoments(jobId: string): Promise<PreviewMoment[]> {
  const res = await fetchWithTimeout(`${BASE}/jobs/${jobId}/preview-moments`, {
    cache: "no-store",
    headers: await authHeaders(),
  });
  if (!res.ok) throw new Error(`getPreviewMoments failed: ${res.status}`);
  const data = (await res.json()) as { moments?: PreviewMoment[] };
  return data.moments ?? [];
}

export async function setCropOverride(
  jobId: string,
  clipId: string,
  version: number,
  body: {
    source_start: number;
    source_end: number;
    mode: "fill" | "fit" | "auto";
    center?: number | null;
    center_b?: number | null;
    fit_style?: string | null;
  },
): Promise<ClipEdit> {
  const res = await fetch(`${BASE}/jobs/${jobId}/clips/${clipId}/edit/crop`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify({ version, ...body }),
  });
  if (res.status === 409) throw new Error("Edit conflict — reload and retry");
  if (!res.ok) throw new Error(`setCropOverride failed: ${res.status}`);
  return res.json();
}

export type Aspect = "9:16" | "1:1" | "4:5" | "16:9";

export async function setClipAspect(
  jobId: string,
  clipId: string,
  version: number,
  aspect: Aspect,
): Promise<ClipEdit> {
  // T5: сменить соотношение сторон клипа (выход + reframe-кроп; кадровая сетка не трогается).
  const res = await fetch(`${BASE}/jobs/${jobId}/clips/${clipId}/edit/aspect`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify({ version, aspect }),
  });
  if (res.status === 409) throw new Error("Edit conflict — reload and retry");
  if (!res.ok) throw new Error(`setClipAspect failed: ${res.status}`);
  return res.json();
}

export async function setClipMotion(
  jobId: string,
  clipId: string,
  version: number,
  lockPerShot: boolean,
): Promise<ClipEdit> {
  // T5-motion: toggle camera motion (Lock = static crop per shot; Follow = camera pans).
  // Render-required: changes pixels identically to aspect — flush + dirty + reload reframe.
  const res = await fetch(`${BASE}/jobs/${jobId}/clips/${clipId}/edit/motion`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify({ version, lock_per_shot: lockPerShot }),
  });
  if (res.status === 409) throw new Error("Edit conflict — reload and retry");
  if (!res.ok) throw new Error(`setClipMotion failed: ${res.status}`);
  return res.json();
}

export async function restoreEdit(
  jobId: string,
  clipId: string,
  version: number,
  content: Omit<ClipEdit, "id" | "version">,
): Promise<ClipEdit> {
  // Undo/redo: atomically replace the whole edit CONTENT under an optimistic version lock.
  // PUT carries a HISTORICAL snapshot forward (409 on version mismatch; the backend bumps the
  // version on success — undo/redo are forward writes, the old version number is never restored).
  // Mirrors setClipAspect/setClipMotion's fetch/error/return shape, but method PUT.
  const { source_intervals, captions, reframe_overrides, aspect, lock_per_shot } = content;
  const res = await fetch(`${BASE}/jobs/${jobId}/clips/${clipId}/edit/restore`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify({
      version,
      source_intervals,
      captions,
      reframe_overrides,
      aspect,
      lock_per_shot,
    }),
  });
  if (res.status === 409) throw new Error("Edit conflict — reload and retry");
  if (!res.ok) throw new Error(`restoreEdit failed: ${res.status}`);
  return res.json();
}

export async function setClipInterval(
  jobId: string,
  clipId: string,
  version: number,
  sourceStart: number,
  sourceEnd: number,
): Promise<ClipEdit> {
  const res = await fetch(`${BASE}/jobs/${jobId}/clips/${clipId}/edit/set-interval`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify({ version, source_start: sourceStart, source_end: sourceEnd }),
  });
  if (res.status === 409) throw new Error("Edit conflict — reload and retry");
  if (!res.ok) throw new Error(`setClipInterval failed: ${res.status}`);
  return res.json();
}

/** P3: persist a manual shot-cut edit. `action:"split"` adds a boundary at `sourceTime` (a cut the
 *  AI missed); `action:"merge"` drops the AI cut nearest `sourceTime`, joining the two shots.
 *  ("unsplit"/"unmerge" undo those — the contract's four actions; the editor currently sends
 *  split/merge.) `sourceTime` is in SOURCE seconds (frame-snapped server-side). Returns the new
 *  ClipEdit (with updated `manual_cuts`/`suppressed_cuts`). Mirrors setCropOverride/setClipMotion. */
export async function editCut(
  jobId: string,
  clipId: string,
  version: number,
  sourceTime: number,
  action: "split" | "merge" | "unsplit" | "unmerge",
): Promise<ClipEdit> {
  const res = await fetch(`${BASE}/jobs/${jobId}/clips/${clipId}/edit/cut`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify({ version, source_time: sourceTime, action }),
  });
  if (res.status === 409) throw new Error("Edit conflict — reload and retry");
  if (!res.ok) throw new Error(`editCut failed: ${res.status}`);
  return res.json();
}

export async function regenerateHook(
  jobId: string,
  clipId: string,
  version: number,
): Promise<ClipEdit> {
  // W4: перегенерировать текст хука под текущий интервал (узкий Gemini-вызов, не чат).
  // Меняет только hook.text; стиль/позицию не трогает. Бампает версию → flush до вызова.
  const res = await fetch(`${BASE}/jobs/${jobId}/clips/${clipId}/hook/regenerate`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify({ version }),
  });
  if (res.status === 409) throw new Error("Edit conflict — reload and retry");
  if (!res.ok) throw new Error(`regenerateHook failed: ${res.status}`);
  return res.json();
}

// ── W3: агентный чат-редактор клипа ──
export async function startAgentRun(
  jobId: string,
  clipId: string,
  message: string,
): Promise<AgentRun> {
  const res = await fetch(`${BASE}/jobs/${jobId}/clips/${clipId}/agent/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify({ message }),
  });
  if (!res.ok) throw new Error(`startAgentRun failed: ${res.status}`);
  return res.json();
}

export async function getAgentRun(
  jobId: string,
  clipId: string,
  runId: string,
): Promise<AgentRun> {
  const res = await fetch(`${BASE}/jobs/${jobId}/clips/${clipId}/agent/${runId}`, {
    cache: "no-store",
    headers: await authHeaders(),
  });
  if (!res.ok) throw new Error(`getAgentRun failed: ${res.status}`);
  return res.json();
}

export async function getActiveAgentRun(
  jobId: string,
  clipId: string,
): Promise<AgentRun | null> {
  const res = await fetch(`${BASE}/jobs/${jobId}/clips/${clipId}/agent/active`, {
    cache: "no-store",
    headers: await authHeaders(),
  });
  if (!res.ok) throw new Error(`getActiveAgentRun failed: ${res.status}`);
  return res.json();
}

export async function cancelAgentRun(
  jobId: string,
  clipId: string,
  runId: string,
): Promise<AgentRun> {
  const res = await fetch(`${BASE}/jobs/${jobId}/clips/${clipId}/agent/${runId}/cancel`, {
    method: "POST",
    headers: await authHeaders(),
  });
  if (!res.ok) throw new Error(`cancelAgentRun failed: ${res.status}`);
  return res.json();
}

export async function getClipAss(jobId: string, clipId: string): Promise<string> {
  // ASS субтитров текущего edit-state — для libass.wasm превью (тот же ASS, что жжёт ffmpeg).
  const res = await fetch(`${BASE}/jobs/${jobId}/clips/${clipId}/ass`, {
    cache: "no-store",
    headers: await authHeaders(),
  });
  if (!res.ok) throw new Error(`getClipAss failed: ${res.status}`);
  return res.text();
}

export interface ReframeRegion {
  t0: number;
  t1: number;
  mode: string;
  points: { t: number; mode?: string; cx: number | null }[];
  points_b?: { t: number; mode?: string; cx: number | null }[];
  fit_style?: string;
}

export async function getClipReframe(
  jobId: string,
  clipId: string,
): Promise<{ regions: ReframeRegion[] }> {
  // D2: reframe-план (fit/fill/split + центры) от ЕДИНОГО frame-accurate пути (как у рендера).
  // Заменяет прямой fetch media/<job>/reframe_<clip>.json — тот 404-ил на облаке (файл только
  // на scratch batch-контейнера) → превью откатывалось в центр-кроп ≠ рендер.
  const res = await fetch(`${BASE}/jobs/${jobId}/clips/${clipId}/reframe`, {
    cache: "no-store",
    headers: await authHeaders(),
  });
  if (!res.ok) throw new Error(`getClipReframe failed: ${res.status}`);
  return res.json();
}

export async function getClipEdit(jobId: string, clipId: string): Promise<ClipEdit> {
  const res = await fetch(`${BASE}/jobs/${jobId}/clips/${clipId}/edit`, {
    cache: "no-store",
    headers: await authHeaders(),
  });
  if (!res.ok) throw new Error(`getClipEdit failed: ${res.status}`);
  return res.json();
}

export async function getClipAnalysis(
  jobId: string,
  clipId: string,
): Promise<{ intervals: { source_start: number; source_end: number }[]; words: Word[] }> {
  const res = await fetch(`${BASE}/jobs/${jobId}/clips/${clipId}/analysis`, {
    cache: "no-store",
    headers: await authHeaders(),
  });
  if (!res.ok) throw new Error(`getClipAnalysis failed: ${res.status}`);
  return res.json();
}

export async function trimClip(
  jobId: string,
  clipId: string,
  version: number,
  wordIndices: number[],
): Promise<ClipEdit> {
  const res = await fetch(`${BASE}/jobs/${jobId}/clips/${clipId}/edit/trim`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify({ version, word_indices: wordIndices }),
  });
  if (res.status === 409) throw new Error("Edit conflict — reload and retry");
  if (!res.ok) throw new Error(`trimClip failed: ${res.status}`);
  return res.json();
}

export async function extendClip(
  jobId: string,
  clipId: string,
  version: number,
  edge: "start" | "end",
  newValue: number,
): Promise<ClipEdit> {
  const res = await fetch(`${BASE}/jobs/${jobId}/clips/${clipId}/edit/extend`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify({ version, edge, new_value: newValue }),
  });
  if (res.status === 409) throw new Error("Edit conflict — reload and retry");
  if (!res.ok) throw new Error(`extendClip failed: ${res.status}`);
  return res.json();
}

// ── Per-clip export endpoints (single source of truth for download URLs). All PURE. ──
// Shared by the single-clip ExportMenu and the grid's "Download all" so the URL contract lives
// in ONE place. `base` is the worker origin (NEXT_PUBLIC_WORKER_URL).

/** On-demand "clean" (no-caption) vertical MP4 — renders the CURRENT edit-state on the fly. PURE.
 *  NB: when a stable clean CDN render already exists (clip.video_url), prefer THAT (instant) over
 *  this re-render — this endpoint is the fallback when no baked clean URL is available. */
export function clipCleanExportUrl(base: string, jobId: string, clipId: string): string {
  return `${base}/jobs/${jobId}/clips/${clipId}/export/clean.mp4`;
}

/** Per-clip SRT subtitles (cheap text; timings match the clip). PURE. */
export function clipSrtUrl(base: string, jobId: string, clipId: string): string {
  return `${base}/jobs/${jobId}/clips/${clipId}/export.srt`;
}

/** On-demand captioned (burned-in subtitles) MP4 — renders the current edit-state on the fly. PURE. */
export function clipCaptionedExportUrl(base: string, jobId: string, clipId: string): string {
  return `${base}/jobs/${jobId}/clips/${clipId}/export/captioned.mp4`;
}

// Which URL the "With captions (MP4)" download should hit. PURE.
//
// `bakedUrl` is the stable CDN render produced by the last "Render" — fast, but it is a
// SNAPSHOT: as soon as the user edits again (e.g. changes the hook font) it is STALE. The
// editor does not clear `bakedUrl` on edit, so we must not trust it once `dirty`. When dirty
// (un-rendered edits pending) we fall back to the on-demand `export/captioned.mp4` endpoint,
// which synchronously burns the CURRENT persisted edit-state and streams it back (no CDN key,
// no stale cache). Not dirty → the baked render matches the edit-state → use it (fast path).
export function captionedDownloadUrl(
  base: string,
  jobId: string,
  clipId: string,
  bakedUrl: string | null,
  dirty: boolean,
): string {
  if (dirty || !bakedUrl) return clipCaptionedExportUrl(base, jobId, clipId);
  return bakedUrl;
}

export async function startRenderClip(jobId: string, clipId: string): Promise<{ status: string }> {
  const res = await fetch(`${BASE}/jobs/${jobId}/clips/${clipId}/render`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: "{}",
  });
  if (!res.ok) throw new Error(`startRenderClip failed: ${res.status}`);
  return res.json();
}

export async function getRenderStatus(
  jobId: string,
  clipId: string,
): Promise<{ status: string; video_url: string | null; error: string | null }> {
  // fetchWithTimeout (не голый fetch): зависший статус-ответ иначе держал бы редактор на
  // «Rendering…» вечно (poll-цикл не получал бы ни ok, ни ошибки).
  const res = await fetchWithTimeout(`${BASE}/jobs/${jobId}/clips/${clipId}/render`, {
    cache: "no-store",
    headers: await authHeaders(),
  });
  if (!res.ok) throw new Error(`getRenderStatus failed: ${res.status}`);
  return res.json();
}

export async function patchClipEdit(
  jobId: string,
  clipId: string,
  version: number,
  captions: CaptionTrack,
): Promise<ClipEdit> {
  const res = await fetch(`${BASE}/jobs/${jobId}/clips/${clipId}/edit`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify({ version, captions }),
  });
  if (res.status === 409) throw new Error("Edit conflict — reload and retry");
  if (!res.ok) throw new Error(`patchClipEdit failed: ${res.status}`);
  return res.json();
}

/**
 * Best-effort persist of caption edits when the page is going away (pagehide /
 * visibilitychange-hidden / unmount). Uses `keepalive` so the request outlives the
 * document — a normal fetch would be cancelled on unload, silently losing edits.
 * Fire-and-forget: no body parsing, swallow errors (the page is leaving). This is
 * the durability backstop so a long edit session is never lost on navigate/close.
 */
export function patchClipEditKeepalive(
  jobId: string,
  clipId: string,
  version: number,
  captions: CaptionTrack,
): void {
  try {
    // Kept SYNC (returns void) so the fire-and-forget call site stays a plain statement. The token
    // is resolved in a `.then` rather than `await` — one microtask later than a purely synchronous
    // keepalive, but getSession resolves from the CACHED session (no network for a valid token), so
    // the request is still initiated within the same unload task's microtask checkpoint, before the
    // page is discarded. Without the header the worker rejects the PATCH (401), so an authed
    // best-effort save beats an unauthed one that's dropped anyway. (Awkwardness noted: if this ever
    // proves unreliable on unload, thread a pre-fetched token in from the editor instead.)
    void authHeaders().then((headers) => {
      void fetch(`${BASE}/jobs/${jobId}/clips/${clipId}/edit`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", ...headers },
        body: JSON.stringify({ version, captions }),
        keepalive: true,
      });
    });
  } catch {
    /* page is leaving — nothing we can do */
  }
}

export async function getPresets(): Promise<CaptionPreset[]> {
  const res = await fetch(`${BASE}/presets`, { cache: "no-store" });
  if (!res.ok) throw new Error(`getPresets failed: ${res.status}`);
  return res.json();
}

export async function applyPreset(
  jobId: string,
  clipId: string,
  version: number,
  presetId: string,
): Promise<ClipEdit> {
  const res = await fetch(`${BASE}/jobs/${jobId}/clips/${clipId}/apply-preset`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify({ version, preset_id: presetId }),
  });
  if (res.status === 409) throw new Error("Edit conflict — reload and retry");
  if (!res.ok) throw new Error(`applyPreset failed: ${res.status}`);
  return res.json();
}

// ── Style memory (domain 5): apply a look to all clips + per-user default style ──
export type StylePreferencePayload = {
  style: CaptionStyle;
  highlight: HighlightStyle | null;
  hook_style: Record<string, unknown> | null;
};

/** Apply the given look (caption style + highlight + hook style) to EVERY clip of the job. */
export async function applyStyleToAll(
  jobId: string,
  payload: StylePreferencePayload,
): Promise<{ applied: number; total: number }> {
  const res = await fetch(`${BASE}/jobs/${jobId}/apply-style-all`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify(payload),
  });
  if (res.status === 403) throw new Error("This isn’t your project");
  if (!res.ok) throw new Error(`applyStyleToAll failed: ${res.status}`);
  return res.json();
}

// ── Named style templates: save a look once, reuse on any clip / whole video ──
export type StyleTemplate = {
  id: string;
  name: string;
  look: StylePreferencePayload;
};
export type TemplatesResponse = { templates: StyleTemplate[]; default_id: string | null };

/** The user's saved style templates + which one (if any) seeds new clips of future videos. */
export async function listTemplates(): Promise<TemplatesResponse> {
  const res = await fetch(`${BASE}/me/templates`, {
    cache: "no-store",
    headers: { ...(await authHeaders()) },
  });
  if (!res.ok) throw new Error(`listTemplates failed: ${res.status}`);
  return res.json();
}

/** Save the current look as a NAMED template (optionally make it the new-clip default).
 *
 * Pass `id` to OVERWRITE an existing template's stored look with the current style instead of
 * creating a new one — the backend `style_prefs.upsert_template` replaces by id (the "Update"
 * action). Omit `id` to create a fresh template (the "Save current" action). When updating, also
 * send the template's existing `name` so the overwrite keeps it. */
export async function saveTemplate(
  payload: StylePreferencePayload & { name: string; set_default?: boolean; id?: string },
): Promise<{ template: StyleTemplate; default_id: string | null }> {
  const res = await fetch(`${BASE}/me/templates`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify(payload),
  });
  if (res.status === 401) throw new Error("Sign in to save a template");
  if (!res.ok) throw new Error(`saveTemplate failed: ${res.status}`);
  return res.json();
}

/** Delete a saved template. */
export async function deleteTemplate(templateId: string): Promise<void> {
  const res = await fetch(`${BASE}/me/templates/${templateId}`, {
    method: "DELETE",
    headers: { ...(await authHeaders()) },
  });
  if (!res.ok) throw new Error(`deleteTemplate failed: ${res.status}`);
}

/** Flag (or unflag) a template as the default that seeds new clips of future videos. */
export async function setDefaultTemplate(templateId: string, isDefault: boolean): Promise<void> {
  const res = await fetch(`${BASE}/me/templates/${templateId}/default`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify({ is_default: isDefault }),
  });
  if (!res.ok) throw new Error(`setDefaultTemplate failed: ${res.status}`);
}
