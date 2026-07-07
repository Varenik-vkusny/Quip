"use client";

import Link from "next/link";
import { Numeral } from "@/components/ui/Numeral";
import { mmss } from "@/lib/format";
import { kindColor } from "@/lib/momentKinds";
import { momentClipId, type ClipLike } from "@/lib/videoMapCoverage";
import { clipNum } from "@/lib/videoMapNarrative";
import type { VideoMoment } from "@/lib/types";
import { ExtractButton } from "./ExtractButton";

// One moment as a flush hairline row — no card, no colored status dot. Structure is typographic:
// title (ink) over a one-line why (muted) over a quiet meta line (time · kind). The right column is
// the single action: a clipped moment links to its clip, an untapped one offers Extract. (This is
// the fix for the old ○-moment lie — momentClipId returns null for a gap moment, so it no longer
// mislinks to a neighbour.) Kind is a calm faint word, not a colored-dot rainbow.

export function MomentRow({
  moment,
  clips,
  jobId,
  onSeek,
}: {
  moment: VideoMoment;
  clips: ClipLike[];
  jobId: string;
  onSeek: (sec: number) => void;
}) {
  const clipId = momentClipId(clips, moment);
  const kind = kindColor(moment.kind).label.toLowerCase();

  return (
    <div className="flex items-start gap-3 py-2.5">
      <button
        type="button"
        onClick={() => onSeek(moment.start)}
        className="group min-w-0 flex-1 text-left"
      >
        <span className="block text-sm font-medium leading-snug text-ink transition-colors duration-150 group-hover:text-accent">
          {moment.label}
        </span>
        <span className="mt-0.5 block line-clamp-2 text-xs leading-relaxed text-muted">
          {moment.why}
        </span>
        <span className="mt-1 block text-xs text-faint">
          <Numeral className="tabular-nums">{mmss(moment.start)}</Numeral>
          <span className="mx-1.5">·</span>
          {kind}
        </span>
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
          <ExtractButton state="coming" disabled />
        )}
      </div>
    </div>
  );
}
