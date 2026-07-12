import { describe, expect, it } from "vitest";
import {
  BACKOFF_CAP_MS,
  backoffDelay,
  classifyPartError,
  classifyResumeError,
  fileFingerprint,
  remainingParts,
} from "./uploadResilience";

describe("fileFingerprint", () => {
  it("is stable for the same file identity", () => {
    const f = { name: "talk.mp4", size: 5_000_000_000, lastModified: 1_700_000_000_000 };
    expect(fileFingerprint(f)).toBe(fileFingerprint({ ...f }));
  });

  it("changes when the size changes (a different file under the same name)", () => {
    const a = { name: "talk.mp4", size: 5_000_000_000, lastModified: 1_700_000_000_000 };
    const b = { ...a, size: 5_000_000_001 };
    expect(fileFingerprint(a)).not.toBe(fileFingerprint(b));
  });

  it("changes when the name or mtime changes", () => {
    const a = { name: "talk.mp4", size: 100, lastModified: 1 };
    expect(fileFingerprint(a)).not.toBe(fileFingerprint({ ...a, name: "other.mp4" }));
    expect(fileFingerprint(a)).not.toBe(fileFingerprint({ ...a, lastModified: 2 }));
  });
});

describe("remainingParts", () => {
  it("drops the already-completed part numbers, preserving order", () => {
    expect(remainingParts([1, 2, 3, 4], new Set([1, 3]))).toEqual([2, 4]);
  });

  it("returns everything when nothing is completed", () => {
    expect(remainingParts([1, 2, 3], new Set())).toEqual([1, 2, 3]);
  });

  it("returns nothing when all are completed", () => {
    expect(remainingParts([1, 2], new Set([1, 2]))).toEqual([]);
  });
});

describe("backoffDelay", () => {
  it("grows with the attempt number (later attempts wait longer)", () => {
    // Ranges for different attempts don't overlap by design, so this holds for any jitter.
    expect(backoffDelay(0)).toBeLessThan(backoffDelay(3));
  });

  it("never exceeds the cap, even for large attempts", () => {
    for (let n = 0; n < 40; n++) {
      expect(backoffDelay(n)).toBeLessThanOrEqual(BACKOFF_CAP_MS);
    }
  });

  it("is deterministic given a seeded rand and stays within its attempt band", () => {
    // With rand()=0 we get the low edge of the band; rand()=1 the high edge. Both <= exp <= cap.
    expect(backoffDelay(1, () => 0)).toBeLessThan(backoffDelay(1, () => 1));
    expect(backoffDelay(2, () => 1)).toBeLessThanOrEqual(BACKOFF_CAP_MS);
  });

  it("returns a non-negative integer", () => {
    const d = backoffDelay(2, () => 0.5);
    expect(Number.isInteger(d)).toBe(true);
    expect(d).toBeGreaterThanOrEqual(0);
  });
});

describe("classifyPartError", () => {
  it("routes an expired presigned URL (403) to a URL refresh", () => {
    expect(classifyPartError(403, "http")).toBe("refresh");
  });

  it("retries transport-level failures regardless of status", () => {
    expect(classifyPartError(null, "stall")).toBe("retry");
    expect(classifyPartError(null, "timeout")).toBe("retry");
    expect(classifyPartError(null, "network")).toBe("retry");
  });

  it("retries 5xx (server-side, transient)", () => {
    expect(classifyPartError(500, "http")).toBe("retry");
    expect(classifyPartError(503, "http")).toBe("retry");
  });

  it("retries rate-limit / request-timeout status codes", () => {
    expect(classifyPartError(429, "http")).toBe("retry");
    expect(classifyPartError(408, "http")).toBe("retry");
  });

  it("treats other 4xx as fatal (a real client error, not worth retrying)", () => {
    expect(classifyPartError(400, "http")).toBe("fatal");
    expect(classifyPartError(404, "http")).toBe("fatal");
  });
});

describe("classifyResumeError", () => {
  // The multipart parts already sit in R2, so a resume-probe failure must NOT nuke the checkpoint
  // (→ silent restart from 0%) unless the multipart is DEFINITIVELY gone. This is the core of the
  // "login drops mid-long-upload → upload restarts from scratch" fix.
  it("treats an expired/absent token (401/403) as an auth blip — refresh & retry, keep the checkpoint", () => {
    expect(classifyResumeError(401)).toBe("auth");
    expect(classifyResumeError(403)).toBe("auth");
  });

  it("discards the checkpoint ONLY on an explicit 404 (the multipart is genuinely gone)", () => {
    expect(classifyResumeError(404)).toBe("gone");
  });

  it("treats server hiccups (5xx) as transient — retry, keep the checkpoint", () => {
    expect(classifyResumeError(500)).toBe("transient");
    expect(classifyResumeError(502)).toBe("transient");
    expect(classifyResumeError(503)).toBe("transient");
  });

  it("treats rate-limit / request-timeout as transient", () => {
    expect(classifyResumeError(429)).toBe("transient");
    expect(classifyResumeError(408)).toBe("transient");
  });

  it("treats a transport-level failure (null status / network) as transient", () => {
    expect(classifyResumeError(null)).toBe("transient");
  });

  it("keeps the checkpoint on any OTHER 4xx (never auto-restart from 0% on an ambiguous error)", () => {
    // 400/413/422 etc. — retrying is harmless and, crucially, we must not lose the uploaded parts.
    expect(classifyResumeError(400)).toBe("transient");
    expect(classifyResumeError(413)).toBe("transient");
    expect(classifyResumeError(422)).toBe("transient");
  });
});
