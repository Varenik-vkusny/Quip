import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getClipReframe, reframeInFlightCount } from "./api";

// ────────────────────────────────────────────────────────────────────────────
// PROD INCIDENT (2026-07-14): GET /jobs/{job}/clips/{clip}/reframe runs heavy CV on the server
// (up to ~3 min, GBs of RAM). The editor fired the SAME clip's /reframe 3–4× CONCURRENTLY
// (load effect + neighbor prefetch + cache-hit revalidation) — each request kicked off its OWN
// computation → the API container got OOM-killed.
//
// This suite locks the API-layer contract that fixes it:
//   • concurrent identical calls share ONE promise (in-flight dedupe) → one fetch, one CV job;
//   • 202 → {status:"computing"} (⚠️ res.ok is TRUE for 202 — a naive client parsed it as
//     success, got regions === undefined and silently painted a FAKE shot strip);
//   • 204 → {status:"absent"} (compute=0 only: nothing cached, server deliberately didn't compute);
//   • compute:false sends ?compute=0 (a clip the user never opened must NEVER trigger a compute);
//   • a real error (503) throws — no silent fallback.
// ────────────────────────────────────────────────────────────────────────────

const JOB = "job_1";
const CLIP = "clip_01";
const REGIONS = [{ t0: 0, t1: 2, mode: "fill", points: [{ t: 0, cx: 0.5 }] }];

/** A Response stub good enough for the fetch shapes getClipReframe reads (status + json). */
function res(status: number, body?: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

/** Deferred promise so a fetch can be held OPEN while a second caller arrives (the stampede). */
function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((r, j) => {
    resolve = r;
    reject = j;
  });
  return { promise, resolve, reject };
}

/** getClipReframe registers the shared promise SYNCHRONOUSLY, but reaches `fetch` only after
 *  `await authHeaders()` → flush the microtask queue before asserting on the spy. */
const flush = () => new Promise((r) => setTimeout(r, 0));

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  expect(reframeInFlightCount()).toBe(0); // every settled request must free its map entry
});

describe("getClipReframe — status discrimination (no silent fallbacks)", () => {
  it("200 with regions → ready", async () => {
    fetchMock.mockResolvedValue(res(200, { regions: REGIONS }));
    const r = await getClipReframe(JOB, CLIP);
    expect(r).toEqual({ status: "ready", regions: REGIONS });
  });

  it("202 → computing (THE TRAP: res.ok is true, but there are NO regions to paint)", async () => {
    fetchMock.mockResolvedValue(res(202, { status: "computing" }));
    const r = await getClipReframe(JOB, CLIP);
    expect(r).toEqual({ status: "computing" });
  });

  it("204 → absent (compute=0: nothing cached, server deliberately did not compute)", async () => {
    fetchMock.mockResolvedValue(res(204));
    const r = await getClipReframe(JOB, CLIP, { compute: false });
    expect(r).toEqual({ status: "absent" });
  });

  it("503 → throws (computation genuinely failed; caller shows an honest error + Retry)", async () => {
    fetchMock.mockResolvedValue(res(503, {}));
    await expect(getClipReframe(JOB, CLIP)).rejects.toThrow("getClipReframe failed: 503");
  });

  it("200 WITHOUT regions → throws (broken contract is not an empty plan)", async () => {
    fetchMock.mockResolvedValue(res(200, {}));
    await expect(getClipReframe(JOB, CLIP)).rejects.toThrow("200 without regions");
  });
});

describe("getClipReframe — compute flag", () => {
  it("defaults to compute (no ?compute=0 on the URL)", async () => {
    fetchMock.mockResolvedValue(res(200, { regions: REGIONS }));
    await getClipReframe(JOB, CLIP);
    expect(String(fetchMock.mock.calls[0][0])).not.toContain("compute=0");
  });

  it("compute:false sends ?compute=0 (neighbor prefetch must NEVER start a server computation)", async () => {
    fetchMock.mockResolvedValue(res(204));
    await getClipReframe(JOB, CLIP, { compute: false });
    expect(String(fetchMock.mock.calls[0][0])).toContain(
      `/jobs/${JOB}/clips/${CLIP}/reframe?compute=0`,
    );
  });

  it("passes the AbortSignal through to fetch (a clip switch must cancel the server work)", async () => {
    fetchMock.mockResolvedValue(res(200, { regions: REGIONS }));
    const ctl = new AbortController();
    await getClipReframe(JOB, CLIP, { signal: ctl.signal });
    expect(fetchMock.mock.calls[0][1].signal).toBe(ctl.signal);
  });
});

