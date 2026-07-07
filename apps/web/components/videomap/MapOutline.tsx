"use client";

import { Eyebrow } from "@/components/ui/Eyebrow";
import { Numeral } from "@/components/ui/Numeral";
import { mmss } from "@/lib/format";
import { KIND_COLOR, KIND_KEYS } from "@/lib/momentKinds";
import { type ClipLike } from "@/lib/videoMapCoverage";
import type { VideoChapter } from "@/lib/types";
import { MomentRow } from "./MomentRow";

// The clickable outline — chapters as collapsible <details>, each listing its moments as
// clickable/extractable rows. Mirrors the old ChapterItem accordion but seek-first (clicking a
// moment jumps the source preview) and moment-centric (●/○) rather than a wall of clip chips.

function ChapterOutlineItem({
  chapter,
  clips,
  jobId,
  onSeek,
  defaultOpen,
}: {
  chapter: VideoChapter;
  clips: ClipLike[];
  jobId: string;
  onSeek: (sec: number) => void;
  defaultOpen: boolean;
}) {
  const clipIds = chapter.clip_ids ?? [];
  const moments = chapter.moments ?? [];

  return (
    <details open={defaultOpen} className="group border-b border-line last:border-b-0">
      <summary
        className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3 text-ink [&::-webkit-details-marker]:hidden"
        style={{ minHeight: "44px" }}
      >
        <div className="min-w-0 flex-1">
          <button
            type="button"
            onClick={(e) => {
              // Seek without toggling the accordion; the chevron/summary area still toggles.
              e.preventDefault();
              e.stopPropagation();
              onSeek(chapter.start);
            }}
            className="text-left text-sm font-semibold leading-snug hover:text-accent"
          >
            {chapter.title}
          </button>
          <Numeral className="ml-2 whitespace-nowrap text-xs text-faint">
            {mmss(chapter.start)}–{mmss(chapter.end)}
          </Numeral>
          {clipIds.length > 0 && (
            <span className="ml-2 font-mono text-eyebrow uppercase tabular-nums text-ink">
              {clipIds.length} clip{clipIds.length === 1 ? "" : "s"}
            </span>
          )}
          {moments.length > 0 && (
            <span className="ml-2 font-mono text-eyebrow uppercase tabular-nums text-muted">
              {moments.length} moment{moments.length === 1 ? "" : "s"}
            </span>
          )}
        </div>
        <svg
          className="size-4 shrink-0 text-muted transition-transform duration-200 ease-snappy group-open:rotate-180"
          viewBox="0 0 16 16"
          fill="none"
          aria-hidden
        >
          <path
            d="M4 6l4 4 4-4"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </summary>

      <div className="space-y-3 px-4 pb-4">
        {chapter.summary && (
          <p className="text-sm leading-relaxed text-muted">{chapter.summary}</p>
        )}
        {moments.length > 0 && (
          <div className="space-y-2">
            {moments.map((m, i) => (
              <MomentRow key={i} moment={m} clips={clips} jobId={jobId} onSeek={onSeek} />
            ))}
          </div>
        )}
      </div>
    </details>
  );
}

function Legend() {
  return (
    <div className="flex flex-wrap gap-x-3 gap-y-1.5 text-xs text-muted">
      {KIND_KEYS.map((k) => (
        <span key={k} className="flex items-center gap-1.5">
          <span className={`size-2 rounded-pill ${KIND_COLOR[k].dot}`} aria-hidden />
          {KIND_COLOR[k].label}
        </span>
      ))}
    </div>
  );
}

export function MapOutline({
  chapters,
  clips,
  jobId,
  onSeek,
}: {
  chapters: VideoChapter[];
  clips: ClipLike[];
  jobId: string;
  onSeek: (sec: number) => void;
}) {
  return (
    <div className="overflow-hidden rounded-lg border border-line bg-surface">
      <div className="flex items-center justify-between gap-3 border-b border-line bg-surface-2 px-4 py-2.5">
        <Eyebrow tone="faint">Outline</Eyebrow>
        <Legend />
      </div>
      <div className="max-h-[560px] overflow-y-auto">
        {chapters.map((ch, i) => (
          <ChapterOutlineItem
            key={i}
            chapter={ch}
            clips={clips}
            jobId={jobId}
            onSeek={onSeek}
            defaultOpen={i === 0}
          />
        ))}
      </div>
    </div>
  );
}
