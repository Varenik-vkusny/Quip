import Link from "next/link";
import type { ClipLike } from "@/lib/videoMapCoverage";

// Narrative parser for the Video Map. Extracted verbatim from the old VideoMap.tsx so both the
// (retired) essay and the new NarrativeCollapse render `[[clip:clip_NN]]` + `[mm:ss]` tokens the
// same way — ONE source of truth. Typed against ClipLike (only id/start/end are read) so it accepts
// both ClipOut[] and the timeline's ClipLike[].

/** Parse an `mm:ss` string → total seconds. */
function parseTimecode(raw: string): number {
  const [m, s] = raw.split(":").map(Number);
  return m * 60 + (s ?? 0);
}

/** Find the clip that covers a given source second (point-containment — narrative timecodes are
 *  points, not ranges). */
function findClipByTime(clips: readonly ClipLike[], sec: number): ClipLike | undefined {
  return clips.find((c) => c.start <= sec && sec <= c.end);
}

/** Extract a clip number label from an id like `clip_03` → `3`. */
export function clipNum(clipId: string): string {
  const m = clipId.match(/(\d+)$/);
  return m ? String(parseInt(m[1], 10)) : clipId;
}

/**
 * Parses narrative text containing `[[clip:clip_NN]]` and `[mm:ss]` tokens.
 * Returns an array of React-renderable segments.
 */
export function parseNarrative(
  text: string,
  jobId: string,
  clips: readonly ClipLike[],
): React.ReactNode[] {
  // Regex: captures [[clip:clip_NN]] OR [mm:ss] tokens.
  // Accept both [[clip:clip_NN]] (prompt format) and [[clip_NN]] (Gemini sometimes drops "clip:").
  const TOKEN_RE = /\[\[(?:clip:)?(clip_\d+)\]\]|\[(\d{1,2}:\d{2})\]/g;
  const nodes: React.ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = TOKEN_RE.exec(text)) !== null) {
    // Push plain text before this token
    if (match.index > lastIndex) {
      nodes.push(text.slice(lastIndex, match.index));
    }

    if (match[1]) {
      // [[clip:clip_NN]] → editor link
      const clipId = match[1];
      nodes.push(
        <Link
          key={`${match.index}`}
          href={`/edit/${jobId}/${clipId}`}
          // inline-block + whitespace-nowrap: тег «Clip #N» АТОМАРЕН — никогда не рвётся между строк
          // («Clip» / «#3»). Лёгкая coral-pill (bg вместо подчёркивания) + mx-px: ряд из многих клипов
          // подряд читается раздельными тегами с воздухом, а не сплошной подчёркнутой стеной.
          className="mx-px inline-block whitespace-nowrap rounded bg-accent/10 px-1.5 py-0.5 align-baseline text-xs font-semibold text-accent hover:bg-accent/20 transition-colors duration-150"
        >
          Clip #{clipNum(clipId)}
        </Link>,
      );
    } else if (match[2]) {
      // [mm:ss] → mono chip; link if a clip covers that second
      const raw = match[2];
      const sec = parseTimecode(raw);
      const clip = findClipByTime(clips, sec);
      const chipClass =
        "font-mono text-xs px-1.5 py-0.5 rounded-md bg-surface-2 border border-line";
      if (clip) {
        nodes.push(
          <Link
            key={`${match.index}`}
            href={`/edit/${jobId}/${clip.id}`}
            className={`${chipClass} text-ink hover:border-line-strong transition-colors duration-150`}
          >
            {raw}
          </Link>,
        );
      } else {
        nodes.push(
          <span key={`${match.index}`} className={`${chipClass} text-faint`}>
            {raw}
          </span>,
        );
      }
    }

    lastIndex = TOKEN_RE.lastIndex;
  }

  // Remaining plain text
  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex));
  }

  return nodes;
}
