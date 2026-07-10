import { describe, expect, it } from "vitest";
import {
  captionedDownloadUrl,
  clipCaptionedExportUrl,
  clipCleanExportUrl,
  clipSrtUrl,
  shouldRefreshToken,
  withToken,
} from "./api";

const BASE = "https://worker.example";
const JOB = "job_123";
const CLIP = "clip_abc";

describe("per-clip export URL builders", () => {
  it("builds the on-demand clean MP4 endpoint", () => {
    expect(clipCleanExportUrl(BASE, JOB, CLIP)).toBe(
      "https://worker.example/jobs/job_123/clips/clip_abc/export/clean.mp4",
    );
  });

  it("builds the SRT endpoint", () => {
    expect(clipSrtUrl(BASE, JOB, CLIP)).toBe(
      "https://worker.example/jobs/job_123/clips/clip_abc/export.srt",
    );
  });

  it("builds the on-demand captioned MP4 endpoint", () => {
    expect(clipCaptionedExportUrl(BASE, JOB, CLIP)).toBe(
      "https://worker.example/jobs/job_123/clips/clip_abc/export/captioned.mp4",
    );
  });

  it("works with an empty base (relative worker path)", () => {
    expect(clipSrtUrl("", JOB, CLIP)).toBe("/jobs/job_123/clips/clip_abc/export.srt");
  });
});

describe("captionedDownloadUrl", () => {
  it("uses the baked CDN render when present and not dirty (fast path)", () => {
    const baked = "https://cdn.example/renders/clip_abc.mp4";
    expect(captionedDownloadUrl(BASE, JOB, CLIP, baked, false)).toBe(baked);
  });

  it("falls back to on-demand render when dirty (baked is stale)", () => {
    const baked = "https://cdn.example/renders/clip_abc.mp4";
    expect(captionedDownloadUrl(BASE, JOB, CLIP, baked, true)).toBe(
      clipCaptionedExportUrl(BASE, JOB, CLIP),
    );
  });

  it("falls back to on-demand render when there is no baked URL", () => {
    expect(captionedDownloadUrl(BASE, JOB, CLIP, null, false)).toBe(
      clipCaptionedExportUrl(BASE, JOB, CLIP),
    );
  });
});

describe("shouldRefreshToken (proactive refresh so long uploads don't finalize with a dead token)", () => {
  const NOW = 1_000_000; // ms; token expiries below are seconds relative to NOW/1000 = 1000s

  it("no expiry info → don't force a refresh", () => {
    expect(shouldRefreshToken(undefined, NOW)).toBe(false);
    expect(shouldRefreshToken(null, NOW)).toBe(false);
    expect(shouldRefreshToken(0, NOW)).toBe(false);
  });

  it("token comfortably valid (10 min out) → no refresh", () => {
    expect(shouldRefreshToken(NOW / 1000 + 600, NOW)).toBe(false);
  });

  it("token within the 120s skew → refresh (this is the 9GB-upload finalize case)", () => {
    expect(shouldRefreshToken(NOW / 1000 + 60, NOW)).toBe(true);
  });

  it("token already expired → refresh", () => {
    expect(shouldRefreshToken(NOW / 1000 - 100, NOW)).toBe(true);
  });

  it("exactly at the skew boundary → refresh (<=)", () => {
    expect(shouldRefreshToken(NOW / 1000 + 120, NOW)).toBe(true);
  });

  it("respects a custom skew window", () => {
    expect(shouldRefreshToken(NOW / 1000 + 200, NOW, 300_000)).toBe(true);
    expect(shouldRefreshToken(NOW / 1000 + 200, NOW, 60_000)).toBe(false);
  });
});

describe("withToken", () => {
  const URL_NO_QUERY = "https://worker.example/jobs/job_123/preview.mp4";

  it("appends the scoped media ticket under the `mt` key by default (NOT `token`, NOT a JWT)", () => {
    expect(withToken(URL_NO_QUERY, "opaque_ticket")).toBe(`${URL_NO_QUERY}?mt=opaque_ticket`);
  });

  it("joins with `&` when the url already has a query string", () => {
    expect(withToken(`${URL_NO_QUERY}?v=2`, "tkt")).toBe(`${URL_NO_QUERY}?v=2&mt=tkt`);
  });

  it("url-encodes the ticket value", () => {
    expect(withToken(URL_NO_QUERY, "a b/c+d")).toBe(`${URL_NO_QUERY}?mt=a%20b%2Fc%2Bd`);
  });

  it("is a pure no-op without a token (dev / no-auth) or an empty url", () => {
    expect(withToken(URL_NO_QUERY, null)).toBe(URL_NO_QUERY);
    expect(withToken(URL_NO_QUERY, undefined)).toBe(URL_NO_QUERY);
    expect(withToken(URL_NO_QUERY, "")).toBe(URL_NO_QUERY);
    expect(withToken("", "tkt")).toBe("");
  });

  it("honors a custom query key when asked", () => {
    expect(withToken(URL_NO_QUERY, "tkt", "token")).toBe(`${URL_NO_QUERY}?token=tkt`);
  });
});
