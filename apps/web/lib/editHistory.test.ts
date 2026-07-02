import { describe, expect, it } from "vitest";
import {
  emptyHistory,
  pushHistory,
  undo,
  redo,
} from "./editHistory";
import type { HistoryState } from "./editHistory";

// ── helpers ────────────────────────────────────────────────────────────────

function makeHistory<T>(past: T[], future: T[] = []): HistoryState<T> {
  return { past, future };
}

// ── emptyHistory ───────────────────────────────────────────────────────────

describe("emptyHistory", () => {
  it("returns empty past and future", () => {
    const h = emptyHistory<number>();
    expect(h.past).toEqual([]);
    expect(h.future).toEqual([]);
  });
});

// ── pushHistory ────────────────────────────────────────────────────────────

describe("pushHistory", () => {
  it("appends present to past", () => {
    const h1 = pushHistory(emptyHistory<string>(), "a");
    expect(h1.past).toEqual(["a"]);
    expect(h1.future).toEqual([]);
  });

  it("clears future on push", () => {
    const h = makeHistory(["a", "b"], ["c", "d"]);
    const h1 = pushHistory(h, "e");
    expect(h1.future).toEqual([]);
    expect(h1.past).toEqual(["a", "b", "e"]);
  });

  it("accumulates multiple pushes in order (oldest first)", () => {
    let h = emptyHistory<number>();
    h = pushHistory(h, 1);
    h = pushHistory(h, 2);
    h = pushHistory(h, 3);
    expect(h.past).toEqual([1, 2, 3]);
    expect(h.future).toEqual([]);
  });

  it("respects default max of 50 by dropping the oldest entry", () => {
    let h = emptyHistory<number>();
    for (let i = 0; i < 55; i++) h = pushHistory(h, i);
    // values 0–4 (oldest) should have been dropped; 5–54 remain
    expect(h.past.length).toBe(50);
    expect(h.past[0]).toBe(5);
    expect(h.past[49]).toBe(54);
  });

  it("respects a custom max", () => {
    let h = emptyHistory<number>();
    for (let i = 0; i < 10; i++) h = pushHistory(h, i, 3);
    expect(h.past.length).toBe(3);
    expect(h.past[0]).toBe(7);
    expect(h.past[2]).toBe(9);
  });

  it("does NOT mutate the original state", () => {
    const h0 = emptyHistory<string>();
    const pastRef = h0.past;
    pushHistory(h0, "x");
    // same array reference, unchanged length
    expect(h0.past).toBe(pastRef);
    expect(h0.past.length).toBe(0);
  });
});

// ── undo ───────────────────────────────────────────────────────────────────

describe("undo", () => {
  it("returns null when past is empty", () => {
    expect(undo(emptyHistory<number>(), 0)).toBeNull();
  });

  it("returns the most-recent past entry as target", () => {
    const h = makeHistory(["a", "b", "c"]);
    const result = undo(h, "present")!;
    expect(result.target).toBe("c");
  });

  it("removes the most-recent entry from the new past", () => {
    const h = makeHistory(["a", "b", "c"]);
    const result = undo(h, "present")!;
    expect(result.state.past).toEqual(["a", "b"]);
  });

  it("appends present to the end of future", () => {
    const h = makeHistory(["a", "b"], ["x"]);
    const result = undo(h, "present")!;
    // existing future stays, present appended at the end
    expect(result.state.future).toEqual(["x", "present"]);
  });

  it("works when past has exactly one entry", () => {
    const result = undo(makeHistory(["only"]), "now")!;
    expect(result.target).toBe("only");
    expect(result.state.past).toEqual([]);
    expect(result.state.future).toEqual(["now"]);
  });

  it("does NOT mutate original state", () => {
    const original = makeHistory(["a", "b"], ["c"]);
    const pastSnap = [...original.past];
    const futureSnap = [...original.future];
    undo(original, "p");
    expect(original.past).toEqual(pastSnap);
    expect(original.future).toEqual(futureSnap);
  });
});

// ── redo ───────────────────────────────────────────────────────────────────

