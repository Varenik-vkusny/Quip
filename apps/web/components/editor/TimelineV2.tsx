"use client";

import { ZoomIn, ZoomOut } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getChapters } from "@/lib/api";
import { Eyebrow } from "@/components/ui/Eyebrow";
import { Numeral } from "@/components/ui/Numeral";
import { mmss } from "@/lib/format";
import { snapTime } from "@/lib/timelineSnap";
import type {
  Chapter,
  ChaptersData,
  ClipType,
  TimelineData,
  TimelineSegment,
  Word,
} from "@/lib/types";

// ────────────────────────────────────────────────────────────────────────────
// TimelineV2 — таймлайн всего видео для страницы редактора.
// Сверху: полоса AI-ГЛАВ (Gemini описал каждый момент видоса — hover даёт
// summary, клик прыгает шортсом на главу). Ниже: дорожка с волной, маркерами
// AI-моментов и оранжевым блоком шортса (drag/resize). Зум 1×–10× (кнопки и
// Ctrl+колесо), пан ползунком — для часовых видео.
// ────────────────────────────────────────────────────────────────────────────

const CLIP_MIN_SEC = 20;
const CLIP_MAX_SEC = 60;

// Keyed to the LOCKED clip-type tokens (globals.css --color-hook/quote/peak/thought) — NOT
// raw hex. Inline style is required (computed glow/positioning), so we read the same CSS
// vars the Badge/ReasonChip/VideoMap use → the timeline's moment ticks match the palette
// everywhere a clip type is shown (previously this map drifted to off-palette amber/green/blue).
const TYPE_COLOR: Record<ClipType, string> = {
  hook: "var(--color-hook)",
  strong_quote: "var(--color-quote)",
  emotional_peak: "var(--color-peak)",
  complete_thought: "var(--color-thought)",
};

// Labels resolve from editor.timeline.<key> in the component (typeLabel helper).
const TYPE_LABEL_KEY: Record<ClipType, string> = {
  hook: "typeHook",
  strong_quote: "typeQuote",
  emotional_peak: "typePeak",
  complete_thought: "typeThought",
};

const WAVE_BUCKETS = 160;
const CHAPTERS_POLL_MS = 4000;
// A press only becomes a drag past this many px — below it a click still seeks (never
// starts a scrub/trim), so click-to-seek stays crisp.
const DRAG_THRESHOLD_PX = 4;
// Trim-snap tolerance as a fraction of the VISIBLE window → tighter (in seconds) the more
// you zoom, matching the on-screen feel (~2% of the track width).
const SNAP_FRAC = 0.02;
// A scrub/click drives the playhead off a local scrubT for instant feedback, then hands it back
// to the real nowSec after this hold (re-armed on every move). Long enough to cover the video-seek
// latency (nowSec catches up in ~1–2 frames via requestVideoFrameCallback), short enough that a
// play started right after a click isn't visibly held.
const SCRUB_HOLD_MS = 350;

const clamp = (n: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, n));

// Parse a user-typed time field: "m:ss", "h:mm:ss", or plain (fractional) seconds → seconds.
// Returns null on anything unparseable so the caller can revert to the committed value. PURE.
function parseTimecode(raw: string): number | null {
  const s = raw.trim();
  if (!s) return null;
  const parts = s.split(":").map((p) => p.trim());
  if (parts.some((p) => !/^\d+(\.\d+)?$/.test(p))) return null;
  let sec = 0;
  for (const p of parts) sec = sec * 60 + Number(p);
  return Number.isFinite(sec) ? sec : null;
}

// Начальное окно зума: клип занимает ~38% видимой области (крупный → ресайз виден и
// удобен). На часовом видео при 1× блок был ~1% и упирался в minWidth — отсюда баг
// «цифры меняются, размер нет». Вызывается в useState-инициализаторе (key=clipId → ремоунт).
function initialView(
  duration: number,
  start: number,
  end: number,
): { zoom: number; viewStart: number } {
  const len = end - start;
  if (duration <= 0 || len <= 0) return { zoom: 1, viewStart: 0 };
  const maxZoom = clamp(duration / 12, 10, 300);
  const z = clamp(duration / Math.min(duration, len * 2.6), 1, maxZoom);
  const vlen = duration / z;
  return {
    zoom: z,
    viewStart: clamp(start + len / 2 - vlen / 2, 0, Math.max(0, duration - vlen)),
  };
}

function buildWave(words: Word[], duration: number, buckets: number): number[] {
  const out = new Array(buckets).fill(0);
  if (duration <= 0 || words.length === 0) return out.map(() => 0.12);
  for (const w of words) {
    const b = clamp(Math.floor((w.start / duration) * buckets), 0, buckets - 1);
    out[b] += 1;
  }
  const max = Math.max(...out, 1);
  return out.map((v) => 0.12 + 0.88 * (v / max));
}

