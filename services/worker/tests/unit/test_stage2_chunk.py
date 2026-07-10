"""Тесты chunked-отбора моментов (Stage 2) — pure-логика, тест-первым.

Проблема (на реальном 86-мин видео, 15795 слов): один Gemini-вызов над гигантским транскриптом
коллапсирует по recall → вернул 2 клипа, оба в первых 25 мин. Фикс: режем на перекрывающиеся
временны́е окна, отбираем в каждом, мержим, ОДИН раз постобрабатываем (глобальный overlap + spread).

Баг-опасные места (все pure): should_chunk (гейт), make_windows (покрытие/нахлёст/края),
сохранение АБСОЛЮТНЫХ индексов через окна, dedup, spread-обрезка. Плюс интеграционный тест
select_segments с фейковым Gemini: длинный синтетический транскрипт → кандидаты по ВСЕМУ таймлайну.
"""

import json
import re
from types import SimpleNamespace

import pytest

from app.errors import JobError
from app.models import ClipType, Segment, Transcript, Word
from app.pipeline import stage2_select as s2
from app.pipeline.stage2_select import (
    build_indexed_transcript,
    dedup_raw_segments,
    make_windows,
    select_segments,
    select_spread,
    should_chunk,
)


def words_dt(n: int, *, dt: float = 1.0, dur: float = 0.9) -> list[Word]:
    """n слов, слово k → start=k*dt, end=k*dt+dur (равномерная сетка для тестов окон)."""
    return [Word(text=f"w{k}", start=k * dt, end=k * dt + dur) for k in range(n)]


# ──────────────────────────────── should_chunk ────────────────────────────────


class TestShouldChunk:
    def test_short_video_not_chunked(self) -> None:
        tr = Transcript(language="en", duration=600.0, words=words_dt(1500))
        assert should_chunk(tr) is False

    def test_long_by_duration(self) -> None:
        tr = Transcript(language="en", duration=1200.0, words=words_dt(1000))
        assert should_chunk(tr) is True  # 20 мин > 15 мин порог

    def test_long_by_word_count(self) -> None:
        tr = Transcript(language="en", duration=600.0, words=words_dt(3500))
        assert should_chunk(tr) is True  # 3500 > 3000 слов

    def test_boundary_below_thresholds_is_false(self) -> None:
        # ровно на порогах (не СТРОГО больше) → одиночный путь
        tr = Transcript(language="en", duration=900.0, words=words_dt(3000))
        assert should_chunk(tr) is False


# ──────────────────────────────── make_windows ────────────────────────────────


class TestMakeWindows:
    def test_empty_input(self) -> None:
        assert make_windows([]) == []

    def test_short_transcript_single_window(self) -> None:
        w = words_dt(10, dt=1.0)  # total ≈ 9с ≤ target
        assert make_windows(w, target_sec=720.0, overlap_sec=75.0) == [(0, 9)]

    def test_exact_boundary_total_equals_target_single_window(self) -> None:
        # total == target ровно → одно окно (граница «≤», не «<»)
        w = words_dt(101, dt=1.0, dur=0.0)  # words[100].start=100, words[0].start=0 → total=100
        assert make_windows(w, target_sec=100.0, overlap_sec=20.0) == [(0, 100)]

    def test_covers_whole_transcript_no_gaps(self) -> None:
        n = 1000
        w = words_dt(n, dt=1.0)
        wins = make_windows(w, target_sec=100.0, overlap_sec=20.0)
        assert len(wins) > 1
        assert wins[0][0] == 0  # первое окно с начала
        assert wins[-1][1] == n - 1  # последнее до конца
        # НЕТ дыр: старт след. окна ≤ конец текущего + 1
        for (s_a, s_b), (n_a, _n_b) in zip(wins, wins[1:], strict=False):
            assert n_a <= s_b + 1, f"gap between {(s_a, s_b)} and {(n_a, _n_b)}"
        # каждый индекс покрыт хотя бы одним окном
        covered: set[int] = set()
        for a, b in wins:
            covered.update(range(a, b + 1))
        assert covered == set(range(n))

    def test_adjacent_windows_overlap_on_dense_speech(self) -> None:
        # плотная речь (без больших пауз) → соседние окна ДЕЛЯТ слова (нахлёст, не встык)
        w = words_dt(1000, dt=1.0)
        wins = make_windows(w, target_sec=100.0, overlap_sec=20.0)
        for (_s_a, s_b), (n_a, _n_b) in zip(wins, wins[1:], strict=False):
            assert n_a <= s_b, "adjacent windows must overlap on dense speech"

    def test_starts_strictly_increasing(self) -> None:
        w = words_dt(500, dt=1.0)
        wins = make_windows(w, target_sec=60.0, overlap_sec=10.0)
        starts = [a for a, _ in wins]
        assert starts == sorted(starts)
        assert len(set(starts)) == len(starts)  # строго возрастают (продвижение)

    def test_overlap_ge_target_raises(self) -> None:
        with pytest.raises(ValueError):
            make_windows(words_dt(100), target_sec=100.0, overlap_sec=100.0)

    def test_86min_like_input_yields_about_eight_windows(self) -> None:
        # ~86 мин, ~15795 слов (реальный кейс Cardi B). Проверяем что окна разумного числа и
        # покрывают ВСЮ длину (не только первую треть).
        n = 15795
        dt = (86.0 * 60.0) / n  # растянуть на 86 мин
        w = words_dt(n, dt=dt, dur=dt * 0.8)
        wins = make_windows(w, target_sec=720.0, overlap_sec=75.0)
        assert 6 <= len(wins) <= 10  # ~8 окон
        assert wins[0][0] == 0 and wins[-1][1] == n - 1


