import { describe, expect, it } from "vitest";
import {
  classifyMoment,
  coverageSegments,
  momentClipId,
} from "./videoMapCoverage";

// Two clips cut from a 400s source: clip_01 covers 100–130, clip_02 covers 300–340.
const clips = [
  { id: "clip_01", start: 100, end: 130 },
  { id: "clip_02", start: 300, end: 340 },
];

describe("momentClipId", () => {
  it("returns the clip whose interval OVERLAPS the moment", () => {
    // moment 120–145 overlaps clip_01 (100–130)
    expect(momentClipId(clips, { start: 120, end: 145 })).toBe("clip_01");
  });

  it("returns null when no clip overlaps (no fallback mislink)", () => {
    // moment 200–220 sits in the untapped gap → must NOT link to a neighbouring clip
    expect(momentClipId(clips, { start: 200, end: 220 })).toBeNull();
  });

  it("does not count a touching-only edge as overlap", () => {
    // moment 130–150 only touches clip_01's end (130) → no positive-length overlap
    expect(momentClipId(clips, { start: 130, end: 150 })).toBeNull();
  });
});

describe("classifyMoment", () => {
  it("marks an overlapping moment clipped", () => {
    expect(classifyMoment(clips, { start: 305, end: 320 })).toBe("clipped");
  });
  it("marks a gap moment untapped", () => {
    expect(classifyMoment(clips, { start: 200, end: 220 })).toBe("untapped");
  });
});

describe("coverageSegments", () => {
  it("emits alternating clipped/untapped segments over [0,duration]", () => {
    const segs = coverageSegments(400, clips);
    // leading gap, clip_01, mid gap, clip_02, trailing gap
    expect(segs).toEqual([
      { start: 0, end: 100, clipped: false },
      { start: 100, end: 130, clipped: true },
      { start: 130, end: 300, clipped: false },
      { start: 300, end: 340, clipped: true },
      { start: 340, end: 400, clipped: false },
    ]);
  });

  it("merges overlapping/adjacent clips into one clipped span", () => {
    // coverageSegments needs only {start,end}
    const overlapping = [
      { start: 10, end: 30 },
      { start: 25, end: 50 },
    ];
    const segs = coverageSegments(60, overlapping);
    expect(segs).toEqual([
      { start: 0, end: 10, clipped: false },
      { start: 10, end: 50, clipped: true },
      { start: 50, end: 60, clipped: false },
    ]);
  });

  it("clamps clip bounds to the source and drops empty intervals", () => {
    const segs = coverageSegments(100, [{ start: -20, end: 40 }]);
    expect(segs).toEqual([
      { start: 0, end: 40, clipped: true },
      { start: 40, end: 100, clipped: false },
    ]);
  });

  it("returns [] for a non-positive duration", () => {
    expect(coverageSegments(0, clips)).toEqual([]);
  });
});
