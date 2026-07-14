"use client";

import { Loader2, Scissors, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { mmss } from "@/lib/format";
import { shotIsManual } from "@/lib/reframeFrame";
import type { CropOverride, SourceInterval } from "@/lib/types";

// ────────────────────────────────────────────────────────────────────────────
// FitTimeline — полоса шотов под превью для ПОШОТОВОГО кадрирования.
// Видео склеено из шотов (смен ракурса камеры). Для каждого шота AI решает,
// как уместить его в 9:16: Wide (весь кадр) / Tight (кроп на спикере) / Split.
// Тут можно ПЕРЕОПРЕДЕЛИТЬ кадр на выбранных шотах.
//
// Честные состояния (никаких выдуманных границ — 2026-07-14, решение фаундера):
//   • loading  — план шотов ещё считается (или сервер вернул 202 и мы поллим) → скелетон.
//   • failed   — расчёт плана НЕ удался → явная ошибка + «Повторить» (раньше тут молча
//                рисовались РАВНЫЕ куски под видом реальных шотов ИИ — это и был обман).
//   • regions  — реальные границы шотов от CV (PySceneDetect): блок = реальная склейка.
//
// Драг по полосе → выбор СМЕЖНОГО диапазона шотов (снап к границам = целый шот).
// [Wide / Tight / Auto] применяет режим к диапазону (Auto = убрать override).
//
// «Split here»: детектор/мердж мог склеить два шота в один — границы внутри нет, форснуть
// половину нельзя. Кнопка вставляет КЛИЕНТСКУЮ границу на текущем плейхеде: время реза
// копится в локальном `userSplits` (НЕ мутируем пришедший `regions`), а отображаемые блоки
// выводятся из regions + userSplits. Выделение/apply работают по этим блокам, поэтому можно
// выбрать пол-региона и форснуть Tight/Wide — apply шлёт source-диапазон именно подблока через
// тот же onApplyRange. Бэкенд режет регион по этим границам (снап к нативным кадрам на сервере).
// ────────────────────────────────────────────────────────────────────────────

/** Регион reframe-плана в КЛИП-времени (совпадает с RawRegion в ClipEditorScreen). */
export interface FitRegion {
  t0: number;
  t1: number;
  mode: string;
}

export type ForceMode = "fit" | "fill" | "auto";

/** A single shot picked in the strip (source-range keyed; carries clip-time t0/t1 for the label). */
export interface SelectedShot {
  source_start: number;
  source_end: number;
  index: number;
  t0: number;
  t1: number;
}

export interface FitTimelineProps {
  /** Регионы reframe-плана (клип-время t0/t1/mode). null/пусто → ничего не рисуем. */
  regions: FitRegion[] | null;
  /** Интервалы клипа (CLIP-порядок) для маппинга клип-время → source-время. */
  intervals: SourceInterval[];
  /** Текущие ручные override'ы — подсветить регионы с форсированным кадром. */
  overrides?: CropOverride[];
  /** Текущее время видео (source-сек) → плейхед + подсветка активного шота. */
  nowSec?: number;
  /** Идёт сохранение/рендер: взаимодействие заблокировано. */
  busy?: boolean;
  /** План шотов ещё грузится (или сервер считает — 202-поллинг) → скелетон вместо полосы. */
  loading?: boolean;
  /** Расчёт плана упал → честная ошибка + «Повторить» (НЕ выдуманные равные куски). */
  failed?: boolean;
  /** Повторить расчёт плана шотов (кнопка в failed-состоянии). */
  onRetry?: () => void;
  /** Применить режим к source-диапазону выделенных регионов. mode:"auto" чистит override. */
  onApplyRange: (sourceStart: number, sourceEnd: number, mode: ForceMode) => void;
  /** P1: шот, к которому привязаны верхние Frame-контролы + драг-пан превью (null = весь клип). */
  selectedShot?: SelectedShot | null;
  /** P1: клик по одному блоку → выбрать этот шот (или null при range-драге/сбросе). */
  onSelectShot?: (shot: SelectedShot | null) => void;
  /** P3: персист ручной правки реза на SOURCE-времени. "split" вставляет границу на плейхеде;
   *  "merge" убирает AI-рез на границе шотов (склеивает два шота). Optional: без него «Split here»
   *  остаётся чисто клиентским (маркер `userSplits`), а merge-контролы не рисуются. */
  onCut?: (sourceTime: number, action: "split" | "merge") => void;
}

const clamp = (n: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, n));

