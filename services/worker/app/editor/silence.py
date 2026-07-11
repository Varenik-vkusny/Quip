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

# после реза интервалы короче 1 кадра выкидываем (артефакт padding-нахлёста на очень коротком слове)
_MIN_SEG_FRAMES = 1


def _gap_within_interval(gs: float, ge: float, intervals: list[SourceInterval]) -> bool:
    """Гэп [gs, ge] целиком внутри одного kept-интервала (иначе уже вырезан / вне клипа). PURE."""
    return any(iv.source_start <= gs and ge <= iv.source_end for iv in intervals)


def compute_silence_cuts(
    words: list[Word],
    intervals: list[SourceInterval],
    cfg: SilenceConfig,
    *,
    fps: float,
) -> list[tuple[float, float]]:
    """Source-интервалы (сек) ТИШИНЫ к вырезанию, границы frame-snapped. PURE.

    Гэп между words[i].end и words[i+1].start режется если min_pause <= gap <= max_pause И гэп
    целиком внутри kept-интервала. padding сохраняется по краям; если padding съедает весь гэп —
    дырку не добавляем (нет инвертированного интервала).
    """
    min_p = cfg.min_pause_ms / 1000.0
    max_p = cfg.max_pause_ms / 1000.0
    pad = cfg.padding_ms / 1000.0
    cuts: list[tuple[float, float]] = []
    for i in range(len(words) - 1):
        gs = words[i].end
        ge = words[i + 1].start
        gap = ge - gs
        if not (min_p <= gap <= max_p):
            continue
        if not _gap_within_interval(gs, ge, intervals):
            continue
        cs = _snap(gs + pad, fps)
        ce = _snap(ge - pad, fps)
        if ce > cs:
            cuts.append((cs, ce))
    return cuts


def apply_silence(
    intervals: list[SourceInterval],
    words: list[Word],
    cfg: SilenceConfig,
    *,
    fps: float,
) -> list[SourceInterval]:
    """intervals − тишина-дырки. cfg.enabled=False → строгий no-op (вход без изменений). PURE.

    Переиспользует ops._subtract_range на каждую дырку; вырожденные (< 1 кадра) интервалы дропаются.
    """
    if not cfg.enabled:
        return intervals
    result = list(intervals)
    for cs, ce in compute_silence_cuts(words, intervals, cfg, fps=fps):
        result = _subtract_range(result, cs, ce)
    min_seg = _MIN_SEG_FRAMES / fps
    return [iv for iv in result if iv.source_end - iv.source_start >= min_seg - 1e-9]
