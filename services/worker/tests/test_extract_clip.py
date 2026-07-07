"""Тесты pure-хелпера on-demand экстракции клипа (app/editor/extract_clip.py), тест-первым.

Ключевое требование фаундера: вручную вырезанный клип (un-clipped момент карты / drag-выделение
на таймлайне) должен кроиться С ТЕМ ЖЕ качеством границ, что и авто-клип. Поэтому extract_clip
ПЕРЕИСПОЛЬЗУЕТ ровно selection-path логику снапа (snap_start_index / snap_end_index /
refine_boundaries_audio / indices_to_times), а не изобретает свою. Здесь мы это и проверяем:
чистый старт предложения, снап конца к тихому вдоху по loudness, обрезка филлеров у краёв,
инвариант «>= min_sec и внутри [0, source_dur]», и стабильность ключа идемпотентности.
"""

import pytest

from app.editor.extract_clip import range_to_segment, segment_key
from app.models import ClipType, Segment, Word


def mkwords(specs: list[tuple[str, float, float]]) -> list[Word]:
    return [Word(text=t, start=s, end=e) for (t, s, e) in specs]


# равномерные слова: индекс k → [k, k+0.8]; "wN." (k in ends) завершает предложение.
def uniform(n: int, sentence_ends: set[int] | None = None) -> list[Word]:
    ends = sentence_ends or set()
    return [
        Word(text=(f"w{k}." if k in ends else f"w{k}"), start=float(k), end=float(k) + 0.8)
        for k in range(n)
    ]


# непрерывная речь (шаг 0.5с, слово 0.4с, зазор 0.1с < min_pause 0.35 → пауза-снап НЕ триггерится).
# word[k].end = 0.5k+0.4 → loudness idx = int((0.5k+0.4)/0.5) = k (hop_s=0.5), т.е. word[k]→loud[k].
def speech(n: int) -> list[Word]:
    return [Word(text=f"w{k}", start=k * 0.5, end=k * 0.5 + 0.4) for k in range(n)]


class TestRangeToSegmentReturnsFullSegment:
    """range_to_segment всегда возвращает валидный, полностью заполненный Segment."""

    def test_returns_segment_with_required_fields(self) -> None:
        words = uniform(50)
        seg = range_to_segment(10.0, 40.0, words, loudness=None, source_dur=60.0)
        assert isinstance(seg, Segment)
        assert seg.start < seg.end
        assert seg.type is ClipType.complete_thought  # sane default type
        assert 0.0 <= seg.score <= 1.0

    def test_threads_explainability_params(self) -> None:
        # endpoint посева хук/причину из момента → опциональные параметры пробрасываются в Segment.
        words = uniform(50)
        seg = range_to_segment(
            10.0,
            40.0,
            words,
            loudness=None,
            source_dur=60.0,
            reason="manual extract",
            score=0.7,
            clip_type=ClipType.strong_quote,
            hook="Он потерял всё",
            why_works="Ставка в первой фразе",
            tone="shocking",
            key_quote="This changed everything.",
        )
        assert seg.reason == "manual extract"
        assert seg.score == pytest.approx(0.7)
        assert seg.type is ClipType.strong_quote
        assert seg.hook == "Он потерял всё"
        assert seg.why_works == "Ставка в первой фразе"
        assert seg.tone == "shocking"
        assert seg.key_quote == "This changed everything."


class TestStartSnapsToSentenceStart:
    """Старт drag-выделения попал в СЕРЕДИНУ предложения → снап назад к чистому началу
    предложения (ровно как snap_start_index в selection-path)."""

    def test_midsentence_start_snaps_back_to_sentence_boundary(self) -> None:
        # Предложение начинается на word[10] (word[9]="w9." его закрыл). Юзер тянет старт в
        # середину (word[13]) → снап возвращает начало предложения = word[10].
        words = uniform(50, sentence_ends={9, 40})
        seg = range_to_segment(13.4, 42.0, words, loudness=None, source_dur=60.0, min_sec=20.0)
        # indices_to_times: word[10].start(10.0) - 0.15, но не раньше word[9].end(9.8) → 9.85.
        assert seg.start == pytest.approx(9.85)
        # конец тоже чист: word[42] (не .?!) снапнулся назад к концу предложения word[40].
        assert seg.end == pytest.approx(40.8)


class TestEndSnapsToLowEnergyPause:
    """При наличии loudness конец снапается к БЛИЖАЙШЕМУ тихому вдоху (низко-энергетич. впадине),
    как refine_boundaries_audio в selection-path."""

    def test_end_snaps_into_breath_trough(self) -> None:
        words = speech(60)  # word 59.end = 29.9
        loud = [-10.0] * 60
        loud[47] = -42.0  # глубокий вдох (тихо) на word[47].end
        seg = range_to_segment(1.2, 25.2, words, loudness=loud, source_dur=30.0, min_sec=20.0)
        # end_sec=25.2 → word[50]; снап конца по вдоху тянет к word[47].end = 23.9.
        assert seg.end == pytest.approx(23.9)
        # без кривой громкости конец остаётся на word[50].end = 25.4 (снап-впадины нет).
        seg_no_audio = range_to_segment(
            1.2, 25.2, words, loudness=None, source_dur=30.0, min_sec=20.0
        )
        assert seg_no_audio.end == pytest.approx(25.4)
        assert seg.end < seg_no_audio.end