describe("redo", () => {
  it("returns null when future is empty", () => {
    expect(redo(emptyHistory<number>(), 0)).toBeNull();
  });

  it("returns the last future entry as target", () => {
    const h = makeHistory([], ["x", "y", "z"]);
    const result = redo(h, "present")!;
    expect(result.target).toBe("z");
  });

  it("removes the last future entry from the new future", () => {
    const h = makeHistory([], ["x", "y", "z"]);
    const result = redo(h, "present")!;
    expect(result.state.future).toEqual(["x", "y"]);
  });

  it("appends present to the end of past", () => {
    const h = makeHistory(["a"], ["x", "y"]);
    const result = redo(h, "present")!;
    expect(result.state.past).toEqual(["a", "present"]);
    expect(result.target).toBe("y");
  });

  it("works when future has exactly one entry", () => {
    const result = redo(makeHistory([], ["only"]), "now")!;
    expect(result.target).toBe("only");
    expect(result.state.future).toEqual([]);
    expect(result.state.past).toEqual(["now"]);
  });

  it("does NOT mutate original state", () => {
    const original = makeHistory(["a"], ["b", "c"]);
    const pastSnap = [...original.past];
    const futureSnap = [...original.future];
    redo(original, "p");
    expect(original.past).toEqual(pastSnap);
    expect(original.future).toEqual(futureSnap);
  });
});

// ── round-trip semantics ───────────────────────────────────────────────────

describe("undo/redo round-trip", () => {
  it("push → undo → redo restores the undone value", () => {
    let h = emptyHistory<string>();
    h = pushHistory(h, "v1");
    h = pushHistory(h, "v2");
    // past=["v1","v2"], present conceptually is "v3"

    const u = undo(h, "v3")!;
    expect(u.target).toBe("v2");
    // state: past=["v1"], future=["v3"]

    const r = redo(u.state, "v2")!;
    // target = last(future) = "v3"
    expect(r.target).toBe("v3");
  });

  it("push after undo clears future — redo becomes unavailable", () => {
    let h = emptyHistory<string>();
    h = pushHistory(h, "a");
    h = pushHistory(h, "b");

    const u = undo(h, "c")!;
    // state: past=["a"], future=["c"]
    expect(u.state.future).toEqual(["c"]);

    // new push forks history, clearing future
    h = pushHistory(u.state, "d");
    expect(h.future).toEqual([]);
    expect(redo(h, "d")).toBeNull();
  });

  it("multiple consecutive undos accumulate future in undo-order", () => {
    let h = emptyHistory<number>();
    h = pushHistory(h, 1);
    h = pushHistory(h, 2);
    h = pushHistory(h, 3);
    // past=[1,2,3]; present is 4 (not yet pushed)

    // undo three times, passing each target as new "present"
    const u1 = undo(h, 4)!;
    // target=3, past=[1,2], future=[4]
    expect(u1.target).toBe(3);

    const u2 = undo(u1.state, u1.target)!;
    // target=2, past=[1], future=[4,3]
    expect(u2.target).toBe(2);
    expect(u2.state.future).toEqual([4, 3]);

    const u3 = undo(u2.state, u2.target)!;
    // target=1, past=[], future=[4,3,2]
    expect(u3.target).toBe(1);
    expect(u3.state.past).toEqual([]);
    expect(u3.state.future).toEqual([4, 3, 2]);

    // redo once: target = last(future) = 2; present (1) pushed onto past
    const r1 = redo(u3.state, u3.target)!;
    expect(r1.target).toBe(2);
    expect(r1.state.past).toEqual([1]);
    expect(r1.state.future).toEqual([4, 3]);

    // redo again: target = last(future) = 3
    const r2 = redo(r1.state, r1.target)!;
    expect(r2.target).toBe(3);
    expect(r2.state.future).toEqual([4]);

    // redo again: target = last(future) = 4; future empty
    const r3 = redo(r2.state, r2.target)!;
    expect(r3.target).toBe(4);
    expect(r3.state.future).toEqual([]);
    expect(redo(r3.state, r3.target)).toBeNull();
  });
});