# ─────────────────────── абсолютные индексы через окна ───────────────────────


class TestAbsoluteIndexPreservation:
    def test_slice_keeps_absolute_indices(self) -> None:
        words = words_dt(50)
        out = build_indexed_transcript(words[20:30], start_index=20)
        assert "[20]" in out  # первый индекс окна = абсолютный 20, НЕ 0
        assert "[29]" in out
        assert "[0]" not in out  # не перезапустился с нуля
        assert "[19]" not in out and "[30]" not in out

    def test_default_start_index_unchanged(self) -> None:
        # без start_index → прежнее поведение (нумерация с 0)
        out = build_indexed_transcript(words_dt(5))
        assert "[0]w0" in out


# ──────────────────────────────── dedup ────────────────────────────────


class TestDedupRawSegments:
    def test_collapses_duplicate_range_keeping_higher_score(self) -> None:
        raw = [
            {"start_word_index": 10, "end_word_index": 20, "score": 0.5, "type": "hook"},
            {"start_word_index": 10, "end_word_index": 20, "score": 0.9, "type": "hook"},  # dup
            {"start_word_index": 30, "end_word_index": 40, "score": 0.7, "type": "hook"},
        ]
        out = dedup_raw_segments(raw)
        assert len(out) == 2
        by_range = {(o["start_word_index"], o["end_word_index"]): o["score"] for o in out}
        assert by_range[(10, 20)] == 0.9  # выше score оставлен
        assert by_range[(30, 40)] == 0.7

    def test_distinct_ranges_all_kept_in_order(self) -> None:
        raw = [
            {"start_word_index": 5, "end_word_index": 9, "score": 0.4, "type": "hook"},
            {"start_word_index": 50, "end_word_index": 60, "score": 0.6, "type": "hook"},
        ]
        out = dedup_raw_segments(raw)
        assert [(o["start_word_index"], o["end_word_index"]) for o in out] == [(5, 9), (50, 60)]

    def test_malformed_items_are_kept(self) -> None:
        # битый (нет индексов) сохраняется — его отбракует postprocess, а не дедуп
        raw = [{"type": "hook"}, {"start_word_index": 1, "end_word_index": 5, "score": 0.5}]
        out = dedup_raw_segments(raw)
        assert len(out) == 2


# ──────────────────────────────── spread ────────────────────────────────


class TestSelectSpread:
    def _seg(self, start: float, end: float, score: float) -> Segment:
        return Segment(start=start, end=end, reason="r", score=score, type=ClipType.hook)

    def test_noop_when_within_limit(self) -> None:
        segs = [self._seg(0, 3, 0.5), self._seg(100, 103, 0.9)]
        out = select_spread(segs, max_clips=5)
        assert [s.start for s in out] == [0, 100]  # отсортировано, ничего не срезано

    def test_distributes_across_timeline_not_clustered(self) -> None:
        # 6 высоко-скоровых кандидатов в первой трети + 3 низких дальше по таймлайну.
        # Голый топ-N по score взял бы все 3 из кластера (start ≤ 25); spread достаёт поздние.
        cluster = [self._seg(st, st + 3, 0.9) for st in (0, 5, 10, 15, 20, 25)]
        late = [self._seg(st, st + 3, 0.4) for st in (100, 200, 290)]
        out = select_spread(cluster + late, max_clips=3)
        assert len(out) == 3
        starts = [s.start for s in out]
        assert starts == sorted(starts)
        assert max(starts) >= 100, "spread must reach the later part of the timeline"

    def test_result_has_exactly_max_clips(self) -> None:
        segs = [self._seg(i * 10.0, i * 10.0 + 3, 0.5 + i * 0.01) for i in range(20)]
        out = select_spread(segs, max_clips=7)
        assert len(out) == 7


# ─────────── интеграция: select_segments с фейковым Gemini (длинное видео) ───────────


def _fake_gemini(fail_starts: frozenset[int] = frozenset(), usage: dict | None = None):  # type: ignore[type-arg]
    """Фейк call_gemini_structured: по промпту определяет окно (мин. индекс `[a]`) и возвращает
    ОДИН валидный сегмент в этом окне. fail_starts → JobError для этих окон (тест правила №8).
    """

    def fake(
        user_prompt,
        *,
        system_prompt,
        response_schema,
        stage="select",
        usage_sink=None,
        thinking_budget=0,
    ):  # type: ignore[no-untyped-def]  # noqa: E501
        idxs = [int(m) for m in re.findall(r"\[(\d+)\]", user_prompt)]
        a = min(idxs)
        if a in fail_starts:
            raise JobError("select", f"boom at window {a}")
        if usage_sink is not None and usage:
            usage_sink.update(usage)
        seg = {
            "start_word_index": a,
            "end_word_index": a + 5,  # ~24с при dt=4 → в гейте [10,40]
            "type": "hook",
            "reason": "r",
            "score": 0.8,
            "hook": "h",
            "why_works": "w",
            "hook_style": "story",
            "tone": "shocking",
        }
        return json.dumps({"segments": [seg]})

    return fake


