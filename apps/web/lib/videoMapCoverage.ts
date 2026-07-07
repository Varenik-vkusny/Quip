// Pure helpers for the Video Map cockpit: classify which moments became clips,
// and compute the source-timeline "coverage" band (clipped vs untapped stretches).
//
// Lives in `lib/` (not `components/`) so vitest — whose include is `lib/**/*.test.ts`
// — picks up its tests, matching the repo's pure-logic convention (see captionFit.ts).

/** Minimal clip shape these helpers need; `ClipOut` satisfies it structurally. */
export type ClipLike = { id: string; start: number; end: number };
type Range = { start: number; end: number };

/** Two intervals overlap iff they share a positive-length span (touching edges do not). */
function overlaps(a: Range, b: Range): boolean {
  return a.start < b.end && b.start < a.end;
}

/**
 * The id of the clip whose [start,end] OVERLAPS the moment, or null if none.
 *
 * Replaces the old `findClipByTime` point-containment + `chapterClipIds[0]` fallback
 * (`VideoMap.tsx:162-164`), which silently linked an un-clipped moment to a neighbouring
 * clip's editor. An untapped moment must report null so the UI offers "extract" instead.
 */
export function momentClipId(clips: readonly ClipLike[], m: Range): string | null {
  const hit = clips.find((c) => overlaps({ start: c.start, end: c.end }, m));
  return hit ? hit.id : null;
}

/** "clipped" if any clip overlaps the moment, else "untapped". */
export function classifyMoment(
  clips: readonly ClipLike[],
  m: Range,
): "clipped" | "untapped" {
  return momentClipId(clips, m) ? "clipped" : "untapped";
}

export type CoverageSegment = { start: number; end: number; clipped: boolean };

/**
 * Walk [0, durationSec] emitting alternating clipped/untapped segments, derived from the
 * clip intervals (clamped to the source, empty intervals dropped, overlaps merged).
 * Drives the coverage band under the timeline.
 */
export function coverageSegments(
  durationSec: number,
  clips: readonly Range[],
): CoverageSegment[] {
  if (durationSec <= 0) return [];

  const clamped = clips
    .map((c) => ({
      start: Math.max(0, Math.min(c.start, durationSec)),
      end: Math.max(0, Math.min(c.end, durationSec)),
    }))
    .filter((c) => c.end > c.start)
    .sort((a, b) => a.start - b.start);

  const merged: Range[] = [];
  for (const c of clamped) {
    const last = merged[merged.length - 1];
    if (last && c.start <= last.end) last.end = Math.max(last.end, c.end);
    else merged.push({ ...c });
  }

  const segs: CoverageSegment[] = [];
  let cursor = 0;
  for (const m of merged) {
    if (m.start > cursor) segs.push({ start: cursor, end: m.start, clipped: false });
    segs.push({ start: m.start, end: m.end, clipped: true });
    cursor = m.end;
  }
  if (cursor < durationSec) segs.push({ start: cursor, end: durationSec, clipped: false });
  return segs;
}
