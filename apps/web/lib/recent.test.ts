import { beforeEach, describe, expect, it } from "vitest";
import { getRecentSnapshot, mergeServerProjects, type RecentProject } from "./recent";

const KEY = "quip:recent-projects";

/** Minimal localStorage stand-in. vitest runs lib/**\/*.test.ts under `environment: "node"`
 *  (see vitest.config.ts) and this repo has no jsdom/happy-dom installed, so `window` doesn't
 *  exist at runtime here. recent.ts SSR-guards every mutator behind
 *  `typeof window === "undefined"` — wiring this onto globalThis.window lets
 *  mergeServerProjects run its real branch instead of the SSR no-op. Only getItem/setItem are
 *  implemented since that's all recent.ts calls. */
function createMemoryStorage() {
  const store = new Map<string, string>();
  return {
    getItem: (key: string) => (store.has(key) ? store.get(key)! : null),
    setItem: (key: string, value: string) => {
      store.set(key, String(value));
    },
  };
}

beforeEach(() => {
  globalThis.window = { localStorage: createMemoryStorage() } as unknown as Window &
    typeof globalThis;
});

describe("mergeServerProjects", () => {
  it("adds new server jobs, refreshes status on known ones, and keeps local-only fields, newest first", () => {
    const local: RecentProject = { id: "L1", label: "local", at: 1000, reviewed: true };
    window.localStorage.setItem(KEY, JSON.stringify([local]));

    mergeServerProjects([
      { id: "S1", label: "S1", at: 2000, status: "queued" }, // new job, created on another device
      { id: "L1", label: "server-label", at: 1000, status: "done" }, // known job, status refresh
    ]);

    const stored = JSON.parse(window.localStorage.getItem(KEY)!) as RecentProject[];
    expect(stored.map((p) => p.id)).toEqual(["S1", "L1"]); // newest-first

    const l1 = stored.find((p) => p.id === "L1");
    expect(l1?.label).toBe("local"); // local field wins over the server's
    expect(l1?.reviewed).toBe(true); // local-only field preserved
    expect(l1?.status).toBe("done"); // server refreshes status

    // getRecentSnapshot reflects the same merged, re-read state.
    expect(getRecentSnapshot().map((p) => p.id)).toEqual(["S1", "L1"]);
  });
});
