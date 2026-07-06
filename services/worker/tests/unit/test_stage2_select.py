"""Тесты pure-постобработки Stage 2 (выбор моментов) — ядро качества, тест-первым.

Баг-опасные места (план §4А B): маппинг индексов слов → секунды, snap-to-sentence,
длительность-гейт, разрешение пересечений, клиппинг score. Всё детерминированно.
"""

import pytest

from app.errors import JobError
from app.models import ClipType, Transcript, Word
from app.pipeline.stage2_select import (
    _backoff_delay,
    _is_filler,
    build_indexed_transcript,
    build_user_prompt,
    clamp_score,
    indices_to_times,
    is_transient_gemini_error,
    pad_clip_end,
    postprocess,
    refine_boundaries_audio,
    resolve_max_clips,
    resolve_overlaps,
    snap_end_index,
    snap_start_index,
)


def mkwords(specs: list[tuple[str, float, float]]) -> list[Word]:
    return [Word(text=t, start=s, end=e) for (t, s, e) in specs]


# равномерные слова: индекс k → [k, k+0.8]; "Sn." заканчивает предложение
def uniform(n: int, sentence_ends: set[int] | None = None) -> list[Word]:
    ends = sentence_ends or set()
    return [
        Word(text=(f"w{k}." if k in ends else f"w{k}"), start=float(k), end=float(k) + 0.8)
        for k in range(n)
    ]


class TestSnapEndBackwardSentence:
    """snap_end_index шаг 4: конец не на .?! → назад к ближайшему концу предложения (широкое окно,
    без диаризации, не короче min_dur_sec). Founder QA: clip_05 садился на "…face" (пауза посреди
    фразы), т.к. .?! был в 9 словах назад — на 1 за окном ±8."""

    def test_pulls_back_to_sentence_beyond_max_extend(self) -> None:
        # .?! только в idx16; кандидат-конец idx25 (не .?!), вперёд +8 нет .?!, пауз нет →
        # тянем НАЗАД к "w16." (9<20 слов назад; раньше окно ±8 его не доставало).
        words = uniform(40, {16})
        assert snap_end_index(words, 25, start_idx=0) == 16

    def test_prefers_nearest_previous_sentence(self) -> None:
        # два .?! (16 и 22) → берём БЛИЖАЙШИЙ назад (22 = длиннее клип), не 16.
        words = uniform(40, {16, 22})
        assert snap_end_index(words, 25, start_idx=0) == 22

    def test_min_dur_guard_keeps_pause(self) -> None:
        # пуллбэк к idx16 дал бы клип 16.8с < min_dur 20с → не уходим под минимум, держим конец.
        words = uniform(40, {16})
        assert snap_end_index(words, 25, start_idx=0, min_dur_sec=20.0) == 25


class TestClampScore:
    def test_above_one(self) -> None:
        assert clamp_score(1.5) == 1.0

    def test_below_zero(self) -> None:
        assert clamp_score(-0.2) == 0.0

    def test_inside(self) -> None:
        assert clamp_score(0.5) == 0.5


class TestResolveMaxClips:
    """Сколько кандидатов отдавать: запрос юзера (UI-степпер) > дефолт, с клампом в диапазон."""

    def test_none_falls_back_to_default(self) -> None:
        assert resolve_max_clips(None, 8) == 8

    def test_request_overrides_default(self) -> None:
        assert resolve_max_clips(3, 8) == 3

    def test_clamps_below_lo(self) -> None:
        assert resolve_max_clips(0, 8, lo=1, hi=12) == 1

    def test_clamps_above_hi(self) -> None:
        assert resolve_max_clips(99, 8, lo=1, hi=12) == 12

    def test_default_cap_is_30(self) -> None:
        # Прод-потолок «как найдётся, максимум 30» (UI Auto-режим шлёт 30).
        assert resolve_max_clips(99, 8) == 30
        assert resolve_max_clips(30, 8) == 30


class TestIsTransientGeminiError:
    """Ретраить ТОЛЬКО транзиентные сбои Gemini (free-tier 429 / 5xx / сеть);
    перманентные (401/403/404/400 — ключ/модель/схема) ронять сразу, не маскируя 60с бэкоффа."""

    def _api_error(self, code: int) -> Exception:
        from google.genai import errors

        # APIError.__init__(code, response_json, response=None); message из response_json
        return errors.APIError(code, {"error": {"message": "boom", "status": "X"}})

    def test_429_rate_limit_is_transient(self) -> None:
        assert is_transient_gemini_error(self._api_error(429)) is True

    def test_503_unavailable_is_transient(self) -> None:
        assert is_transient_gemini_error(self._api_error(503)) is True

    def test_500_server_error_is_transient(self) -> None:
        assert is_transient_gemini_error(self._api_error(500)) is True

    def test_401_auth_is_permanent(self) -> None:
        assert is_transient_gemini_error(self._api_error(401)) is False

    def test_403_permission_is_permanent(self) -> None:
        assert is_transient_gemini_error(self._api_error(403)) is False

    def test_404_bad_model_is_permanent(self) -> None:
        assert is_transient_gemini_error(self._api_error(404)) is False

    def test_400_bad_request_is_permanent(self) -> None:
        assert is_transient_gemini_error(self._api_error(400)) is False

    def test_network_timeout_is_transient(self) -> None:
        import httpx

        assert is_transient_gemini_error(httpx.ReadTimeout("slow")) is True
        assert is_transient_gemini_error(httpx.ConnectError("down")) is True

    def test_unknown_exception_is_transient_by_default(self) -> None:
        # неизвестный тип → лучше ретрайнуть (консервативно), чем уронить из-за классификации
        assert is_transient_gemini_error(RuntimeError("???")) is True