/**
 * Маппинг точки КЛИП-времени → source-время (обратный к backend regions_to_clip_time).
 * Клип-время = конкатенация intervals: интервал i занимает [offset, offset+dur). Точка
 * clipT лежит в интервале, чей кумулятивный offset её содержит → source = source_start +
 * (clipT - offset). Одно-интервальный кейс = source = intervals[0].source_start + clipT. PURE.
 */
export function clipTimeToSource(clipT: number, intervals: SourceInterval[]): number {
  if (intervals.length === 0) return clipT;
  let offset = 0;
  for (let i = 0; i < intervals.length; i++) {
    const iv = intervals[i];
    const dur = iv.source_end - iv.source_start;
    // A point ON an inner seam (clipT === offset+dur, i.e. the join between interval i and i+1)
    // belongs to the NEXT interval's START, not this one's end — use `<` for inner intervals so a
    // region/shot starting exactly on a seam maps correctly. The LAST interval keeps `<=` to catch
    // the tail (clipT == total clip length). (Single-interval clips are unaffected.)
    const isLast = i === intervals.length - 1;
    if (isLast ? clipT <= offset + dur : clipT < offset + dur) {
      return iv.source_start + (clipT - offset);
    }
    offset += dur;
  }
  // за пределами клипа → конец последнего интервала
  return intervals[intervals.length - 1].source_end;
}

/** Активен ли ручной override на регионе [t0,t1] клип-времени (по пересечению source-окна). */
function regionOverride(
  region: FitRegion,
  intervals: SourceInterval[],
  overrides: CropOverride[],
): CropOverride | null {
  if (overrides.length === 0) return null;
  const s0 = clipTimeToSource(region.t0, intervals);
  const s1 = clipTimeToSource(region.t1, intervals);
  // последний выигрывает (как в ClipEditorScreen.frame) — берём с конца
  for (let i = overrides.length - 1; i >= 0; i--) {
    const ov = overrides[i];
    if (ov.source_start < s1 && ov.source_end > s0) return ov;
  }
  return null;
}

const MODE_TINT: Record<string, string> = {
  fill: "bg-quote/30", // tight crop (синий)
  fit: "bg-thought/30", // wide / horizontal (зелёный)
  split: "bg-peak/30", // split (фиолетовый)
};

// Labels/hints resolve from editor.shots.<labelKey|hintKey> in the component.
const FORCE_OPTIONS: { value: ForceMode; labelKey: string; hintKey: string }[] = [
  { value: "fit", labelKey: "wide", hintKey: "wideHint" },
  { value: "fill", labelKey: "tight", hintKey: "tightHint" },
  { value: "auto", labelKey: "auto", hintKey: "autoHint" },
];

