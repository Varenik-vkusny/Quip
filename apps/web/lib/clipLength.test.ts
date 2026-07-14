import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  CLIP_GAP,
  clampClipRange,
  DEFAULT_CLIP_RANGE,
  getClipRangeSnapshot,
  getServerClipRangeSnapshot,
  MAX_CLIP_SEC,
  matchPreset,
  MIN_CLIP_MAX_SEC,
  MIN_CLIP_SEC,
  readStoredRange,
  resetClipRangeCache,
  setClipRange,
  subscribeClipRange,
  writeStoredRange,
} from "./clipLength";

// Pure logic behind the dashboard's "clip length" control (min/max clip duration, seconds).
// Extracted from SourceForm.tsx so the clamp / preset / storage rules get direct unit coverage
// instead of only being exercised by hand through the stepper UI.

/** Minimal Storage stand-in — enough for readStoredRange/writeStoredRange, no jsdom needed
 *  (this suite runs under vitest's "node" environment, same as the rest of lib/). */
function fakeLocalStorage(initial: Record<string, string> = {}): Storage {
  const store = new Map(Object.entries(initial));
  return {
    getItem: (k: string) => (store.has(k) ? (store.get(k) as string) : null),
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
    key: (i: number) => [...store.keys()][i] ?? null,
    get length() {
      return store.size;
    },
  } as unknown as Storage;
}

describe("clampClipRange", () => {
  it("min-edit: clamps min up to max - CLIP_GAP when dragged past max; max untouched", () => {
    expect(clampClipRange({ min: 100, max: 60 }, "min")).toEqual({ min: 58, max: 60 });
  });

  it("max-edit: clamps max down to min + CLIP_GAP when dragged below min; min untouched", () => {
    expect(clampClipRange({ min: 50, max: 10 }, "max")).toEqual({ min: 50, max: 52 });
  });

  it("max-edit: never drops below MIN_CLIP_MAX_SEC even when min is tiny", () => {
    const r = clampClipRange({ min: 4, max: 1 }, "max");
    expect(r.max).toBe(MIN_CLIP_MAX_SEC);
    expect(r.min).toBe(4);
  });

  it("min-edit: never below MIN_CLIP_SEC (4)", () => {
    expect(clampClipRange({ min: 1, max: 60 }, "min").min).toBe(MIN_CLIP_SEC);
  });

  it("max-edit: never above MAX_CLIP_SEC (180)", () => {
    expect(clampClipRange({ min: 20, max: 999 }, "max").max).toBe(MAX_CLIP_SEC);
  });

  it("min-edit: NaN / 0 / negative all land on a legal bound, never NaN", () => {
    for (const bad of [Number.NaN, 0, -5]) {
      const r = clampClipRange({ min: bad, max: 60 }, "min");
      expect(Number.isNaN(r.min)).toBe(false);
      expect(r.min).toBe(MIN_CLIP_SEC);
      expect(r.max).toBe(60);
    }
  });

  it("max-edit: NaN / 0 / negative all land on a legal bound, never NaN", () => {
    for (const bad of [Number.NaN, 0, -5]) {
      const r = clampClipRange({ min: 20, max: bad }, "max");
      expect(Number.isNaN(r.max)).toBe(false);
      expect(r.max).toBe(Math.max(MIN_CLIP_MAX_SEC, 20 + CLIP_GAP));
      expect(r.min).toBe(20);
    }
  });

  it("always returns integers, on both edit paths", () => {
    const r1 = clampClipRange({ min: 20.7, max: 60.2 }, "min");
    expect(Number.isInteger(r1.min)).toBe(true);
    expect(Number.isInteger(r1.max)).toBe(true);
    const r2 = clampClipRange({ min: 20.7, max: 60.2 }, "max");
    expect(Number.isInteger(r2.min)).toBe(true);
    expect(Number.isInteger(r2.max)).toBe(true);
  });
});

describe("matchPreset", () => {
  it("returns 'standard' for {20,60}", () => {
    expect(matchPreset({ min: 20, max: 60 })).toBe("standard");
  });

  it("returns null for {20,61} (no exact preset match)", () => {
    expect(matchPreset({ min: 20, max: 61 })).toBeNull();
  });

  it("matches the other presets exactly", () => {
    expect(matchPreset({ min: 15, max: 30 })).toBe("short");
    expect(matchPreset({ min: 60, max: 120 })).toBe("long");
  });
});

