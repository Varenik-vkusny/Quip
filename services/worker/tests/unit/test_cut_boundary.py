"""Машина B (стадия РЕЗ): детерминированная дисциплина границ IN/OUT — pure-функции, тест-первым.

Спека: docs/superpowers/specs/2026-07-11-viral-moment-selection-design.md §6.
Ядро: клип НЕ должен открываться словом-связкой/дейктиком (бан-лист) или висячей отсылкой.
Прямой кейс из A/B: наш clip_10 стартовал с «Тут я сначала акцентировал внимание…» — «тут» ∈
BANLIST_HARD → форс-реснэп вперёд к первому самодостаточному предложению.
"""

import pytest

from app.models import Word
from app.pipeline.cut_boundary import (
    banned_opener_len,
    opening_is_bare_ref,
    snap_start_off_banlist,
)
from app.pipeline.stage2_select import postprocess


def W(texts: list[str]) -> list[Word]:
    """Слова из списка строк (пунктуация в строке = граница предложения)."""
    return [Word(text=t, start=float(k), end=float(k) + 0.8) for k, t in enumerate(texts)]


def mkwords(specs: list[tuple[str, float, float]]) -> list[Word]:
    return [Word(text=t, start=s, end=e) for (t, s, e) in specs]


class TestBannedOpenerLen:
    """Сколько ведущих слов образуют HARD-запрещённый зачин (0 = зачин чистый)."""

    def test_clean_opener_is_zero(self) -> None:
        assert banned_opener_len(W(["Европейские", "учёные", "говорят"]), 0) == 0

    def test_single_ru_deictic_tut(self) -> None:
        # прямой кейс clip_10: «Тут …» — дейктик-зачин
        assert banned_opener_len(W(["Тут", "я", "сначала"]), 0) == 1

    def test_single_ru_conjunction(self) -> None:
        assert banned_opener_len(W(["И", "вот", "что"]), 0) == 1
        assert banned_opener_len(W(["Ну", "смотри"]), 0) == 1

    def test_single_en_conjunction(self) -> None:
        assert banned_opener_len(W(["So", "basically"]), 0) == 1
        assert banned_opener_len(W(["But", "here"]), 0) == 1

    def test_multiword_ru_phrase(self) -> None:
        # «то есть» = 2-словная связка
        assert banned_opener_len(W(["То", "есть", "нельзя"]), 0) == 2

    def test_multiword_en_phrase(self) -> None:
        assert banned_opener_len(W(["You", "know", "the"]), 0) == 2

    def test_longest_phrase_wins(self) -> None:
        # «как я говорил» (3) должно матчиться целиком, а не частично
        assert banned_opener_len(W(["Как", "я", "говорил", "раньше"]), 0) == 3

    def test_normalizes_case_and_punctuation(self) -> None:
        assert banned_opener_len(W(["Тут,", "дело"]), 0) == 1
        assert banned_opener_len(W(["So,", "yeah"]), 0) == 1

    def test_bare_ref_is_not_hard_banned(self) -> None:
        # местоимение-отсылка обрабатывается coref'ом, НЕ hard-баном
        assert banned_opener_len(W(["Это", "изменило", "всё"]), 0) == 0
        assert banned_opener_len(W(["He", "did", "it"]), 0) == 0


class TestSnapStartOffBanlist:
    """Два тира: JUMP-дейктик → след. предложение; TRIM-филлер отрезается, сохраняя фразу."""

    def test_clean_opener_unchanged(self) -> None:
        words = W(["Европейские", "учёные", "говорят", "правду."])
        assert snap_start_off_banlist(words, 0) == 0

    def test_jump_deictic_past_whole_sentence_clip10(self) -> None:
        # «Тут я сначала акцентировал внимание.» — дейктик, ВСЯ фраза вода → к «Нельзя …» (idx5)
        words = W(["Тут", "я", "сначала", "акцентировал", "внимание.", "Нельзя", "так", "жить."])
        assert snap_start_off_banlist(words, 0) == 5

    def test_jump_multiword_deictic_phrase(self) -> None:
        # «то есть» — JUMP-фраза → на следующее предложение
        words = W(["То", "есть", "нельзя.", "Реальный", "хук."])
        assert snap_start_off_banlist(words, 0) == 3

    def test_trim_leading_filler_keeps_sentence(self) -> None:
        # «Вы знаете, почти 65% …» — филлер отрезаем, СИЛЬНЫЙ контент (65%) сохраняем (не прыгаем!)
        words = W(["Вы", "знаете", "почти", "65", "процентов", "воровали", "мелочь."])
        assert snap_start_off_banlist(words, 0) == 2  # → «почти 65% …»

    def test_trim_chained_leading_fillers(self) -> None:
        # «Ну вот 65% …» — цепочка филлеров отрезается до первого сильного слова
        words = W(["Ну", "вот", "65", "процентов", "людей."])
        assert snap_start_off_banlist(words, 0) == 2

    def test_jump_no_sentence_ahead_falls_back(self) -> None:
        # «Так что …» — JUMP-фраза, но .?! впереди нет → за сами токены (не схлопывая клип)
        words = W(["Так", "что", "дальше", "без", "точки"])
        assert snap_start_off_banlist(words, 0) == 2

    def test_never_crosses_end_idx(self) -> None:
        words = W(["Ну", "вот", "и", "всё"])
        # end_idx=1 → реснэп не может уйти за конец клипа
        assert snap_start_off_banlist(words, 0, end_idx=1) <= 1


