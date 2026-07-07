"use client";

import Link from "next/link";
import { Numeral } from "@/components/ui/Numeral";
import { mmss } from "@/lib/format";
import { kindColor } from "@/lib/momentKinds";
import { momentClipId, type ClipLike } from "@/lib/videoMapCoverage";
import { clipNum } from "@/lib/videoMapNarrative";
import type { VideoMoment } from "@/lib/types";
import { ExtractButton } from "./ExtractButton";

// One moment in the outline. Clicking the body seeks the source to the moment start. A clipped
// moment (a clip OVERLAPS it) shows an "Open clip #N" link; an untapped moment shows the (M1:
// disabled) Extract CTA. This is the fix for the old ○-moment lie — momentClipId returns null for a
// gap moment instead of mislinking it to a neighbouring clip.

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
  const { dot } = kindColor(moment.kind);
  const clipId = momentClipId(clips, moment);

  return (
    <div className="flex items-start gap-2.5 rounded-lg border border-line bg-surface px-3 py-2.5 transition-colors duration-150 hover:border-line-strong">
      <button
        type="button"
        onClick={() => onSeek(moment.start)}
        className="flex min-w-0 flex-1 items-start gap-2.5 text-left"
      >
        <span className={`mt-1 size-2 shrink-0 rounded-pill ${dot}`} aria-hidden />
        <span className="min-w-0 flex-1">
          <span className="block font-semibold leading-snug text-ink">{moment.label}</span>
          <span className="mt-0.5 block text-xs leading-relaxed text-muted">{moment.why}</span>
          <Numeral className="mt-1 block text-xs text-faint">{mmss(moment.start)}</Numeral>
        </span>
      </button>

      <div className="shrink-0 pt-0.5">
        {clipId ? (
          <Link
            href={`/edit/${jobId}/${clipId}`}
            className="inline-flex items-center gap-1.5 rounded-md border border-line bg-surface-2 px-2.5 py-1 text-xs font-semibold text-ink transition-colors duration-150 hover:border-line-strong"
          >
            <span className="size-1.5 rounded-pill bg-accent" aria-hidden />
            Open clip #{clipNum(clipId)}
          </Link>
        ) : (
          <ExtractButton state="coming" disabled />
        )}
      </div>
    </div>
  );
}
