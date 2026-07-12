"""Удаление пауз — ЧИСТЫЕ функции (спека 2026-07-12 §5). No I/O.

Тишина = гэпы между словами ∈ [min_pause, max_pause] → вырезаем как доп. дырки в source_intervals.
Границы FRAME-SNAPPED на native-fps грид (round(t*fps)/fps — инвариант REFRAME_FPS, Δ=0).
apply_silence вычитает дырки (reuse ops._subtract_range); disabled → no-op. Композится с
мульти-интервальным входом (после ручных правок).
"""

from __future__ import annotations

from app.editor.ops import _subtract_range
from app.editor.reframe_cache import _snap
from app.models import SilenceConfig, SourceInterval, Word
from app.pipeline.cut_boundary import _is_sentence_end

# после реза интервалы короче 1 кадра выкидываем (артефакт padding-нахлёста на очень коротком слове)
_MIN_SEG_FRAMES = 1
_MIN_SIDE = 0.035  # абсолютный минимум оставляемой тишины по краю (не режем впритык к согласной)
_BOUNDARY_GAP_SEC = 0.7  # гэп длиннее — сам по себе граница мысли (пауза «на подумать»/смена темы)


def _is_boundary(words: list[Word], i: int, gap: float) -> bool:
    """Гэп на ГРАНИЦЕ мысли: конец предложения / длинный гэп / смена спикера. PURE."""
    if _is_sentence_end(words[i].text):
        return True
    if gap > _BOUNDARY_GAP_SEC:
        return True
    a, b = words[i].speaker, words[i + 1].speaker
    return a is not None and b is not None and a != b


def _gap_within_interval(gs: float, ge: float, intervals: list[SourceInterval]) -> bool:
    """Гэп [gs, ge] целиком внутри одного kept-интервала (иначе уже вырезан / вне клипа). PURE."""
    return any(iv.source_start <= gs and ge <= iv.source_end for iv in intervals)


def _confirmed_silence(
    gs: float, ge: float, silence_regions: list[tuple[float, float]]
) -> tuple[float, float] | None:
    """Пересечение гэпа [gs, ge] с аудио-тишиной (ffmpeg silencedetect) → крупнейший подтверждённо
    тихий суб-span, или None если аудио НЕ тихое (ложное срабатывание word-gap). PURE."""
    best: tuple[float, float] | None = None
    for ss, se in silence_regions:
        s, e = max(gs, ss), min(ge, se)
        if e > s and (best is None or (e - s) > (best[1] - best[0])):
            best = (s, e)
    return best


def compute_silence_cuts(
    words: list[Word],
    intervals: list[SourceInterval],
    cfg: SilenceConfig,
    *,
    fps: float,
    silence_regions: list[tuple[float, float]] | None = None,
) -> list[tuple[float, float]]:
    """Source-интервалы (сек) ТИШИНЫ к вырезанию, границы frame-snapped. PURE.

    Гэп между words[i].end и words[i+1].start режется если min_pause <= gap <= max_pause И гэп
    целиком внутри kept-интервала. padding сохраняется по краям.

    ГИБРИД (ресёрч 2026-07-12): word-gap решает ЧТО резать; `silence_regions` (ffmpeg silencedetect)
    уточняют ГДЕ — режем только аудио-подтверждённую тишину внутри гэпа (точный стык, не округлённый
    ASR-тайминг) И отсекаем ложные срабатывания (гэп есть, но аудио НЕ тихое → не режем).
    silence_regions=None → чистый word-gap (обратная совместимость). Если padding съедает весь
    (под)интервал — дырку не добавляем (нет инвертированного интервала).
    """
    min_p = cfg.min_pause_ms / 1000.0
    max_p = cfg.max_pause_ms / 1000.0
    within_side = max(_MIN_SIDE, cfg.within_keep_ms / 2000.0)
    bound_side = max(_MIN_SIDE, cfg.boundary_keep_ms / 2000.0)
    cuts: list[tuple[float, float]] = []
    for i in range(len(words) - 1):
        gs = words[i].end
        ge = words[i + 1].start
        gap = ge - gs
        if not (min_p <= gap <= max_p):
            continue
        if not _gap_within_interval(gs, ge, intervals):
            continue
        # Слой A: на ГРАНИЦЕ мысли оставляем больший вдох (естественно), ВНУТРИ — минимум (плотно).
        side = bound_side if _is_boundary(words, i, gap) else within_side
        if silence_regions is not None:
            conf = _confirmed_silence(gs, ge, silence_regions)
            if conf is None:
                continue  # аудио НЕ подтверждает тишину → ложный word-gap, пропускаем
            gs, ge = conf  # режем ровно подтверждённо-тихий суб-span
        cs = _snap(gs + side, fps)
        ce = _snap(ge - side, fps)
        if ce > cs:
            cuts.append((cs, ce))
    return cuts


def apply_silence(
    intervals: list[SourceInterval],
    words: list[Word],
    cfg: SilenceConfig,
    *,
    fps: float,
    silence_regions: list[tuple[float, float]] | None = None,
) -> list[SourceInterval]:
    """intervals − тишина-дырки. cfg.enabled=False → строгий no-op (вход без изменений). PURE.

    `silence_regions` (опц.) — аудио-подтверждённая тишина (ffmpeg silencedetect) для уточнения
    границ реза (гибрид, см. compute_silence_cuts). None → чистый word-gap.
    Переиспользует ops._subtract_range на каждую дырку; вырожденные (< 1 кадра) интервалы дропаются.
    """
    if not cfg.enabled:
        return intervals
    result = list(intervals)
    cuts = compute_silence_cuts(words, intervals, cfg, fps=fps, silence_regions=silence_regions)
    for cs, ce in cuts:
        result = _subtract_range(result, cs, ce)
    min_seg = _MIN_SEG_FRAMES / fps
    return [iv for iv in result if iv.source_end - iv.source_start >= min_seg - 1e-9]
