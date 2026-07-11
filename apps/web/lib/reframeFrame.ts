import type { CropOverride } from "@/lib/types";

// The reframe override that applies AT a given source-time `t` (last-wins), or null.
//
// A per-shot override only covers its own [source_start, source_end) — it must NOT colour the
// whole clip's live preview. A whole-clip override naturally contains every t. When none contains
// t, the caller falls through to the AI per-shot plan so non-overridden shots preview their real
// mode. (This mirrors the render, which recolours only the covered shots — the old preview took
// the last override unconditionally, so a single-shot "wide" turned the whole preview wide.)
export function pickActiveOverride(overrides: CropOverride[], t: number): CropOverride | null {
  let found: CropOverride | null = null;
  for (const o of overrides) {
    if (o.source_start <= t && t < o.source_end) found = o; // last match wins
  }
  return found;
}

// A shot in SOURCE seconds. Its [source_start, source_end) is the range the per-shot override paints.
export interface ShotRange {
  source_start: number;
  source_end: number;
}

// A per-shot control change. `mode:"auto"` clears the shot's manual override (back to the AI plan).
// `center`/`fit_style` omitted → the field is preserved from any existing override (so changing the
// background never wipes the pan, and vice-versa). `center: null` explicitly resets pan to face-lock.
export interface ShotPatch {
  mode?: "auto" | "fill" | "fit";
  center?: number | null;
  fit_style?: string;
}

// The backend stores an override's [source_start, source_end] VERBATIM (set_crop_override, no snap),
// so a shot re-edit computes the same range and matches its own override. A small epsilon tolerates
// frame-level float drift; shots are seconds apart, so it can never match a neighbour by accident.
const RANGE_EPS = 0.05;

function rangeMatches(o: ShotRange, shot: ShotRange): boolean {
  return (
    Math.abs(o.source_start - shot.source_start) < RANGE_EPS &&
    Math.abs(o.source_end - shot.source_end) < RANGE_EPS
  );
}

// The single per-shot override on `shot`'s range, or null. Used by the editor to seed a control from
// the shot's current manual framing and to read back the merged override to POST.
export function findShotOverride(overrides: CropOverride[], shot: ShotRange): CropOverride | null {
  return overrides.find((o) => rangeMatches(o, shot)) ?? null;
}

// Merge `patch` into `shot`'s override (or create it), returning a NEW array. The edited override is
// scoped to `shot`'s range ONLY — every other override is copied through untouched, so editing one
// shot can never leak to its neighbours (the founder's #1 guarantee, enforced structurally). Fields
// the patch omits are preserved from the existing override; `mode:"auto"` drops the override.
// One shared builder for BOTH per-shot and whole-clip framing edits (the whole clip is just a range
// spanning [outerStart, outerEnd)). Pure.
export function upsertShotOverride(
  overrides: CropOverride[],
  shot: ShotRange,
  patch: ShotPatch,
): CropOverride[] {
  const rest = overrides.filter((o) => !rangeMatches(o, shot));
  if (patch.mode === "auto") return rest; // auto = remove the shot's manual override
  const existing = findShotOverride(overrides, shot);
  const merged: CropOverride = {
    source_start: shot.source_start,
    source_end: shot.source_end,
    mode: patch.mode ?? existing?.mode ?? "fill",
    // `center` present in patch (incl. explicit null) wins; else preserve existing.
    center: patch.center !== undefined ? patch.center : (existing?.center ?? null),
    center_b: existing?.center_b ?? null,
    fit_style: patch.fit_style ?? existing?.fit_style,
  };
  return [...rest, merged];
}

// A shot is "manual" iff an override covers its MIDPOINT (robust to frame-level range drift). Drives
// the per-shot AI↔manual provenance dot in the timeline.
export function shotIsManual(overrides: CropOverride[], shot: ShotRange): boolean {
  const mid = (shot.source_start + shot.source_end) / 2;
  return overrides.some((o) => o.source_start <= mid && mid < o.source_end);
}
