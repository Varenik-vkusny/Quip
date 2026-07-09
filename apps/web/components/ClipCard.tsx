"use client";

import { Check, Loader2, Pencil } from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { memo, useEffect, useRef, useState } from "react";
import { ExportMenu } from "@/components/ExportMenu";
import { Card } from "@/components/ui/Card";
import { Numeral } from "@/components/ui/Numeral";
import { computeSignalBars } from "@/lib/clipSignals";
import { clipRange } from "@/lib/format";
import type { ClipOut, Word } from "@/lib/types";
import { ClipPreview } from "./ClipPreview";
import { PendingThumb } from "./PendingThumb";

function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

const WORKER_BASE = process.env.NEXT_PUBLIC_WORKER_URL ?? "";

export function resolveUrl(videoUrl: string): string {
  if (videoUrl.startsWith("http") || videoUrl.startsWith("/")) return videoUrl;
  return `${WORKER_BASE}/${videoUrl}`;
}

const TONE_EMOJI: Record<string, string> = {
  shocking: '😮', funny: '😂', touching: '🥺',
  relatable: '🙌', inspiring: '🔥', controversial: '🤔', insightful: '💡',
};

// Semantic score tiers (thresholds unchanged): high → ok, mid (the former amber+orange
// bands) → warn, low → bad. Flat token fills — no gradients/glows (brand rule: no neon).
function getScoreTier(score100: number): { textClass: string; bar: string } {
  if (score100 >= 90) return { textClass: 'text-ok', bar: 'var(--t-ok)' };
  if (score100 >= 50) return { textClass: 'text-warn', bar: 'var(--t-warn)' };
  return { textClass: 'text-bad', bar: 'var(--t-bad)' };
}

type ClipCardProps = {
  jobId: string;
  clip: ClipOut;
  selected: boolean;
  onToggle: (id: string) => void;
  topClip?: boolean;
};

function wordsEqual(a: Word[], b: Word[]): boolean {
  if (a === b) return true;
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    const wa = a[i];
    const wb = b[i];
    if (
      wa.text !== wb.text ||
      wa.start !== wb.start ||
      wa.end !== wb.end ||
      wa.speaker !== wb.speaker ||
      wa.confidence !== wb.confidence
    ) {
      return false;
    }
  }
  return true;
}

// Every field this component (or its children, ClipPreview/PendingThumb) reads off a clip,
// compared by VALUE. useJob() polls every 2.5s and replaces the whole `Job` object wholesale (a
// fresh `res.json()` tree — see lib/useJob.ts + lib/api.ts:getJob), so `clip` is a brand-new
// object reference every poll even when its content is byte-identical to the last one. Reference
// equality alone (plain shallow memo) can therefore NEVER bail out here — this is why the memo
// below needs a custom comparator instead. Deliberately exhaustive (every ClipOut field, not just
// the ones read today) so a future field added to this component's render can't silently go
// uncompared and get dropped by the memo.
function clipEqual(a: ClipOut, b: ClipOut): boolean {
  return (
    a.id === b.id &&
    a.video_url === b.video_url &&
    a.score === b.score &&
    a.start === b.start &&
    a.end === b.end &&
    a.duration === b.duration &&
    a.energy === b.energy &&
    a.hook === b.hook &&
    a.hook_style === b.hook_style &&
    a.key_quote === b.key_quote &&
    a.reason === b.reason &&
    a.thumbnail_url === b.thumbnail_url &&
    a.tone === b.tone &&
    a.transcript === b.transcript &&
    a.type === b.type &&
    a.why_works === b.why_works &&
    wordsEqual(a.words, b.words)
  );
}

// `onToggle` is a single stable function reference shared by every card (ClipGrid wraps it in
// useCallback with no deps and passes the SAME `toggle` to all cards, rather than a fresh
// `() => toggle(id)` closure per card) — so a plain `!==` here is correct and never flaps.
function clipCardPropsEqual(prev: ClipCardProps, next: ClipCardProps): boolean {
  return (
    prev.jobId === next.jobId &&
    prev.selected === next.selected &&
    prev.topClip === next.topClip &&
    prev.onToggle === next.onToggle &&
    clipEqual(prev.clip, next.clip)
  );
}

