"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Eyebrow } from "@/components/ui/Eyebrow";
import { Numeral } from "@/components/ui/Numeral";
import { Skeleton } from "@/components/ui/Skeleton";
import { createClipFromRange, getVideoMap } from "@/lib/api";
import { mmss } from "@/lib/format";
import { canExtractClip, EXTRACT } from "@/lib/plans";
import { classifyMoment, type ClipLike } from "@/lib/videoMapCoverage";
import type { ClipOut, VideoMap } from "@/lib/types";
import { ExtractButton, type ExtractState } from "./ExtractButton";
import { MapOutline } from "./MapOutline";
import { MapTimeline } from "./MapTimeline";
import { NarrativeCollapse } from "./NarrativeCollapse";
import { SourcePreview } from "./SourcePreview";

// The Video Map cockpit: a left rail (source preview → the whole-video timeline/scrubber → a
// contextual you-are-here / extract panel) beside the clickable outline, then the demoted narrative.
// The timeline sits directly under the video so the rail has no dead space and both columns end near
// the same height. Everything shares ONE playhead (click = seek+play; timeline drag = scrub, seek
// without play). `initialMap` (fixture harness) short-circuits
// the poll. An un-clipped moment (or a timeline drag-select) can be EXTRACTED into a new clip via
// POST /jobs/{id}/clips (the worker enforces the Pro/Studio gate + free-cap paywall); the new clip
// is merged optimistically so the moment flips to "Clip N" and coverage updates live. `demoMode`
// (fixture, no backend) simulates that merge so the flow is viewable without a worker.

function PendingSkeleton() {
  return (
    <div className="space-y-3 py-4" role="status" aria-label="AI is reading the video…">
      <p className="text-sm text-muted">
        Reading the whole video to map its strongest moments.
        <span className="text-faint"> Usually under a minute.</span>
      </p>
      <Skeleton className="h-4 w-full" />
      <Skeleton className="h-4 w-5/6" />
      <Skeleton className="h-40 w-full" />
    </div>
  );
}

