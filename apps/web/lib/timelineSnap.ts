// Pure timeline snap helper (used by the editor's TimelineV2 trim grips). Given a candidate
// time (seconds) and a set of snap targets — moment ticks, chapter edges, shot boundaries —
// return the nearest target within `tolSec`, else the raw time unchanged.
//
// Kept pure + framework-free so it's unit-tested in isolation (timelineSnap.test.ts). The
// existing snapEngine.ts is a 2D pixel guide-line engine for the on-video caption box — a
// different domain (x/y pixels + guides); this is 1D time-in-seconds, hence a separate helper.

/**
 * Snap `t` (seconds) to the nearest of `targets` within `tolSec`, else return `t` unchanged.
 *
 * - Nearest wins when several targets are within tolerance.
 * - `tolSec` is inclusive at the boundary (dist === tolSec snaps).
 * - `tolSec <= 0` disables snapping (raw `t` passes through).
 * - Non-finite targets are ignored; a non-finite `t` is returned unchanged.
 */
export function snapTime(t: number, targets: readonly number[], tolSec: number): number {
  if (!Number.isFinite(t) || !(tolSec > 0) || targets.length === 0) return t;
  let best = t;
  let bestDist = tolSec;
  for (const target of targets) {
    if (!Number.isFinite(target)) continue;
    const dist = Math.abs(target - t);
    if (dist <= bestDist) {
      // `<=` lets a later, equidistant target win deterministically; for strictly-nearest
      // selection ties are vanishingly rare on real timelines.
      bestDist = dist;
      best = target;
    }
  }
  return best;
}