export function FitTimeline({
  regions,
  intervals,
  overrides = [],
  nowSec,
  busy = false,
  loading = false,
  failed = false,
  onRetry,
  onApplyRange,
  selectedShot,
  onSelectShot,
  onCut,
}: FitTimelineProps) {
  const t = useTranslations("editor.shots");
  // User-facing framing-mode label (fit→Wide, fill→Tight, split→Split; unknown → raw).
  const mlabel = (m: string) =>
    m === "fit" ? t("wide") : m === "fill" ? t("tight") : m === "split" ? t("split") : m;
  const trackRef = useRef<HTMLDivElement>(null);
  // выделенный диапазон БЛОКОВ [from, to] (индексы по отображаемым блокам, оба включительно) или null
  const [sel, setSel] = useState<{ from: number; to: number } | null>(null);
  const [mode, setMode] = useState<ForceMode>("fit");
  // КЛИЕНТСКИЕ резы (клип-время). Дробят пришедшие регионы на подблоки чисто для
  // отображения/выделения — НЕ мутируем `regions`. Округляем до мс, чтобы не плодить
  // почти-совпадающие резы и не делать нулевых блоков.
  const [userSplits, setUserSplits] = useState<number[]>([]);
  const dragRef = useRef<{ anchor: number; moved: boolean } | null>(null);

  // суммарная клип-длительность = конец последнего региона (= длина клипа в reframe-плане)
  const clipDur = useMemo(() => {
    if (!regions || regions.length === 0) return 0;
    return Math.max(0.001, regions[regions.length - 1].t1 - regions[0].t0);
  }, [regions]);

  const t0 = regions && regions.length > 0 ? regions[0].t0 : 0;

  // Пришёл новый план шотов (другой клип / перечитанный /reframe) → старые резы больше не
  // относятся к этим регионам. Сброс в ФАЗЕ РЕНДЕРА при смене prop (паттерн React "adjust state
  // on prop change", НЕ эффект) — без каскадного ре-рендера и до первой отрисовки новых регионов.
  const [prevRegions, setPrevRegions] = useState(regions);
  if (prevRegions !== regions) {
    setPrevRegions(regions);
    setUserSplits([]);
  }

  // Отображаемые блоки = регионы, порезанные клиентскими резами. БЕЗ резов список блоков
  // 1:1 совпадает с regions (тот же t0/t1/mode) → путь «выбрать целый шот» не меняется.
  // Каждый блок наследует mode своего региона и помнит, что он подблок (для подписи).
  const blocks = useMemo<{ t0: number; t1: number; mode: string; split: boolean }[]>(() => {
    if (!regions || regions.length === 0) return [];
    if (userSplits.length === 0) {
      return regions.map((r) => ({ t0: r.t0, t1: r.t1, mode: r.mode, split: false }));
    }
    const out: { t0: number; t1: number; mode: string; split: boolean }[] = [];
    for (const reg of regions) {
      // резы строго ВНУТРИ региона, по возрастанию
      const cuts = userSplits.filter((s) => s > reg.t0 + 1e-4 && s < reg.t1 - 1e-4).sort((a, b) => a - b);
      const wasSplit = cuts.length > 0;
      let start = reg.t0;
      for (const c of cuts) {
        out.push({ t0: start, t1: c, mode: reg.mode, split: true });
        start = c;
      }
      out.push({ t0: start, t1: reg.t1, mode: reg.mode, split: wasSplit });
    }
    return out;
  }, [regions, userSplits]);

  // индекс БЛОКА под clientX (снап к границам блоков)
  const regionAtX = useCallback(
    (clientX: number): number | null => {
      const el = trackRef.current;
      if (!el || blocks.length === 0) return null;
      const r = el.getBoundingClientRect();
      const clipT = t0 + clamp((clientX - r.left) / r.width, 0, 1) * clipDur;
      for (let i = 0; i < blocks.length; i++) {
        if (clipT >= blocks[i].t0 && clipT < blocks[i].t1) return i;
      }
      return blocks.length - 1; // хвост → последний блок
    },
    [blocks, t0, clipDur],
  );

  const onPointerDown = useCallback(
    (e: React.PointerEvent) => {
      if (busy) return;
      const idx = regionAtX(e.clientX);
      if (idx === null) return;
      e.preventDefault();
      (e.target as HTMLElement).setPointerCapture(e.pointerId);
      dragRef.current = { anchor: idx, moved: false };
      setSel({ from: idx, to: idx });
    },
    [busy, regionAtX],
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      const idx = regionAtX(e.clientX);
      if (idx === null) return;
      if (idx !== drag.anchor) drag.moved = true;
      setSel({ from: Math.min(drag.anchor, idx), to: Math.max(drag.anchor, idx) });
    },
    [regionAtX],
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
      // Клик по ОДНОМУ блоку (без драга) → выбрать этот шот: верхние Frame-контролы + драг-пан
      // превью привязываются к нему. Range-драг (moved) — мультивыбор для кнопки Apply → снимаем
      // одиночный выбор, чтобы верхняя панель не думала, что редактирует один шот.
      if (!drag.moved) {
        const blk = blocks[drag.anchor];
        if (blk) {
          onSelectShot?.({
            source_start: clipTimeToSource(blk.t0, intervals),
            source_end: clipTimeToSource(blk.t1, intervals),
            index: drag.anchor,
            t0: blk.t0,
            t1: blk.t1,
          });
        }
      } else {
        onSelectShot?.(null);
      }
    },
    [blocks, intervals, onSelectShot],
  );

  const apply = useCallback(() => {
    if (!sel || blocks.length === 0 || busy) return;
    const lo = blocks[sel.from];
    const hi = blocks[sel.to];
    if (!lo || !hi) return;
    // Берём source-границы ВЫДЕЛЕННОГО подблока (его t0/t1), а не целого региона —
    // так apply на пол-региона шлёт диапазон именно этой половины.
    const sourceStart = clipTimeToSource(lo.t0, intervals);
    const sourceEnd = clipTimeToSource(hi.t1, intervals);
    if (sourceEnd <= sourceStart) return;
    onApplyRange(sourceStart, sourceEnd, mode);
    setSel(null);
  }, [sel, blocks, intervals, mode, busy, onApplyRange]);

  // «Split here»: вставить клиентский рез на текущем плейхеде. Рез валиден, только если
  // плейхед СТРОГО внутри какого-то блока (не на его границе) — иначе резать нечего.
  const splitHere = useCallback(() => {
    if (busy || blocks.length === 0) return;
    const phClipT =
      nowSec !== undefined && intervals.length > 0
        ? clipTimeFromSource(nowSec, intervals)
        : null;
    if (phClipT === null) return;
    const rounded = Math.round(phClipT * 1000) / 1000; // мс-точность отображаемого реза
    const inside = blocks.some((b) => rounded > b.t0 + 1e-3 && rounded < b.t1 - 1e-3);
    if (!inside) return;
    // не дублируем почти совпадающий рез
    if (userSplits.some((s) => Math.abs(s - rounded) < 1e-3)) return;
    // Оптимистичный клиентский маркер для МГНОВЕННОГО фидбека (истина — персистнутые manual_cuts:
    // когда родитель перечитает /reframe, рез станет реальной границей региона, а userSplits сбросится).
    setUserSplits((prev) => [...prev, rounded].sort((a, b) => a - b));
    setSel(null); // границы блоков сдвинулись → старое выделение неактуально
    // P3: персист реза на его SOURCE-времени (round-trip clip-время маркера → source через тот же
    // clipTimeToSource, что и apply) — чтобы рез пережил перезагрузку и дошёл до рендера.
    onCut?.(clipTimeToSource(rounded, intervals), "split");
  }, [busy, blocks, nowSec, intervals, userSplits, onCut]);

  // можно ли резать на текущем плейхеде (для disabled-состояния кнопки)
  const canSplit = useMemo(() => {
    if (blocks.length === 0) return false;
    const phClipT =
      nowSec !== undefined && intervals.length > 0
        ? clipTimeFromSource(nowSec, intervals)
        : null;
    if (phClipT === null) return false;
    const rounded = Math.round(phClipT * 1000) / 1000;
    if (userSplits.some((s) => Math.abs(s - rounded) < 1e-3)) return false;
    return blocks.some((b) => rounded > b.t0 + 1e-3 && rounded < b.t1 - 1e-3);
  }, [blocks, nowSec, intervals, userSplits]);

  const unit = "shot";

  // ── loading: план шотов считается (в т.ч. сервер отдал 202 и мы поллим) → скелетон ──
  if (loading) {
    return (
      <div className="mt-2 space-y-1.5">
        <div className="flex h-14 w-full gap-px overflow-hidden rounded-lg border border-line bg-surface-2">
          {Array.from({ length: 8 }).map((_, i) => (
            <div key={i} className="h-full flex-1 animate-pulse bg-surface-3/60" />
          ))}
        </div>
        <p className="flex items-center justify-center gap-1.5 text-[11px] text-muted">
          <Loader2 className="size-3 animate-spin" />
          {t("detecting")}
        </p>
      </div>
    );
  }

  // ── расчёт упал → ЧЕСТНАЯ ошибка + «Повторить» ──
  // Раньше здесь молча рисовались равные временны́е куски под видом шотов ИИ. Не показываем
  // выдуманные границы: говорим правду и даём перезапустить расчёт.
  if (failed) {
    return (
      <div className="mt-2 flex flex-col items-center gap-2 rounded-lg border border-dashed border-line bg-surface-2 px-3 py-3 text-center">
        <p className="text-[11px] text-muted">{t("shotPlanFailed")}</p>
        {onRetry && (
          <Button type="button" variant="secondary" size="sm" onClick={onRetry} className="text-xs">
            {t("retryShotPlan")}
          </Button>
        )}
      </div>
    );
  }

  // ── пусто/нет плана → честная подсказка (не ломаемся) ──
  if (!regions || regions.length === 0) {
    return (
      <div className="mt-2 rounded-lg border border-dashed border-line bg-surface-2 px-3 py-2 text-center text-[11px] text-muted">
        {t("shotPlanUnavailable")}
      </div>
    );
  }

  const playheadClipT =
    nowSec !== undefined && intervals.length > 0
      ? clipTimeFromSource(nowSec, intervals)
      : null;
  const playheadFrac =
    playheadClipT !== null ? clamp((playheadClipT - t0) / clipDur, 0, 1) : null;
  const activeIdx =
    playheadClipT !== null
      ? blocks.findIndex((b) => playheadClipT >= b.t0 && playheadClipT < b.t1)
      : -1;

  // Блок, привязанный к верхним Frame-контролам (по source-диапазону; переживает пересчёт плана).
  const selShotIdx =
    selectedShot != null
      ? blocks.findIndex(
          (b) =>
            Math.abs(clipTimeToSource(b.t0, intervals) - selectedShot.source_start) < 0.05 &&
            Math.abs(clipTimeToSource(b.t1, intervals) - selectedShot.source_end) < 0.05,
        )
      : -1;

  // подпись выделения. Без резов блок = шот/часть → старый текст. С резами в наборе есть
  // подблоки → нейтральное «part(s)», чтобы номера-блоков не путались с номерами шотов.
  const hasSplits = userSplits.length > 0;
  const selUnit = hasSplits ? "part" : unit;
  const isShotUnit = selUnit === "shot";
  const selLabel = sel
    ? sel.from === sel.to
      ? t(isShotUnit ? "shotSelected" : "partSelected", { n: sel.from + 1 })
      : t(isShotUnit ? "shotsRangeSelected" : "partsRangeSelected", {
          from: sel.from + 1,
          to: sel.to + 1,
        })
    : t(isShotUnit ? "dragToPickShots" : "dragToPickParts");

  return (
    <div className="mt-2 select-none">
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <span className="text-[11px] font-semibold text-ink">
          {t(unit === "shot" ? "shotsCount" : "partsCount", { count: regions.length })}
          {hasSplits && (
            <span className="ml-1.5 font-normal text-muted">
              · {t("partsCount", { count: blocks.length })}
            </span>
          )}
        </span>
        <span className="text-[10px] text-muted">{selLabel}</span>
      </div>

      <div
        ref={trackRef}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        className={`group relative flex h-14 w-full gap-px overflow-hidden rounded-lg border border-line bg-surface-2 touch-none ${
          busy ? "cursor-wait opacity-60" : "cursor-pointer"
        }`}
      >
        {blocks.map((blk, i) => {
          const widthFrac = (blk.t1 - blk.t0) / clipDur;
          // regionOverride принимает {t0,t1,mode} — блок этой формы, подсветка override по
          // пересечению source-окна работает и для подблока.
          const ov = regionOverride(blk, intervals, overrides);
          const selected = sel !== null && i >= sel.from && i <= sel.to;
          const isActive = i === activeIdx;
          const isEditing = i === selShotIdx; // привязан к верхним Frame-контролам
          // provenance: шот "manual", если override покрывает его середину (совпадает с рендером).
          const manual = shotIsManual(overrides, {
            source_start: clipTimeToSource(blk.t0, intervals),
            source_end: clipTimeToSource(blk.t1, intervals),
          });
          // эффективный режим: override приоритетнее AI-плана
          const effMode = ov ? ov.mode : blk.mode;
          const tint = MODE_TINT[effMode] ?? "bg-surface-3";
          const widthPct = Math.max(widthFrac * 100, 1.5);
          const wide = widthPct > 7; // влезает ли подпись внутрь блока
          const blockNoun = !hasSplits && unit === "shot" ? t("shotNoun") : t("partNoun");
          return (
            <div
              key={i}
              className={`relative flex h-full flex-col items-center justify-center overflow-hidden ${tint} transition`}
              style={{ width: `${widthPct}%` }}
              title={t("blockTitle", {
                noun: blockNoun,
                n: i + 1,
                split: blk.split ? t("splitSuffix") : "",
                mode: ov ? t("forcedMode", { mode: mlabel(ov.mode) }) : mlabel(blk.mode),
                range: `${mmss(blk.t0)}–${mmss(blk.t1)}`,
              })}
            >
              {wide && (
                <>
                  <span className="pointer-events-none text-[10px] font-bold leading-none text-ink/80">
                    {i + 1}
                  </span>
                  <span className="pointer-events-none mt-0.5 text-[8px] font-semibold uppercase leading-none tracking-wide text-ink/55">
                    {mlabel(effMode)}
                  </span>
                </>
              )}
              {ov && <span className="absolute inset-x-0 top-0 h-1 bg-accent" aria-hidden />}
              {/* provenance: "manual" — маленькая точка в углу (шот с ручным override). */}
              {manual && (
                <span
                  className="pointer-events-none absolute right-1 top-1 size-1.5 rounded-full bg-accent"
                  aria-hidden
                />
              )}
              {/* подблок (результат «Split here») — пунктирная левая кромка как маркер реза */}
              {blk.split && i > 0 && (
                <span
                  className="pointer-events-none absolute inset-y-0 left-0 w-px bg-peak/70"
                  aria-hidden
                />
              )}
              {isActive && !selected && !isEditing && (
                <span
                  className="pointer-events-none absolute inset-0 ring-2 ring-inset ring-ink/70"
                  aria-hidden
                />
              )}
              {selected && (
                <span
                  className="pointer-events-none absolute inset-0 bg-accent/25 ring-2 ring-inset ring-accent"
                  aria-hidden
                />
              )}
              {/* редактируемый шот (привязан к верхним Frame-контролам) — стойкое кольцо-акцент */}
              {isEditing && !selected && (
                <span
                  className="pointer-events-none absolute inset-0 ring-2 ring-inset ring-accent"
                  aria-hidden
                />
              )}
            </div>
          );
        })}

        {playheadFrac !== null && (
          <div
            className="pointer-events-none absolute top-0 bottom-0 z-10 w-0.5 bg-accent shadow-[0_0_4px_var(--t-accent-line)]"
            style={{ left: `${playheadFrac * 100}%` }}
            aria-hidden
          />
        )}

        {/* P3: merge (✕) на КАЖДОЙ внутренней границе шотов → убрать AI-рез, склеив два соседних
            шота. Появляется на hover полосы (чистый NLE-паттерн). stopPropagation на pointerdown,
            чтобы клик по ✕ не стартовал драг-выделение. Рисуется, когда родитель дал onCut
            (границы всегда реальные — фейкового плана больше нет). Граница на blocks[i+1].t0 =
            стык blocks[i]/blocks[i+1]. */}
        {onCut &&
          blocks.length > 1 &&
          blocks.slice(1).map((blk, i) => {
            const frac = clamp((blk.t0 - t0) / clipDur, 0, 1);
            const srcTime = clipTimeToSource(blk.t0, intervals);
            return (
              <button
                key={`merge-${i}`}
                type="button"
                disabled={busy}
                onPointerDown={(e) => e.stopPropagation()}
                onClick={(e) => {
                  e.stopPropagation();
                  setSel(null);
                  onCut(srcTime, "merge");
                }}
                title={t("mergeShotsTitle")}
                aria-label={t("mergeShotsAria")}
                style={{ left: `${frac * 100}%` }}
                className="absolute top-1/2 z-20 flex size-4 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border border-line bg-surface text-muted opacity-0 transition enabled:hover:border-accent enabled:hover:text-accent group-hover:opacity-100 focus-visible:opacity-100 disabled:cursor-wait"
              >
                <X className="size-2.5" aria-hidden />
              </button>
            );
          })}
      </div>

      {/* Split here: вставить клиентскую границу на плейхеде, чтобы форснуть половину
          склеенного шота. Сброс резов рядом, появляется только когда они есть. */}
      <div className="mt-2 flex items-center gap-2">
        <button
          type="button"
          disabled={busy || !canSplit}
          onClick={splitHere}
          title={canSplit ? t("splitHereTitleCan") : t("splitHereTitleCant")}
          aria-label={t("splitAtPlayheadAria")}
          className="inline-flex items-center gap-1.5 rounded-md border border-line bg-surface px-2.5 py-1 text-[11px] font-semibold text-muted transition enabled:hover:border-line-strong enabled:hover:text-ink disabled:opacity-40"
        >
          <Scissors className="size-3" aria-hidden />
          {t("splitHere")}
        </button>
        {hasSplits && (
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              setUserSplits([]);
              setSel(null);
            }}
            title={t("clearSplitsTitle")}
            className="rounded-md px-2 py-1 text-[11px] font-semibold text-muted transition enabled:hover:text-ink disabled:opacity-40"
          >
            {t("clearSplits")}
          </button>
        )}
      </div>

      {/* действие: выбрать режим → применить к выделенным шотам */}
      <div className="mt-2 flex items-center gap-2">
        <div
          className="flex gap-0.5 rounded-md border border-line bg-surface p-0.5"
          role="radiogroup"
          aria-label={t("forcedFramingMode")}
        >
          {FORCE_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              role="radio"
              aria-checked={mode === opt.value}
              disabled={busy}
              title={t(opt.hintKey)}
              onClick={() => setMode(opt.value)}
              className={`rounded px-2.5 py-1 text-[11px] font-semibold transition disabled:opacity-40 ${
                mode === opt.value ? "bg-surface-3 text-accent" : "text-muted hover:text-ink"
              }`}
            >
              {t(opt.labelKey)}
            </button>
          ))}
        </div>
        <button
          type="button"
          disabled={busy || !sel}
          onClick={apply}
          aria-label={t("applyToSelectedAria")}
          className="ml-auto rounded-md border border-accent/60 bg-accent/10 px-3 py-1 text-[11px] font-semibold text-accent transition enabled:hover:bg-accent/20 disabled:border-line disabled:bg-transparent disabled:text-muted disabled:opacity-50"
        >
          {sel
            ? t(isShotUnit ? "applyToShots" : "applyToParts", {
                mode: t(FORCE_OPTIONS.find((o) => o.value === mode)?.labelKey ?? "wide"),
                count: sel.from === sel.to ? 1 : sel.to - sel.from + 1,
              })
            : t(isShotUnit ? "selectShotsFirst" : "selectPartsFirst")}
        </button>
      </div>

      {/* легенда */}
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[10px] text-muted">
        <span className="inline-flex items-center gap-1">
          <span className="size-2 rounded-sm bg-thought/40" /> {t("wideFull")}
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="size-2 rounded-sm bg-quote/40" /> {t("tightCrop")}
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="size-2 rounded-sm bg-peak/40" /> {t("legendSplit")}
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-0.5 w-3 bg-accent" /> {t("forced")}
        </span>
      </div>
    </div>
  );
}

/**
 * Маппинг source-время → КЛИП-время (для плейхеда). Обратный к clipTimeToSource:
 * source попадает в интервал [source_start, source_end) → clipT = offset + (source - source_start).
 * За пределами клипа клампится. PURE.
 */
function clipTimeFromSource(sourceT: number, intervals: SourceInterval[]): number {
  let offset = 0;
  for (const iv of intervals) {
    const dur = iv.source_end - iv.source_start;
    if (sourceT < iv.source_end) {
      return offset + Math.max(0, sourceT - iv.source_start);
    }
    offset += dur;
  }
  return offset; // после конца → суммарная длина
}

export default FitTimeline;