export function VideoMapCockpit({
  jobId,
  clips = [],
  initialMap,
  previewUrl,
  plan,
  demoMode = false,
}: {
  jobId: string;
  clips?: ClipOut[];
  initialMap?: VideoMap;
  previewUrl?: string;
  /** Owner's plan — drives the Extract button's appearance only; the worker enforces the real gate. */
  plan?: string | null;
  /** Fixture harness: simulate extraction locally (no backend) so the flow is viewable. */
  demoMode?: boolean;
}) {
  const [map, setMap] = useState<VideoMap | null>(initialMap ?? null);
  const [error, setError] = useState<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // ONE shared playhead: SourcePreview pushes source time here; outline/timeline seek through the
  // imperative seekRef (published by SourcePreview) so a jump doesn't wait on a React re-render.
  const [playheadSec, setPlayheadSec] = useState(0);
  const seekRef = useRef<((sec: number, opts?: { play?: boolean }) => void) | null>(null);
  const [selection, setSelection] = useState<{ start: number; end: number } | null>(null);

  // Clips pulled this session (optimistic): merged over the prop clips so classification/coverage
  // update immediately. Only id/start/end are needed downstream.
  const [extraClips, setExtraClips] = useState<ClipLike[]>([]);
  const [notice, setNotice] = useState<{ tone: "info" | "warn"; text: string } | null>(null);

  // The pending re-poll recurses; route it through a ref so `poll` never references itself before
  // it's declared (react-hooks/immutability).
  const pollRef = useRef<(retry?: boolean) => void>(() => {});
  const poll = useCallback(
    (retry = false) => {
      getVideoMap(jobId, retry)
        .then((data) => {
          setMap(data);
          if (data.status === "pending") {
            timerRef.current = setTimeout(() => pollRef.current(false), 2500);
          }
          if (data.status === "failed") {
            setError(data.error ?? "Couldn’t build the video map");
          }
        })
        .catch((err: unknown) => {
          setError(err instanceof Error ? err.message : "Failed to load the video map");
        });
    },
    [jobId],
  );
  useEffect(() => {
    pollRef.current = poll;
  }, [poll]);

  useEffect(() => {
    if (initialMap) return; // fixture path: data is already here, never poll
    poll(false);
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [initialMap, poll]);

  // Click-to-jump: move the playhead AND play from there.
  const seek = useCallback((sec: number) => {
    setPlayheadSec(sec);
    seekRef.current?.(sec, { play: true });
  }, []);
  // Scrub: seek the frame WITHOUT play, so dragging the timeline previews without auto-playing.
  const scrub = useCallback((sec: number) => {
    setPlayheadSec(sec);
    seekRef.current?.(sec, { play: false });
  }, []);

  const chapters = map?.chapters ?? [];
  const allMoments = chapters.flatMap((ch) => ch.moments ?? []);

  // Merge prop clips + this-session extractions (dedupe by id). Downstream only needs id/start/end.
  const clipList: ClipLike[] = useMemo(() => {
    const seen = new Set<string>();
    const out: ClipLike[] = [];
    for (const c of [...clips.map((c) => ({ id: c.id, start: c.start, end: c.end })), ...extraClips]) {
      if (!seen.has(c.id)) {
        seen.add(c.id);
        out.push(c);
      }
    }
    return out;
  }, [clips, extraClips]);

  // duration = the furthest edge across chapters, clips, and moments (seed 1 so Math.max is finite).
  const durationSec = Math.max(
    1,
    ...chapters.map((c) => c.end),
    ...clipList.map((c) => c.end),
    ...allMoments.map((m) => m.end),
  );

  const clipCount = clipList.length;
  const untappedCount = allMoments.filter(
    (m) => classifyMoment(clipList, m) === "untapped",
  ).length;

  const ready = map?.status === "done" && (chapters.length > 0 || !!map.narrative);

  // Calm summary — no middot chain, no uppercase. The two numbers an editor acts on.
  const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;
  const summaryLine = ready
    ? untappedCount > 0
      ? `${plural(clipCount, "clip")}, ${plural(untappedCount, "untapped moment")}`
      : plural(clipCount, "clip")
    : null;

  // The chapter under the playhead — a quiet "you are here" for the media rail.
  const nowChapter =
    chapters.find((c) => playheadSec >= c.start && playheadSec < c.end) ?? chapters[0];

  // Extract-button appearance (worker is authoritative on the gate; this is UI only): non-paid → an
  // upgrade nudge; at/over the free cap → paywall; else the live accent CTA.
  const extractState: ExtractState = !canExtractClip(plan)
    ? "gated"
    : clipCount >= EXTRACT.freeCap
      ? "paywall"
      : "available";

  const handleExtract = useCallback(
    async (start: number, end: number) => {
      setSelection(null);
      if (demoMode) {
        // No backend in the fixture harness — simulate the merge so the flow is viewable.
        const id = `clip_${clips.length + extraClips.length + 1}`;
        setExtraClips((prev) => [...prev, { id, start, end: Math.max(end, start + 20) }]);
        setNotice({ tone: "info", text: "Rendering your new clip, it appears here as it finishes." });
        return;
      }
      const r = await createClipFromRange(jobId, { start, end });
      if (r.ok) {
        setExtraClips((prev) => [
          ...prev,
          { id: r.clip.id, start: r.clip.start, end: r.clip.end },
        ]);
        setNotice({ tone: "info", text: "Pulling your clip, it appears here as it renders." });
      } else {
        setNotice({ tone: "warn", text: r.message });
      }
    },
    [demoMode, jobId, clips, extraClips],
  );

  return (
    <section className="mb-6 space-y-4">
      {/* Header */}
      <header>
        <Eyebrow tone="faint">Video map</Eyebrow>
        <h2 className="mt-1.5 font-display text-h3 tracking-tight text-ink">Your video, mapped</h2>
        {summaryLine ? (
          <Numeral className="mt-1 block text-xs text-muted">{summaryLine}</Numeral>
        ) : (
          <p className="mt-1 text-xs text-muted">
            The full picture of your video, and where every clip came from.
          </p>
        )}
      </header>

      {/* Pending / error / empty (poll path only — the fixture is always done) */}
      {!ready && (
        <div className="rounded-lg border border-line bg-surface px-5 py-4">
          {error ? (
            <div className="space-y-3">
              <p className="text-sm text-bad">{error}</p>
              <button
                type="button"
                onClick={() => {
                  setError(null);
                  poll(true);
                }}
                className="inline-flex h-9 items-center gap-2 rounded-md border border-line bg-surface-2 px-3.5 text-sm font-semibold text-ink transition-colors duration-150 hover:border-line-strong"
              >
                Retry
              </button>
            </div>
          ) : map?.status === "done" ? (
            <p className="text-sm text-muted">No video map yet.</p>
          ) : (
            <PendingSkeleton />
          )}
        </div>
      )}

      {/* Cockpit */}
      {ready && (
        <>
          {notice && (
            <div
              className={`rounded-lg border px-4 py-2.5 text-sm ${
                notice.tone === "warn"
                  ? "border-line bg-surface-2 text-ink"
                  : "border-line bg-surface text-muted"
              }`}
            >
              {notice.text}
            </div>
          )}

          {/* Left rail (preview → timeline → contextual panel) beside the outline. Stacking the
              timeline UNDER the short 16:9 preview fills what used to be dead space below it, and
              the two columns now end near the same height. */}
          <div className="grid gap-4 lg:grid-cols-[1fr_minmax(320px,380px)] lg:items-start">
            <div className="space-y-4">
              {previewUrl ? (
                <SourcePreview previewUrl={previewUrl} onTime={setPlayheadSec} seekRef={seekRef} />
              ) : (
                <div className="flex aspect-video w-full items-center justify-center rounded-lg border border-dashed border-line bg-surface-2 px-4 text-center">
                  <p className="text-xs text-faint">
                    Source preview is not available for this project yet.
                  </p>
                </div>
              )}

              {/* Whole-video timeline — the scrubber, directly under the video. */}
              <div className="rounded-lg border border-line bg-surface px-4 py-4">
                <MapTimeline
                  chapters={chapters}
                  clips={clipList}
                  durationSec={durationSec}
                  playheadSec={playheadSec}
                  onSeek={seek}
                  onScrub={scrub}
                  onSelectRange={setSelection}
                />
              </div>

              {/* Contextual panel. A drag-selection becomes a LOUD extract target (accent bar so it's
                  unmissable); otherwise a quiet you-are-here so the rail never reads as empty space. */}
              <div
                className={`rounded-lg border px-4 py-3 transition-colors duration-150 ${
                  selection ? "border-accent/60 bg-accent/10" : "border-line bg-surface"
                }`}
              >
                {selection ? (
                  <div className="flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <span className="block text-xs font-medium text-accent">Selected range</span>
                      <Numeral className="mt-0.5 block text-sm font-semibold tabular-nums text-ink">
                        {mmss(selection.start)}-{mmss(selection.end)}
                      </Numeral>
                    </div>
                    <div className="flex shrink-0 items-center gap-3">
                      <button
                        type="button"
                        onClick={() => setSelection(null)}
                        className="text-xs font-medium text-muted transition-colors duration-150 hover:text-ink"
                      >
                        Clear
                      </button>
                      <ExtractButton
                        state={extractState}
                        onExtract={() => handleExtract(selection.start, selection.end)}
                      />
                    </div>
                  </div>
                ) : nowChapter ? (
                  <p className="text-xs leading-relaxed">
                    <Numeral className="tabular-nums text-faint">{mmss(playheadSec)}</Numeral>{" "}
                    <span className="text-muted">{nowChapter.title}</span>
                  </p>
                ) : (
                  <p className="text-xs leading-relaxed text-muted">
                    Click a moment to jump here, or use Select on the timeline to pull a new clip.
                  </p>
                )}
              </div>
            </div>

            {/* Outline — sticky on desktop, height-capped to balance the left rail. */}
            <div className="lg:sticky lg:top-4">
              <MapOutline
                chapters={chapters}
                clips={clipList}
                jobId={jobId}
                onSeek={seek}
                extractState={extractState}
                onExtract={handleExtract}
              />
            </div>
          </div>

          {/* Demoted narrative */}
          {map?.narrative && (
            <NarrativeCollapse narrative={map.narrative} jobId={jobId} clips={clipList} />
          )}
        </>
      )}
    </section>
  );
}
