"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Numeral } from "@/components/ui/Numeral";
import { mmss } from "@/lib/format";
import { kindColor } from "@/lib/momentKinds";
import { coverageSegments, type ClipLike } from "@/lib/videoMapCoverage";
import type { VideoChapter } from "@/lib/types";

// ────────────────────────────────────────────────────────────────────────────
// MapTimeline — the Video Map scrubber. An editor-grade interactive timeline
// borrowing TimelineV2's interaction model (self-contained, no editor imports):
//   • zoom/pan a VISIBLE window [viewStart, viewStart+viewLen] ⊆ [0, duration]
//     (Ctrl/Meta+wheel anchored under the cursor; +/- buttons; a pan slider at zoom>1).
//   • two drag modes via a toolbar toggle: "Scrub" (drag = live seek) and "Select"
//     (drag = build an extract range). A plain click always seeks (with play).
//   • layers bottom→top: coverage band (clipped vs untapped), chapter segments
//     (click→seek), moment ticks (colored by kind, click→seek), selection overlay,
//     the white playhead, the hover cursor + tooltip.
// Scrubbing is lag-free: the playhead is driven off a local `scrubT` (no round-trip),
// and onScrub (the video seek) is coalesced to one call per animation frame.
// ────────────────────────────────────────────────────────────────────────────

const DRAG_THRESHOLD_PX = 4;
const RULER_TICKS = 6;

const clamp = (n: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, n));

type DragMode = "scrub" | "select";

interface Gesture {
  mode: DragMode;
  startX: number;
  startT: number;
  moved: boolean;
}

