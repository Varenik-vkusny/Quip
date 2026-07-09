"""Гейт «извлечь новый клип из неотобранного момента» (PURE, TDD).

Экономика (одобрено фаундером):
  • Извлечение БЕСПЛАТНО, пока общее число клипов джоба ≤ cap (дефолт 30) — это в рамках уже
    оплаченного энтайтлмента «до 30 клипов на видео», нового риска маржи нет.
  • За cap → жёсткий per-clip пейволл (платишь за этот конкретный лишний клип).
  • Извлечение доступно ТОЛЬКО платным Pro/Studio; Free/Starter видят карту, но не извлекают.

Обе функции — чистые (без БД): нет persist-dedup ловушек, тесты гоняем напрямую.
"""

from __future__ import annotations

import types

from app import billing
from app.billing import can_extract_clip, extract_requires_payment


class TestCanExtractClip:
    """Гейт по плану: извлекать может ТОЛЬКО pro/studio; всё остальное → False."""

    def test_paid_extract_tiers_can(self) -> None:
        assert can_extract_clip("pro") is True
        assert can_extract_clip("studio") is True

    def test_free_and_starter_cannot(self) -> None:
        # Free/Starter видят карту моментов, но извлечение — платная фича (Pro+).
        assert can_extract_clip("free") is False
        assert can_extract_clip("starter") is False

    def test_unknown_or_empty_cannot(self) -> None:
        # Безопасный дефолт: неизвестный/пустой план НЕ получает платную фичу.
        assert can_extract_clip("garbage") is False
        assert can_extract_clip("") is False


class TestExtractRequiresPayment:
    """Порог платности: клипов < cap → бесплатно; ≥ cap → per-clip пейволл."""

    def test_below_cap_is_free(self) -> None:
        # 0..(cap-1) клипов — ещё в бесплатном энтайтлменте.
        for n in range(0, 30):
            assert extract_requires_payment(n, cap=30) is False

    def test_at_cap_requires_payment(self) -> None:
        # Ровно на cap → следующий клип уже платный (общее число станет > энтайтлмента).
        assert extract_requires_payment(30, cap=30) is True

    def test_above_cap_requires_payment(self) -> None:
        assert extract_requires_payment(31, cap=30) is True
        assert extract_requires_payment(99, cap=30) is True

    def test_explicit_cap_override_respected(self) -> None:
        # Явный cap= переопределяет дефолт (напр. персональный/тарифный лимит).
        assert extract_requires_payment(4, cap=5) is False
        assert extract_requires_payment(5, cap=5) is True
        assert extract_requires_payment(9, cap=10) is False
        assert extract_requires_payment(10, cap=10) is True

    def test_default_cap_reads_config_knob(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        # cap=None → берётся config-кнопка extract_free_cap (с env-override). Мокаем get_settings.
        monkeypatch.setattr(
            billing, "get_settings", lambda: types.SimpleNamespace(extract_free_cap=30)
        )
        assert extract_requires_payment(29) is False
        assert extract_requires_payment(30) is True

    def test_default_cap_honors_env_override(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        # Если фаундер поднял EXTRACT_FREE_CAP → дефолтная ветка это уважает (через get_settings).
        monkeypatch.setattr(
            billing, "get_settings", lambda: types.SimpleNamespace(extract_free_cap=50)
        )
        assert extract_requires_payment(49) is False
        assert extract_requires_payment(50) is True
