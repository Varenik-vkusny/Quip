"use client";

import { useState } from "react";
import Link from "next/link";
import { Numeral } from "@/components/ui/Numeral";
import { mmss } from "@/lib/format";
import { kindEmoji } from "@/lib/momentKinds";
import { momentClipId, type ClipLike } from "@/lib/videoMapCoverage";
import { clipNum } from "@/lib/videoMapNarrative";
import type { VideoMoment } from "@/lib/types";
import { ExtractButton, type ExtractState } from "./ExtractButton";

// One moment row. Every element has ONE clearly-labeled job so it's obvious what a click does:
//   • the row (emoji + title) and the chevron → expand/collapse the "why"
//   • a labeled clip action on the right → "Open clip N →" (this moment lives in that clip) or
//     "Extract" (un-clipped); visible at a glance so clipped vs untapped is legible
//   • inside the expanded panel → "▶ Play from mm:ss" seeks the source preview to the moment
// No more one-click-does-two-things (the old title-click both seeked AND toggled).

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      className={`size-3.5 shrink-0 text-faint transition-transform duration-200 ${
        open ? "rotate-180" : ""
      }`}
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
  );
}

export function MomentRow({
  moment,
  clips,
  jobId,
  onSeek,
  extractState = "gated",
  onExtract,
}: {
  moment: VideoMoment;
  clips: ClipLike[];
  jobId: string;
  onSeek: (sec: number) => void;
  extractState?: ExtractState;
  onExtract?: (start: number, end: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const clipId = momentClipId(clips, moment);
  const emoji = kindEmoji(moment.kind);

  return (
    <div>
      <div className="flex items-center gap-1.5">
        {/* Emoji + title + chevron → expand/collapse the description. */}
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          className="group flex min-w-0 flex-1 items-center gap-2 rounded-md px-1 py-2 text-left transition-colors hover:bg-surface-2/60"
        >
          <span className="shrink-0 text-base leading-none" aria-hidden>
            {emoji}
          </span>
          <span className="min-w-0 flex-1 truncate text-sm font-medium text-ink">
            {moment.label}
          </span>
          <Chevron open={open} />
        </button>

        {/* The clip action — clearly labeled, visible at a glance. */}
        <div className="shrink-0">
          {clipId ? (
            <Link
              href={`/edit/${jobId}/${clipId}`}
              className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-muted transition-colors duration-150 hover:bg-surface-2/60 hover:text-ink"
            >
              Open clip {clipNum(clipId)}
              <span aria-hidden>→</span>
            </Link>
          ) : (
            <ExtractButton
              state={extractState}
              onExtract={onExtract ? () => onExtract(moment.start, moment.end) : undefined}
            />
          )}
        </div>
      </div>

      {open && (
        <div className="space-y-2 pb-1.5 pl-[30px] pr-1 pt-0.5">
          <p className="text-xs leading-relaxed text-muted">{moment.why}</p>
          <button
            type="button"
            onClick={() => onSeek(moment.start)}
            className="inline-flex items-center gap-1.5 rounded-md border border-line px-2 py-1 text-xs font-medium text-muted transition-colors duration-150 hover:border-line-strong hover:text-ink"
          >
            <svg viewBox="0 0 10 10" className="size-2.5 fill-current" aria-hidden>
              <path d="M2 1l6 4-6 4z" />
            </svg>
            Play from <Numeral className="tabular-nums">{mmss(moment.start)}</Numeral>
          </button>
        </div>
      )}
    </div>
  );
}
