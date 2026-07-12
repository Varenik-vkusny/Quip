"""Удаление пауз — ЧИСТЫЕ функции (спека 2026-07-12 §5, алгоритм v3). No I/O.

Тишина = гэпы между словами ∈ [min_pause, max_pause] → вырезаем как доп. дырки в source_intervals.
v3 (ресёрч 2026-07-12 — Descript/TimeBolt/CrisperWhisper):
- АСИММЕТРИЧНЫЙ отступ: lead_out (после слова — хвост звенит) > lead_in (перед след. словом — онсет
  резкий); на границе мысли (конец предложения / длинный гэп) lead_out больше (вдох/ритм).
- СМЕНА ГОВОРЯЩЕГО → НЕ режем (турн-тейкинг осмыслен + тайминг ASR на стыке спикеров плывёт).
- Защита краёв клипа (не режем впритык к старту/концу) + abort при коллизии отступов.
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
_MIN_CUT_FRAMES = 2  # рез короче 2 кадров — отступы почти съели гэп, не стоит (avoid harsh cuts)
_CLIP_EDGE_SEC = 0.4  # не режем впритык к старту/концу клипа (резкий старт / обрыв слова)


def _is_speaker_change(words: list[Word], i: int) -> bool:
    """Смена говорящего между words[i] и words[i+1] — турн-тейкинг (не пауза-филлер). PURE."""
    a, b = words[i].speaker, words[i + 1].speaker
    return a is not None and b is not None and a != b


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
    """Source-интервалы (сек) ПАУЗ к вырезанию, границы frame-snapped. PURE (алгоритм v3).

    Гэп между words[i].end и words[i+1].start режется если:
    - min_pause <= gap <= max_pause, И гэп целиком внутри kept-интервала,
    - НЕ смена говорящего (турн-тейкинг оставляем),
    - после асимметр. отступа остаётся >= _MIN_CUT_FRAMES (иначе отступы съели гэп → не режем),
    - рез не впритык к краю клипа (_CLIP_EDGE_SEC).
    Отступ: lead_out после слова (хвост звенит), lead_in перед след. словом (онсет резкий); на
    границе мысли lead_out = lead_out_boundary (вдох). Весь гэп между отступами вырезается целиком
    (в т.ч. голосовые эээ/ммм — их word-gap НЕ распознал как слово → это не-слово → под нож).
    """
    if not intervals:
        return []
    min_p = cfg.min_pause_ms / 1000.0
    max_p = cfg.max_pause_ms / 1000.0
    lead_in = cfg.lead_in_ms / 1000.0
    lead_out = cfg.lead_out_ms / 1000.0
    lead_out_bnd = cfg.lead_out_boundary_ms / 1000.0
    clip_start = intervals[0].source_start
    clip_end = intervals[-1].source_end
    min_cut = _MIN_CUT_FRAMES / fps
    cuts: list[tuple[float, float]] = []
    for i in range(len(words) - 1):
        gs = words[i].end
        ge = words[i + 1].start
        gap = ge - gs
        if not (min_p <= gap <= max_p):
            continue
        if not _gap_within_interval(gs, ge, intervals):
            continue
        if _is_speaker_change(words, i):
            continue  # смена говорящего → турн-тейкинг, НЕ режем
        # конец предложения → больший вдох (сигнал парсинга); длинный гэп ВНУТРИ мысли = dead-air/
        # эээ → режем плотно (lead_out), не бережём.
        lo = lead_out_bnd if _is_sentence_end(words[i].text) else lead_out
        cs = _snap(gs + lo, fps)
        ce = _snap(ge - lead_in, fps)
        if ce - cs < min_cut:
            continue  # отступы съели гэп → не режем (Descript "avoid harsh cuts")
        if cs - clip_start < _CLIP_EDGE_SEC or clip_end - ce < _CLIP_EDGE_SEC:
            continue  # впритык к краю клипа → резкий старт / обрыв
        cuts.append((cs, ce))
    return cuts


def apply_silence(
    intervals: list[SourceInterval],
    words: list[Word],
    cfg: SilenceConfig,
    *,
    fps: float,
) -> list[SourceInterval]:
    """intervals − паузы-дырки. cfg.enabled=False → строгий no-op (вход без изменений). PURE.

    Переиспользует ops._subtract_range на каждую дырку; вырожденные (< 1 кадра) интервалы дропаются.
    """
    if not cfg.enabled:
        return intervals
    result = list(intervals)
    cuts = compute_silence_cuts(words, intervals, cfg, fps=fps)
    for cs, ce in cuts:
        result = _subtract_range(result, cs, ce)
    min_seg = _MIN_SEG_FRAMES / fps
    return [iv for iv in result if iv.source_end - iv.source_start >= min_seg - 1e-9]
