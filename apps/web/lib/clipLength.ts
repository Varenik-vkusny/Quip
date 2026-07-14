/**
 * Pure logic for the dashboard's "clip length" range control (min/max clip duration, seconds).
 * No React, no DOM — localStorage access is guarded for SSR (typeof window === "undefined").
 * Extracted from SourceForm.tsx so the clamp/preset/storage rules have direct unit coverage.
 */

export type ClipRange = { min: number; max: number };
export type PresetId = "short" | "standard" | "long";

export const MIN_CLIP_SEC = 4;
export const MAX_CLIP_SEC = 180;
// The worker pins clip_max_sec >= 10 (main.py request models) — never let max clamp below
// this, or a typed value would 422 the upload.
export const MIN_CLIP_MAX_SEC = 10;
// Invariant enforced by every clamp/read below: min + CLIP_GAP <= max.
export const CLIP_GAP = 2;

export const CLIP_PRESETS: readonly { id: PresetId; min: number; max: number }[] = [
  { id: "short", min: 15, max: 30 },
  { id: "standard", min: 20, max: 60 },
  { id: "long", min: 60, max: 120 },
];

export const DEFAULT_CLIP_RANGE: ClipRange = { min: 20, max: 60 };

const LS_KEY_MIN = "quip:clipMinSec";
const LS_KEY_MAX = "quip:clipMaxSec";

/** Clamps ONLY the handle that was just edited — the other side is returned unchanged. This
 *  preserves the semantics of the stepper it replaces (SourceForm.tsx clampMinSec/clampMaxSec):
 *  dragging min past max never moves max, and vice versa. Non-finite/zero/negative input for
 *  the edited field falls back to the nearest legal bound instead of propagating NaN. */
export function clampClipRange(r: ClipRange, edited: "min" | "max"): ClipRange {
  if (edited === "min") {
    const n = Number.isFinite(r.min) && r.min > 0 ? r.min : MIN_CLIP_SEC;
    const min = Math.round(Math.min(r.max - CLIP_GAP, Math.max(MIN_CLIP_SEC, n)));
    return { min, max: Math.round(r.max) };
  }
  const n = Number.isFinite(r.max) && r.max > 0 ? r.max : MIN_CLIP_MAX_SEC;
  const floor = Math.max(MIN_CLIP_MAX_SEC, r.min + CLIP_GAP);
  const max = Math.round(Math.min(MAX_CLIP_SEC, Math.max(floor, n)));
  return { min: Math.round(r.min), max };
}

/** The preset whose min AND max both equal `r` exactly, else null (UI shows "Custom"). */
export function matchPreset(r: ClipRange): PresetId | null {
  return CLIP_PRESETS.find((p) => p.min === r.min && p.max === r.max)?.id ?? null;
}

function isValidRange(min: number, max: number): boolean {
  return (
    Number.isFinite(min) &&
    Number.isFinite(max) &&
    min >= MIN_CLIP_SEC &&
    min <= MAX_CLIP_SEC &&
    max >= MIN_CLIP_MAX_SEC &&
    max <= MAX_CLIP_SEC &&
    min + CLIP_GAP <= max
  );
}

/** Reads the persisted clip-length range. Missing / non-numeric / out-of-bounds /
 *  invariant-violating pairs fall back to the default — a corrupt pair is never half-trusted. */
export function readStoredRange(): ClipRange {
  if (typeof window === "undefined") return DEFAULT_CLIP_RANGE;
  const min = Number(window.localStorage.getItem(LS_KEY_MIN));
  const max = Number(window.localStorage.getItem(LS_KEY_MAX));
  return isValidRange(min, max) ? { min, max } : DEFAULT_CLIP_RANGE;
}

/** Persists the clip-length range under the existing keys so users don't lose their setting. */
export function writeStoredRange(r: ClipRange): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(LS_KEY_MIN, String(r.min));
  window.localStorage.setItem(LS_KEY_MAX, String(r.max));
}

// ── useSyncExternalStore adapter ────────────────────────────────────────────────
// The range lives in localStorage, which the SERVER cannot see. Reading it during render
// (module scope or a useState initializer) makes the first client render disagree with the
// server HTML — and since a non-preset range also opens the slider, that disagreement is
// STRUCTURAL, which React cannot patch. Reading it in an effect instead is a cascading
// render (eslint react-hooks/set-state-in-effect). useSyncExternalStore is the one path that
// is neither: React uses getServerSnapshot for BOTH the server render and hydration, then
// re-reads getSnapshot afterwards. getSnapshot must return a STABLE reference or it loops —
// hence the module-level cache.

let cached: ClipRange | null = null;
const listeners = new Set<() => void>();

export function subscribeClipRange(onChange: () => void): () => void {
  listeners.add(onChange);
  return () => listeners.delete(onChange);
}

export function getClipRangeSnapshot(): ClipRange {
  cached ??= readStoredRange();
  return cached;
}

export function getServerClipRangeSnapshot(): ClipRange {
  return DEFAULT_CLIP_RANGE;
}

/** Single writer: updates the cache, persists, and notifies subscribers. */
export function setClipRange(r: ClipRange): void {
  cached = r;
  writeStoredRange(r);
  for (const l of listeners) l();
}

/** Test-only: drop the module cache so a fresh localStorage state is observable. */
export function resetClipRangeCache(): void {
  cached = null;
}