def _chunk_settings(**over):  # type: ignore[no-untyped-def]
    base = dict(
        max_clips=12,
        clip_min_sec=10,
        clip_max_sec=40,
        clip_tail_pad_sec=0.0,
        select_audio_aware=False,
        select_chunk_enabled=True,
        select_chunk_target_sec=240,
        select_chunk_overlap_sec=48,
        select_thinking_budget=0,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _long_transcript() -> Transcript:
    # 300 слов по 4с → ~1200с (> 15 мин) → should_chunk True; окна target=240с кроют весь диапазон
    return Transcript(language="en", duration=1200.0, words=words_dt(300, dt=4.0, dur=3.5))


class TestChunkedSelectSpansTimeline:
    """Ключевой тест фикса: длинный транскрипт → кандидаты по ВСЕМУ таймлайну, не в старте."""

    def test_candidates_span_whole_timeline(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(s2, "get_settings", _chunk_settings)
        monkeypatch.setattr(s2, "call_gemini_structured", _fake_gemini())
        tr = _long_transcript()
        segs = select_segments(tr, "title")
        assert len(segs) >= 5  # не 2, как в баге
        starts = [s.start for s in segs]
        assert min(starts) < 30.0, "should have an early clip"
        # ГЛАВНОЕ: есть клип в последней трети таймлайна (total ≈ 1199.5с; баг возвращал только
        # первые ~25 мин из 86). Кандидат из последнего окна кончается ~983.5с (≈82% длины).
        assert max(s.end for s in segs) > 900.0, "must include clips from the last third"

    def test_accumulates_usage_across_windows(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(s2, "get_settings", _chunk_settings)
        monkeypatch.setattr(
            s2, "call_gemini_structured", _fake_gemini(usage={"prompt": 10, "output": 5})
        )
        tr = _long_transcript()
        usage: dict[str, int] = {}
        select_segments(tr, "title", usage_sink=usage)
        # суммируется по окнам (не перезаписывается последним) → prompt = 10 * n_windows
        wins = make_windows(tr.words, target_sec=240.0, overlap_sec=48.0)
        assert usage["prompt"] == 10 * len(wins)
        assert usage["output"] == 5 * len(wins)


class TestChunkedNoSilentFallback:
    """Правило №8: сбой ОДНОГО окна не должен обнулять отбор; сбой ВСЕХ окон → JobError."""

    def test_one_window_failure_still_yields_others(self, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(s2, "get_settings", _chunk_settings)
        # роняем окно, стартующее со слова 0 (первое) — остальные должны выжить
        monkeypatch.setattr(s2, "call_gemini_structured", _fake_gemini(fail_starts=frozenset({0})))
        tr = _long_transcript()
        segs = select_segments(tr, "title")
        assert len(segs) >= 3  # выжившие окна дали кандидатов
        assert "failed, skipping" in capsys.readouterr().err  # сбой ЗАЛОГИРОВАН, не проглочен

    def test_all_windows_failure_raises(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(s2, "get_settings", _chunk_settings)
        tr = _long_transcript()
        wins = make_windows(tr.words, target_sec=240.0, overlap_sec=48.0)
        all_starts = frozenset(a for a, _ in wins)
        monkeypatch.setattr(s2, "call_gemini_structured", _fake_gemini(fail_starts=all_starts))
        with pytest.raises(JobError):
            select_segments(tr, "title")


class TestChunkGatingAndSingleCallParity:
    """Короткое видео → одиночный путь (1 вызов); kill-switch → всегда один вызов."""

    def test_short_video_uses_single_call(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(s2, "get_settings", _chunk_settings)
        calls: list[str] = []

        def counting(user_prompt, **kw):  # type: ignore[no-untyped-def]
            calls.append(user_prompt)
            return json.dumps({"segments": []})

        monkeypatch.setattr(s2, "call_gemini_structured", counting)
        short = Transcript(language="en", duration=120.0, words=words_dt(200, dt=0.6, dur=0.5))
        assert should_chunk(short) is False
        select_segments(short, "t")
        assert len(calls) == 1  # ровно один Gemini-вызов (не чанкится)

    def test_kill_switch_forces_single_call(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(s2, "get_settings", lambda: _chunk_settings(select_chunk_enabled=False))
        calls: list[str] = []

        def counting(user_prompt, **kw):  # type: ignore[no-untyped-def]
            calls.append(user_prompt)
            return json.dumps({"segments": []})

        monkeypatch.setattr(s2, "call_gemini_structured", counting)
        select_segments(_long_transcript(), "t")  # длинное, но чанк ВЫКЛ
        assert len(calls) == 1
