import { describe, expect, it } from "vitest";
import {
  INSPECTOR_WIDTH_KEY,
  keysToPurge,
  LAST_USER_ID_KEY,
  LEGACY_RECENT_KEY,
  REVIEWED_KEY_PREFIX,
  shouldPurge,
} from "./authReset";

// Pure reconciliation for the cross-account client-cache purge. When the signed-in identity
// changes (OAuth redirect, multi-tab account switch) or the user signs out, residual per-device
// localStorage (the old global recents blob, editor inspector width, another user's reviewed set)
// must be wiped so account B never sees account A's leftovers. This is the pure decision layer;
// the DOM sweep lives in the components.
describe("shouldPurge", () => {
  it("purges when the ids differ (account switch)", () => {
    expect(shouldPurge("user-a", "user-b")).toBe(true);
  });

  it("purges when there is no prior id but a user just signed in (first login on device)", () => {
    expect(shouldPurge(null, "user-a")).toBe(true);
  });

  it("purges when the current user is null (sign-out)", () => {
    expect(shouldPurge("user-a", null)).toBe(true);
  });

  it("purges when both are null (sign-out with no prior marker)", () => {
    expect(shouldPurge(null, null)).toBe(true);
  });

  it("keeps caches when the same user is still signed in (e.g. token refresh)", () => {
    expect(shouldPurge("user-a", "user-a")).toBe(false);
  });
});

describe("keysToPurge", () => {
  it("selects the legacy recents blob, the inspector width, and every reviewed set", () => {
    const all = [
      LEGACY_RECENT_KEY,
      INSPECTOR_WIDTH_KEY,
      `${REVIEWED_KEY_PREFIX}user-a`,
      `${REVIEWED_KEY_PREFIX}user-b`,
      LAST_USER_ID_KEY, // preserved — the caller rewrites it, so it must NOT be in the purge set
      "unrelated:key",
      "NEXT_LOCALE",
    ];
    expect(keysToPurge(all).sort()).toEqual(
      [
        INSPECTOR_WIDTH_KEY,
        LEGACY_RECENT_KEY,
        `${REVIEWED_KEY_PREFIX}user-a`,
        `${REVIEWED_KEY_PREFIX}user-b`,
      ].sort(),
    );
  });

  it("returns nothing to purge for an empty store", () => {
    expect(keysToPurge([])).toEqual([]);
  });

  it("never selects the lastUserId marker or unrelated keys", () => {
    expect(keysToPurge([LAST_USER_ID_KEY, "theme", "cart:items"])).toEqual([]);
  });
});
