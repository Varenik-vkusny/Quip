import { describe, expect, it } from "vitest";
import { snapTime } from "./timelineSnap";

describe("snapTime", () => {
  it("snaps to a target within tolerance", () => {
    expect(snapTime(10.1, [10, 20, 30], 0.25)).toBe(10);
    expect(snapTime(19.9, [10, 20, 30], 0.25)).toBe(20);
  });

  it("returns the raw time when no target is within tolerance", () => {
    expect(snapTime(15, [10, 20, 30], 0.25)).toBe(15);
    expect(snapTime(10.5, [10, 20, 30], 0.25)).toBe(10.5);
  });

  it("picks the NEAREST target when several are in range", () => {
    // 10.12 is closer to 10 (0.12) than to 10.2 (0.08)? no — nearest is 10.2.
    expect(snapTime(10.12, [10, 10.2], 0.5)).toBe(10.2);
    expect(snapTime(10.08, [10, 10.2], 0.5)).toBe(10);
  });

  it("returns the raw time for empty targets", () => {
    expect(snapTime(42, [], 0.25)).toBe(42);
  });

  it("treats tolerance as inclusive at the boundary", () => {
    expect(snapTime(10.25, [10], 0.25)).toBe(10);
    expect(snapTime(10.26, [10], 0.25)).toBe(10.26);
  });

  it("ignores non-finite targets and a non-positive tolerance", () => {
    expect(snapTime(10.1, [Number.NaN, 10, Number.POSITIVE_INFINITY], 0.25)).toBe(10);
    // tol <= 0 → snapping is disabled, raw time passes through.
    expect(snapTime(10, [10], 0)).toBe(10);
    expect(snapTime(10.0001, [10], 0)).toBe(10.0001);
  });

  it("returns the raw time unchanged when it is non-finite", () => {
    expect(snapTime(Number.NaN, [10], 0.25)).toBeNaN();
  });
});
