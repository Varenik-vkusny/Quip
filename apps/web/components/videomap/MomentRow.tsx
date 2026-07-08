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

// One moment as a compact, scannable row: a kind emoji (the "emotion", colourful + legible) + the
// title + timecode, with the "why" description COLLAPSED by default. Clicking the row expands the
// why AND jumps the source there. The right column is the single action: a clipped moment links to
// its clip, an un-clipped one offers Extract (momentClipId returns null for a gap moment, so it no
// longer mislinks to a neighbour).

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
    <div className="py-2">
      <div className="flex items-center gap-2.5">
        <button
          type="button"
          onClick={() => {
            setOpen((o) => !o);
            onSeek(moment.start);
          }}
          aria-expanded={open}
          className="group flex min-w-0 flex-1 items-center gap-2 text-left"
        >
          <span className="shrink-0 text-base leading-none" aria-hidden>
            {emoji}
          </span>
          <span className="min-w-0 flex-1 truncate text-sm font-medium text-ink transition-colors duration-150 group-hover:text-accent">
            {moment.label}
          </span>
          <Numeral className="shrink-0 text-xs tabular-nums text-faint">
            {mmss(moment.start)}
          </Numeral>
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
        </button>

        <div className="shrink-0">
          {clipId ? (
            <Link
              href={`/edit/${jobId}/${clipId}`}
              className="text-xs font-medium text-muted underline-offset-4 transition-colors duration-150 hover:text-ink hover:underline"
            >
              Clip {clipNum(clipId)}
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
        <p className="mt-1 pl-[26px] pr-2 text-xs leading-relaxed text-muted">{moment.why}</p>
      )}
    </div>
  );
}
