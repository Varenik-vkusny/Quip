"use client";

import { useCallback, useRef, useState } from "react";
import { Numeral } from "@/components/ui/Numeral";
import { mmss } from "@/lib/format";
import { kindColor } from "@/lib/momentKinds";
import { coverageSegments, type ClipLike } from "@/lib/videoMapCoverage";
import type { VideoChapter } from "@/lib/types";

// Whole-video timeline. x = time / duration. Bottom→top layers: coverage band (clipped vs untapped),
// chapter segments (labelled, click→seek), moment ticks (colored by kind, click→seek), a playhead
// line, and a pointer drag that selects a range → onSelectRange. Chapter/tick clicks and the
// background click-to-seek all consult one `suppressClick` ref so a drag never also fires a seek.

const DRAG_THRESHOLD_PX = 4;

export function MapTimeline({
  chapters,
  clips,
  durationSec,
  playheadSec,
  onSeek,
  onSelectRange,
}: {
  chapters: VideoChapter[];
  clips: ClipLike[];
  durationSec: number;
  playheadSec: number;
  onSeek: (sec: number) => void;
  onSelectRange?: (r: { start: number; end: number }) => void;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ startX: number; startFrac: number; moved: boolean } | null>(null);
  const suppressClick = useRef(false);
  const [sel, setSel] = useState<{ a: number; b: number } | null>(null);

  const dur = durationSec > 0 ? durationSec : 1;
  const left = (sec: number) => `${Math.max(0, Math.min(1, sec / dur)) * 100}%`;
  const width = (a: number, b: number) => {
    const lo = Math.max(0, Math.min(1, a / dur));
    const hi = Math.max(0, Math.min(1, b / dur));
    return `${Math.max(0, hi - lo) * 100}%`;
  };

  const fracAtX = useCallback((clientX: number) => {
    const el = trackRef.current;
    if (!el) return 0;
    const rect = el.getBoundingClientRect();
    return Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
  }, []);

  const moments = chapters.flatMap((ch) =>
    (ch.moments ?? []).map((m) => ({ start: m.start, kind: m.kind, label: m.label })),
  );

  const onPointerDown = (e: React.PointerEvent) => {
    suppressClick.current = false; // fresh gesture is never suppressed
    if (!onSelectRange) return;
    dragRef.current = { startX: e.clientX, startFrac: fracAtX(e.clientX), moved: false };
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const d = dragRef.current;
    if (!d) return;
    if (!d.moved && Math.abs(e.clientX - d.startX) < DRAG_THRESHOLD_PX) return;
    d.moved = true;
    try {
      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    } catch {
      /* capture unsupported — drag still works via move coords */
    }
    setSel({ a: d.startFrac, b: fracAtX(e.clientX) });
  };
  const onPointerUp = (e: React.PointerEvent) => {
    const d = dragRef.current;
    dragRef.current = null;
    if (!d || !d.moved || !onSelectRange) return;
    const endFrac = fracAtX(e.clientX);
    const a = Math.min(d.startFrac, endFrac) * dur;
    const b = Math.max(d.startFrac, endFrac) * dur;
    suppressClick.current = true; // the click that follows this drag must not seek
    if (b - a > 0.05) {
      setSel({ a: d.startFrac, b: endFrac });
      onSelectRange({ start: a, end: b });
    } else {
      setSel(null);
    }
  };

  // Background click → seek to the clicked position. Chapter/tick buttons stopPropagation so their
  // own onClick wins; a just-finished drag is swallowed via suppressClick.
  const onBackgroundClick = (e: React.MouseEvent) => {
    if (suppressClick.current) {
      suppressClick.current = false;
      return;
    }
    onSeek(fracAtX(e.clientX) * dur);
  };

  const seekFromChild = (sec: number) => {
    if (suppressClick.current) {
      suppressClick.current = false;
      return;
    }
    onSeek(sec);
  };

  return (
    <div className="space-y-2">
      <div className="overflow-x-auto">
        <div
          ref={trackRef}
          className="relative min-w-[640px] cursor-crosshair select-none touch-none"
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onClick={onBackgroundClick}
        >
          {/* Chapters */}
          <div className="relative h-12 rounded-md bg-surface-2">
            {chapters.map((ch, i) => (
              <button
                key={i}
                type="button"
                title={ch.title}
                onClick={(e) => {
                  e.stopPropagation();
                  seekFromChild(ch.start);
                }}
                style={{ left: left(ch.start), width: width(ch.start, ch.end) }}
                className="absolute inset-y-0 overflow-hidden border-l border-line px-2 py-1 text-left transition-colors first:border-l-0 hover:bg-surface"
              >
                <span className="block truncate text-xs font-semibold leading-tight text-ink">
                  {ch.title}
                </span>
                <Numeral className="block text-xs text-faint">{mmss(ch.start)}</Numeral>
              </button>
            ))}
          </div>

          {/* Moment ticks */}
          <div className="relative h-5">
            {moments.map((m, i) => (
              <button
                key={i}
                type="button"
                title={`${m.label} · ${mmss(m.start)}`}
                aria-label={`${m.label} at ${mmss(m.start)}`}
                onClick={(e) => {
                  e.stopPropagation();
                  seekFromChild(m.start);
                }}
                style={{ left: left(m.start) }}
                className="absolute inset-y-0 flex -translate-x-1/2 items-stretch justify-center px-1"
              >
                <span className={`w-0.5 rounded-full ${kindColor(m.kind).dot}`} />
              </button>
            ))}
          </div>

          {/* Coverage band */}
          <div className="relative h-2.5 rounded-full border border-line bg-surface-2">
            {coverageSegments(dur, clips).map((s, i) => (
              <div
                key={i}
                style={{ left: left(s.start), width: width(s.start, s.end) }}
                className={`absolute inset-y-0 ${s.clipped ? "bg-accent" : "bg-transparent"}`}
              />
            ))}
          </div>

          {/* Selection overlay */}
          {sel && (
            <div
              className="pointer-events-none absolute inset-y-0 z-10 border-x border-accent bg-accent/15"
              style={{
                left: `${Math.min(sel.a, sel.b) * 100}%`,
                width: `${Math.abs(sel.b - sel.a) * 100}%`,
              }}
            />
          )}

          {/* Playhead */}
          <div
            className="pointer-events-none absolute inset-y-0 z-20 w-px -translate-x-1/2 bg-ink"
            style={{ left: left(playheadSec) }}
          >
            <span className="absolute -top-1 left-1/2 size-2 -translate-x-1/2 rounded-full bg-ink" />
          </div>
        </div>
      </div>

      <div className="flex items-center justify-between gap-3 px-0.5">
        <span className="text-xs text-faint">
          {onSelectRange ? "Drag the timeline to select a range" : "Tap to jump the preview"}
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
