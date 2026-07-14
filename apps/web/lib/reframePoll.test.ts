import { describe, expect, it, vi } from "vitest";
import type { ReframeResult } from "./api";
import {
  REFRAME_DEADLINE_MS,
  REFRAME_HIDDEN_POLL_MS,
  REFRAME_MAX_ATTEMPTS,
  REFRAME_POLL_MID_MS,
  REFRAME_POLL_MS,
  REFRAME_POLL_SLOW_MS,
  classifyReframeError,
  nextReframePoll,
  reframePollDelayMs,
  runReframePoll,
  startReframePoll,
} from "./reframePoll";

// ────────────────────────────────────────────────────────────────────────────
// THE BUG THIS SUITE LOCKS (adversarial review, 2026-07-14): the editor's 202 "computing" poll of
// GET /reframe had NO attempt cap, NO deadline and NO terminal state. A bake that genuinely fails
// server-side keeps answering 202 forever →
//   • the shot strip is stuck on "Detecting shots…" FOREVER;
//   • the honest failure UI (FitTimeline `failed` + Retry) is UNREACHABLE (202 isn't an error);
//   • the server's re-spawn guard has a 300 s TTL, so every open tab orders a fresh 8-vCPU /
//     32-GiB Cloud Run Job every 5 minutes — forever, on one bad clip.
//
// Invariants below: the poll ALWAYS terminates, a 503 is terminal on the FIRST response, Retry
// starts a fresh budget, and no GET is ever issued at/after the server's 300 s re-spawn TTL.
// ────────────────────────────────────────────────────────────────────────────

const REGIONS = [{ t0: 0, t1: 2, mode: "fill", points: [{ t: 0, cx: 0.5 }] }];
const ready: ReframeResult = { status: "ready", regions: REGIONS };
const computing: ReframeResult = { status: "computing" };

/** Real microtask/timer flush — the loop awaits fetchPlan between scheduled ticks. */
const flush = () => new Promise((r) => setTimeout(r, 0));

/** Fake clock + fake scheduler: the whole loop runs in virtual time, deterministically. */
function makeClock() {
  let t = 0;
  let seq = 0;
  let queue: { id: number; at: number; fn: () => void }[] = [];
  return {
    now: () => t,
    schedule: (fn: () => void, ms: number) => {
      const id = ++seq;
      queue.push({ id, at: t + ms, fn });
      return id;
    },
    cancel: (h: unknown) => {
      queue = queue.filter((q) => q.id !== h);
    },
    pending: () => queue.length,
    /** Run every timer due within `ms`, flushing the loop's awaits after each tick. */
    async advance(ms: number) {
      const target = t + ms;
      for (;;) {
        const due = queue.filter((q) => q.at <= target).sort((a, b) => a.at - b.at)[0];
        if (!due) break;
        queue = queue.filter((q) => q !== due);
        t = due.at;
        due.fn();
        await flush();
      }
      t = target;
    },
  };
}

describe("nextReframePoll — the budget (cap + deadline)", () => {
  it("backs off 2.5s → 5s → 10s (the useJob ladder), instead of hammering 2.5s forever", () => {
    expect(reframePollDelayMs(0)).toBe(REFRAME_POLL_MS);
    expect(reframePollDelayMs(29_999)).toBe(REFRAME_POLL_MS);
    expect(reframePollDelayMs(30_000)).toBe(REFRAME_POLL_MID_MS);
    expect(reframePollDelayMs(89_999)).toBe(REFRAME_POLL_MID_MS);
    expect(reframePollDelayMs(90_000)).toBe(REFRAME_POLL_SLOW_MS);
    expect(reframePollDelayMs(250_000)).toBe(REFRAME_POLL_SLOW_MS);
  });

  it("a hidden tab polls on the slow cadence at ANY elapsed time", () => {
    expect(reframePollDelayMs(0, true)).toBe(REFRAME_HIDDEN_POLL_MS);
    expect(reframePollDelayMs(120_000, true)).toBe(REFRAME_HIDDEN_POLL_MS);
  });

  it("gives up on the DEADLINE — and never issues a GET at/after the 300s re-spawn TTL", () => {
    const budget = startReframePoll(0);
    // 289 s + a 10 s tick = 299 s → still inside the TTL, so this GET is allowed.
    const ok = nextReframePoll(budget, { now: 289_000 });
    expect(ok).toEqual({ kind: "poll", delayMs: 10_000, budget: { startedAt: 0, attempts: 1 } });
    // 291 s + a 10 s tick = 301 s → that GET would land AFTER the guard expired and order a
    // SECOND Cloud Run Job. We stop instead.
    expect(nextReframePoll(budget, { now: 291_000 })).toEqual({
      kind: "give-up",
      reason: "deadline",
    });
    expect(nextReframePoll(budget, { now: REFRAME_DEADLINE_MS })).toEqual({
      kind: "give-up",
      reason: "deadline",
    });
  });

  it("gives up on the ATTEMPT cap even if the wall clock never moves (belt to the deadline's braces)", () => {
    const frozen = { startedAt: 0, attempts: REFRAME_MAX_ATTEMPTS };
    expect(nextReframePoll(frozen, { now: 0 })).toEqual({ kind: "give-up", reason: "attempts" });
  });

  it("startReframePoll resets both counters (this is what Retry leans on)", () => {
    expect(startReframePoll(1_234)).toEqual({ startedAt: 1_234, attempts: 0 });
  });
});