function wordsAround(words: Word[], t: number, pad: number): string {
  const lo = t - pad;
  const hi = t + pad;
  return words
    .filter((w) => w.end >= lo && w.start <= hi)
    .map((w) => w.text)
    .join(" ")
    .trim();
}

// The moment the cursor is actually ON (inside its span, expanded by a small pad) — NOT the
// nearest one regardless of distance. So the tooltip's "this moment" section only appears when
// you're really over a moment; hovering empty track shows just the chapter + what's said.
function segmentAt(segments: TimelineSegment[], t: number, pad: number): TimelineSegment | null {
  let best: TimelineSegment | null = null;
  let bestSpan = Infinity;
  for (const s of segments) {
    if (t >= s.start - pad && t <= s.end + pad) {
      const span = s.end - s.start;
      if (span < bestSpan) {
        bestSpan = span; // prefer the tightest moment under the cursor when they overlap
        best = s;
      }
    }
  }
  return best;
}

function chapterAt(chapters: Chapter[], t: number): Chapter | null {
  return chapters.find((c) => t >= c.start && t < c.end) ?? null;
}

// Editable m:ss field (start / end / length). Uncontrolled draft synced by `key={seconds}` in
// the parent — a commit changes the interval → remount with the fresh value; while you type the
// interval is unchanged so your keystrokes persist. Commits the SAME server path as the grips.
function TimeField({
  label,
  seconds,
  disabled,
  onCommit,
}: {
  label: string;
  seconds: number;
  disabled: boolean;
  onCommit: (sec: number) => void;
}) {
  const [val, setVal] = useState(() => mmss(seconds));
  return (
    <label className="flex min-w-0 flex-col gap-0.5">
      <span className="font-mono text-eyebrow uppercase leading-none text-faint">{label}</span>
      <input
        type="text"
        inputMode="numeric"
        value={val}
        disabled={disabled}
        onChange={(e) => setVal(e.target.value)}
        onFocus={(e) => e.target.select()}
        onKeyDown={(e) => {
          if (e.key === "Enter") e.currentTarget.blur();
          else if (e.key === "Escape") {
            setVal(mmss(seconds));
            e.currentTarget.blur();
          }
        }}
        onBlur={() => {
          const parsed = parseTimecode(val);
          if (parsed === null) {
            setVal(mmss(seconds)); // unparseable → revert to committed value
            return;
          }
          setVal(mmss(parsed));
          onCommit(parsed);
        }}
        className="w-14 rounded-md border border-line bg-surface-2 px-1.5 py-1 text-center font-mono text-xs font-semibold tabular-nums text-ink transition duration-150 ease-snappy focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/30 disabled:opacity-50"
      />
    </label>
  );
}

type DragMode = "move" | "resize-l" | "resize-r";

interface DragState {
  mode: DragMode;
  pointerStart: number;
  pointerStartX: number;
  moved: boolean;
  intervalStart: number;
  intervalEnd: number;
}

interface ScrubState {
  startX: number;
  moved: boolean;
}

export interface TimelineV2Props {
  jobId: string;
  clipId: string;
  version: number;
  data: TimelineData;
  interval: { source_start: number; source_end: number };
  /** Идёт сохранение интервала: драг заблокирован (иначе гонка версий → 409 → reload). */
  busy?: boolean;
  /** Текущее время воспроизведения (sec источника) → реальный плейхед на дорожке. */
  nowSec?: number;
  /** Live-seek (CapCut-стиль): двигаешь/тянешь край клипа → превью сикается на границу
   *  СРАЗУ, без round-trip. Зовётся на каждый pointermove (троттл через rAF в родителе). */
  onScrub?: (sourceSec: number) => void;
  /** Client-only seek (клик/скраб по дорожке → плейхед превью прыгает СРАЗУ, без сервера).
   *  Родитель клампит в границы клипа и троттлит через rAF. НИКОГДА не коммитит интервал. */
  onSeek?: (sourceSec: number) => void;
  onIntervalChange: (start: number, end: number) => void;
}

