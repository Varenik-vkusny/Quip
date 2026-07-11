import { describe, expect, it } from "vitest";
import type { CropOverride } from "@/lib/types";
import { findShotOverride, pickActiveOverride, shotIsManual, upsertShotOverride } from "./reframeFrame";

const ov = (source_start: number, source_end: number, mode = "fit"): CropOverride => ({
  source_start,
  source_end,
  mode,
});

describe("pickActiveOverride", () => {
  it("a whole-clip override applies at any t inside it", () => {
    expect(pickActiveOverride([ov(0, 100)], 50)?.source_start).toBe(0);
    expect(pickActiveOverride([ov(0, 100)], 0)?.source_start).toBe(0);
  });

  it("a PER-SHOT override applies ONLY inside its range (not the whole clip)", () => {
    const overrides = [ov(10, 12, "fit")];
    expect(pickActiveOverride(overrides, 11)?.mode).toBe("fit"); // inside → applies
    expect(pickActiveOverride(overrides, 50)).toBeNull(); // outside → fall through to AI plan
    expect(pickActiveOverride(overrides, 9.99)).toBeNull(); // just before
    expect(pickActiveOverride(overrides, 12)).toBeNull(); // end exclusive
  });

  it("the last matching override wins (override-on-override)", () => {
    const r = pickActiveOverride([ov(0, 100, "fill"), ov(10, 20, "fit")], 15);
    expect(r?.mode).toBe("fit");
  });

  it("no overrides / none containing t → null", () => {
    expect(pickActiveOverride([], 5)).toBeNull();
    expect(pickActiveOverride([ov(10, 20)], 5)).toBeNull();
  });
});

// Per-shot framing (P1): editing ONE shot must write ONLY that shot's [source_start, source_end]
// range and never touch its neighbours — the founder's #1 guarantee. `upsertShotOverride` is the
// pure builder the editor uses to merge a control change into the shot's override (carrying center
// + fit_style, never wiping them). `shotIsManual` derives per-shot provenance from override
// coverage. Immutable array ops, matched on the shot's range within a small epsilon.

const shotA = { source_start: 10, source_end: 20 };
const shotB = { source_start: 20, source_end: 30 };

describe("upsertShotOverride", () => {
  it("appends a new override when the shot has none", () => {
    const next = upsertShotOverride([], shotA, { mode: "fill", center: 0.3 });
    expect(next).toHaveLength(1);
    expect(next[0]).toMatchObject({
      source_start: 10,
      source_end: 20,
      mode: "fill",
      center: 0.3,
    });
  });

  it("returns a NEW array (immutable — does not mutate the input)", () => {
    const input: CropOverride[] = [];
    const next = upsertShotOverride(input, shotA, { mode: "fit" });
    expect(next).not.toBe(input);
    expect(input).toHaveLength(0);
  });

  it("replaces the existing override on the same range (no duplicate)", () => {
    const start: CropOverride[] = [
      { source_start: 10, source_end: 20, mode: "fill", center: 0.3, fit_style: "blur" },
    ];
    const next = upsertShotOverride(start, shotA, { fit_style: "black" });
    const forShot = next.filter((o) => o.source_start === 10 && o.source_end === 20);
    expect(forShot).toHaveLength(1); // no duplicate
    // fit_style patched, center PRESERVED (not nulled)
    expect(forShot[0].fit_style).toBe("black");
    expect(forShot[0].center).toBe(0.3);
    expect(forShot[0].mode).toBe("fill");
  });

  it("preserves fit_style when the patch only changes the pan center", () => {
    const start: CropOverride[] = [
      { source_start: 10, source_end: 20, mode: "fill", center: 0.5, fit_style: "black" },
    ];
    const next = upsertShotOverride(start, shotA, { center: 0.8 });
    expect(next[0].center).toBe(0.8);
    expect(next[0].fit_style).toBe("black");
  });

  it("editing shot B leaves shot A's override untouched", () => {
    const start: CropOverride[] = [
      { source_start: 10, source_end: 20, mode: "fill", center: 0.25 },
    ];
    const next = upsertShotOverride(start, shotB, { mode: "fit", fit_style: "blur" });
    const a = next.find((o) => o.source_start === 10 && o.source_end === 20);
    const b = next.find((o) => o.source_start === 20 && o.source_end === 30);
    expect(a).toMatchObject({ mode: "fill", center: 0.25 }); // A unchanged
    expect(b).toMatchObject({ mode: "fit", fit_style: "blur" }); // B added
    expect(next).toHaveLength(2);
  });

  it("mode:auto drops the shot's override (returns to AI)", () => {
    const start: CropOverride[] = [
      { source_start: 10, source_end: 20, mode: "fill", center: 0.3 },
      { source_start: 20, source_end: 30, mode: "fit" },
    ];
    const next = upsertShotOverride(start, shotA, { mode: "auto" });
    expect(next.find((o) => o.source_start === 10)).toBeUndefined(); // A cleared
    expect(next.find((o) => o.source_start === 20)).toBeDefined(); // B kept
    expect(next).toHaveLength(1);
  });

  it("matches an existing override within a small epsilon (frame-level drift)", () => {
    const start: CropOverride[] = [
      { source_start: 10.0004, source_end: 19.9996, mode: "fill", center: 0.4 },
    ];
    const next = upsertShotOverride(start, shotA, { center: 0.6 });
    expect(next).toHaveLength(1); // matched & replaced, not appended
    expect(next[0].center).toBe(0.6);
  });
});

describe("shotIsManual", () => {
  const overrides: CropOverride[] = [
    { source_start: 10, source_end: 20, mode: "fill", center: 0.3 },
  ];

  it("is true for a shot whose midpoint an override covers", () => {
    expect(shotIsManual(overrides, shotA)).toBe(true);
  });

  it("is false for an uncovered shot", () => {
    expect(shotIsManual(overrides, shotB)).toBe(false);
  });

  it("is false when there are no overrides", () => {
    expect(shotIsManual([], shotA)).toBe(false);
  });
});

describe("findShotOverride", () => {
  const overrides: CropOverride[] = [
    { source_start: 10, source_end: 20, mode: "fill", center: 0.3 },
  ];

  it("returns the override on the shot's range", () => {
    expect(findShotOverride(overrides, shotA)?.center).toBe(0.3);
  });

  it("returns null when the shot has no override", () => {
    expect(findShotOverride(overrides, shotB)).toBeNull();
  });
});
