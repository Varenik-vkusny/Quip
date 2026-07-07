import { describe, expect, it } from "vitest";
import type { ClipOut } from "./types";
import { computeSignalBars } from "./clipSignals";

// Minimal ClipOut factory — only the fields computeSignalBars reads matter; the rest are filler.
function clip(overrides: Partial<ClipOut> = {}): ClipOut {
  return {
    id: "c1",
    video_url: "",
    score: 0.6,
    start: 0,
    end: 10,
    duration: 10,
    reason: "",
    transcript: "",
    type: "complete_thought",
    words: [],
    ...overrides,
  } as ClipOut;
}

function energyBars(c: ClipOut): number {
  const s = computeSignalBars(c).find((x) => x.key === "energy");
  if (!s) throw new Error("no energy signal");
  return s.bars;
}

describe("computeSignalBars — energy from real loudness", () => {
  it("maps a very loud clip (energy≈0.95) to the full 4 bars", () => {
    expect(energyBars(clip({ energy: 0.95 }))).toBe(4);
  });

  it("maps a quiet clip (energy≈0.1) to the minimum 1 bar", () => {
    expect(energyBars(clip({ energy: 0.1 }))).toBe(1);
  });

  it("maps a mid-loudness clip (energy≈0.5) to 3 bars", () => {
    expect(energyBars(clip({ energy: 0.5 }))).toBe(3);
  });

  it("clamps into 1..4 for the extremes", () => {
    expect(energyBars(clip({ energy: 0 }))).toBe(1);
    expect(energyBars(clip({ energy: 1 }))).toBe(4);
  });

  it("energy takes precedence over tone when present", () => {
    // tone would say 4 (shocking), but a measured-quiet energy wins → 1.
    expect(energyBars(clip({ energy: 0.05, tone: "shocking" }))).toBe(1);
  });
});

describe("computeSignalBars — tone fallback (back-compat for clips with no energy)", () => {
  it("falls back to the tone heuristic when energy is null/undefined", () => {
    expect(energyBars(clip({ energy: null, tone: "shocking" }))).toBe(4);
    expect(energyBars(clip({ tone: "shocking" }))).toBe(4); // undefined energy
    expect(energyBars(clip({ energy: null, tone: "inspiring" }))).toBe(3);
    expect(energyBars(clip({ energy: null, tone: "touching" }))).toBe(2);
    expect(energyBars(clip({ energy: null, tone: null }))).toBe(1);
  });
});

describe("computeSignalBars — other signals still present and clamped", () => {
  it("returns exactly the four signal keys", () => {
    expect(computeSignalBars(clip()).map((s) => s.key)).toEqual([
      "hook",
      "standalone",
      "energy",
      "speaker",
    ]);
  });

  it("keeps every bar count within 1..4", () => {
    for (const s of computeSignalBars(clip({ score: 0.99, energy: 0.9, hook: "x".repeat(80) }))) {
      expect(s.bars).toBeGreaterThanOrEqual(1);
      expect(s.bars).toBeLessThanOrEqual(4);
    }
  });
});
