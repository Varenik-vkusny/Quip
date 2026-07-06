import { describe, expect, it } from "vitest";
import { clipRange, etaSeconds, formatRate, mmss, usd } from "./format";

describe("mmss", () => {
  it("formats whole seconds as m:ss", () => {
    expect(mmss(0)).toBe("0:00");
    expect(mmss(5)).toBe("0:05");
    expect(mmss(65)).toBe("1:05");
    expect(mmss(600)).toBe("10:00");
  });

  it("rolls over to h:mm:ss past an hour", () => {
    expect(mmss(3600)).toBe("1:00:00");
    expect(mmss(3661)).toBe("1:01:01");
  });

  it("rounds float seconds cleanly — no FP noise (regression: 3:21.751000000000005)", () => {
    // Elapsed time arrives as a float; the old inline math leaked `21.751000000000005`.
    expect(mmss(201.751000000000005)).toBe("3:22");
    expect(mmss(136.251000000000005)).toBe("2:16");
    expect(mmss(5.4)).toBe("0:05");
    // No fractional/garbage characters in the output, ever.
    expect(mmss(201.751000000000005)).not.toContain(".");
  });

  it("guards against NaN / non-finite from the wire", () => {
    expect(mmss(Number.NaN)).toBe("0:00");
    expect(mmss(Number.POSITIVE_INFINITY)).toBe("0:00");
    expect(mmss(-5)).toBe("0:00");
  });
});

describe("clipRange", () => {
  it("renders a source-coordinate range", () => {
    expect(clipRange(12, 45)).toBe("0:12–0:45");
  });
});

describe("usd", () => {
  it("formats dollars and guards NaN", () => {
    expect(usd(0.16)).toBe("$0.16");
    expect(usd(Number.NaN)).toBe("$0.00");
  });
});

describe("etaSeconds", () => {
  it("returns null when there's no signal yet (honest 'estimating…', never a fake number)", () => {
    // Too little elapsed → wild first-second estimates suppressed.
    expect(etaSeconds(1, 0.5)).toBeNull();
    expect(etaSeconds(3.9, 0.5)).toBeNull();
    // Progress too small to trust (e.g. the stage floor jumping to a few %).
    expect(etaSeconds(30, 0)).toBeNull();
    expect(etaSeconds(30, 0.02)).toBeNull();
    // Already complete → no remaining time to show.
    expect(etaSeconds(30, 1)).toBeNull();
  });

  it("guards NaN / non-finite from the wire", () => {
    expect(etaSeconds(Number.NaN, 0.5)).toBeNull();
    expect(etaSeconds(30, Number.NaN)).toBeNull();
    expect(etaSeconds(Number.POSITIVE_INFINITY, 0.5)).toBeNull();
    expect(etaSeconds(30, Number.POSITIVE_INFINITY)).toBeNull();
  });

  it("extrapolates remaining time from cumulative progress (average-rate projection)", () => {
    // At 50% after 120s, ~120s remain → snaps to the 2-min bucket.
    expect(etaSeconds(120, 0.5)).toBe(120);
    // At 50% after 60s, ~60s remain → 1-min bucket.
    expect(etaSeconds(60, 0.5)).toBe(60);
    // At 25% after 60s, ~180s remain → 3-min bucket.
    expect(etaSeconds(60, 0.25)).toBe(180);
  });

  it("buckets near-done into a small calm value (never 0 / never negative)", () => {
    // 90% after 300s → ~33s remain → 30s bucket (calm, finishes early = pleasant).
    expect(etaSeconds(300, 0.9)).toBe(30);
    // 95% after 20s → ~1s remain → still the 30s floor, never 0 or negative.
    const near = etaSeconds(20, 0.95);
    expect(near).toBe(30);
    expect(near).toBeGreaterThan(0);
  });

  it("coarse-buckets so the readout doesn't jitter second to second", () => {
    // A cluster of nearby raw estimates all collapse onto the same coarse bucket.
    expect(etaSeconds(46, 0.5)).toBe(etaSeconds(52, 0.5)); // ~46s vs ~52s → both 60
    expect(etaSeconds(118, 0.5)).toBe(etaSeconds(122, 0.5)); // ~118s vs ~122s → both 120
  });

  it("suppresses absurdly large estimates (stays honest instead of showing hours)", () => {
    // 5% after 600s → ~3.2h remain → beyond the sane ceiling → null (worker caps videos at 3h).
    expect(etaSeconds(600, 0.05)).toBeNull();
    // But a plausible long estimate is still shown (rounded to whole minutes).
    expect(etaSeconds(60, 0.05)).toBe(1140); // 19 min
  });
});

describe("formatRate", () => {
  it("formats bytes/sec into a human transfer rate", () => {
    expect(formatRate(6.5 * 1024 * 1024)).toBe("6.5 MB/s");
    expect(formatRate(1024 * 1024)).toBe("1.0 MB/s");
    expect(formatRate(800 * 1024)).toBe("800 KB/s");
    expect(formatRate(500)).toBe("500 B/s");
  });

  it("returns null with no signal (zero / negative / non-finite) so the caller stays quiet", () => {
    expect(formatRate(0)).toBeNull();
    expect(formatRate(-5)).toBeNull();
    expect(formatRate(Number.NaN)).toBeNull();
    expect(formatRate(Number.POSITIVE_INFINITY)).toBeNull();
  });
});
