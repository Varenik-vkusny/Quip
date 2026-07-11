"use client";

// PausesTab — pause removal panel (spec 2026-07-12). Toggle + tightness slider per clip.
// Non-destructive: writes ClipEdit.silence_config; the worker bakes the cut at render time
// (subtracts silent word-gaps from source_intervals). Preview does not skip pauses yet (Phase 2) —
// like trim, you re-render to see it. Editor strings are English (editor i18n deferred, CLAUDE.md).

import { useMemo, useState } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import type { SilenceConfig, SourceInterval, Word } from "@/lib/types";

// codegen'd SilenceConfig has all fields optional (pydantic defaults) → normalize with these.
const DEF_MIN = 400;
const DEF_MAX = 2000;
const DEF_PAD = 50;

// tightness slider 0 (Natural, 800ms) .. 100 (Tight, 200ms)
function sliderToMs(v: number): number {
  return Math.round(800 - (800 - 200) * (v / 100));
}
function msToSlider(ms: number): number {
  return Math.round((800 - ms) / 6);
}

// Rough client estimate of seconds removed (ignores frame-snap; server is authoritative).
function estimateSaved(
  words: Word[],
  intervals: SourceInterval[],
  minMs: number,
  maxMs: number,
  padMs: number,
): number {
  const minP = minMs / 1000;
  const maxP = maxMs / 1000;
  const pad = padMs / 1000;
  const inClip = (a: number, b: number) =>
    intervals.some((iv) => iv.source_start <= a && b <= iv.source_end);
  let saved = 0;
  for (let i = 0; i < words.length - 1; i++) {
    const gap = words[i + 1].start - words[i].end;
    if (gap >= minP && gap <= maxP && inClip(words[i].end, words[i + 1].start)) {
      saved += Math.max(0, gap - 2 * pad);
    }
  }
  return saved;
}

export function PausesTab({
  config,
  words,
  intervals,
  busy = false,
  onChange,
}: {
  config: SilenceConfig | null;
  words: Word[];
  intervals: SourceInterval[];
  busy?: boolean;
  onChange: (next: SilenceConfig) => void;
}) {
  const enabled = config?.enabled ?? false;
  const minMs = config?.min_pause_ms ?? DEF_MIN;
  const maxMs = config?.max_pause_ms ?? DEF_MAX;
  const padMs = config?.padding_ms ?? DEF_PAD;
  const [advancedOpen, setAdvancedOpen] = useState(false);

  const saved = useMemo(
    () => (enabled ? estimateSaved(words, intervals, minMs, maxMs, padMs) : 0),
    [enabled, minMs, maxMs, padMs, words, intervals],
  );

  // Always emit a full config (all fields present) so a toggle/slick never drops sibling fields.
  const emit = (partial: Partial<SilenceConfig>) =>
    onChange({ enabled, min_pause_ms: minMs, max_pause_ms: maxMs, padding_ms: padMs, ...partial });

  return (
    <div className="space-y-5 p-4">
      <div>
        <p className="text-sm font-medium text-ink">Remove pauses</p>
        <p className="mt-0.5 text-xs text-muted">
          Cut silent gaps between words for a tighter, denser clip. Re-render to apply.
        </p>
      </div>

      {/* on/off toggle */}
      <div className="flex items-center justify-between">
        <span className="text-sm text-ink">
          {enabled ? "On" : "Off"}
          {enabled && saved > 0.15 && (
            <span className="ml-2 text-xs text-muted">saves ~{saved.toFixed(1)}s</span>
          )}
        </span>
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          disabled={busy}
          onClick={() => emit({ enabled: !enabled })}
          className={`relative inline-flex h-6 w-11 cursor-pointer rounded-full border-2 border-transparent transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:opacity-50 ${
            enabled ? "bg-accent" : "bg-line"
          }`}
        >
          <span
            className={`pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow-sm transition-transform ${
              enabled ? "translate-x-5" : "translate-x-0"
            }`}
          />
        </button>
      </div>

      {/* tightness slider */}
      {enabled && (
        <div className="space-y-2">
          <div className="flex justify-between text-xs text-muted">
            <span>Natural</span>
            <span className="font-medium text-ink">cut pauses &gt; {minMs}ms</span>
            <span>Tight</span>
          </div>
          <input
            type="range"
            min={0}
            max={100}
            value={msToSlider(minMs)}
            disabled={busy}
            onChange={(e) => emit({ min_pause_ms: sliderToMs(Number(e.target.value)) })}
            aria-label="Pause tightness"
            className="h-1.5 w-full cursor-pointer appearance-none rounded-full bg-line accent-accent disabled:opacity-50"
          />
        </div>
      )}

      {/* advanced */}
      {enabled && (
        <div className="rounded-lg border border-line">
          <button
            type="button"
            onClick={() => setAdvancedOpen((o) => !o)}
            className="flex w-full items-center justify-between px-3 py-2.5 text-xs font-medium text-muted transition-colors hover:text-ink"
          >
            <span>Advanced</span>
            {advancedOpen ? (
              <ChevronUp className="size-3.5" />
            ) : (
              <ChevronDown className="size-3.5" />
            )}
          </button>
          {advancedOpen && (
            <div className="space-y-4 border-t border-line px-3 pb-3 pt-3">
              <label className="block space-y-1">
                <div className="flex justify-between text-xs">
                  <span className="text-muted">Keep dramatic pauses longer than</span>
                  <span className="font-mono text-ink">{maxMs}ms</span>
                </div>
                <input
                  type="range"
                  min={800}
                  max={5000}
                  step={100}
                  value={maxMs}
                  disabled={busy}
                  onChange={(e) => emit({ max_pause_ms: Number(e.target.value) })}
                  aria-label="Max pause to cut"
                  className="h-1 w-full cursor-pointer appearance-none rounded-full bg-line accent-accent disabled:opacity-50"
                />
              </label>
              <label className="block space-y-1">
                <div className="flex justify-between text-xs">
                  <span className="text-muted">Padding around speech</span>
                  <span className="font-mono text-ink">{padMs}ms</span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={150}
                  step={10}
                  value={padMs}
                  disabled={busy}
                  onChange={(e) => emit({ padding_ms: Number(e.target.value) })}
                  aria-label="Padding around speech"
                  className="h-1 w-full cursor-pointer appearance-none rounded-full bg-line accent-accent disabled:opacity-50"
                />
              </label>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
