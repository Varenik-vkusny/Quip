"use client";

import { Eyebrow } from "@/components/ui/Eyebrow";
import { type ClipLike } from "@/lib/videoMapCoverage";
import { parseNarrative } from "@/lib/videoMapNarrative";

// The demoted narrative: the old "why these clips work" essay, now a collapsed <details> at the
// bottom of the cockpit (the interactive map is the headline; the prose is the footnote).

export function NarrativeCollapse({
  narrative,
  jobId,
  clips,
}: {
  narrative: string;
  jobId: string;
  clips: ClipLike[];
}) {
  if (!narrative) return null;

  return (
    <details className="group overflow-hidden rounded-lg border border-line bg-surface">
      <summary
        className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3 text-ink [&::-webkit-details-marker]:hidden"
        style={{ minHeight: "44px" }}
      >
        <Eyebrow tone="faint">Why these clips work</Eyebrow>
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
      <div className="border-t border-line px-4 py-3">
        <p className="text-sm leading-relaxed text-muted">
          {parseNarrative(narrative, jobId, clips)}
        </p>
      </div>
    </details>
  );
}
