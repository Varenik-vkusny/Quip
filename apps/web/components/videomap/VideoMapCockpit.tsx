"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Eyebrow } from "@/components/ui/Eyebrow";
import { Numeral } from "@/components/ui/Numeral";
import { Skeleton } from "@/components/ui/Skeleton";
import { getVideoMap } from "@/lib/api";
import { mmss } from "@/lib/format";
import { classifyMoment, type ClipLike } from "@/lib/videoMapCoverage";
import type { ClipOut, VideoMap } from "@/lib/types";
import { ExtractButton } from "./ExtractButton";
import { MapOutline } from "./MapOutline";
import { MapTimeline } from "./MapTimeline";
import { NarrativeCollapse } from "./NarrativeCollapse";
import { SourcePreview } from "./SourcePreview";

// The Video Map cockpit: whole-video timeline (the map) on top, then a sticky media rail (source
// preview + a contextual you-are-here / extract panel) beside the clickable outline, then the
// demoted narrative. Everything shares ONE playhead. `initialMap` (fixture harness) short-circuits
// the poll; otherwise it keeps the old VideoMap poll (getVideoMap every 2500ms while pending).

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
}: {
  jobId: string;
  clips?: ClipOut[];
  initialMap?: VideoMap;
  previewUrl?: string;
}) {
  const [map, setMap] = useState<VideoMap | null>(initialMap ?? null);
  const [error, setError] = useState<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // ONE shared playhead: SourcePreview pushes source time here; outline/timeline seek through the
  // imperative seekRef (published by SourcePreview) so a jump doesn't wait on a React re-render.
  const [playheadSec, setPlayheadSec] = useState(0);
  const seekRef = useRef<((sec: number) => void) | null>(null);
  const [selection, setSelection] = useState<{ start: number; end: number } | null>(null);

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
  const clipList: ClipLike[] = clips;

  // duration = the furthest edge across chapters, clips, and moments (seed 1 so Math.max is finite).
  const durationSec = Math.max(
    1,
    ...chapters.map((c) => c.end),
    ...clips.map((c) => c.end),
    ...allMoments.map((m) => m.end),
  );

  const clipCount = clips.length;
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
                    <ExtractButton state="coming" disabled />
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

            <MapOutline chapters={chapters} clips={clipList} jobId={jobId} onSeek={seek} />
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
