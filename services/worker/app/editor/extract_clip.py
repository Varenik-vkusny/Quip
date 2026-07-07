"""On-demand экстракция клипа: произвольный source-диапазон [start, end] → чистый Segment.

Требование фаундера: вручную вырезанный клип (un-clipped момент AI-карты ИЛИ drag-выделение юзера
на таймлайне) должен кроиться С ТЕМ ЖЕ качеством границ, что и авто-выбранный клип. Поэтому здесь
мы НЕ изобретаем снап заново, а ПЕРЕИСПОЛЬЗУЕМ ровно ту же цепочку, что selection-path
(``stage2_select.postprocess``):

    _nearest_word_index      (video_map)      — секунды → индекс ближайшего слова
    snap_start_index         (stage2_select)  — старт → чистое начало предложения
    snap_end_index           (stage2_select)  — конец → конец предложения / вдох (speaker-aware)
    refine_boundaries_audio  (stage2_select)  — обрезка филлеров у краёв + снап конца в вдох
    indices_to_times         (stage2_select)  — индексы слов → секунды (+150мс safe-паддинг)
    clamp_interval           (editor.ops)     — гарантия >= min_sec и кламп в [0, source_dur]

Последовательность зеркалит ``postprocess`` дословно (start-снап первым — от него зависит
speaker-aware логика конца; аудио-слой поверх, no-op без loudness), поэтому граница извлечённого
клипа идентична по качеству авто-клипу.

PURE-модуль: без I/O и сети. Поля объяснимости (reason/hook/why_works/tone/…) — опциональны:
endpoint засеет их из исходного момента; тут дефолты держат фокус на математике границ.
"""

from __future__ import annotations

from app.editor.ops import clamp_interval
from app.editor.video_map import _nearest_word_index
from app.models import ClipType, Segment, Word
from app.pipeline.stage2_select import (
    indices_to_times,
    refine_boundaries_audio,
    snap_end_index,
    snap_start_index,
)


def range_to_segment(
    start_sec: float,
    end_sec: float,
    words: list[Word],
    *,
    loudness: list[float] | None,
    source_dur: float,
    min_sec: float = 20.0,
    reason: str = "",
    score: float = 0.0,
    clip_type: ClipType = ClipType.complete_thought,
    hook: str | None = None,
    why_works: str | None = None,
    hook_style: str | None = None,
    tone: str | None = None,
    key_quote: str | None = None,
) -> Segment:
    """Снапнуть произвольный source-диапазон [start_sec, end_sec] в чистый Segment.

    Зеркалит selection-path (``stage2_select.postprocess``) дословно, чтобы on-demand клип кроился
    так же чисто, как авто-клип: секунды → индексы ближайших слов; снап старта к началу предложения
    и конца к концу предложения / вдоху (speaker-aware); при наличии ``loudness`` — аудио-уточнение
    (обрезка филлеров у краёв + снап конца в тихую впадину); обратно в секунды (+150мс safe-паддинг
    старта); затем гарантия ``>= min_sec`` внутри ``[0, source_dur]`` (``clamp_interval``).

    Поля объяснимости (reason/hook/why_works/hook_style/tone/key_quote) опциональны — endpoint
    засевает их из исходного момента; дефолты оставляют фокус на границах. Возвращает полностью
    заполненный ``Segment`` (конструируется всегда: start < end гарантирован clamp_interval).
    """
    if not words:
        # Вырожденный случай: нет транскрипта → только сырой кламп (как moment_to_interval).
        s, e = clamp_interval(
            start_sec, end_sec, duration=source_dur, min_sec=min_sec, max_sec=source_dur
        )
    else:
        start_idx = _nearest_word_index(words, start_sec)
        end_idx = _nearest_word_index(words, end_sec)
        # Порядок индексов (очень короткое/инвертированное выделение мог отдать start > end).
        if start_idx > end_idx:
            start_idx, end_idx = end_idx, start_idx

        # Та же цепочка, что в postprocess(): старт первым (speaker-aware конец зависит от него),
        # затем аудио-слой поверх снапа (строгий no-op без кривой loudness).
        si = snap_start_index(words, start_idx)
        ei = snap_end_index(words, end_idx, start_idx=si, min_dur_sec=min_sec)
        if loudness:
            si, ei = refine_boundaries_audio(words, loudness, si, ei)
        # Защита: снап/refine в патологии мог инвертировать индексы — не роняем indices_to_times.
        if si > ei:
            si, ei = ei, si

        s, e = indices_to_times(words, si, ei)
        # Раздвинуть до min_sec и загнать в [0, source_dur] (max_sec=source_dur → длину не режем).
        s, e = clamp_interval(s, e, duration=source_dur, min_sec=min_sec, max_sec=source_dur)

    return Segment(
        start=s,
        end=e,
        reason=reason,
        score=score,
        type=clip_type,
        hook=hook,
        why_works=why_works,
        hook_style=hook_style,
        tone=tone,
        key_quote=key_quote,
    )


def segment_key(start: float, end: float) -> str:
    """Стабильный ключ идемпотентности для source-диапазона [start, end] (секунды).

    Округляем обе границы до 0.1с, чтобы повторная экстракция «того же» участка (float-/drag-jitter)
    схлопывалась в ОДИН ключ (endpoint дедупит). Разные диапазоны → разные ключи. ``+ 0.0``
    нормализует -0.0 → 0.0 (иначе ``f"{-0.0:.1f}"`` дал бы "-0.0" ≠ "0.0" при равных числах).
    """
    s = round(start, 1) + 0.0
    e = round(end, 1) + 0.0
    return f"{s:.1f}-{e:.1f}"