export function MapTimeline({
  chapters,
  clips,
  durationSec,
  playheadSec,
  onSeek,
  onScrub,
  onSelectRange,
}: {
  chapters: VideoChapter[];
  clips: ClipLike[];
  durationSec: number;
  playheadSec: number;
  onSeek: (sec: number) => void;
  onScrub: (sec: number) => void;
  onSelectRange?: (r: { start: number; end: number }) => void;
}) {
  const dur = Math.max(durationSec, 0.001);
  const maxZoom = clamp(dur / 12, 10, 300);

  const trackRef = useRef<HTMLDivElement>(null);
  const gestureRef = useRef<Gesture | null>(null);
  // The click that trails a real drag must not also seek — swallowed on the next click.
  const suppressClick = useRef(false);

  const [mode, setMode] = useState<DragMode>("scrub");
  const [zoom, setZoom] = useState(1);
  const [viewStart, setViewStart] = useState(0);
  const [hover, setHover] = useState<{ x: number; t: number } | null>(null);
  const [sel, setSel] = useState<{ a: number; b: number } | null>(null);
  // Local scrub position → drives the playhead during a scrub drag so it never lags the finger.
  const [scrubT, setScrubT] = useState<number | null>(null);

  const viewLen = dur / zoom;
  const viewStartClamped = clamp(viewStart, 0, Math.max(0, dur - viewLen));

  // ── rAF-throttled scrub: coalesce a burst of pointermoves into one video seek per frame. The
  // latest onScrub lives in a ref so the queued frame never fires a stale closure. ──
  const onScrubRef = useRef(onScrub);
  useEffect(() => {
    onScrubRef.current = onScrub;
  }, [onScrub]);
  const rafRef = useRef<number | null>(null);
  const pendingRef = useRef<number | null>(null);
  const queueScrub = useCallback((t: number) => {
    pendingRef.current = t;
    if (rafRef.current != null) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      const next = pendingRef.current;
      pendingRef.current = null;
      if (next != null) onScrubRef.current(next);
    });
  }, []);
  const cancelScrub = useCallback(() => {
    if (rafRef.current != null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    pendingRef.current = null;
  }, []);
  useEffect(() => () => cancelScrub(), [cancelScrub]);

  // ── time (sec) ⇄ visible-window geometry ──
  const pct = useCallback(
    (t: number) => `${clamp(((t - viewStartClamped) / viewLen) * 100, -10, 110)}%`,
    [viewStartClamped, viewLen],
  );
  const fracOf = useCallback(
    (t: number) => (t - viewStartClamped) / viewLen,
    [viewStartClamped, viewLen],
  );
  // Exact (unclamped) left for wide layers (chapters/coverage/selection) that can straddle the
  // window edge — pct's clamp would drag a wide span's right edge inward. overflow-hidden clips it.
  const leftPct = useCallback((t: number) => `${fracOf(t) * 100}%`, [fracOf]);
  const pxToTime = useCallback(
    (clientX: number) => {
      const el = trackRef.current;
      if (!el) return 0;
      const r = el.getBoundingClientRect();
      const frac = clamp((clientX - r.left) / r.width, 0, 1);
      return viewStartClamped + frac * viewLen;
    },
    [viewStartClamped, viewLen],
  );

  const setZoomAround = useCallback(
    (nextZoom: number, anchorT: number) => {
      const z = clamp(nextZoom, 1, maxZoom);
      const nextLen = dur / z;
      // Anchor the point under the cursor: its fractional position in the window is preserved.
      const frac = clamp((anchorT - viewStartClamped) / viewLen, 0, 1);
      setZoom(z);
      setViewStart(clamp(anchorT - frac * nextLen, 0, Math.max(0, dur - nextLen)));
    },
    [dur, viewStartClamped, viewLen, maxZoom],
  );

  const onWheel = useCallback(
    (e: React.WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) return; // plain page scroll isn't hijacked
      e.preventDefault();
      setZoomAround(zoom * (e.deltaY < 0 ? 1.25 : 0.8), pxToTime(e.clientX));
    },
    [zoom, pxToTime, setZoomAround],
  );

  // ── pointer gesture: begin on down, become a drag only past the threshold (so a plain click still
  // lands its native onClick → seek, and chapter/tick clicks aren't hijacked by a would-be drag). ──
  const onPointerDown = useCallback(
    (e: React.PointerEvent) => {
      suppressClick.current = false; // a fresh gesture is never suppressed
      gestureRef.current = { mode, startX: e.clientX, startT: pxToTime(e.clientX), moved: false };
    },
    [mode, pxToTime],
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent) => {
      const g = gestureRef.current;
      if (!g) {
        const el = trackRef.current;
        if (el) {
          const r = el.getBoundingClientRect();
          setHover({ x: e.clientX - r.left, t: pxToTime(e.clientX) });
        }
        return;
      }
      if (!g.moved && Math.abs(e.clientX - g.startX) < DRAG_THRESHOLD_PX) return;
      if (!g.moved) {
        g.moved = true;
        try {
          e.currentTarget.setPointerCapture(e.pointerId);
        } catch {
          /* capture unsupported — the drag still tracks via move coords */
        }
        setHover(null);
      }
      const t = pxToTime(e.clientX);
      if (g.mode === "scrub") {
        setScrubT(t); // immediate playhead
        queueScrub(t); // throttled video seek (no play)
      } else {
        setSel({ a: g.startT, b: t });
      }
    },
    [pxToTime, queueScrub],
  );

  const onPointerUp = useCallback(
    (e: React.PointerEvent) => {
      const g = gestureRef.current;
      gestureRef.current = null;
      if (g) {
        try {
          e.currentTarget.releasePointerCapture(e.pointerId);
        } catch {
          /* capture may never have been set */
        }
      }
      if (!g || !g.moved) return; // a plain click → let the native onClick seek
      suppressClick.current = true;
      if (g.mode === "scrub") {
        const t = pxToTime(e.clientX);
        cancelScrub();
        onScrub(t); // final position (batched with clearing scrubT → no flicker)
        setScrubT(null);
      } else if (onSelectRange) {
        const a = Math.min(g.startT, pxToTime(e.clientX));
        const b = Math.max(g.startT, pxToTime(e.clientX));
        if (b - a > 0.05) {
          setSel({ a, b });
          onSelectRange({ start: a, end: b });
        } else {
          setSel(null);
        }
      }
    },
    [pxToTime, cancelScrub, onScrub, onSelectRange],
  );

  const onPointerCancel = useCallback(
    (e: React.PointerEvent) => {
      const g = gestureRef.current;
      gestureRef.current = null;
      if (g) {
        try {
          e.currentTarget.releasePointerCapture(e.pointerId);
        } catch {
          /* capture may never have been set */
        }
      }
      cancelScrub();
      setScrubT(null);
    },
    [cancelScrub],
  );

  // Background click → seek (with play). Chapter/tick buttons stopPropagation so their own onClick
  // wins; a click trailing a real drag is swallowed once via suppressClick.
  const onBackgroundClick = useCallback(
    (e: React.MouseEvent) => {
      if (suppressClick.current) {
        suppressClick.current = false;
        return;
      }
      onSeek(pxToTime(e.clientX));
    },
    [onSeek, pxToTime],
  );
  const seekFromChild = useCallback(
    (sec: number) => {
      if (suppressClick.current) {
        suppressClick.current = false;
        return;
      }
      onSeek(sec);
    },
    [onSeek],
  );

  // ── derived ──
  const coverage = coverageSegments(dur, clips);
  const moments = chapters.flatMap((ch) =>
    (ch.moments ?? []).map((m) => ({ start: m.start, kind: m.kind, label: m.label })),
  );
  const hoverChapter = hover
    ? (chapters.find((c) => hover.t >= c.start && hover.t < c.end) ?? null)
    : null;
  const playT = scrubT ?? playheadSec;
  const playFrac = fracOf(playT);
  const selLo = sel ? Math.min(sel.a, sel.b) : 0;
  const selHi = sel ? Math.max(sel.a, sel.b) : 0;

  return (
    <div className="w-full select-none" onWheel={onWheel}>
      {/* Toolbar: drag-mode toggle (left) + zoom (right). */}
      <div className="mb-2.5 flex items-center justify-between gap-3">
        <div className="inline-flex items-center gap-0.5 rounded-md border border-line p-0.5">
          <button
            type="button"
            onClick={() => setMode("scrub")}
            aria-pressed={mode === "scrub"}
            className={`rounded px-2.5 py-1 text-xs font-medium transition-colors duration-150 ${
              mode === "scrub" ? "bg-accent/15 text-accent" : "text-muted hover:text-ink"
            }`}
          >
            Scrub
          </button>
          {onSelectRange && (
            <button
              type="button"
              onClick={() => setMode("select")}
              aria-pressed={mode === "select"}
              className={`rounded px-2.5 py-1 text-xs font-medium transition-colors duration-150 ${
                mode === "select" ? "bg-accent/15 text-accent" : "text-muted hover:text-ink"
              }`}
            >
              Select
            </button>
          )}
        </div>

        <div className="flex shrink-0 items-center gap-1.5">
          <button
            type="button"
            aria-label="Zoom out"
            disabled={zoom <= 1}
            onClick={() => setZoomAround(zoom / 1.5, viewStartClamped + viewLen / 2)}
            className="inline-flex size-7 items-center justify-center rounded-md border border-line text-muted transition-colors duration-150 enabled:hover:text-ink disabled:opacity-30"
          >
            <svg viewBox="0 0 16 16" fill="none" className="size-3.5" aria-hidden>
              <path d="M3.5 8h9" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          </button>
          <Numeral className="w-9 text-center text-[11px] text-muted">{zoom.toFixed(1)}×</Numeral>
          <button
            type="button"
            aria-label="Zoom in"
            disabled={zoom >= maxZoom}
            onClick={() => setZoomAround(zoom * 1.5, viewStartClamped + viewLen / 2)}
            className="inline-flex size-7 items-center justify-center rounded-md border border-line text-muted transition-colors duration-150 enabled:hover:text-ink disabled:opacity-30"
          >
            <svg viewBox="0 0 16 16" fill="none" className="size-3.5" aria-hidden>
              <path
                d="M8 3.5v9M3.5 8h9"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
              />
            </svg>
          </button>
        </div>
      </div>

      {/* Track — one tall grab surface; lanes are stacked inside it. */}
      <div className="relative">
        <div
          ref={trackRef}
          className={`relative h-20 w-full touch-none overflow-hidden rounded-lg border border-line bg-gradient-to-b from-surface-2 to-surface shadow-inner ${
            mode === "select" ? "cursor-crosshair" : "cursor-pointer"
          }`}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerCancel}
          onPointerLeave={() => {
            if (!gestureRef.current) setHover(null);
          }}
          onClick={onBackgroundClick}
        >
          {/* Chapter segments (top lane) */}
          {chapters.map((ch, i) => {
            const leftFrac = fracOf(ch.start);
            const widthFrac = (ch.end - ch.start) / viewLen;
            if (leftFrac + widthFrac < 0 || leftFrac > 1) return null;
            return (
              <button
                key={i}
                type="button"
                title={ch.title}
                onClick={(e) => {
                  e.stopPropagation();
                  seekFromChild(ch.start);
                }}
                style={{ left: leftPct(ch.start), width: `${Math.max(widthFrac * 100, 0.5)}%` }}
                className="group/ch absolute top-0 flex h-[52px] items-start gap-1.5 overflow-hidden border-l border-line px-2 py-1.5 text-left transition-colors hover:bg-surface-2/70"
              >
                <span
                  className="mt-0.5 h-3 w-0.5 shrink-0 rounded-full bg-line-strong transition-colors group-hover/ch:bg-accent"
                  aria-hidden
                />
                <span className="min-w-0">
                  <span className="block truncate text-xs font-semibold leading-tight text-ink">
                    {ch.title}
                  </span>
                  <Numeral className="mt-0.5 block text-[11px] leading-none text-faint">
                    {mmss(ch.start)}
                  </Numeral>
                </span>
              </button>
            );
          })}

          {/* Moment ticks (mid lane), colored by kind */}
          {moments.map((m, i) => {
            const frac = fracOf(m.start);
            if (frac < -0.02 || frac > 1.02) return null;
            return (
              <button
                key={i}
                type="button"
                title={`${m.label} · ${mmss(m.start)}`}
                aria-label={`${m.label} at ${mmss(m.start)}`}
                onClick={(e) => {
                  e.stopPropagation();
                  seekFromChild(m.start);
                }}
                style={{ left: pct(m.start) }}
                className="absolute top-[52px] flex h-4 -translate-x-1/2 items-center px-1"
              >
                <span className={`h-3 w-0.5 rounded-full ${kindColor(m.kind).dot}`} />
              </button>
            );
          })}

          {/* Coverage band (bottom lane): clipped = accent, untapped = empty */}
          <div className="absolute inset-x-0 bottom-0 h-3 overflow-hidden border-t border-line bg-surface-2">
            {coverage.map((s, i) => {
              const leftFrac = fracOf(s.start);
              const widthFrac = (s.end - s.start) / viewLen;
              if (leftFrac + widthFrac < 0 || leftFrac > 1) return null;
              return (
                <div
                  key={i}
                  style={{ left: leftPct(s.start), width: `${Math.max(widthFrac * 100, 0)}%` }}
                  className={`absolute inset-y-0 ${s.clipped ? "bg-accent" : "bg-transparent"}`}
                />
              );
            })}
          </div>

          {/* Selection overlay */}
          {sel && fracOf(selLo) <= 1 && fracOf(selHi) >= 0 && (
            <div
              className="pointer-events-none absolute inset-y-0 z-20 border-x border-accent bg-accent/15"
              style={{
                left: leftPct(selLo),
                width: `${Math.max(((selHi - selLo) / viewLen) * 100, 0)}%`,
              }}
            />
          )}

          {/* Hover cursor (thin ink line, distinct from the white playhead) */}
          {hover && (
            <div
              className="pointer-events-none absolute inset-y-0 z-20 w-px bg-ink/50"
              style={{ left: `${hover.x}px` }}
            />
          )}

          {/* Playhead — white line + triangle head (driven by scrubT while scrubbing) */}
          {playFrac >= -0.001 && playFrac <= 1.001 && (
            <div
              className="pointer-events-none absolute inset-y-0 z-30 w-px bg-white shadow-[0_0_8px_rgba(255,255,255,.5)]"
              style={{ left: pct(playT) }}
            >
              <span className="absolute -top-px left-1/2 size-0 -translate-x-1/2 border-x-[5px] border-x-transparent border-t-[7px] border-t-white" />
            </div>
          )}
        </div>

        {/* Hover tooltip — the chapter under the cursor + the timecode. Lightweight, one line. */}
        {hover && (
          <div
            className="pointer-events-none absolute z-40 -translate-x-1/2 rounded-md border border-line bg-surface px-2.5 py-1.5 shadow-lg"
            style={{
              left: `clamp(64px, ${hover.x}px, calc(100% - 64px))`,
              bottom: "calc(100% + 6px)",
            }}
          >
            <div className="flex items-center gap-2 whitespace-nowrap">
              <span className="max-w-[190px] truncate text-xs font-semibold text-ink">
                {hoverChapter ? hoverChapter.title : "Position"}
              </span>
              <Numeral className="text-[10px] text-faint">{mmss(hover.t)}</Numeral>
            </div>
          </div>
        )}
      </div>

      {/* Pan slider (zoomed in only) */}
      {zoom > 1 && (
        <input
          type="range"
          min={0}
          max={1000}
          value={Math.round((viewStartClamped / Math.max(0.001, dur - viewLen)) * 1000)}
          onChange={(e) => setViewStart((Number(e.target.value) / 1000) * (dur - viewLen))}
          aria-label="Scroll the timeline"
          className="mt-2 h-1 w-full cursor-pointer appearance-none rounded-full bg-surface-2 accent-accent"
        />
      )}

      {/* Time ruler over the visible window */}
      <div className="relative mt-1.5 h-4">
        {Array.from({ length: RULER_TICKS }).map((_, i) => {
          const frac = i / (RULER_TICKS - 1);
          return (
            <span
              key={i}
              className="absolute font-mono text-[10px] text-faint"
              style={{
                left: `${frac * 100}%`,
                transform:
                  i === 0
                    ? "translateX(0)"
                    : i === RULER_TICKS - 1
                      ? "translateX(-100%)"
                      : "translateX(-50%)",
              }}
            >
              {mmss(viewStartClamped + frac * viewLen)}
            </span>
          );
        })}
      </div>

      {/* Hint + coverage legend */}
      <div className="mt-1.5 flex items-center justify-between gap-3">
        <span className="text-xs text-faint">
          {mode === "scrub" ? "Drag to scrub, click to jump" : "Drag to select a range"}
        </span>
        <div className="flex items-center gap-3 text-xs text-muted">
          <span className="flex items-center gap-1.5">
            <span className="h-2 w-3 rounded-full bg-accent" aria-hidden />
            Clipped
          </span>
          <span className="flex items-center gap-1.5">
            <span className="h-2 w-3 rounded-full border border-line bg-surface-2" aria-hidden />
            Untapped
          </span>
        </div>
      </div>
    </div>
  );
}