describe("readStoredRange / writeStoredRange", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("SSR-safe: returns the default when window is undefined", () => {
    // `window` is undefined by default under vitest's "node" test environment.
    expect(readStoredRange()).toEqual(DEFAULT_CLIP_RANGE);
  });

  it("returns the default when localStorage is empty", () => {
    vi.stubGlobal("window", { localStorage: fakeLocalStorage() });
    expect(readStoredRange()).toEqual(DEFAULT_CLIP_RANGE);
  });

  it("returns the default when the stored values are garbage (non-numeric)", () => {
    vi.stubGlobal("window", {
      localStorage: fakeLocalStorage({ "quip:clipMinSec": "banana", "quip:clipMaxSec": "nope" }),
    });
    expect(readStoredRange()).toEqual(DEFAULT_CLIP_RANGE);
  });

  it("returns the default when the stored pair violates min + 2 <= max", () => {
    vi.stubGlobal("window", {
      localStorage: fakeLocalStorage({ "quip:clipMinSec": "50", "quip:clipMaxSec": "51" }),
    });
    expect(readStoredRange()).toEqual(DEFAULT_CLIP_RANGE);
  });

  it("returns the default when the stored values are out of bounds", () => {
    vi.stubGlobal("window", {
      localStorage: fakeLocalStorage({ "quip:clipMinSec": "1", "quip:clipMaxSec": "60" }),
    });
    expect(readStoredRange()).toEqual(DEFAULT_CLIP_RANGE);
  });

  it("returns the stored pair when it's valid", () => {
    vi.stubGlobal("window", {
      localStorage: fakeLocalStorage({ "quip:clipMinSec": "30", "quip:clipMaxSec": "90" }),
    });
    expect(readStoredRange()).toEqual({ min: 30, max: 90 });
  });

  it("writeStoredRange persists under the EXISTING keys (quip:clipMinSec / quip:clipMaxSec)", () => {
    const storage = fakeLocalStorage();
    vi.stubGlobal("window", { localStorage: storage });
    writeStoredRange({ min: 25, max: 75 });
    expect(storage.getItem("quip:clipMinSec")).toBe("25");
    expect(storage.getItem("quip:clipMaxSec")).toBe("75");
  });

  it("writeStoredRange is SSR-safe (no throw when window is undefined)", () => {
    expect(() => writeStoredRange({ min: 20, max: 60 })).not.toThrow();
  });
});

// The store exists to keep a browser-only value out of render. Its contract is what makes the
// dashboard hydrate cleanly: the SERVER snapshot must be the default no matter what's in
// localStorage (React reuses it for the hydration render), and the client snapshot must be
// reference-STABLE or useSyncExternalStore re-renders forever.
describe("clip-range store (useSyncExternalStore adapter)", () => {
  beforeEach(() => resetClipRangeCache());

  it("the server snapshot is the DEFAULT even when localStorage holds another range", () => {
    vi.stubGlobal("window", {
      localStorage: fakeLocalStorage({ "quip:clipMinSec": "30", "quip:clipMaxSec": "90" }),
    });
    expect(getServerClipRangeSnapshot()).toEqual(DEFAULT_CLIP_RANGE);
  });

  it("the client snapshot reads localStorage and is reference-stable across calls", () => {
    vi.stubGlobal("window", {
      localStorage: fakeLocalStorage({ "quip:clipMinSec": "30", "quip:clipMaxSec": "90" }),
    });
    const a = getClipRangeSnapshot();
    expect(a).toEqual({ min: 30, max: 90 });
    expect(getClipRangeSnapshot()).toBe(a); // SAME reference — otherwise the hook loops
  });

  it("setClipRange persists, updates the snapshot, and notifies subscribers", () => {
    const storage = fakeLocalStorage();
    vi.stubGlobal("window", { localStorage: storage });
    const seen = vi.fn();
    const unsubscribe = subscribeClipRange(seen);

    setClipRange({ min: 15, max: 30 });

    expect(seen).toHaveBeenCalledTimes(1);
    expect(getClipRangeSnapshot()).toEqual({ min: 15, max: 30 });
    expect(storage.getItem("quip:clipMinSec")).toBe("15");

    unsubscribe();
    setClipRange({ min: 20, max: 60 });
    expect(seen).toHaveBeenCalledTimes(1); // unsubscribed → no further notifications
  });
});