class TestCallGeminiRetryBudget:
    """Gemini у Google регулярно отдаёт транзиентные 429/503 — джоба НЕ должна падать от
    одного чиха. Долбимся в primary-модель МНОГО раз (≥20) с бэкоффом, и только потом сдаёмся.
    """

    def _api_error(self, code: int) -> Exception:
        from google.genai import errors

        return errors.APIError(code, {"error": {"message": "transient", "status": "UNAVAILABLE"}})

    def test_primary_hammered_at_least_20_times_on_transient(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        import types as _pytypes
        from types import SimpleNamespace

        from pydantic import BaseModel

        from app.pipeline import stage2_select as s2

        # бэкофф не должен реально спать в тесте
        monkeypatch.setattr(s2.time, "sleep", lambda *_a, **_k: None)
        monkeypatch.setattr(
            s2,
            "get_settings",
            lambda: SimpleNamespace(
                gemini_api_key="k", llm_model="gemini-2.5-flash", llm_max_output_tokens=2048
            ),
        )

        calls: dict[str, int] = {}
        err = self._api_error(503)

        class _Models:
            def generate_content(self, *, model, contents, config):  # type: ignore[no-untyped-def]
                calls[model] = calls.get(model, 0) + 1
                raise err

        def _fake_client(*_a, **_k):  # type: ignore[no-untyped-def]
            return _pytypes.SimpleNamespace(models=_Models())

        import google.genai as _genai

        monkeypatch.setattr(_genai, "Client", _fake_client)

        class _Schema(BaseModel):
            x: int

        with pytest.raises(JobError):
            s2.call_gemini_structured("u", system_prompt="s", response_schema=_Schema)

        # пользовательское требование: «бейся 20 раз». primary долбится ровно _MAX_ATTEMPTS,
        # и сам бюджет ≥ 20 (иначе сдаёмся слишком рано на временном сбое Google).
        assert s2._MAX_ATTEMPTS >= 20
        assert calls.get("gemini-2.5-flash") == s2._MAX_ATTEMPTS


class TestIndicesToTimes:
    def test_maps_to_word_boundaries(self) -> None:
        words = uniform(10)
        start, end = indices_to_times(words, 2, 5)
        assert start == 1.85  # words[2].start - 0.15
        assert end == 5.8  # words[5].end

    def test_out_of_range_raises(self) -> None:
        words = uniform(5)
        with pytest.raises(JobError):
            indices_to_times(words, 0, 10)
        with pytest.raises(JobError):
            indices_to_times(words, -1, 3)

    def test_start_after_end_raises(self) -> None:
        words = uniform(5)
        with pytest.raises(JobError):
            indices_to_times(words, 4, 2)


class TestSnapEnd:
    def test_extends_to_next_sentence_end(self) -> None:
        words = uniform(10, sentence_ends={4})
        # конец на idx2 (не конец предложения) → тянем до idx4 ("w4.")
        assert snap_end_index(words, 2, max_extend=5) == 4

    def test_no_change_if_already_sentence_end(self) -> None:
        words = uniform(10, sentence_ends={2})
        assert snap_end_index(words, 2, max_extend=5) == 2

    def test_no_change_if_none_within_window(self) -> None:
        words = uniform(10, sentence_ends={9})
        # с idx2 до idx9 семь слов — за пределом окна 5 → без изменений
        assert snap_end_index(words, 2, max_extend=5) == 2


class TestSnapEndPause:
    """W1: при отсутствии .?! в окне — снап конца к ПАУЗЕ по word-таймингам (чистый вдох),
    а не резать посреди фразы. .?! приоритетнее паузы."""

    def test_snaps_to_pause_when_no_sentence_end(self) -> None:
        # нет .?!; ясная пауза 1.0с после idx3 → конец снапается к idx3 (вдох)
        words = mkwords(
            [
                ("a", 0.0, 0.5),
                ("b", 0.5, 1.0),
                ("c", 1.0, 1.5),
                ("d", 1.5, 2.0),  # idx3 заканчивается на 2.0
                ("e", 3.0, 3.5),  # пауза 1.0с после idx3
                ("f", 3.5, 4.0),
            ]
        )
        assert snap_end_index(words, 2, max_extend=5, min_pause=0.35) == 3

    def test_prefers_sentence_end_over_pause(self) -> None:
        words = mkwords(
            [
                ("a", 0.0, 0.5),
                ("b.", 0.5, 1.0),  # idx1 конец предложения, паузы нет
                ("c", 1.5, 2.0),  # пауза перед c
            ]
        )
        assert snap_end_index(words, 0, max_extend=5, min_pause=0.35) == 1

    def test_no_pause_no_sentence_keeps_index(self) -> None:
        # сплошная речь, нет .?!, нет паузы ≥ порога → без изменений
        words = mkwords([(f"w{k}", k * 0.5, k * 0.5 + 0.45) for k in range(8)])
        assert snap_end_index(words, 2, max_extend=5, min_pause=0.35) == 2

    def test_wider_default_window_finds_far_sentence_end(self) -> None:
        # дефолтное окно расширено (было 5) → .?! на +7 слов теперь достижим
        words = uniform(20, sentence_ends={9})
        assert snap_end_index(words, 2) == 9


class TestPadClipEnd:
    """W1: хвостовой паддинг — добить конец клипа тишиной для чистого лупа,
    но не залезть в следующее слово и не выйти за длительность."""

    def test_pads_into_trailing_silence(self) -> None:
        words = mkwords([("a", 0.0, 1.0), ("b", 1.0, 2.0), ("c", 5.0, 6.0)])
        assert pad_clip_end(2.0, words, 1, duration=10.0, pad=0.3) == pytest.approx(2.3)

    def test_caps_at_next_word_start(self) -> None:
        words = mkwords([("a", 0.0, 1.0), ("b", 1.0, 2.0), ("c", 2.1, 3.0)])
        # зазор лишь 0.1с → паддинг кламп к 2.1 (не 2.3) — не залезаем в следующее слово
        assert pad_clip_end(2.0, words, 1, duration=10.0, pad=0.3) == pytest.approx(2.1)

    def test_caps_at_duration_for_last_word(self) -> None:
        words = mkwords([("a", 0.0, 1.0), ("b", 1.0, 2.0)])
        assert pad_clip_end(2.0, words, 1, duration=2.15, pad=0.3) == pytest.approx(2.15)

    def test_zero_pad_is_noop(self) -> None:
        words = mkwords([("a", 0.0, 1.0), ("b", 1.0, 2.0), ("c", 5.0, 6.0)])
        assert pad_clip_end(2.0, words, 1, duration=10.0, pad=0.0) == 2.0

    def test_never_shortens_on_overlapping_timings(self) -> None:
        # ASR иногда отдаёт ПЕРЕКРЫВАЮЩИЕСЯ тайминги: next.start < current.end (поймано на
        # реальном 1ч-видео). Паддинг НЕ должен укорачивать клип (резать конечное слово) —
        # кламп к next_start не может уйти раньше конца слова.
        words = mkwords([("a", 0.0, 1.0), ("end.", 1.0, 3.0), ("next", 2.0, 2.5)])
        assert pad_clip_end(3.0, words, 1, duration=100.0, pad=0.3) >= 3.0


class TestHookRegen:
    """W4: одноразовая перегенерация хука под новый интервал (узкий Gemini-вызов, не чат)."""

    def test_build_prompt_includes_text_duration_and_styles(self) -> None:
        from app.pipeline.stage2_select import build_hook_regen_prompt

        prompt = build_hook_regen_prompt("привет это клип про деньги", language="ru", duration=23.0)
        assert "привет это клип про деньги" in prompt
        assert "23" in prompt
        assert "hook_style" in prompt.lower()

    def test_parse_response_returns_hook_and_normalized_style(self) -> None:
        import json

        from app.pipeline.stage2_select import parse_hook_response

        text = json.dumps({"tone": "shock", "hook_style": "  SHOCK ", "hook": "  Он потерял всё "})
        hook, style = parse_hook_response(text)
        assert hook == "Он потерял всё"
        assert style == "shock"

    def test_parse_response_empty_style_is_none(self) -> None:
        import json

        from app.pipeline.stage2_select import parse_hook_response

        hook, style = parse_hook_response(json.dumps({"hook": "Текст", "hook_style": ""}))
        assert hook == "Текст"
        assert style is None

    def test_parse_response_missing_hook_raises(self) -> None:
        import json

        from app.errors import JobError
        from app.pipeline.stage2_select import parse_hook_response

        with pytest.raises(JobError):
            parse_hook_response(json.dumps({"hook_style": "pov", "hook": "   "}))

    def test_parse_response_bad_json_raises(self) -> None:
        from app.errors import JobError
        from app.pipeline.stage2_select import parse_hook_response

        with pytest.raises(JobError):
            parse_hook_response("not json{")


class TestSystemPromptV2:
    """W2: системный промпт вынесен в файл v2 (версионируем без передеплоя) и описывает
    двухступенчатую генерацию хука (тон → стиль → текст)."""

    def test_prompt_path_is_v2(self) -> None:
        from app.pipeline.stage2_select import _PROMPT_PATH

        assert _PROMPT_PATH.name == "select_moments.v2.txt"

    def test_v2_file_exists_and_describes_hook_styles(self) -> None:
        from app.pipeline.stage2_select import _PROMPT_PATH

        assert _PROMPT_PATH.exists(), "prompts/select_moments.v2.txt должен существовать"
        text = _PROMPT_PATH.read_text(encoding="utf-8").lower()
        assert "hook_style" in text
        # перечислены стили нового брифа (story/insight/question/bold_claim/number; POV выпилен)
        for style in ("story", "insight", "question", "bold_claim", "number"):
            assert style in text

    def test_load_system_prompt_reads_v2_file(self) -> None:
        from app.pipeline.stage2_select import load_system_prompt

        assert "hook_style" in load_system_prompt().lower()


class TestBuildUserPromptLength:
    """W1: реальные clip_min/max_sec уходят в промпт как ЖЁСТКИЙ лимит (модель обязана соблюсти)."""

    def test_includes_hard_length_limits(self) -> None:
        words = uniform(20)
        tr = Transcript(language="en", duration=20.0, words=words)
        indexed = build_indexed_transcript(words)
        prompt = build_user_prompt("title", tr, indexed, max_clips=5, min_sec=15, max_sec=45)
        assert "45" in prompt
        assert "15" in prompt
        assert "second" in prompt.lower()


class TestSnapEndSpeaker:
    """D2: конец клипа не должен захватывать реплику ДРУГОГО спикера (diarize). Без speaker-меток
    (speaker=None) поведение = legacy (snap_end_index работает как раньше; start_idx опционален)."""

    def _w(self, text: str, start: float, end: float, speaker: int | None) -> Word:
        return Word(text=text, start=start, end=end, speaker=speaker)

    def test_trims_trailing_other_speaker_outburst(self) -> None:
        # A договаривает мысль («quit.»), затем B выкрикивает 2 слова. LLM-конец сел на B.
        words = [
            self._w("I", 0.0, 0.4, 0),
            self._w("really", 0.4, 0.8, 0),
            self._w("quit.", 0.8, 1.2, 0),
            self._w("Yeah", 1.5, 1.8, 1),
            self._w("totally", 1.8, 2.1, 1),
        ]
        assert snap_end_index(words, 4, start_idx=0) == 2  # назад к концу реплики A

    def test_forward_extend_does_not_cross_speaker(self) -> None:
        # A кончает без точки; следующий .?! принадлежит B → НЕ тянем границу в B.
        words = [
            self._w("so", 0.0, 0.4, 0),
            self._w("anyway", 0.4, 0.9, 0),
            self._w("Right!", 1.5, 1.9, 1),
        ]
        assert snap_end_index(words, 1, start_idx=0) == 1

    def test_primary_is_majority_speaker(self) -> None:
        # primary = тот, кто говорит БОЛЬШИНСТВО клипа (A: 4 слова) → хвост B отрезан.
        words = [
            self._w("a0", 0.0, 0.4, 0),
            self._w("a1", 1.0, 1.4, 0),
            self._w("a2", 2.0, 2.4, 0),
            self._w("done.", 3.0, 3.4, 0),
            self._w("oh.", 4.0, 4.4, 1),
        ]
        assert snap_end_index(words, 4, start_idx=0) == 3

    def test_keeps_clip_when_primary_ends_clean(self) -> None:
        words = [
            self._w("hello", 0.0, 0.4, 0),
            self._w("world.", 0.4, 0.8, 0),
        ]
        assert snap_end_index(words, 1, start_idx=0) == 1

    def test_no_speaker_labels_matches_legacy(self) -> None:
        # speaker=None всюду → как раньше (тянем вперёд к .?! на индексе 4), со start_idx и без.
        words = uniform(6, sentence_ends={4})
        assert snap_end_index(words, 2, start_idx=0) == 4
        assert snap_end_index(words, 2) == 4


class TestSnapStart:
    def test_moves_to_sentence_start(self) -> None:
        words = uniform(10, sentence_ends={0})
        # idx0 "w0." заканчивает предложение → начало след. предложения = idx1
        assert snap_start_index(words, 3, max_extend=5) == 1

    def test_no_change_if_already_at_sentence_start(self) -> None:
        words = uniform(10, sentence_ends={1})
        # words[1] заканчивает предложение → idx2 уже начало предложения
        assert snap_start_index(words, 2, max_extend=5) == 2

    def test_zero_index_stays(self) -> None:
        words = uniform(10)
        assert snap_start_index(words, 0, max_extend=5) == 0

    def test_skips_dangling_tail_to_next_sentence(self) -> None:
        # старт на ХВОСТЕ длинного предложения (само слово завершает его, а начало
        # предложения недостижимо назад в окне) → уходим в начало след. предложения.
        # Это баг «Антимошенника»: клип не должен начинаться с последнего слова мысли.
        words = uniform(20, sentence_ends={9})  # [9] завершает предложение, назад в 5 концов нет
        assert snap_start_index(words, 9, max_extend=5) == 10

    def test_preserves_short_opening_sentence(self) -> None:
        # [4] завершает предыдущее предложение → [5] уже чистый старт короткого
        # предложения "w5 w6 w7." — НЕ перепрыгиваем его (иначе теряем короткий хук).
        words = uniform(20, sentence_ends={4, 7})
        assert snap_start_index(words, 5, max_extend=5) == 5


class TestResolveOverlaps:
    def _seg(self, start: float, end: float, score: float) -> object:
        from app.models import Segment

        return Segment(start=start, end=end, reason="r", score=score, type=ClipType.hook)

    def test_keeps_higher_score_on_overlap(self) -> None:
        from app.models import Segment

        a = Segment(start=0, end=20, reason="r", score=0.9, type=ClipType.hook)
        b = Segment(start=10, end=30, reason="r", score=0.5, type=ClipType.hook)  # overlaps a
        out = resolve_overlaps([a, b])
        assert len(out) == 1
        assert out[0].score == 0.9

    def test_keeps_all_when_disjoint(self) -> None:
        from app.models import Segment

        a = Segment(start=0, end=20, reason="r", score=0.5, type=ClipType.hook)
        b = Segment(start=25, end=45, reason="r", score=0.9, type=ClipType.hook)
        out = resolve_overlaps([a, b])
        assert len(out) == 2
        assert [s.start for s in out] == [0, 25]  # отсортировано по start


# words[0..24] plain, words[25] ends sentence → snap_end stays at 25; duration 25.8s passes gate
_WORDS_30 = uniform(30, sentence_ends={25})


# minimal valid raw item (indices + required LLM fields)
def _raw(extra: dict) -> list[dict]:  # type: ignore[type-arg]
    base: dict = {  # type: ignore[type-arg]
        "start_word_index": 0,
        "end_word_index": 25,
        "type": "emotional_peak",
        "reason": "test reason",
        "score": 0.8,
        "hook": "test hook",
        "why_works": "it works",
        "hook_style": "story",
        "tone": "shocking",
    }
    base.update(extra)
    return [base]


class TestPostprocessToneAndKeyQuote:
    """Clip-card redesign: postprocess() must thread tone + key_quote through to Segment."""

    def test_passes_tone_and_key_quote(self) -> None:
        raw = _raw({"tone": "shocking", "key_quote": "This changed everything."})
        result = postprocess(raw, _WORDS_30)
        assert len(result) == 1
        assert result[0].tone == "shocking"
        assert result[0].key_quote == "This changed everything."

    def test_tone_normalized_to_lowercase(self) -> None:
        raw = _raw({"tone": "  FUNNY  ", "key_quote": None})
        result = postprocess(raw, _WORDS_30)
        assert result[0].tone == "funny"

    def test_tone_none_when_absent(self) -> None:
        item = _raw({})[0]
        del item["tone"]
        result = postprocess([item], _WORDS_30)
        assert len(result) == 1
        assert result[0].tone is None

    def test_key_quote_none_when_absent(self) -> None:
        raw = _raw({})
        if "key_quote" in raw[0]:
            del raw[0]["key_quote"]
        result = postprocess(raw, _WORDS_30)
        assert len(result) == 1
        assert result[0].key_quote is None

    def test_key_quote_stripped(self) -> None:
        raw = _raw({"key_quote": "  Leading whitespace.  "})
        result = postprocess(raw, _WORDS_30)
        assert result[0].key_quote == "Leading whitespace."

    def test_empty_key_quote_becomes_none(self) -> None:
        raw = _raw({"key_quote": "   "})
        result = postprocess(raw, _WORDS_30)
        assert result[0].key_quote is None


class TestPostprocess:
    def test_happy_path_snaps_gates_and_validates(self) -> None:
        words = uniform(40, sentence_ends={2, 25})
        raw = [
            {
                "start_word_index": 3,
                "end_word_index": 22,
                "reason": "  concrete why  ",
                "score": 1.4,  # клип в 1.0
                "type": "hook",
            }
        ]
        segs = postprocess(raw, words, min_sec=15, max_sec=60)
        assert len(segs) == 1
        s = segs[0]
        assert s.score == 1.0  # клипнут
        assert s.reason == "concrete why"  # тримнут
        assert s.type is ClipType.hook
        # A1 (снапперы восстановлены): старт уже чист (words[2]="w2." → clean start), но конец
        # idx22 (не .?!) тянется вперёд к ближайшему .?! = words[25] (в окне max_extend=8).
        assert s.start == 2.85  # words[3].start - 0.15
        assert s.end == 25.8  # words[25].end после snap_end_index

    def test_drops_too_short(self) -> None:
        words = uniform(40)
        raw = [
            {
                "start_word_index": 0,
                "end_word_index": 5,
                "reason": "r",
                "score": 0.9,
                "type": "hook",
            }
        ]
        assert postprocess(raw, words, min_sec=15, max_sec=60) == []

    def test_drops_too_long(self) -> None:
        words = mkwords([("a", 0.0, 0.5), ("b", 70.0, 70.5)])
        raw = [
            {
                "start_word_index": 0,
                "end_word_index": 1,
                "reason": "r",
                "score": 0.9,
                "type": "hook",
            }
        ]
        assert postprocess(raw, words, min_sec=15, max_sec=60) == []

    def test_drops_invalid_type(self) -> None:
        words = uniform(40)
        raw = [
            {
                "start_word_index": 0,
                "end_word_index": 20,
                "reason": "r",
                "score": 0.9,
                "type": "bogus",
            }
        ]
        assert postprocess(raw, words, min_sec=15, max_sec=60) == []

    def test_caps_to_max_clips_by_score(self) -> None:
        words = uniform(120)
        raw = [
            {
                "start_word_index": 0,
                "end_word_index": 20,
                "reason": "a",
                "score": 0.5,
                "type": "hook",
            },
            {
                "start_word_index": 25,
                "end_word_index": 45,
                "reason": "b",
                "score": 0.9,
                "type": "hook",
            },
            {
                "start_word_index": 50,
                "end_word_index": 70,
                "reason": "c",
                "score": 0.7,
                "type": "hook",
            },
            {
                "start_word_index": 75,
                "end_word_index": 95,
                "reason": "d",
                "score": 0.95,
                "type": "hook",
            },
        ]
        segs = postprocess(raw, words, min_sec=15, max_sec=60, max_clips=2)
        assert len(segs) == 2
        assert {s.reason for s in segs} == {"b", "d"}  # топ-2 по score
        assert [s.start for s in segs] == sorted(s.start for s in segs)  # сортировка по start

    def test_carries_hook_and_why_works(self) -> None:
        # T1/T2: LLM-поля hook/why_works пробрасываются в Segment (объяснимость как продукт)
        words = uniform(40, sentence_ends={2, 25})
        raw = [
            {
                "start_word_index": 3,
                "end_word_index": 22,
                "reason": "concrete why",
                "score": 0.8,
                "type": "hook",
                "hook": "Он потерял всё за день",
                "why_works": "Ставка и конфликт в первой фразе",
            }
        ]
        segs = postprocess(raw, words, min_sec=15, max_sec=60)
        assert len(segs) == 1
        assert segs[0].hook == "Он потерял всё за день"
        assert segs[0].why_works == "Ставка и конфликт в первой фразе"

    def test_carries_and_normalizes_hook_style(self) -> None:
        # W2: hook_style пробрасывается в Segment и нормализуется (trim + lower).
        words = uniform(40, sentence_ends={2, 25})
        raw = [
            {
                "start_word_index": 3,
                "end_word_index": 22,
                "reason": "r",
                "score": 0.8,
                "type": "hook",
                "hook": "POV: тот самый темщик",
                "hook_style": "  POV  ",
                "why_works": "w",
            }
        ]
        segs = postprocess(raw, words, min_sec=15, max_sec=60)
        assert segs[0].hook_style == "pov"

    def test_missing_hook_style_is_none(self) -> None:
        # старый raw без hook_style → None (обратная совместимость)
        words = uniform(40, sentence_ends={2, 25})
        raw = [
            {
                "start_word_index": 3,
                "end_word_index": 22,
                "reason": "r",
                "score": 0.8,
                "type": "hook",
                "hook": "h",
                "why_works": "w",
            }
        ]
        segs = postprocess(raw, words, min_sec=15, max_sec=60)
        assert segs[0].hook_style is None

    def test_missing_hook_fields_are_none(self) -> None:
        # старый raw без hook/why_works → поля None (не падаем, обратная совместимость)
        words = uniform(40, sentence_ends={2, 25})
        raw = [
            {
                "start_word_index": 3,
                "end_word_index": 22,
                "reason": "r",
                "score": 0.8,
                "type": "hook",
            }
        ]
        segs = postprocess(raw, words, min_sec=15, max_sec=60)
        assert len(segs) == 1
        assert segs[0].hook is None and segs[0].why_works is None

    def test_applies_tail_padding(self) -> None:
        # W1 + A1: snap_end_index тянет конец idx22 → words[25]="w25." (.?! в окне). end = w25.end
        # = 25.8; w26 старт = 26.0; tail_pad 0.3 → min(26.1, 26.0) = 26.0.
        words = uniform(40, sentence_ends={2, 25})
        raw = [
            {
                "start_word_index": 3,
                "end_word_index": 22,
                "reason": "r",
                "score": 0.8,
                "type": "hook",
            }
        ]
        segs = postprocess(raw, words, min_sec=15, max_sec=60, duration=100.0, tail_pad=0.3)
        assert len(segs) == 1
        assert segs[0].end == pytest.approx(26.0)

    def test_tail_padding_gate_uses_unpadded_length(self) -> None:
        # Гейт длины считается по РЕЧИ (без паддинга): клип ровно на max_sec проходит,
        # а паддинг (в реальную тишину) потом добавляет хвост сверх max — и НЕ дропает клип.
        words = mkwords(
            [
                ("Start.", 0.0, 0.5),  # idx0 конец предложения → старт снапнется к idx1
                ("a", 1.0, 1.5),  # idx1 — старт клипа = 1.0
                ("end.", 60.5, 61.0),  # idx2 конец → речь = 61.0-1.0 = 60.0 == max
                ("next", 65.0, 65.5),  # 4с тишины после idx2
            ]
        )
        raw = [
            {
                "start_word_index": 1,
                "end_word_index": 2,
                "reason": "r",
                "score": 0.9,
                "type": "hook",
            }
        ]
        segs = postprocess(raw, words, min_sec=15, max_sec=60, duration=200.0, tail_pad=0.3)
        assert len(segs) == 1  # гейт по речи (60.0) → проходит
        assert segs[0].end == pytest.approx(61.3)  # 61.0 + 0.3 паддинга в тишину

    def test_resolves_overlap_keeping_higher_score(self) -> None:
        words = uniform(60)
        raw = [
            {
                "start_word_index": 0,
                "end_word_index": 20,
                "reason": "a",
                "score": 0.6,
                "type": "hook",
            },
            {
                "start_word_index": 10,
                "end_word_index": 30,
                "reason": "b",
                "score": 0.95,
                "type": "strong_quote",
            },
        ]
        segs = postprocess(raw, words, min_sec=15, max_sec=60)
        assert len(segs) == 1
        assert segs[0].reason == "b"


class TestBackoffDelay:
    """Backoff с jitter: де-коррелирует ретраи параллельных джоб под provider-wide 503."""

    def test_within_bounds_for_attempts_0_to_6(self) -> None:
        # Верхняя граница = min(2**attempt, cap=30); нижняя = 0 (jitter не уходит в минус).
        for attempt in range(7):
            ceil = min(2**attempt, 30)
            for _ in range(200):
                d = _backoff_delay(attempt)
                assert 0.0 <= d <= ceil, f"attempt={attempt}: {d} not in [0, {ceil}]"

    def test_equal_jitter_keeps_minimum_wait(self) -> None:
        # equal-jitter: гарантированный минимум = 0.5 * min(2**attempt, cap).
        for attempt in range(7):
            ceil = min(2**attempt, 30)
            for _ in range(200):
                d = _backoff_delay(attempt)
                assert d >= 0.5 * ceil, f"attempt={attempt}: {d} < {0.5 * ceil}"

    def test_cap_honoured_at_high_attempts(self) -> None:
        # При attempt >> log2(cap) потолок остаётся cap (не растёт экспоненциально дальше).
        for _ in range(200):
            d = _backoff_delay(20)
            assert 15.0 <= d <= 30.0

    def test_decorrelated_not_all_identical(self) -> None:
        # Главная цель фикса: повторные вызовы для одного attempt не совпадают
        # (jitter рассинхронизирует волны ретраев). Детерминированный delay → set размера 1.
        vals = {_backoff_delay(5) for _ in range(50)}
        assert len(vals) > 1


class TestBuildIndexedTranscriptFormatting:
    def test_formats_speaker_tags_pauses_and_inline_indices(self) -> None:
        words = [
            Word(text="Hello", start=1.0, end=1.5, speaker=0),
            Word(text="world", start=1.5, end=2.0, speaker=0),
            # Пауза 1.0 сек
            Word(text="How", start=3.0, end=3.5, speaker=0),
            # Смена спикера
            Word(text="Fine", start=3.5, end=4.0, speaker=1),
        ]
        res = build_indexed_transcript(words)
        assert "<Speaker 0>:" in res
        assert "[0]Hello [1]world" in res
        assert "<pause: 1.0s>" in res
        assert "[2]How" in res
        assert "<Speaker 1>:" in res
        assert "[3]Fine" in res


class TestIndicesToTimesClamping:
    def test_clamps_start_padding_to_prev_word_end(self) -> None:
        words = [
            Word(text="first", start=1.0, end=1.9),
            # Пауза 0.1 сек (меньше 0.15 сек)
            Word(text="second", start=2.0, end=2.5),
        ]
        start, end = indices_to_times(words, 1, 1)
        # Должен зажаться на 1.9, а не уйти на 1.85 (2.0 - 0.15 = 1.85)
        assert start == 1.9
        assert end == 2.5


class TestPostprocessSnappersRestored:
    """A1 (регресс 18abe24): postprocess снова ДЕТЕРМИНИРОВАННО снапит границы к предложениям —
    снапперы вызываются ДО indices_to_times (были молча удалены). Иначе клип кончается на фрагменте.
    """

    def _raw_idx(self, si: int, ei: int) -> list[dict]:  # type: ignore[type-arg]
        return [
            {
                "start_word_index": si,
                "end_word_index": ei,
                "reason": "r",
                "score": 0.9,
                "type": "hook",
            }
        ]

    def test_end_snaps_forward_to_sentence_end(self) -> None:
        # конец LLM сел на idx22 (середина фразы); ближайший .?! = words[25] → снап тянет туда.
        words = uniform(40, sentence_ends={2, 25})
        segs = postprocess(self._raw_idx(3, 22), words, min_sec=15, max_sec=60)
        assert len(segs) == 1
        # end = words[25].end (снапнуто вперёд), а НЕ words[22].end=22.8 (без снапа)
        assert segs[0].end == pytest.approx(25.8)

    def test_start_snaps_off_dangling_tail(self) -> None:
        # старт LLM сел на ХВОСТ предыдущей мысли (idx26 сразу после конца предложения idx25) —
        # проверяем что снап-старт активен: idx26 уже чистый старт (words[25] завершает пред.).
        words = uniform(60, sentence_ends={25, 50})
        segs = postprocess(self._raw_idx(26, 48), words, min_sec=15, max_sec=60)
        assert len(segs) == 1
        # старт остаётся на words[26] (чистое начало), конец тянется к words[50]
        assert segs[0].start == pytest.approx(25.85)  # words[26].start - 0.15
        assert segs[0].end == pytest.approx(50.8)


class TestIsFiller:
    """A2: детекция слов-паразитов (Deepgram filler_words=true даёт им таймстемпы, но inline)."""

    def test_english_fillers(self) -> None:
        assert _is_filler("uh")
        assert _is_filler("Um")
        assert _is_filler("UH")

    def test_russian_fillers(self) -> None:
        assert _is_filler("э")
        assert _is_filler("а")
        assert _is_filler("ну")

    def test_strips_surrounding_punctuation(self) -> None:
        # ASR часто пунктуирует филлер («Uh,» / «um.»)
        assert _is_filler("Uh,")
        assert _is_filler("um.")

    def test_real_words_are_not_fillers(self) -> None:
        assert not _is_filler("money")
        assert not _is_filler("думаю")
        assert not _is_filler("")


class TestRefineBoundariesAudio:
    """A2: аудио-осознанное уточнение границ (чистые вдохи, обрезка филлеров у краёв).
    Без кривой громкости (None/пусто) — строгий no-op (правило №8: явная ветка, не тихий фолбэк)."""

    def _speech(self, texts: list[str]) -> list[Word]:
        # непрерывная речь: word[k].end = k*0.5+0.4 → маппится в loudness[k] (hop_s=0.5);
        # зазор между словами = 0.1с (< min_pause 0.35 → снап-паузой НЕ триггерится).
        return [Word(text=t, start=k * 0.5, end=k * 0.5 + 0.4) for k, t in enumerate(texts)]

    def test_none_loudness_is_noop(self) -> None:
        words = self._speech([f"w{k}" for k in range(10)])
        assert refine_boundaries_audio(words, None, 1, 5) == (1, 5)

    def test_empty_loudness_is_noop(self) -> None:
        words = self._speech([f"w{k}" for k in range(10)])
        assert refine_boundaries_audio(words, [], 1, 5) == (1, 5)

    def test_snaps_end_to_breath_trough(self) -> None:
        # непрерывная речь, нет .?!/паузы → конец снапается к ближайшей низко-энергетич. впадине.
        words = self._speech([f"w{k}" for k in range(10)])
        loud = [-10.0] * 10
        loud[7] = -42.0  # глубокая впадина (вдох) на word[7].end
        assert refine_boundaries_audio(words, loud, 1, 5) == (1, 7)

    def test_prefers_nearest_trough_on_ties(self) -> None:
        words = self._speech([f"w{k}" for k in range(12)])
        loud = [-10.0] * 12
        loud[3] = -40.0  # впадина слева (расстояние 2 от ei=5)
        loud[8] = -40.0  # впадина справа (расстояние 3 от ei=5)
        # обе равной глубины → ближайшая к ei (idx3, дистанция 2) выигрывает
        assert refine_boundaries_audio(words, loud, 1, 5) == (1, 3)

    def test_keeps_end_when_sentence_final(self) -> None:
        # words[5]=".": конец уже чист → впадину игнорируем (лестница: .?! приоритетнее аудио).
        words = self._speech(["w0", "w1", "w2", "w3", "w4", "done.", "w6", "w7", "w8", "w9"])
        loud = [-10.0] * 10
        loud[7] = -42.0
        assert refine_boundaries_audio(words, loud, 1, 5) == (1, 5)

    def test_keeps_end_when_pause_after(self) -> None:
        # естественная пауза ≥ min_pause сразу после ei → аудио-снап конца пропускаем.
        words = [Word(text=f"w{k}", start=k * 0.5, end=k * 0.5 + 0.4) for k in range(10)]
        words[6] = Word(text="w6", start=3.5, end=3.9)  # зазор words[5].end=2.9 → 3.5 = 0.6 ≥ 0.35
        loud = [-10.0] * 10
        loud[8] = -42.0
        assert refine_boundaries_audio(words, loud, 1, 5) == (1, 5)

    def test_flat_loudness_no_trough_keeps_index(self) -> None:
        # ровная громкость (нет впадины глубже margin) → конец не двигаем.
        words = self._speech([f"w{k}" for k in range(10)])
        assert refine_boundaries_audio(words, [-10.0] * 10, 1, 5) == (1, 5)

    def test_trims_trailing_filler(self) -> None:
        # конец сел на филлер «uh» → отрезаем к предыдущему настоящему слову (words[4]="real.").
        words = self._speech(["w0", "w1", "w2", "w3", "real.", "uh", "w6", "w7"])
        loud = [-10.0] * 8
        # ei=5 (uh) → триммится к 4; words[4]=".", аудио-снап конца НЕ триггерится
        assert refine_boundaries_audio(words, loud, 1, 5) == (1, 4)

    def test_trims_leading_filler(self) -> None:
        # старт сел на филлер → сдвигаем вперёд к настоящему слову.
        words = self._speech(["w0", "um", "real", "w3", "w4", "done.", "w6"])
        loud = [-10.0] * 7
        si, ei = refine_boundaries_audio(words, loud, 1, 5)
        assert si == 2  # off "um"

    def test_does_not_collapse_clip(self) -> None:
        # оба края — филлеры и клип крошечный → не схлопываем si за ei.
        words = self._speech(["uh", "um"])
        si, ei = refine_boundaries_audio(words, [-10.0, -10.0], 0, 1)
        assert si <= ei


class TestPostprocessAudioAware:
    """A2/A3: postprocess прокидывает кривую громкости в refine_boundaries_audio; None → детерм."""

    def test_loudness_none_matches_deterministic(self) -> None:
        words = uniform(40, sentence_ends={2, 25})
        raw = [
            {
                "start_word_index": 3,
                "end_word_index": 22,
                "reason": "r",
                "score": 0.9,
                "type": "hook",
            }
        ]
        segs = postprocess(raw, words, min_sec=15, max_sec=60, loudness=None)
        assert len(segs) == 1
        assert segs[0].end == pytest.approx(25.8)  # чисто детерминированный снап

    def test_trailing_filler_trimmed_with_loudness(self) -> None:
        # непрерывная речь без .?!/пауз, конец на филлере «uh» → с кривой громкости он отрезается.
        words = [Word(text=f"w{k}", start=k * 0.5, end=k * 0.5 + 0.4) for k in range(60)]
        words[40] = Word(text="uh", start=20.0, end=20.4)  # филлер на конце-кандидате
        loud = [-10.0] * 60
        raw = [
            {
                "start_word_index": 0,
                "end_word_index": 40,
                "reason": "r",
                "score": 0.9,
                "type": "hook",
            }
        ]
        # без громкости: конец остаётся на «uh» (words[40].end=20.4)
        no_audio = postprocess(raw, words, min_sec=15, max_sec=60, loudness=None)
        assert no_audio[0].end == pytest.approx(20.4)
        # с громкостью: филлер отрезан → конец на words[39].end=19.9
        with_audio = postprocess(raw, words, min_sec=15, max_sec=60, loudness=loud)
        assert with_audio[0].end == pytest.approx(19.9)


class TestSelectAudioAwareConfig:
    """A4: конфиг-тумблер SELECT_AUDIO_AWARE (env), дефолт включён."""

    def test_default_is_true(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from app.config import Settings

        monkeypatch.setenv("DEEPGRAM_API_KEY", "x")  # провайдер-гейт (иначе ValidationError)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.select_audio_aware is True

    def test_env_can_disable(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from app.config import Settings

        monkeypatch.setenv("DEEPGRAM_API_KEY", "x")
        monkeypatch.setenv("SELECT_AUDIO_AWARE", "false")
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.select_audio_aware is False