class TestEdgeFillersTrimmed:
    """Слова-паразиты ("um"/"uh") у САМЫХ краёв выделения обрезаются (только при наличии loudness —
    так же, как selection-path: refine_boundaries_audio трогает филлеры лишь с аудио-слоем)."""

    def test_leading_and_trailing_fillers_trimmed_with_loudness(self) -> None:
        words = speech(60)
        words[0] = Word(text="um", start=0.0, end=0.4)  # ведущий филлер (начало клипа)
        words[44] = Word(text="real", start=22.0, end=22.4)
        words[45] = Word(text="uh", start=22.5, end=22.9)  # хвостовой филлер (конец клипа)
        loud = [-10.0] * 60  # ровная кривая → без ложного вдох-снапа, чистая проверка филлеров
        seg = range_to_segment(0.2, 22.7, words, loudness=loud, source_dur=30.0, min_sec=20.0)
        # "um" срезан → старт на word[1] (0.5 - 0.15, но не раньше word[0].end 0.4) = 0.4.
        assert seg.start == pytest.approx(0.4)
        # "uh" срезан → конец на word[44].end = 22.4.
        assert seg.end == pytest.approx(22.4)

    def test_fillers_kept_without_loudness(self) -> None:
        # Без кривой громкости филлер-обрезка НЕ работает (провайдер-агностик no-op) — края целы.
        words = speech(60)
        words[0] = Word(text="um", start=0.0, end=0.4)
        words[44] = Word(text="real", start=22.0, end=22.4)
        words[45] = Word(text="uh", start=22.5, end=22.9)
        seg = range_to_segment(0.2, 22.7, words, loudness=None, source_dur=30.0, min_sec=20.0)
        assert seg.start == pytest.approx(0.0)  # "um" сохранён (word[0])
        assert seg.end == pytest.approx(22.9)  # "uh" сохранён (word[45])


class TestMinSecAndBoundsInvariant:
    """Результат ВСЕГДА >= min_sec секунд и внутри [0, source_dur]."""

    def test_tiny_selection_is_expanded_to_min_sec(self) -> None:
        words = uniform(50)
        seg = range_to_segment(10.0, 12.0, words, loudness=None, source_dur=60.0, min_sec=20.0)
        assert seg.end - seg.start == pytest.approx(20.0)
        assert seg.start >= 0.0
        assert seg.end <= 60.0

    def test_selection_at_source_end_never_exceeds_duration(self) -> None:
        # Крошечное выделение у самого конца → окно раздвигается НАЗАД (start), а не за source_dur.
        words = uniform(50)  # word[49].end = 49.8
        seg = range_to_segment(47.0, 49.0, words, loudness=None, source_dur=49.8, min_sec=20.0)
        assert seg.end <= 49.8 + 1e-9
        assert seg.start >= 0.0
        assert seg.end - seg.start >= 20.0 - 1e-9

    def test_custom_min_sec_respected(self) -> None:
        words = uniform(60)
        seg = range_to_segment(5.0, 8.0, words, loudness=None, source_dur=80.0, min_sec=30.0)
        assert seg.end - seg.start >= 30.0 - 1e-9

    def test_empty_words_degrades_to_clamp(self) -> None:
        # Нет транскрипта → сырой кламп (>= min_sec, внутри [0, source_dur]); не падаем.
        seg = range_to_segment(5.0, 8.0, [], loudness=None, source_dur=40.0, min_sec=20.0)
        assert seg.end - seg.start >= 20.0 - 1e-9
        assert 0.0 <= seg.start
        assert seg.end <= 40.0


class TestSegmentKey:
    """Ключ идемпотентности: стабилен для одинаковых (округлённых до 0.1с) границ, различается
    для разных диапазонов → endpoint может дедупить повторную экстракцию того же участка."""

    def test_deterministic_for_same_bounds(self) -> None:
        assert segment_key(12.34, 40.86) == segment_key(12.34, 40.86)

    def test_same_within_rounding_tolerance(self) -> None:
        # 12.30 и 12.34 → 12.3; 40.86 и 40.90 → 40.9 → один и тот же ключ (jitter схлопывается).
        assert segment_key(12.30, 40.90) == segment_key(12.34, 40.86)

    def test_differs_for_different_start(self) -> None:
        assert segment_key(12.3, 40.9) != segment_key(15.0, 40.9)

    def test_differs_for_different_end(self) -> None:
        assert segment_key(12.3, 40.9) != segment_key(12.3, 45.0)

    def test_crossing_rounding_boundary_differs(self) -> None:
        # 12.34 → 12.3, 12.36 → 12.4: пересекли шаг 0.1с → ключи РАЗНЫЕ.
        assert segment_key(12.34, 40.9) != segment_key(12.36, 40.9)

    def test_negative_zero_normalized(self) -> None:
        # -0.0 и 0.0 равны как числа → ключ обязан совпасть (никаких "-0.0" vs "0.0").
        assert segment_key(-0.0, 20.0) == segment_key(0.0, 20.0)
