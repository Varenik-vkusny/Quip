import { describe, expect, it, vi } from "vitest";
import type { UsageInfo } from "./api";
import { deriveUsage, FREE_DEFAULT } from "./useUsage";
import {
  classifyUsageError,
  retryUsageFetch,
  USAGE_RETRY_DELAYS_MS,
  UsageHttpError,
} from "./usageRetry";

// ────────────────────────────────────────────────────────────────────────────
// PROD INCIDENT (2026-07-14): `quip-api` is scale-to-zero and containerConcurrency dropped 80 → 8,
// so cold starts are frequent. The FIRST /usage request after idle dies before a container exists;
// the browser surfaces it as a CORS error ("No 'Access-Control-Allow-Origin' header") and a rejected
// fetch (TypeError). A reload right after is clean — CORS is configured correctly.
//
// What made it a BILLING-trust bug: a failed /usage used to paint the FREE plan, so a paying
// Pro/Studio customer landing on a cold container was told they were on Free. This suite locks the
// contract that prevents it ever coming back:
//   • a transient failure (CORS/network/timeout/5xx) is RETRIED, then succeeds → the real plan;
//   • a total failure REJECTS → the caller shows an honest error, NEVER Free;
//   • a fatal answer (401) is NOT retried (retrying can't change it);
//   • a genuinely-Free user still renders as Free.
// ────────────────────────────────────────────────────────────────────────────

const PRO: UsageInfo = {
  plan: "pro",
  plan_name: "Pro",
  monthly_videos: 20,
  monthly_minutes: 1200,
  used_minutes: 120,
  remaining_minutes: 1080,
  remaining_videos: 18,
  payg_videos: 0,
  payg_minutes: 0,
};

/** Deps with timers/network stubbed out: sleep is a no-op (we assert the ladder VALUES, not real
 *  waits), the browser is online. Tests override `fetchOnce`. */
function deps<T>(fetchOnce: () => Promise<T>, over: Partial<Parameters<typeof retryUsageFetch<T>>[0]> = {}) {
  return { fetchOnce, sleep: async () => {}, isOnline: () => true, ...over };
}

describe("classifyUsageError", () => {
  it("treats the cold-start CORS failure (fetch rejecting with TypeError) as transient", () => {
    // This is EXACTLY the observed prod symptom: the browser can't see the blocked response, so
    // fetch() rejects with "Failed to fetch". It must be retried, not shown as a plan failure.
    expect(classifyUsageError(new TypeError("Failed to fetch"), true)).toBe("transient");
  });

  it("treats our fetch timeout (AbortError) as transient", () => {
    // A cold container can hold the socket open and say nothing → fetchWithTimeout aborts.
    expect(classifyUsageError(new DOMException("Aborted", "AbortError"), true)).toBe("transient");
  });

  it("treats 5xx / 408 / 429 as transient (container cold, crashed, or rate-limited)", () => {
    expect(classifyUsageError(new UsageHttpError(503), true)).toBe("transient");
    expect(classifyUsageError(new UsageHttpError(502), true)).toBe("transient");
    expect(classifyUsageError(new UsageHttpError(500), true)).toBe("transient");
    expect(classifyUsageError(new UsageHttpError(408), true)).toBe("transient");
    expect(classifyUsageError(new UsageHttpError(429), true)).toBe("transient");
  });

  it("treats any error as transient while the browser is offline", () => {
    expect(classifyUsageError(new UsageHttpError(500), false)).toBe("transient");
    expect(classifyUsageError(new TypeError("Failed to fetch"), false)).toBe("transient");
  });

  it("treats 401/403 and other 4xx as fatal — retrying cannot change the answer", () => {
    expect(classifyUsageError(new UsageHttpError(401), true)).toBe("fatal");
    expect(classifyUsageError(new UsageHttpError(403), true)).toBe("fatal");
    expect(classifyUsageError(new UsageHttpError(404), true)).toBe("fatal");
  });

  it("treats an unknown throw as fatal (never retry blindly on something we don't understand)", () => {
    expect(classifyUsageError(new Error("boom"), true)).toBe("fatal");
    expect(classifyUsageError("boom", true)).toBe("fatal");
  });
});

