"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Numeral } from "@/components/ui/Numeral";
import { mmss } from "@/lib/format";
import { classifyMoment, type ClipLike } from "@/lib/videoMapCoverage";
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
  // Only UNTAPPED moments get a marker (the actionable "pull me" spots); clipped ones are already
  // shown by the clip blocks, and the kind-colour rainbow was noise.
  const untappedMoments = chapters
    .flatMap((ch) => ch.moments ?? [])
    .filter((m) => classifyMoment(clips, m) === "untapped");
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
            title="Drag along the timeline to move through the video; click to jump to a point"
            className={`rounded px-2.5 py-1 text-xs font-medium transition-colors duration-150 ${
              mode === "scrub" ? "bg-accent/15 text-accent" : "text-muted hover:text-ink"
            }`}
          >
            Move
          </button>
          {onSelectRange && (
            <button
              type="button"
              onClick={() => setMode("select")}
              aria-pressed={mode === "select"}
              title="Drag across the part you want, then Extract it as a new clip"
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
          className={`relative h-24 w-full touch-none overflow-hidden rounded-lg border border-line-strong bg-surface-2 shadow-sm ${
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
                className={`group/ch absolute top-0 h-[52px] overflow-hidden border-l border-line-strong px-2 py-1.5 text-left transition-colors hover:bg-surface-3/70 ${
                  i % 2 === 1 ? "bg-white/[0.03]" : ""
                }`}
              >
                <span className="block truncate text-xs font-semibold leading-tight text-ink">
                  {ch.title}
                </span>
                <Numeral className="mt-0.5 block text-[11px] leading-none text-faint">
                  {mmss(ch.start)}
                </Numeral>
              </button>
            );
          })}

          {/* Coverage lane (bottom): each CLIP is a filled block ("what you've cut"); each UNTAPPED
              moment is a bright accent tick ("a spot worth pulling"). No kind-colour rainbow. */}
          <div className="absolute inset-x-0 bottom-0 top-[52px] border-t border-line-strong bg-black/25">
            <span className="absolute left-2 top-1 font-mono text-[9px] uppercase tracking-wide text-faint">
              coverage
            </span>
            {clips.map((c, i) => {
              const leftFrac = fracOf(c.start);
              const widthFrac = (c.end - c.start) / viewLen;
              if (leftFrac + widthFrac < 0 || leftFrac > 1) return null;
              return (
                <div
                  key={`clip-${i}`}
                  title={`Clip ${i + 1}`}
                  style={{ left: leftPct(c.start), width: `${Math.max(widthFrac * 100, 0.4)}%` }}
                  className="absolute bottom-1.5 h-3.5 rounded-[3px] bg-line-strong/80"
                />
              );
            })}
            {untappedMoments.map((m, i) => {
              const frac = fracOf(m.start);
              if (frac < -0.02 || frac > 1.02) return null;
              return (
                <button
                  key={`u-${i}`}
                  type="button"
                  title={`Untapped: ${m.label} · ${mmss(m.start)}`}
                  aria-label={`Untapped moment: ${m.label} at ${mmss(m.start)}`}
                  onClick={(e) => {
                    e.stopPropagation();
                    seekFromChild(m.start);
                  }}
                  style={{ left: pct(m.start) }}
                  className="group/u absolute bottom-1 flex h-6 -translate-x-1/2 items-end px-1.5"
                >
                  <span className="h-5 w-[3px] rounded-full bg-accent shadow-[0_0_6px_rgba(0,0,0,.4)] transition-transform group-hover/u:scale-y-110" />
                </button>
              );
            })}
          </div>

          {/* Selection overlay (accent — strong so it's obvious a range is chosen) */}
          {sel && fracOf(selLo) <= 1 && fracOf(selHi) >= 0 && (
            <div
              className="pointer-events-none absolute inset-y-0 z-20 border-x-2 border-accent bg-accent/25"
              style={{
                left: leftPct(selLo),
                width: `${Math.max(((selHi - selLo) / viewLen) * 100, 0)}%`,
              }}
            />
          )}

          {/* Select-mode instruction, until the user draws a range. */}
          {mode === "select" && !sel && (
            <div className="pointer-events-none absolute inset-0 z-20 flex items-center justify-center">
              <span className="rounded-md bg-black/60 px-2.5 py-1 text-xs font-medium text-white/90">
                Drag across the section you want as a clip
              </span>
            </div>
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
          {mode === "scrub"
            ? "Drag to move through the video, click to jump here"
            : "Drag across a section to pull it as a new clip"}
        </span>
        <div className="flex items-center gap-3 text-xs text-muted">
          <span className="flex items-center gap-1.5">
            <span className="h-2.5 w-3.5 rounded-[2px] bg-line-strong/80" aria-hidden />
            Clip
          </span>
          <span className="flex items-center gap-1.5">
            <span className="h-3 w-[3px] rounded-full bg-accent" aria-hidden />
            Untapped moment
          </span>
        </div>
      </div>
    </div>
  );
}