class TestOpeningIsBareRef:
    """Детектор висячей отсылки: первое слово — голое местоимение/указатель без антецедента."""

    def test_ru_bare_refs(self) -> None:
        assert opening_is_bare_ref(W(["Это", "было", "круто."]), 0) is True
        assert opening_is_bare_ref(W(["Он", "ушёл."]), 0) is True

    def test_en_bare_refs(self) -> None:
        assert opening_is_bare_ref(W(["That", "changed", "everything."]), 0) is True
        assert opening_is_bare_ref(W(["It", "works."]), 0) is True

    def test_normal_word_is_not_bare_ref(self) -> None:
        assert opening_is_bare_ref(W(["Деньги", "решают."]), 0) is False
        assert opening_is_bare_ref(W(["Money", "talks."]), 0) is False

    def test_normalizes_punctuation(self) -> None:
        assert opening_is_bare_ref(W(["Это,", "кстати,"]), 0) is True


class TestPostprocessResnapsBannedStart:
    """Интеграция: postprocess вшивает snap_start_off_banlist после snap_start_index → клип
    больше НЕ открывается запрещённым зачином. Прямой регресс-гард против старта clip_10."""

    def test_banned_opener_dropped_from_clip_start(self) -> None:
        words = mkwords(
            [
                ("Тут", 0.0, 0.8),
                ("я", 1.0, 1.8),
                ("сначала", 2.0, 2.8),
                ("акцентировал", 3.0, 3.8),
                ("внимание.", 4.0, 4.8),  # конец запрещённого зачина-предложения
                ("Деньги", 5.0, 5.8),  # ← реальный старт клипа
                ("решают", 6.0, 6.8),
                ("очень", 7.0, 7.8),
                ("многое", 8.0, 8.8),
                ("в", 9.0, 9.8),
                ("этой", 10.0, 10.8),
                ("жизни", 11.0, 11.8),
                ("каждый", 12.0, 12.8),
                ("день", 13.0, 13.8),
                ("реально.", 14.0, 14.8),
            ]
        )
        raw = [
            {
                "start_word_index": 0,
                "end_word_index": 14,
                "reason": "r",
                "score": 0.9,
                "type": "hook",
            }
        ]
        segs = postprocess(raw, words, min_sec=5, max_sec=60)
        assert len(segs) == 1
        # старт больше НЕ на «Тут…»; клип начинается с «Деньги» (idx5): words[5].start − 0.15 = 4.85
        assert segs[0].start == pytest.approx(4.85)

    def test_leading_filler_trimmed_but_content_kept(self) -> None:
        # «Вы знаете, почти 65% …» — TRIM режет филлер, но контент («65%») остаётся в клипе.
        words = mkwords(
            [
                ("Вы", 0.0, 0.8),
                ("знаете", 1.0, 1.8),  # ← филлер «вы знаете» отрезается
                ("почти", 2.0, 2.8),  # ← новый старт клипа
                ("65", 3.0, 3.8),
                ("процентов", 4.0, 4.8),
                ("людей", 5.0, 5.8),
                ("воровали", 6.0, 6.8),
                ("мелочь", 7.0, 7.8),
                ("у", 8.0, 8.8),
                ("родителей", 9.0, 9.8),
                ("каждый", 10.0, 10.8),
                ("божий", 11.0, 11.8),
                ("день", 12.0, 12.8),
                ("это", 13.0, 13.8),
                ("правда.", 14.0, 14.8),
            ]
        )
        raw = [
            {
                "start_word_index": 0,
                "end_word_index": 14,
                "reason": "r",
                "score": 0.9,
                "type": "hook",
            }
        ]
        segs = postprocess(raw, words, min_sec=5, max_sec=60)
        assert len(segs) == 1
        assert segs[0].start == pytest.approx(1.85)  # words[2]«почти».start − 0.15
        assert segs[0].start < 3.0  # «65» (idx3) осталось внутри клипа — фразу не выкинули
