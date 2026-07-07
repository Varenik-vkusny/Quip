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

// The Video Map cockpit: source preview + whole-video timeline + clickable outline + demoted
// narrative, all sharing ONE playhead. `initialMap` (fixture harness) short-circuits the poll;
// otherwise it keeps the old VideoMap poll (getVideoMap every 2500ms while pending, error+Retry).

function PendingSkeleton() {
  return (
    <div className="space-y-3 py-4" role="status" aria-label="AI is reading the video…">
      <p className="text-sm text-muted">
        Reading the whole video to map its strongest moments
        <span className="text-faint"> — usually under a minute.</span>
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
      // No synchronous setState here (this runs inside the mount effect) — the error is cleared in
      // the Retry handler, an event where setState is allowed. Success/failure set state async in
      // the promise callbacks, which the effect lint permits.
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
  const momentCount = allMoments.length;
  const untappedCount = allMoments.filter(
    (m) => classifyMoment(clipList, m) === "untapped",
  ).length;

  const ready = map?.status === "done" && (chapters.length > 0 || !!map.narrative);
  const summaryLine = ready
    ? [
        `${chapters.length} chapter${chapters.length === 1 ? "" : "s"}`,
        `${momentCount} moment${momentCount === 1 ? "" : "s"}`,
        `${clipCount} clip${clipCount === 1 ? "" : "s"}`,
        `${untappedCount} untapped`,
      ].join(" · ")
    : null;

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
            The full picture of your video — and where every clip came from.
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
          <div className="grid gap-4 lg:grid-cols-2">
            {/* Source preview (left). Only defaulted from a real previewUrl — for a real job with no
                preview wired yet we show a calm placeholder rather than guess a 404-prone URL. */}
            <div>
              {previewUrl ? (
                <SourcePreview previewUrl={previewUrl} onTime={setPlayheadSec} seekRef={seekRef} />
              ) : (
                <div className="flex aspect-video w-full items-center justify-center rounded-lg border border-dashed border-line bg-surface-2 px-4 text-center">
                  <p className="text-xs text-faint">
                    Source preview isn’t available for this project yet.
                  </p>
                </div>
              )}
            </div>

            {/* Outline (right) */}
            <MapOutline
              chapters={chapters}
              clips={clipList}
              jobId={jobId}
              onSeek={seek}
            />
          </div>

          {/* Whole-video timeline (full width) */}
          <div className="rounded-lg border border-line bg-surface px-4 py-4">
            <MapTimeline
              chapters={chapters}
              clips={clipList}
              durationSec={durationSec}
              playheadSec={playheadSec}
              onSeek={seek}
              onSelectRange={setSelection}
            />
            {selection && (
              <div className="mt-3 flex flex-wrap items-center justify-between gap-3 border-t border-line pt-3">
                <span className="text-xs text-muted">
                  Selected{" "}
                  <Numeral className="text-ink">
                    {mmss(selection.start)}–{mmss(selection.end)}
                  </Numeral>
                </span>
                <ExtractButton state="coming" disabled />
              </div>
            )}
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
