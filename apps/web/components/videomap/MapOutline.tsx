"use client";

import { Numeral } from "@/components/ui/Numeral";
import { mmss } from "@/lib/format";
import { classifyMoment, type ClipLike } from "@/lib/videoMapCoverage";
import type { VideoChapter } from "@/lib/types";
import type { ExtractState } from "./ExtractButton";
import { MomentRow } from "./MomentRow";

// The clickable outline: chapters as clean collapsible sections, moments as flush hairline rows.
// Taste pass (2026-07-08) stripped the slop — no eyebrow header, no colour legend, no per-row status
// dots, no uppercase count badges. Structure comes from hierarchy + hairlines. The one number kept
// in a chapter header is "N untapped": the editor's actionable cue for where unmined clips still are.

function ChapterOutlineItem({
  chapter,
  clips,
  jobId,
  onSeek,
  defaultOpen,
  extractState,
  onExtract,
}: {
  chapter: VideoChapter;
  clips: ClipLike[];
  jobId: string;
  onSeek: (sec: number) => void;
  defaultOpen: boolean;
  extractState: ExtractState;
  onExtract?: (start: number, end: number) => void;
}) {
  const moments = chapter.moments ?? [];
  const untapped = moments.filter(
    (m) => classifyMoment(clips, m) === "untapped",
  ).length;

  return (
    <details open={defaultOpen} className="group border-b border-line last:border-b-0">
      <summary
        className="flex cursor-pointer list-none items-center gap-3 px-4 py-3 [&::-webkit-details-marker]:hidden"
        style={{ minHeight: "44px" }}
      >
        <button
          type="button"
          onClick={(e) => {
            // Seek without toggling the accordion; the rest of the summary still toggles.
            e.preventDefault();
            e.stopPropagation();
            onSeek(chapter.start);
          }}
          className="min-w-0 flex-1 text-left text-sm font-semibold leading-snug text-ink transition-colors duration-150 hover:text-accent"
        >
          {chapter.title}
        </button>
        <span className="flex shrink-0 items-center gap-2.5 text-xs">
          <Numeral className="tabular-nums text-faint">{mmss(chapter.start)}</Numeral>
          {untapped > 0 && <span className="text-muted">{untapped} untapped</span>}
        </span>
        <svg
          className="size-4 shrink-0 text-faint transition-transform duration-200 ease-snappy group-open:rotate-180"
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

      <div className="px-4 pb-3">
        {chapter.summary && (
          <p className="mb-1 text-sm leading-relaxed text-muted">{chapter.summary}</p>
        )}
        {moments.length > 0 && (
          <div className="divide-y divide-line">
            {moments.map((m, i) => (
              <MomentRow
                key={i}
                moment={m}
                clips={clips}
                jobId={jobId}
                onSeek={onSeek}
                extractState={extractState}
                onExtract={onExtract}
              />
            ))}
          </div>
        )}
      </div>
    </details>
  );
}

export function MapOutline({
  chapters,
  clips,
  jobId,
  onSeek,
  extractState,
  onExtract,
}: {
  chapters: VideoChapter[];
  clips: ClipLike[];
  jobId: string;
  onSeek: (sec: number) => void;
  extractState: ExtractState;
  onExtract?: (start: number, end: number) => void;
}) {
  return (
    <div className="max-h-[560px] overflow-y-auto rounded-lg border border-line bg-surface lg:max-h-[640px]">
      {chapters.map((ch, i) => (
        <ChapterOutlineItem
          key={i}
          chapter={ch}
          clips={clips}
          jobId={jobId}
          onSeek={onSeek}
          defaultOpen={i === 0}
          extractState={extractState}
          onExtract={onExtract}
        />
      ))}
    </div>
  );
}
