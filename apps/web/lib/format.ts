/** Секунды → `M:SS` (или `H:MM:SS` от часа). NaN/неконечное → `0:00`. */
export function mmss(seconds: number): string {
  // Источник до 90 мин (90:00 читался как «минуты», а это 1:30:00) + защита от NaN с провода.
  const s = Number.isFinite(seconds) ? Math.max(0, Math.round(seconds)) : 0;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = (s % 60).toString().padStart(2, "0");
  if (h > 0) return `${h}:${m.toString().padStart(2, "0")}:${sec}`;
  return `${m}:${sec}`;
}

/** Диапазон клипа в координатах источника, `M:SS–M:SS`. */
export function clipRange(start: number, end: number): string {
  return `${mmss(start)}–${mmss(end)}`;
}

/** Доллары: `$0.16`. NaN/неконечное → `$0.00` (значение с провода). */
export function usd(n: number): string {
  return `$${(Number.isFinite(n) ? n : 0).toFixed(2)}`;
}

/** Длительность процесса: `42s` или `1m 03s`. */
export function elapsed(seconds: number): string {
  const s = Number.isFinite(seconds) ? Math.max(0, Math.round(seconds)) : 0;
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const sec = (s % 60).toString().padStart(2, "0");
  return `${m}m ${sec}s`;
}

// Не показываем ETA раньше этого (иначе дикие оценки в первую секунду).
const ETA_MIN_ELAPSED_SEC = 4;
// И пока прогресс совсем мал (стадийный пол 8% и т.п.) — экстраполяция ненадёжна.
const ETA_MIN_FRACTION = 0.05;
// Абсурдный потолок: воркер режет видео на 3 ч → оценка выше = не показываем (честнее «estimating…»).
const ETA_MAX_SEC = 3 * 3600;

/** Snap a raw seconds estimate to a coarse, human bucket so a live ETA reads calm, not jittery:
 *  ≤45s → 30s, <75s → 1 min, then whole minutes. A cluster of nearby raw values collapses onto one
 *  bucket, so the displayed label only moves when it crosses a boundary. */
function bucketEtaSeconds(sec: number): number {
  const s = Math.max(0, sec);
  if (s <= 45) return 30;
  if (s < 75) return 60;
  return Math.round(s / 60) * 60;
}

/** Coarse remaining-time estimate (SECONDS) for a running job/upload, from cumulative progress.
 *  `fractionDone` is 0–1. Average-rate projection `elapsed·(1−f)/f` — uses cumulative elapsed (not
 *  per-poll deltas), so it's naturally smooth — then snapped to a coarse bucket to kill jitter.
 *  Honest, never fake: returns `null` (→ caller shows a calm "estimating…") until there's enough
 *  signal, and never a negative or absurd value. PURE. */
export function etaSeconds(elapsedSec: number, fractionDone: number): number | null {
  if (!Number.isFinite(elapsedSec) || !Number.isFinite(fractionDone)) return null;
  // Not enough signal: too little time, progress too small to trust, or already complete.
  if (elapsedSec < ETA_MIN_ELAPSED_SEC || fractionDone < ETA_MIN_FRACTION || fractionDone >= 1) {
    return null;
  }
  const remaining = (elapsedSec * (1 - fractionDone)) / fractionDone;
  if (!Number.isFinite(remaining) || remaining < 0 || remaining > ETA_MAX_SEC) return null;
  return bucketEtaSeconds(remaining);
}

/** Human transfer rate from bytes-per-second, e.g. `6.4 MB/s` / `840 KB/s` / `500 B/s`. Unit is
 *  locale-neutral (no translation needed). Returns `null` when there's no signal (non-finite or
 *  ≤0 rate) so the caller can stay quiet during instant/idle uploads. PURE. */
export function formatRate(bytesPerSec: number): string | null {
  if (!Number.isFinite(bytesPerSec) || bytesPerSec <= 0) return null;
  const mb = bytesPerSec / (1024 * 1024);
  if (mb >= 1) return `${mb.toFixed(1)} MB/s`;
  const kb = bytesPerSec / 1024;
  if (kb >= 1) return `${Math.round(kb)} KB/s`;
  return `${Math.round(bytesPerSec)} B/s`;
}