describe("classifyReframeError", () => {
  it("503 (bake genuinely failed) is TERMINAL — no silent fallback, no endless retry", () => {
    expect(classifyReframeError(new Error("getClipReframe failed: 503"))).toBe("fail");
  });

  it("AbortError (another consumer cancelled the SHARED request) is retryable, not a failure", () => {
    const e = new Error("aborted");
    e.name = "AbortError";
    expect(classifyReframeError(e)).toBe("retry");
  });
});

describe("runReframePoll — the loop", () => {
  it("202 forever → the poll STOPS at the deadline and lands in the honest failure state", async () => {
    const clock = makeClock();
    const fetchPlan = vi.fn(async (): Promise<ReframeResult> => computing);
    const onReady = vi.fn();
    const onFailed = vi.fn();

    runReframePoll({
      fetchPlan,
      onReady,
      onFailed,
      alive: () => true,
      now: clock.now,
      schedule: clock.schedule,
      cancel: clock.cancel,
    });
    await flush();

    // Drive an HOUR of virtual time: the old code would still be polling (and re-spawning a Cloud
    // Run Job every 5 min). The capped loop must be long dead.
    await clock.advance(60 * 60_000);

    expect(onFailed).toHaveBeenCalledTimes(1);
    expect(onFailed.mock.calls[0][0]).toBe("timeout");
    expect(onReady).not.toHaveBeenCalled();
    expect(clock.pending()).toBe(0); // no timer left behind

    // Bounded work: ≈ 12×2.5s + 12×5s + ~21×10s ≈ 45 GETs, and NONE after the 300s TTL.
    expect(fetchPlan.mock.calls.length).toBeLessThanOrEqual(REFRAME_MAX_ATTEMPTS + 1);
    expect(fetchPlan.mock.calls.length).toBeGreaterThan(10);

    const before = fetchPlan.mock.calls.length;
    await clock.advance(60 * 60_000); // another hour — still nothing
    expect(fetchPlan).toHaveBeenCalledTimes(before);
  });

  it("503 is terminal on the FIRST response (one GET, no polling)", async () => {
    const clock = makeClock();
    const boom = new Error("getClipReframe failed: 503");
    const fetchPlan = vi.fn(async (): Promise<ReframeResult> => {
      throw boom;
    });
    const onFailed = vi.fn();

    runReframePoll({
      fetchPlan,
      onReady: vi.fn(),
      onFailed,
      alive: () => true,
      now: clock.now,
      schedule: clock.schedule,
      cancel: clock.cancel,
    });
    await flush();

    expect(onFailed).toHaveBeenCalledWith("error", boom);
    await clock.advance(REFRAME_DEADLINE_MS);
    expect(fetchPlan).toHaveBeenCalledTimes(1); // terminal → the loop never re-polled
    expect(clock.pending()).toBe(0);
  });

  it("Retry after a timeout starts a FRESH budget (a transient failure must not be sticky)", async () => {
    const clock = makeClock();
    let plan: ReframeResult = computing;
    const fetchPlan = vi.fn(async (): Promise<ReframeResult> => plan);
    const onReady = vi.fn();
    const onFailed = vi.fn();
    const deps = {
      fetchPlan,
      onReady,
      onFailed,
      alive: () => true,
      now: clock.now,
      schedule: clock.schedule,
      cancel: clock.cancel,
    };

    runReframePoll(deps); // cycle 1 — server stuck on 202 → times out
    await flush();
    await clock.advance(REFRAME_DEADLINE_MS + 60_000);
    expect(onFailed).toHaveBeenCalledTimes(1);
    const spentOnCycle1 = fetchPlan.mock.calls.length;

    plan = ready; // the bake finally landed (or the user fixed it) …
    runReframePoll(deps); // … and Retry re-dispatches: counter + deadline reset
    await flush();

    expect(onReady).toHaveBeenCalledWith(REGIONS);
    expect(onFailed).toHaveBeenCalledTimes(1); // no new failure
    expect(fetchPlan.mock.calls.length).toBe(spentOnCycle1 + 1); // Retry really hit the server
  });

  it("202 → 202 → 200: polls on the fast cadence, then resolves with the real regions", async () => {
    const clock = makeClock();
    const fetchPlan = vi
      .fn<() => Promise<ReframeResult>>()
      .mockResolvedValueOnce(computing)
      .mockResolvedValueOnce(computing)
      .mockResolvedValue(ready);
    const onReady = vi.fn();
    const onFailed = vi.fn();

    runReframePoll({
      fetchPlan,
      onReady,
      onFailed,
      alive: () => true,
      now: clock.now,
      schedule: clock.schedule,
      cancel: clock.cancel,
    });
    await flush();
    expect(fetchPlan).toHaveBeenCalledTimes(1);

    await clock.advance(REFRAME_POLL_MS);
    expect(fetchPlan).toHaveBeenCalledTimes(2);
    await clock.advance(REFRAME_POLL_MS);
    expect(fetchPlan).toHaveBeenCalledTimes(3);

    expect(onReady).toHaveBeenCalledWith(REGIONS);
    expect(onFailed).not.toHaveBeenCalled();

    await clock.advance(60_000); // terminal → the loop stopped polling
    expect(fetchPlan).toHaveBeenCalledTimes(3);
    expect(clock.pending()).toBe(0);
  });

  it("stop() cancels the PENDING timer — a clip switch / unmount must not keep polling", async () => {
    const clock = makeClock();
    const fetchPlan = vi.fn(async (): Promise<ReframeResult> => computing);
    const onFailed = vi.fn();

    const handle = runReframePoll({
      fetchPlan,
      onReady: vi.fn(),
      onFailed,
      alive: () => true,
      now: clock.now,
      schedule: clock.schedule,
      cancel: clock.cancel,
    });
    await flush();
    expect(clock.pending()).toBe(1); // the 202 re-poll is queued

    handle.stop();
    expect(clock.pending()).toBe(0); // …and stop() actually cancelled it

    await clock.advance(10 * 60_000);
    expect(fetchPlan).toHaveBeenCalledTimes(1);
    expect(onFailed).not.toHaveBeenCalled(); // a deliberate stop is NOT a failure
  });

  it("a dead cycle (alive() false — clip switched) drops the in-flight result silently", async () => {
    const clock = makeClock();
    const fetchPlan = vi.fn(async (): Promise<ReframeResult> => ready);
    const onReady = vi.fn();
    const onFailed = vi.fn();

    runReframePoll({
      fetchPlan,
      onReady,
      onFailed,
      alive: () => false, // the user already switched clips
      now: clock.now,
      schedule: clock.schedule,
      cancel: clock.cancel,
    });
    await flush();

    expect(onReady).not.toHaveBeenCalled(); // no cross-clip clobber
    expect(onFailed).not.toHaveBeenCalled(); // and not a failure either
  });

  it("a hidden tab polls at 15s, and pollNow() (tab back in front) re-polls immediately", async () => {
    const clock = makeClock();
    const fetchPlan = vi.fn(async (): Promise<ReframeResult> => computing);

    const handle = runReframePoll({
      fetchPlan,
      onReady: vi.fn(),
      onFailed: vi.fn(),
      alive: () => true,
      hidden: () => true,
      now: clock.now,
      schedule: clock.schedule,
      cancel: clock.cancel,
    });
    await flush();
    expect(fetchPlan).toHaveBeenCalledTimes(1);

    await clock.advance(REFRAME_POLL_MS); // the visible cadence must NOT fire
    expect(fetchPlan).toHaveBeenCalledTimes(1);
    await clock.advance(REFRAME_HIDDEN_POLL_MS - REFRAME_POLL_MS);
    expect(fetchPlan).toHaveBeenCalledTimes(2);

    handle.pollNow(); // tab is visible again → don't sit out the 15s
    await clock.advance(0);
    expect(fetchPlan).toHaveBeenCalledTimes(3);
  });

  it("AbortError from the SHARED request re-polls (not a failure) — but still burns budget", async () => {
    const clock = makeClock();
    const abort = new Error("aborted");
    abort.name = "AbortError";
    const fetchPlan = vi
      .fn<() => Promise<ReframeResult>>()
      .mockRejectedValueOnce(abort)
      .mockResolvedValue(ready);
    const onReady = vi.fn();
    const onFailed = vi.fn();

    runReframePoll({
      fetchPlan,
      onReady,
      onFailed,
      alive: () => true,
      now: clock.now,
      schedule: clock.schedule,
      cancel: clock.cancel,
    });
    await flush();
    expect(onFailed).not.toHaveBeenCalled(); // an abort by ANOTHER consumer is not a bake failure

    await clock.advance(REFRAME_POLL_MS);
    expect(onReady).toHaveBeenCalledWith(REGIONS);
  });

  it("an AbortError storm cannot spin forever either (the same budget caps it)", async () => {
    const clock = makeClock();
    const abort = new Error("aborted");
    abort.name = "AbortError";
    const fetchPlan = vi.fn(async (): Promise<ReframeResult> => {
      throw abort;
    });
    const onFailed = vi.fn();

    runReframePoll({
      fetchPlan,
      onReady: vi.fn(),
      onFailed,
      alive: () => true,
      now: clock.now,
      schedule: clock.schedule,
      cancel: clock.cancel,
    });
    await flush();
    await clock.advance(60 * 60_000);

    expect(onFailed).toHaveBeenCalledTimes(1);
    expect(onFailed.mock.calls[0][0]).toBe("timeout");
    expect(clock.pending()).toBe(0);
  });
});
