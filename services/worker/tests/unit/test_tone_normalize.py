"""B6: normalize_tone — свести свободный tone LLM к ЗАКРЫТОМУ набору UI (pure, TDD).

UI-набор (ClipCard emoji/glow/energy-fallback): shocking / funny / controversial / inspiring /
relatable / insightful / touching. Модель регулярно уходит из набора («infuriating», «deeply
moving», «emotional»), тогда emoji/цвет карточки молча ломались. normalize_tone маппит синонимы в
канон и коэрсит неизвестное в документированный дефолт. None → None (тон не определён).
"""

from __future__ import annotations

from app.pipeline.stage2_select import normalize_tone

# Документированный дефолт для НЕИЗВЕСТНОГО тона (не роняем в None — карточке нужен хоть какой-то
# осмысленный тон; «insightful» — самый нейтральный из закрытого набора).
_DEFAULT = "insightful"


def test_canonical_pass_through() -> None:
    for t in (
        "shocking",
        "funny",
        "controversial",
        "inspiring",
        "relatable",
        "insightful",
        "touching",
    ):
        assert normalize_tone(t) == t


def test_case_and_whitespace_normalized() -> None:
    assert normalize_tone("FUNNY ") == "funny"
    assert normalize_tone("  Shocking  ") == "shocking"


def test_synonym_infuriating_to_controversial() -> None:
    assert normalize_tone("infuriating") == "controversial"


def test_synonym_moving_and_emotional_to_touching() -> None:
    assert normalize_tone("moving") == "touching"
    assert normalize_tone("emotional") == "touching"
    assert normalize_tone("deeply moving") == "touching"  # мультисловное → по токену


def test_unknown_falls_back_to_documented_default() -> None:
    assert normalize_tone("banana") == _DEFAULT
    assert normalize_tone("zzzz-unknown") == _DEFAULT


def test_none_and_empty_return_none() -> None:
    assert normalize_tone(None) is None
    assert normalize_tone("") is None
    assert normalize_tone("   ") is None