describe("retryUsageFetch", () => {
  it("retries a transient cold-start failure and then returns the REAL plan", async () => {
    // The exact prod sequence: request #1 dies on the cold container, request #2 (warm) succeeds.
    const fetchOnce = vi
      .fn<() => Promise<UsageInfo>>()
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(PRO);

    const usage = await retryUsageFetch(deps(fetchOnce));

    expect(fetchOnce).toHaveBeenCalledTimes(2);
    expect(usage.plan).toBe("pro"); // ← a Pro customer sees Pro, not Free
  });

  it("survives a run of transient failures within the ladder (4 attempts total)", async () => {
    const fetchOnce = vi
      .fn<() => Promise<UsageInfo>>()
      .mockRejectedValueOnce(new UsageHttpError(503))
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockRejectedValueOnce(new DOMException("Aborted", "AbortError"))
      .mockResolvedValueOnce(PRO);

    await expect(retryUsageFetch(deps(fetchOnce))).resolves.toEqual(PRO);
    expect(fetchOnce).toHaveBeenCalledTimes(1 + USAGE_RETRY_DELAYS_MS.length);
  });

  it("waits out the useJob backoff ladder (2.5s → 5s → 10s) between attempts", async () => {
    const waits: number[] = [];
    const fetchOnce = vi.fn<() => Promise<UsageInfo>>().mockRejectedValue(new UsageHttpError(503));

    await expect(
      retryUsageFetch(deps(fetchOnce, { sleep: async (ms: number) => void waits.push(ms) })),
    ).rejects.toBeInstanceOf(UsageHttpError);

    expect(waits).toEqual([2_500, 5_000, 10_000]); // one wait BEFORE each retry, none after the last
  });

  it("a TOTAL failure rejects — the caller must show an honest error, NOT Free", async () => {
    const fetchOnce = vi
      .fn<() => Promise<UsageInfo>>()
      .mockRejectedValue(new TypeError("Failed to fetch"));

    // The whole point: no value is ever invented. There is nothing to render a plan from.
    await expect(retryUsageFetch(deps(fetchOnce))).rejects.toBeInstanceOf(TypeError);
    expect(fetchOnce).toHaveBeenCalledTimes(1 + USAGE_RETRY_DELAYS_MS.length);
  });

  it("does NOT retry a fatal 401 — it fails fast with the real error", async () => {
    const fetchOnce = vi.fn<() => Promise<UsageInfo>>().mockRejectedValue(new UsageHttpError(401));

    await expect(retryUsageFetch(deps(fetchOnce))).rejects.toMatchObject({ status: 401 });
    expect(fetchOnce).toHaveBeenCalledTimes(1); // no pointless hammering
  });

  it("stops retrying once cancelled (unmount / superseded reload) — no background storm", async () => {
    let cancelled = false;
    const fetchOnce = vi.fn<() => Promise<UsageInfo>>().mockRejectedValue(new UsageHttpError(503));

    await expect(
      retryUsageFetch(
        deps(fetchOnce, {
          isCancelled: () => cancelled,
          sleep: async () => {
            cancelled = true; // the component unmounted while we were backing off
          },
        }),
      ),
    ).rejects.toBeInstanceOf(UsageHttpError);

    expect(fetchOnce).toHaveBeenCalledTimes(1); // first failure → backoff → cancelled → stop
  });

  it("reports each retry so the UI can say 'taking longer than usual'", async () => {
    const onRetry = vi.fn();
    const fetchOnce = vi
      .fn<() => Promise<UsageInfo>>()
      .mockRejectedValueOnce(new UsageHttpError(503))
      .mockResolvedValueOnce(PRO);

    await retryUsageFetch(deps(fetchOnce, { onRetry }));

    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(onRetry).toHaveBeenCalledWith(expect.objectContaining({ attempt: 1, delayMs: 2_500 }));
  });

  it("a genuinely-Free user still renders as Free (the fix must not break the real Free plan)", async () => {
    const fetchOnce = vi.fn<() => Promise<UsageInfo>>().mockResolvedValue(FREE_DEFAULT);

    const usage = await retryUsageFetch(deps(fetchOnce));
    const view = deriveUsage(usage);

    expect(fetchOnce).toHaveBeenCalledTimes(1); // success → no retries
    expect(view.planId).toBe("free");
    expect(view.planName).toBe("Free");
    expect(view.videosLeft).toBe(1); // free = 1 video / 60 min
  });
});