function ClipCardComponent({ jobId, clip, selected, onToggle }: ClipCardProps) {
  const t = useTranslations("clipCard");
  const pending = !clip.video_url;
  const videoSrc = pending ? "" : resolveUrl(clip.video_url);

  const reduced = prefersReducedMotion();
  const [mounted, setMounted] = useState(reduced);
  const [displayScore, setDisplayScore] = useState(() => (reduced ? clip.score : 0));
  const [open, setOpen] = useState(false);
  const originalStart = useRef(clip.start);
  const originalEnd = useRef(clip.end);
  const [analysisStale, setAnalysisStale] = useState(false);

  useEffect(() => {
    if (prefersReducedMotion()) return;

    const raf = requestAnimationFrame(() => setMounted(true));

    const target = clip.score;
    const duration = 600;
    let start: number | null = null;
    let countRaf = 0;
    const tick = (ts: number) => {
      if (start === null) start = ts;
      const t = Math.min(1, (ts - start) / duration);
      const eased = 1 - (1 - t) ** 3;
      setDisplayScore(target * eased);
      if (t < 1) countRaf = requestAnimationFrame(tick);
      else setDisplayScore(target);
    };
    countRaf = requestAnimationFrame(tick);

    return () => {
      cancelAnimationFrame(raf);
      cancelAnimationFrame(countRaf);
    };
  }, [clip.score]);

  useEffect(() => {
    const movedStart = Math.abs(clip.start - originalStart.current) > 5;
    const movedEnd = Math.abs(clip.end - originalEnd.current) > 5;
    setAnalysisStale(movedStart || movedEnd);
  }, [clip.start, clip.end]);

  const score100 = Math.round(displayScore * 100);
  const tier = analysisStale ? null : getScoreTier(score100);
  const emoji = clip.tone && TONE_EMOJI[clip.tone];

  const handleRefresh = async () => {
    try {
      const res = await fetch(
        `${WORKER_BASE}/jobs/${jobId}/clips/${clip.id}/refresh-analysis`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ start: clip.start, end: clip.end }),
        }
      );
      if (!res.ok) return;
      originalStart.current = clip.start;
      originalEnd.current = clip.end;
      setAnalysisStale(false);
    } catch {
      // silently ignore — stale banner stays
    }
  };

  return (
    <Card
      selected={!pending && selected}
      className={`flex flex-col overflow-hidden shadow-card transition duration-200 ease-snappy hover:border-line-strong ${
        pending ? "opacity-90" : "opacity-100"
      } ${mounted ? "translate-y-0 scale-100 opacity-100" : "translate-y-1 scale-95 opacity-0"}`}
    >
      {/* ── video preview ── */}
      <div className="relative">
        {pending ? (
          <PendingThumb jobId={jobId} clipStart={clip.start} />
        ) : (
          <ClipPreview
            src={videoSrc}
            jobId={jobId}
            clipId={clip.id}
            words={clip.words}
            clipStart={clip.start}
          />
        )}
        {emoji && !analysisStale && (
          <span
            className="pointer-events-none absolute left-2 top-2 z-40 select-none rounded-md text-[22px] leading-none"
            style={{ padding: '3px 4px', background: 'rgba(0,0,0,0.62)' }}
          >
            {emoji}
          </span>
        )}
        {!pending && (
          <button
            type="button"
            onClick={() => onToggle(clip.id)}
            aria-pressed={selected}
            aria-label={selected ? t("deselect") : t("select")}
            className={`absolute right-2 top-2 z-40 inline-flex size-7 items-center justify-center rounded-sm border transition duration-150 ease-snappy active:scale-95 ${
              selected
                ? "border-ink bg-ink text-bg"
                : "border-line-strong bg-bg/70 text-transparent backdrop-blur hover:border-line-strong hover:text-ink/40"
            }`}
          >
            <Check className="size-4" strokeWidth={3} />
          </button>
        )}
      </div>

      <div className="flex flex-1 flex-col p-3.5">
        {/* ── score + timecode ── */}
        <div className="flex items-start justify-between gap-3">
          <div className="mb-2.5 min-w-0">
            <div className="flex items-baseline gap-0.5 mb-1.5">
              <span className={`text-[28px] font-extrabold leading-none tabular-nums ${tier?.textClass ?? 'text-surface-3'}`}>
                {score100}
              </span>
              <span className="text-xs text-muted">/100</span>
            </div>
            <div className="h-1 bg-surface-2 rounded-full overflow-hidden">
              <div
                className={`h-full rounded-full ${analysisStale ? 'bg-surface-3' : ''}`}
                style={tier ? {
                  width: `${displayScore * 100}%`,
                  background: tier.bar,
                } : undefined}
              />
            </div>
          </div>
          <Numeral className="shrink-0 pt-0.5 text-xs text-muted">
            {clipRange(clip.start, clip.end)}
          </Numeral>
        </div>

        {/* ── hook ── */}
        {clip.hook && (
          <p className={`mt-3.5 line-clamp-2 text-[15px] font-bold leading-tight ${analysisStale ? 'text-muted/40' : 'text-ink'}`}>
            &ldquo;{clip.hook}&rdquo;
          </p>
        )}

        {/* ── key moment or stale banner ── */}
        {clip.key_quote && !analysisStale && (
          <div className="mt-3 rounded-sm border-l-2 border-ok bg-thought-tint px-2.5 py-1.5">
            <p className="mb-1 text-[7px] font-bold uppercase tracking-widest text-ok">
              ★ {t("keyMoment")}
            </p>
            <p className="text-[10px] italic leading-relaxed text-muted">
              &ldquo;{clip.key_quote}&rdquo;
            </p>
          </div>
        )}
        {analysisStale && (
          <div className="mt-3 flex items-center justify-between gap-2 rounded-sm border border-line bg-surface-2 px-2 py-1.5">
            <span className="text-[9px] text-muted">{t("staleBanner")}</span>
            <button
              onClick={handleRefresh}
              className="shrink-0 rounded border border-line bg-[color-mix(in_srgb,var(--t-warn)_10%,transparent)] px-1.5 py-0.5 text-[9px] font-bold text-warn"
            >
              ↻ {t("refresh")}
            </button>
          </div>
        )}

        {/* ── accordion ── */}
        <button
          type="button"
          onClick={() => setOpen(o => !o)}
          className="mt-2 text-[10px] text-muted hover:text-ink transition-colors"
        >
          {t("whyThisClip")} {open ? '↑' : '↓'}
        </button>

        {open && (
          <div className="mt-2 space-y-2 border-t border-line pt-2">
            <p className="text-[7.5px] font-bold uppercase tracking-wider text-muted">{t("qualitySignals")}</p>
            <div className="flex gap-2">
              {computeSignalBars(clip).map(({ key, bars }) => (
                <div key={key} className="flex flex-1 flex-col items-center gap-1">
                  <div className="flex h-3.5 items-end gap-0.5">
                    {[4, 7, 11, 14].map((h, i) => (
                      <div
                        key={i}
                        className="w-1 rounded-[1px]"
                        style={{
                          height: h,
                          background: (!analysisStale && i < bars) ? 'var(--t-ok)' : 'var(--t-surface-3)',
                        }}
                      />
                    ))}
                  </div>
                  <span className="text-center text-[7px] text-muted">{t(`signal.${key}`)}</span>
                </div>
              ))}
            </div>
            <p className={`border-t border-line pt-2 text-[10.5px] leading-relaxed ${analysisStale ? 'text-muted/30' : 'text-muted'}`}>
              {clip.why_works ?? clip.reason}
            </p>
          </div>
        )}

        {/* ── actions ── */}
        {pending ? (
          <div className="mt-4 flex items-center justify-center gap-1.5 rounded-sm border border-line bg-surface-2 px-3 py-2 text-sm font-semibold text-muted">
            <Loader2 className="size-4 animate-spin" />
            {t("rendering")}
          </div>
        ) : (
          <div className="mt-4 flex gap-2">
            <ExportMenu
              jobId={jobId}
              clipId={clip.id}
              cleanBakedUrl={videoSrc || null}
              align="left"
              placement="up"
              className="flex-1"
            />
            <Link
              href={`/edit/${jobId}/${clip.id}`}
              className="inline-flex items-center justify-center gap-1.5 rounded-sm border border-line bg-surface-2 px-3 py-2 text-sm font-semibold text-ink transition duration-200 ease-snappy hover:-translate-y-px hover:border-line-strong hover:bg-surface-3"
            >
              <Pencil className="size-4" />
              {t("edit")}
            </Link>
          </div>
        )}
      </div>
    </Card>
  );
}

ClipCardComponent.displayName = "ClipCard";

export const ClipCard = memo(ClipCardComponent, clipCardPropsEqual);
