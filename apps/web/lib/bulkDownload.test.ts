// Prod bug 2026-07-14: «Скачать (N)» returned {"detail":"missing media ticket"} — and later
// {"detail":"clip export busy"} — as DOWNLOADED FILES.
//
// Chain: /export/captioned.mp4 302s a baked clip to cdn.quip.ink → the browser, per the Fetch spec,
// rewrites the origin to opaque on a cross-origin redirect and sends `Origin: null` → the bucket
// echoes CORS only for explicit origins (verified live: `Origin: null` → 200, NO
// Access-Control-Allow-Origin) → the blob fetch dies with a CORS TypeError → the catch fell back to
// a bare <a download> with neither a header nor a ?mt= ticket → 401 JSON, saved as the "clip".
//
// These tests pin the two decisions that fix it: go STRAIGHT to the baked CDN object (no redirect to
// follow), and treat a 503 as "wait", not as "fail".

import { describe, expect, it } from "vitest";

import { BUSY_MAX_ATTEMPTS, busyRetryDelayMs, captionedCdnUrl, isBusyStatus } from "./bulkDownload";

const CDN = "https://cdn.quip.ink/job_abc/clip_02.mp4";

describe("captionedCdnUrl — reach the baked object WITHOUT the redirect that breaks CORS", () => {
  it("derives the captioned object from the clean CDN url", () => {
    expect(captionedCdnUrl(CDN, "clip_02")).toBe(
      "https://cdn.quip.ink/job_abc/clip_02_captioned.mp4",
    );
  });

  it("preserves the ?v= cache-buster (a re-render reuses the same R2 key)", () => {
    expect(captionedCdnUrl(`${CDN}?v=7`, "clip_02")).toBe(
      "https://cdn.quip.ink/job_abc/clip_02_captioned.mp4?v=7",
    );
  });

  it("REFUSES a presigned url — the path is signed, rewriting it would 403", () => {
    const signed = `${CDN}?X-Amz-Signature=abc&X-Amz-Expires=3600`;
    expect(captionedCdnUrl(signed, "clip_02")).toBeNull();
  });

  it("refuses when the filename is not exactly <clipId>.mp4 (never guess)", () => {
    expect(captionedCdnUrl("https://cdn.quip.ink/job_abc/other.mp4", "clip_02")).toBeNull();
    expect(captionedCdnUrl("https://cdn.quip.ink/job_abc/clip_02_captioned.mp4", "clip_02")).toBeNull();
  });

  it("refuses empty / malformed input instead of building a broken link", () => {
    expect(captionedCdnUrl(null, "clip_02")).toBeNull();
    expect(captionedCdnUrl("", "clip_02")).toBeNull();
    expect(captionedCdnUrl("not a url", "clip_02")).toBeNull();
  });
});

describe("busyRetryDelayMs — a 503 is the server saying WAIT, not FAIL", () => {
  it("honours Retry-After (seconds)", () => {
    expect(busyRetryDelayMs(1, "5")).toBe(5000);
    expect(busyRetryDelayMs(3, "2")).toBe(2000);
  });

  it("caps a hostile Retry-After so one clip cannot stall the batch forever", () => {
    expect(busyRetryDelayMs(1, "99999")).toBe(30_000);
  });

  it("falls back to exponential backoff when the header is absent or junk", () => {
    expect(busyRetryDelayMs(1)).toBe(2000);
    expect(busyRetryDelayMs(2)).toBe(4000);
    expect(busyRetryDelayMs(3)).toBe(8000);
    expect(busyRetryDelayMs(1, "soon")).toBe(2000);
    expect(busyRetryDelayMs(1, "-3")).toBe(2000);
  });

  it("caps the backoff", () => {
    expect(busyRetryDelayMs(10)).toBe(20_000);
  });

  it("the attempt budget is bounded (a permanently busy server must not spin forever)", () => {
    expect(BUSY_MAX_ATTEMPTS).toBeGreaterThan(3);
    expect(BUSY_MAX_ATTEMPTS).toBeLessThan(20);
  });
});

describe("isBusyStatus", () => {
  it("503 and 429 mean busy", () => {
    expect(isBusyStatus(503)).toBe(true);
    expect(isBusyStatus(429)).toBe(true);
  });

  it("everything else is a real failure (401/404/500 must NOT be retried as 'busy')", () => {
    for (const s of [200, 401, 403, 404, 500, 504]) expect(isBusyStatus(s)).toBe(false);
  });
});
