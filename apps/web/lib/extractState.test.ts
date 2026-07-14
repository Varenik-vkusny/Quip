// Extract-button state. The bug this pins: an UNKNOWN plan is NOT the same as a FREE plan.
//
// `/usage` can be in flight (a cold `quip-api` container makes that window seconds long — observed
// live on prod 2026-07-14), and `canExtractClip(undefined)` returns false. So the old ternary
// collapsed "we don't know yet" into "gated" and showed the **"Upgrade to Pro" nudge to a PAYING
// Pro customer** — silently telling them they weren't paying. Same class as the UsageMeter rendering
// "Free" on a failed /usage.

import { describe, expect, it } from "vitest";

import { EXTRACT, extractStateFor } from "./plans";

describe("extractStateFor — an unknown plan must never read as 'free'", () => {
  it("plan undefined (/usage still loading) → loading, NOT gated", () => {
    expect(extractStateFor(undefined, 0)).toBe("loading");
  });

  it("plan null (/usage failed) → loading, NOT gated", () => {
    expect(extractStateFor(null, 0)).toBe("loading");
  });

  it("a PRO user is never shown the upgrade nudge once the plan is known", () => {
    expect(extractStateFor("pro", 0)).toBe("available");
    expect(extractStateFor("studio", 0)).toBe("available");
  });

  it("an unknown plan wins over the clip cap (we don't paywall someone we can't identify)", () => {
    expect(extractStateFor(undefined, EXTRACT.freeCap + 5)).toBe("loading");
  });
});

describe("extractStateFor — the real gates still work", () => {
  it("a genuinely FREE user is gated", () => {
    expect(extractStateFor("free", 0)).toBe("gated");
    expect(extractStateFor("starter", 0)).toBe("gated");
  });

  it("a paid user at/over the free cap hits the paywall", () => {
    expect(extractStateFor("pro", EXTRACT.freeCap)).toBe("paywall");
    expect(extractStateFor("pro", EXTRACT.freeCap + 1)).toBe("paywall");
  });

  it("a paid user just under the cap is live", () => {
    expect(extractStateFor("pro", EXTRACT.freeCap - 1)).toBe("available");
  });
});