export function TimelineV2({
  jobId,
  data,
  interval,
  busy = false,
  nowSec,
  onScrub,
  onSeek,
  onIntervalChange,
}: TimelineV2Props) {
  const t = useTranslations("editor.timeline");
  // User-facing clip-type label (hook→Hook, strong_quote→Quote, …) resolved from the catalog.
  const typeLabel = (type: ClipType) => t(TYPE_LABEL_KEY[type]);
  const duration = Math.max(data.duration, 0.001);
  // На часовом видео клип 40с при 1× = ~1% дорожки → блок упирается в minWidth и «не
  // меняет размер при ресайзе». Разрешаем зумить сильнее; начальный зум кадрирует клип.
  const maxZoom = clamp(duration / 12, 10, 300);
  const trackRef = useRef<HTMLDivElement>(null);

  const [live, setLive] = useState<{ start: number; end: number } | null>(null);
  const dragRef = useRef<DragState | null>(null);
  const [hover, setHover] = useState<{ x: number; t: number } | null>(null);

  // ── seek/scrub (client-only, no server) ──
  // Local playhead override: a click/scrub drives it instantly so the head never lags the finger;
  // handed back to the real nowSec once the video seek lands (catch-up effect + safety timeout).
  const [scrubT, setScrubT] = useState<number | null>(null);
  const scrubRef = useRef<ScrubState | null>(null);
  const scrubHoldRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // A snap fired on the trim edge → a bright guide line at this time (cleared on release).
  const [snapLine, setSnapLine] = useState<number | null>(null);

  // ── зум/пан: видимое окно [viewStart, viewStart+viewLen] ⊆ [0, duration] ──
  // Начальное окно кадрирует клип (key=clipId в родителе → ремоунт на смену клипа,
  // инициализатор пересчитывается; собственный зум юзера внутри клипа сохраняется).
  const [zoom, setZoom] = useState(
    () => initialView(duration, interval.source_start, interval.source_end).zoom,
  );
  const [viewStart, setViewStart] = useState(
    () => initialView(duration, interval.source_start, interval.source_end).viewStart,
  );
  const viewLen = duration / zoom;
  const viewStartClamped = clamp(viewStart, 0, duration - viewLen);

  // ── AI-главы: кэш-эндпоинт + поллинг пока pending ──
  const [chapters, setChapters] = useState<ChaptersData | null>(null);
  // T4 #9: инкремент → перезапуск генерации (первый запрос цикла с retry=true)
  const [chReloadKey, setChReloadKey] = useState(0);
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    async function poll(force: boolean) {
      try {
        const ch = await getChapters(jobId, force);
        if (cancelled) return;
        setChapters(ch);
        if (ch.status === "pending") timer = setTimeout(() => poll(false), CHAPTERS_POLL_MS);
      } catch {
        if (!cancelled) setChapters({ status: "failed", chapters: [], error: "unavailable" });
      }
    }
    void poll(chReloadKey > 0); // retry только при ручном «Повторить»
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [jobId, chReloadKey]);

  const retryChapters = () => {
    setChapters({ status: "pending", chapters: [], error: null });
    setChReloadKey((k) => k + 1);
  };

  const segStart = live ? live.start : interval.source_start;
  const segEnd = live ? live.end : interval.source_end;

  const wave = useMemo(() => buildWave(data.words, duration, WAVE_BUCKETS), [data.words, duration]);

  // t (sec источника) → проценты ВИДИМОГО окна
  const pct = useCallback(
    (t: number) => `${clamp(((t - viewStartClamped) / viewLen) * 100, -10, 110)}%`,
    [viewStartClamped, viewLen],
  );
  const fracOf = useCallback(
    (t: number) => (t - viewStartClamped) / viewLen,
    [viewStartClamped, viewLen],
  );

  const pxToTime = useCallback(
    (clientX: number): number => {
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
      const nextLen = duration / z;
      // якорим точку под курсором: её доля в окне сохраняется
      const frac = clamp((anchorT - viewStartClamped) / viewLen, 0, 1);
      setZoom(z);
      setViewStart(clamp(anchorT - frac * nextLen, 0, duration - nextLen));
    },
    [duration, viewStartClamped, viewLen, maxZoom],
  );

  const onWheel = useCallback(
    (e: React.WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) return; // обычный скролл страницы не перехватываем
      e.preventDefault();
      const anchor = pxToTime(e.clientX);
      setZoomAround(zoom * (e.deltaY < 0 ? 1.25 : 0.8), anchor);
    },
    [zoom, pxToTime, setZoomAround],
  );

  const commit = useCallback(
    (start: number, end: number) => onIntervalChange(start, end),
    [onIntervalChange],
  );

  // Seeking is only meaningful WITHIN the clip — the preview loops [source_start, source_end].
  // Clamp there so a click anywhere reliably moves the playhead (outside → nearest clip edge)
  // instead of the preview bouncing back to the clip start.
  const clampToClip = useCallback(
    (sec: number) => clamp(sec, interval.source_start, interval.source_end),
    [interval.source_start, interval.source_end],
  );

  // Client-only seek: drive the local playhead NOW, ask the parent to seek the video (rAF-
  // coalesced, no server), and arm a safety timeout to release the head back to nowSec.
  const seekTo = useCallback(
    (sec: number) => {
      const target = clampToClip(sec);
      setScrubT(target);
      onSeek?.(target);
      if (scrubHoldRef.current) clearTimeout(scrubHoldRef.current);
      scrubHoldRef.current = setTimeout(() => setScrubT(null), SCRUB_HOLD_MS);
    },
    [clampToClip, onSeek],
  );
  useEffect(
    () => () => {
      if (scrubHoldRef.current) clearTimeout(scrubHoldRef.current);
    },
    [],
  );

  // Snap targets for the trim edge: every AI-moment tick + every chapter edge (source seconds).
  // Shot boundaries aren't surfaced to the timeline, so those two content lines are the anchors.
  const snapTargets = useMemo(() => {
    const out: number[] = [];
    for (const s of data.segments) out.push(s.start, s.end);
    const chs = chapters?.status === "done" ? (chapters.chapters ?? []) : [];
    for (const c of chs) out.push(c.start, c.end);
    return out;
  }, [data.segments, chapters]);

  // ── numeric trim fields → same server commit as the grips (clamped to CLIP_MIN/MAX) ──
  const commitStart = useCallback(
    (raw: number) => {
      const end = interval.source_end;
      const s = clamp(raw, Math.max(0, end - CLIP_MAX_SEC), end - CLIP_MIN_SEC);
      commit(s, end);
    },
    [interval.source_end, commit],
  );
  const commitEnd = useCallback(
    (raw: number) => {
      const start = interval.source_start;
      const e = clamp(raw, start + CLIP_MIN_SEC, Math.min(duration, start + CLIP_MAX_SEC));
      commit(start, e);
    },
    [interval.source_start, duration, commit],
  );
  const commitLength = useCallback(
    (rawLen: number) => {
      const start = interval.source_start;
      const len = clamp(rawLen, CLIP_MIN_SEC, CLIP_MAX_SEC);
      let end = start + len;
      let s = start;
      if (end > duration) {
        end = duration;
        s = Math.max(0, end - len);
      }
      commit(s, end);
    },
    [interval.source_start, duration, commit],
  );

  const onPointerDown = useCallback(
    (dragMode: DragMode) => (e: React.PointerEvent) => {
      e.preventDefault();
      e.stopPropagation();
      if (busy) return; // предыдущий сдвиг ещё сохраняется — не плодим конфликты версий
      (e.target as HTMLElement).setPointerCapture(e.pointerId);
      dragRef.current = {
        mode: dragMode,
        pointerStart: pxToTime(e.clientX),
        pointerStartX: e.clientX,
        moved: false,
        intervalStart: interval.source_start,
        intervalEnd: interval.source_end,
      };
      setLive({ start: interval.source_start, end: interval.source_end });
    },
    [interval.source_start, interval.source_end, pxToTime, busy],
  );

  // Background press (empty track) → scrub/seek gesture. The clip window & markers stopPropagation
  // on their own pointerdown, so this never hijacks a trim drag or a marker click.
  const onTrackPointerDown = useCallback((e: React.PointerEvent) => {
    if (dragRef.current) return;
    scrubRef.current = { startX: e.clientX, moved: false };
    try {
      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    } catch {
      /* capture unsupported — the gesture still tracks via move coords */
    }
    setHover(null);
  }, []);

  const onPointerMove = useCallback(
    (e: React.PointerEvent) => {
      const drag = dragRef.current;
      if (drag) {
        // Below the threshold a press is still a click (→ seek on release), not a trim.
        if (!drag.moved) {
          if (Math.abs(e.clientX - drag.pointerStartX) < DRAG_THRESHOLD_PX) return;
          drag.moved = true;
        }
        const tCur = pxToTime(e.clientX);
        const delta = tCur - drag.pointerStart;
        let start = drag.intervalStart;
        let end = drag.intervalEnd;
        const tol = viewLen * SNAP_FRAC;
        let snapAt: number | null = null;

        // focusT = граница, которую юзер сейчас «держит» → её и показываем в превью.
        // move → начало клипа; resize-l → новый старт; resize-r → новый конец. Каждый край
        // липнет к ближайшему AI-моменту/границе главы (snapTime) — CapCut-фидбэк без round-trip.
        let focusT: number;
        if (drag.mode === "move") {
          const len = drag.intervalEnd - drag.intervalStart;
          start = clamp(drag.intervalStart + delta, 0, duration - len);
          const snapped = snapTime(start, snapTargets, tol);
          if (snapped !== start) {
            start = clamp(snapped, 0, duration - len);
            snapAt = start;
          }
          end = start + len;
          focusT = start;
        } else if (drag.mode === "resize-l") {
          const minStart = Math.max(0, end - CLIP_MAX_SEC);
          const maxStart = end - CLIP_MIN_SEC;
          start = clamp(drag.intervalStart + delta, minStart, maxStart);
          const snapped = snapTime(start, snapTargets, tol);
          if (snapped !== start && snapped >= minStart && snapped <= maxStart) {
            start = snapped;
            snapAt = start;
          }
          focusT = start;
        } else {
          const minEnd = start + CLIP_MIN_SEC;
          const maxEnd = Math.min(duration, start + CLIP_MAX_SEC);
          end = clamp(drag.intervalEnd + delta, minEnd, maxEnd);
          const snapped = snapTime(end, snapTargets, tol);
          if (snapped !== end && snapped >= minEnd && snapped <= maxEnd) {
            end = snapped;
            snapAt = end;
          }
          focusT = end;
        }
        setLive({ start, end });
        setSnapLine(snapAt);
        onScrub?.(focusT);
        return;
      }

      // Background scrub: past the threshold, drag = live seek off scrubT (instant playhead).
      const sc = scrubRef.current;
      if (sc) {
        if (!sc.moved && Math.abs(e.clientX - sc.startX) < DRAG_THRESHOLD_PX) return;
        sc.moved = true;
        setHover(null);
        seekTo(pxToTime(e.clientX));
        return;
      }

      // Idle: track the hover cursor for the tooltip.
      if (trackRef.current) {
        const r = trackRef.current.getBoundingClientRect();
        setHover({ x: e.clientX - r.left, t: pxToTime(e.clientX) });
      }
    },
    [duration, pxToTime, onScrub, seekTo, viewLen, snapTargets],
  );

  const endDrag = useCallback(
    (e: React.PointerEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      try {
        (e.target as HTMLElement).releasePointerCapture(e.pointerId);
      } catch {
        /* capture мог не стоять */
      }
      dragRef.current = null;
      const cur = live;
      setLive(null);
      setSnapLine(null);
      if (!drag.moved) {
        // Never moved = a click. On the window BODY → seek to that point (a bare grip click
        // does nothing). Client-only — never a server round-trip.
        if (drag.mode === "move") seekTo(pxToTime(e.clientX));
        return;
      }
      if (cur && (cur.start !== drag.intervalStart || cur.end !== drag.intervalEnd)) {
        commit(cur.start, cur.end);
      }
    },
    [live, commit, seekTo, pxToTime],
  );

  // Track-level up/cancel: hand off to endDrag when trimming, else resolve the scrub gesture
  // (click OR final scrub position → seek).
  const onTrackPointerUp = useCallback(
    (e: React.PointerEvent) => {
      if (dragRef.current) return endDrag(e);
      const sc = scrubRef.current;
      scrubRef.current = null;
      if (!sc) return;
      try {
        (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
      } catch {
        /* capture may never have been set */
      }
      seekTo(pxToTime(e.clientX));
    },
    [endDrag, seekTo, pxToTime],
  );

  const onTrackPointerCancel = useCallback(
    (e: React.PointerEvent) => {
      if (dragRef.current) return endDrag(e);
      scrubRef.current = null;
    },
    [endDrag],
  );

  const hoverSeg = hover ? segmentAt(data.segments, hover.t, viewLen * 0.015) : null;
  const chapterList = chapters?.status === "done" ? (chapters.chapters ?? []) : [];
  const hoverChapter = hover ? chapterAt(chapterList, hover.t) : null;
  const hoverText = hover ? wordsAround(data.words, hover.t, 3) : "";
  // Playhead = the local scrub override (instant, lag-free) when active, else the real video time.
  const headT = scrubT ?? nowSec;

  return (
    <div className="w-full select-none" onWheel={onWheel}>
      {/* ── control row: editable trim fields (start / end / length) on the left; zoom on the
          right. Typing a field commits the SAME server trim as the grips. ── */}
      <div className="mb-1.5 flex flex-wrap items-end justify-between gap-x-3 gap-y-2">
        <div className="flex flex-wrap items-end gap-2.5">
          <TimeField
            key={`start-${interval.source_start.toFixed(2)}`}
            label={t("startLabel")}
            seconds={interval.source_start}
            disabled={busy}
            onCommit={commitStart}
          />
          <TimeField
            key={`end-${interval.source_end.toFixed(2)}`}
            label={t("endLabel")}
            seconds={interval.source_end}
            disabled={busy}
            onCommit={commitEnd}
          />
          <TimeField
            key={`len-${(interval.source_end - interval.source_start).toFixed(2)}`}
            label={t("lengthLabel")}
            seconds={interval.source_end - interval.source_start}
            disabled={busy}
            onCommit={commitLength}
          />
        </div>
        <div className="flex shrink-0 items-center gap-2 self-end">
          <button
            type="button"
            aria-label={t("zoomOut")}
            disabled={zoom <= 1}
            onClick={() => setZoomAround(zoom / 1.5, viewStartClamped + viewLen / 2)}
            className="inline-flex size-9 sm:size-6 items-center justify-center rounded-md border border-line text-muted transition duration-150 ease-snappy enabled:hover:text-ink disabled:opacity-30"
          >
            <ZoomOut className="size-3.5" />
          </button>
          <Numeral className="w-9 text-center text-[10px] text-muted">{zoom.toFixed(1)}×</Numeral>
          <button
            type="button"
            aria-label={t("zoomIn")}
            disabled={zoom >= maxZoom}
            onClick={() => setZoomAround(zoom * 1.5, segStart + (segEnd - segStart) / 2)}
            className="inline-flex size-9 sm:size-6 items-center justify-center rounded-md border border-line text-muted transition duration-150 ease-snappy enabled:hover:text-ink disabled:opacity-30"
          >
            <ZoomIn className="size-3.5" />
          </button>
        </div>
      </div>

      {/* ── chapters band — the GLOBAL themes of the whole video (clip = a slice of one) ── */}
      <Eyebrow tone="faint" className="mb-1.5 block leading-none">
        {t("chapters")}
      </Eyebrow>
      <div className="relative mb-2.5 h-8 w-full overflow-hidden rounded-md border border-line bg-surface-2/50">
        {chapters === null || chapters.status === "pending" ? (
          <div className="flex h-full items-center gap-2 px-3">
            <span className="size-2 animate-pulse rounded-full bg-accent" />
            <span className="text-[11px] text-muted">{t("describingMoments")}</span>
          </div>
        ) : chapters.status === "failed" ? (
          <div className="flex h-full items-center gap-2 px-3">
            <span className="truncate text-[11px] text-muted">
              {t("mapUnavailable", { error: chapters.error ?? "error" })}
            </span>
            <button
              type="button"
              onClick={retryChapters}
              className="shrink-0 rounded-md border border-line px-2 py-0.5 text-[11px] font-semibold text-muted transition duration-150 ease-snappy hover:border-line-strong hover:bg-surface-3 hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
            >
              {t("retry")}
            </button>
          </div>
        ) : (
          chapterList.map((c, i) => {
            const leftFrac = fracOf(c.start);
            const widthFrac = (c.end - c.start) / viewLen;
            if (leftFrac + widthFrac < 0 || leftFrac > 1) return null;
            return (
              <button
                key={i}
                type="button"
                onClick={() => seekTo(c.start)}
                title={`${c.title}\n${c.summary}`}
                className="group/ch absolute top-0 flex h-full items-center gap-1.5 truncate border-l border-line px-2 text-left text-[11px] leading-none text-muted transition hover:bg-surface-3 hover:text-ink"
                style={{ left: pct(c.start), width: `${Math.max(widthFrac * 100, 0.5)}%` }}
              >
                {/* start-of-chapter tick → clear boundary; lights coral on hover */}
                <span
                  className="h-3 w-0.5 shrink-0 rounded-pill bg-line-strong transition group-hover/ch:bg-accent"
                  aria-hidden
                />
                <span className="truncate">{c.title}</span>
              </button>
            );
          })
        )}
      </div>

      <div className="relative">
        {/* дорожка */}
        <div
          ref={trackRef}
          className="relative h-20 w-full touch-none cursor-pointer overflow-hidden rounded-lg border border-line bg-gradient-to-b from-surface-2 to-surface shadow-inner"
          onPointerDown={onTrackPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onTrackPointerUp}
          onPointerCancel={onTrackPointerCancel}
          onPointerLeave={() => {
            if (!dragRef.current && !scrubRef.current) setHover(null);
          }}
        >
          {/* вертикальные грид-линии (выровнены с линейкой времени) */}
          {Array.from({ length: 6 }).map((_, i) => (
            <div
              key={`grid-${i}`}
              className="pointer-events-none absolute inset-y-0 z-0 w-px bg-line/30"
              style={{ left: `${(i / 5) * 100}%` }}
              aria-hidden
            />
          ))}
          {/* волна (декор): рисуем только видимое окно */}
          <div className="pointer-events-none absolute inset-0 flex items-end gap-px px-px">
            {wave.map((h, i) => {
              const t0 = (i / WAVE_BUCKETS) * duration;
              if (t0 < viewStartClamped - duration / WAVE_BUCKETS || t0 > viewStartClamped + viewLen)
                return null;
              return (
                <div
                  key={i}
                  className="absolute bottom-0 rounded-t-sm bg-muted/25"
                  style={{
                    left: pct(t0),
                    width: `${(duration / WAVE_BUCKETS / viewLen) * 100}%`,
                    height: `${h * 100}%`,
                  }}
                />
              );
            })}
          </div>

          {/* маркеры сегментов ИИ — наглядные капсулы по типу момента */}
          {data.segments.map((s, i) => {
            const widthFrac = Math.max((s.end - s.start) / viewLen, 0.004);
            const c = TYPE_COLOR[s.type];
            return (
              <button
                key={`${s.clip_id ?? "seg"}-${i}`}
                type="button"
                onPointerDown={(e) => e.stopPropagation()}
                onClick={(e) => {
                  e.stopPropagation();
                  seekTo(s.start);
                }}
                title={`${typeLabel(s.type)} · ${s.score.toFixed(2)} · ${s.reason}`}
                aria-label={t("goToMoment", { type: typeLabel(s.type) })}
                className="group/marker absolute bottom-1.5 z-[1] cursor-pointer rounded-full opacity-75 transition hover:opacity-100"
                style={{
                  left: pct(s.start),
                  width: `${widthFrac * 100}%`,
                  height: "7px",
                  minWidth: "3px",
                  background: c,
                }}
              >
                <span
                  className="absolute left-1/2 top-1/2 size-1 -translate-x-1/2 -translate-y-1/2 rounded-full bg-bg/85 opacity-0 transition group-hover/marker:opacity-100"
                  aria-hidden
                />
              </button>
            );
          })}

          {/* затемнение ВНЕ выбранного клипа (trim-аффорданс: выбранный кусок ярче остального) */}
          <div
            className="pointer-events-none absolute inset-y-0 left-0 z-[2] bg-bg/60"
            style={{ width: `${clamp(fracOf(segStart) * 100, 0, 100)}%` }}
            aria-hidden
          />
          <div
            className="pointer-events-none absolute inset-y-0 right-0 z-[2] bg-bg/60"
            style={{ left: `${clamp(fracOf(segEnd) * 100, 0, 100)}%` }}
            aria-hidden
          />

          {/* блок шортса — «герой» дорожки (хватай и тяни) */}
          <div
            onPointerDown={onPointerDown("move")}
            className={`absolute top-0 bottom-0 z-[3] touch-none rounded-lg border-2 border-accent bg-gradient-to-b from-accent/30 to-accent/10 shadow-card-hover ${
              busy ? "animate-pulse cursor-wait" : "cursor-grab active:cursor-grabbing"
            }`}
            style={{
              left: pct(segStart),
              width: `${((segEnd - segStart) / viewLen) * 100}%`,
              minWidth: "2rem",
            }}
          >
            {/* ручка слева */}
            <div
              onPointerDown={onPointerDown("resize-l")}
              className="group/grip absolute -left-2.5 sm:-left-1.5 top-0 bottom-0 flex w-5 sm:w-3.5 cursor-ew-resize items-center justify-center"
            >
              <span className="flex h-9 items-center justify-center gap-[2px] rounded-full bg-accent px-1 shadow transition group-hover/grip:brightness-110">
                <span className="h-4 w-px rounded-full bg-on-accent/70" />
                <span className="h-4 w-px rounded-full bg-on-accent/70" />
              </span>
            </div>
            {/* ручка справа */}
            <div
              onPointerDown={onPointerDown("resize-r")}
              className="group/grip absolute -right-2.5 sm:-right-1.5 top-0 bottom-0 flex w-5 sm:w-3.5 cursor-ew-resize items-center justify-center"
            >
              <span className="flex h-9 items-center justify-center gap-[2px] rounded-full bg-accent px-1 shadow transition group-hover/grip:brightness-110">
                <span className="h-4 w-px rounded-full bg-on-accent/70" />
                <span className="h-4 w-px rounded-full bg-on-accent/70" />
              </span>
            </div>
            {/* длительность — пилюля сверху */}
            <span className="pointer-events-none absolute left-1/2 top-1 -translate-x-1/2 rounded-full bg-accent px-2 py-0.5 font-mono text-[10px] font-bold text-on-accent shadow">
              {busy ? t("saving") : mmss(segEnd - segStart)}
            </span>
            {!busy && segEnd - segStart <= CLIP_MIN_SEC + 0.5 && (
              <span className="pointer-events-none absolute inset-x-0 bottom-1 text-center text-[9px] font-medium text-ink/70">
                {t("minSec", { n: CLIP_MIN_SEC })}
              </span>
            )}
          </div>

          {/* snap guide — a bright line where the dragged trim edge is snapping to a moment/chapter */}
          {snapLine !== null && fracOf(snapLine) >= -0.001 && fracOf(snapLine) <= 1.001 && (
            <div
              className="pointer-events-none absolute top-0 bottom-0 z-[12] w-0.5 bg-accent/90 shadow-[0_0_6px_var(--t-accent-line)]"
              style={{ left: pct(snapLine) }}
              aria-hidden
            />
          )}

          {/* плейхед — по headT (scrubT во время клика/скраба → мгновенно, иначе реальный nowSec) */}
          {headT !== undefined &&
            fracOf(headT) >= -0.001 &&
            fracOf(headT) <= 1.001 && (
              <div
                className="pointer-events-none absolute top-0 bottom-0 z-[11] w-px bg-accent shadow-[0_0_8px_var(--t-accent-line)]"
                style={{ left: pct(headT) }}
              >
                {/* треугольная головка сверху — явно отличает плейхед от hover-курсора */}
                <span className="absolute -top-px left-1/2 size-0 -translate-x-1/2 border-x-[5px] border-x-transparent border-t-[7px] border-t-accent" />
              </div>
            )}

          {/* hover-курсор (следует за мышью) — тонкая ink-линия, отлична от акцентного плейхеда */}
          {hover && (
            <div
              className="pointer-events-none absolute top-0 bottom-0 z-[10] w-px bg-ink/60"
              style={{ left: `${hover.x}px` }}
            />
          )}
        </div>

        {/* hover tooltip — TWO clear levels: the GLOBAL chapter (the theme of this part of the
            video, where you are) on top; below a hairline the SPECIFIC moment under the cursor —
            or, when you're not on a moment, just what's being said there. No more wall of mixed text. */}
        {hover && (hoverChapter || hoverSeg || hoverText) && (
          <div
            className="pointer-events-none absolute z-10 w-64 -translate-x-1/2 overflow-hidden rounded-lg border border-line bg-surface shadow-lg"
            style={{
              left: `clamp(132px, ${hover.x}px, calc(100% - 132px))`,
              bottom: "calc(100% + 8px)",
            }}
          >
            {/* GLOBAL — the chapter / where you are in the whole video */}
            <div className="border-b border-line bg-surface-2/60 px-3 py-2">
              <div className="flex items-center justify-between gap-2">
                <Eyebrow tone="faint">{hoverChapter ? t("chapter") : t("position")}</Eyebrow>
                <Numeral className="text-[10px] text-faint">{mmss(hover.t)}</Numeral>
              </div>
              <p className="mt-0.5 truncate text-xs font-semibold text-ink">
                {hoverChapter ? hoverChapter.title : t("inThisPart")}
              </p>
            </div>
            {/* SPECIFIC — the moment right here, or (off a moment) what's being said */}
            <div className="px-3 py-2">
              {hoverSeg ? (
                <>
                  <div className="flex items-center justify-between gap-2">
                    <span
                      className="inline-flex items-center gap-1.5 text-[11px] font-semibold"
                      style={{ color: TYPE_COLOR[hoverSeg.type] }}
                    >
                      <span
                        className="size-1.5 rounded-pill"
                        style={{ background: TYPE_COLOR[hoverSeg.type] }}
                      />
                      {typeLabel(hoverSeg.type)}
                    </span>
                    <Numeral className="text-[10px] text-muted">
                      {t("pctMatch", { pct: Math.round(hoverSeg.score * 100) })}
                    </Numeral>
                  </div>
                  {hoverSeg.hook && (
                    <p className="mt-1.5 line-clamp-2 text-[11px] font-semibold leading-snug text-ink">
                      “{hoverSeg.hook}”
                    </p>
                  )}
                  <p className="mt-1 line-clamp-2 text-[11px] leading-snug text-muted">
                    {hoverSeg.why_works || hoverSeg.reason}
                  </p>
                  <p className="mt-1.5 text-[10px] font-medium text-accent">{t("clickToSeek")}</p>
                </>
              ) : hoverText ? (
                <>
                  <Eyebrow tone="faint" className="block">
                    {t("saidHere")}
                  </Eyebrow>
                  <p className="mt-1 line-clamp-2 text-[11px] leading-snug text-muted">
                    “{hoverText}”
                  </p>
                </>
              ) : null}
            </div>
          </div>
        )}
      </div>

      {/* ── пан при зуме + линейка ── */}
      {zoom > 1 && (
        <input
          type="range"
          min={0}
          max={1000}
          value={Math.round((viewStartClamped / Math.max(0.001, duration - viewLen)) * 1000)}
          onChange={(e) =>
            setViewStart((Number(e.target.value) / 1000) * (duration - viewLen))
          }
          aria-label={t("scrollTimeline")}
          className="range-touch mt-1 h-1 w-full cursor-pointer appearance-none rounded-full bg-surface-2 accent-accent"
        />
      )}
      <div className="relative mt-1.5 h-4">
        {Array.from({ length: 6 }).map((_, i) => {
          const frac = i / 5;
          return (
            <span
              key={i}
              className="absolute font-mono text-[10px] text-muted"
              style={{
                left: `${frac * 100}%`,
                transform:
                  i === 0 ? "translateX(0)" : i === 5 ? "translateX(-100%)" : "translateX(-50%)",
              }}
            >
              {mmss(viewStartClamped + frac * viewLen)}
            </span>
          );
        })}
      </div>
    </div>
  );
}

export default TimelineV2;