describe("getClipReframe — in-flight dedupe (this is what killed the API container)", () => {
  it("concurrent identical calls share ONE promise and ONE fetch", async () => {
    const d = deferred<Response>();
    fetchMock.mockReturnValue(d.promise);

    const p1 = getClipReframe(JOB, CLIP);
    const p2 = getClipReframe(JOB, CLIP);
    const p3 = getClipReframe(JOB, CLIP);
    expect(p1).toBe(p2); // literally the same promise object
    expect(p1).toBe(p3);
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(1); // 3 callers → 1 server-side CV job (was 3–4)

    d.resolve(res(200, { regions: REGIONS }));
    expect(await Promise.all([p1, p2, p3])).toEqual([
      { status: "ready", regions: REGIONS },
      { status: "ready", regions: REGIONS },
      { status: "ready", regions: REGIONS },
    ]);
  });

  it("different clips / different compute flags are NOT deduped together", async () => {
    const d = deferred<Response>();
    fetchMock.mockReturnValue(d.promise);

    const a = getClipReframe(JOB, CLIP);
    const b = getClipReframe(JOB, "clip_02"); // other clip
    const c = getClipReframe(JOB, CLIP, { compute: false }); // same clip, cache-only read
    expect(a).not.toBe(b);
    expect(a).not.toBe(c);
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(3);

    d.resolve(res(200, { regions: REGIONS }));
    await Promise.all([a, b, c]);
  });

  it("the entry is released once settled → a later call re-fetches (poll / retry still work)", async () => {
    fetchMock.mockResolvedValue(res(202, {}));
    const first = await getClipReframe(JOB, CLIP);
    expect(first).toEqual({ status: "computing" });

    fetchMock.mockResolvedValue(res(200, { regions: REGIONS }));
    const second = await getClipReframe(JOB, CLIP); // the poll tick
    expect(second).toEqual({ status: "ready", regions: REGIONS });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("aborting frees the slot SYNCHRONOUSLY → the very next call re-fetches, it does not join the dying request", async () => {
    // THE RACE: abort rejects the promise asynchronously and `.finally` clears the map even later.
    // A caller that arrives right after `.abort()` (clip switch → reload; React StrictMode remount)
    // must NOT be handed the already-aborted promise — it would surface AbortError as a genuine
    // "computation failed" and paint an error the user never caused.
    const d = deferred<Response>();
    fetchMock.mockReturnValue(d.promise);

    const ctl = new AbortController();
    const first = getClipReframe(JOB, CLIP, { signal: ctl.signal });
    await flush();
    expect(reframeInFlightCount()).toBe(1);

    ctl.abort();
    expect(reframeInFlightCount()).toBe(0); // freed synchronously, inside .abort()

    const second = getClipReframe(JOB, CLIP); // the reload right behind the abort
    expect(second).not.toBe(first); // a FRESH request, not the dying one
    d.resolve(res(200, { regions: REGIONS })); // our fetch stub ignores the signal
    await expect(second).resolves.toEqual({ status: "ready", regions: REGIONS });
    await first; // stub-resolved as well → nothing left dangling
  });

  it("a REJECTED shared request is released too (Retry after a 503 must reach the server)", async () => {
    fetchMock.mockResolvedValue(res(503, {}));
    const p1 = getClipReframe(JOB, CLIP);
    const p2 = getClipReframe(JOB, CLIP);
    expect(p1).toBe(p2);
    await expect(p1).rejects.toThrow();
    await expect(p2).rejects.toThrow();

    fetchMock.mockResolvedValue(res(200, { regions: REGIONS }));
    await expect(getClipReframe(JOB, CLIP)).resolves.toEqual({
      status: "ready",
      regions: REGIONS,
    });
    expect(fetchMock).toHaveBeenCalledTimes(2); // 1 shared failure + 1 retry
  });
});
