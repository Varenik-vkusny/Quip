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

// The Video Map cockpit: whole-video timeline (the map) on top, then a sticky media rail (source
// preview + a contextual you-are-here / extract panel) beside the clickable outline, then the
// demoted narrative. Everything shares ONE playhead. `initialMap` (fixture harness) short-circuits
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
  const seekRef = useRef<((sec: number) => void) | null>(null);
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

  const seek = useCallback((sec: number) => {
    setPlayheadSec(sec);
    seekRef.current?.(sec);
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

          {/* Whole-video timeline — the map. Full width, on top. */}
          <div className="rounded-lg border border-line bg-surface px-4 py-4">
            <MapTimeline
              chapters={chapters}
              clips={clipList}
              durationSec={durationSec}
              playheadSec={playheadSec}
              onSeek={seek}
              onSelectRange={setSelection}
            />
          </div>

          {/* Media rail (sticky) + outline */}
          <div className="grid gap-4 lg:grid-cols-[minmax(0,400px)_1fr] lg:items-start">
            <div className="space-y-3 lg:sticky lg:top-4">
              {previewUrl ? (
                <SourcePreview previewUrl={previewUrl} onTime={setPlayheadSec} seekRef={seekRef} />
              ) : (
                <div className="flex aspect-video w-full items-center justify-center rounded-lg border border-dashed border-line bg-surface-2 px-4 text-center">
                  <p className="text-xs text-faint">
                    Source preview is not available for this project yet.
                  </p>
                </div>
              )}

              {/* Contextual panel: a drag-selection becomes an extract target; otherwise a quiet
                  you-are-here so the rail never reads as empty space. */}
              <div className="rounded-lg border border-line bg-surface px-4 py-3">
                {selection ? (
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-sm text-muted">
                      Selected{" "}
                      <Numeral className="tabular-nums text-ink">
                        {mmss(selection.start)}-{mmss(selection.end)}
                      </Numeral>
                    </span>
                    <ExtractButton
                      state={extractState}
                      onExtract={() => handleExtract(selection.start, selection.end)}
                    />
                  </div>
                ) : nowChapter ? (
                  <p className="text-xs leading-relaxed">
                    <Numeral className="tabular-nums text-faint">{mmss(playheadSec)}</Numeral>{" "}
                    <span className="text-muted">{nowChapter.title}</span>
                  </p>
                ) : (
                  <p className="text-xs leading-relaxed text-muted">
                    Click a moment to jump here, or drag the timeline to pull a new clip.
                  </p>
                )}
              </div>
            </div>

            <MapOutline
              chapters={chapters}
              clips={clipList}
              jobId={jobId}
              onSeek={seek}
              extractState={extractState}
              onExtract={handleExtract}
            />
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
