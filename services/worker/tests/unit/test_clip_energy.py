"""B4: clip_energy — реальная энергия клипа из RMS-кривой громкости (pure, TDD).

Кривая — та же, что отдаёт preview_moments.extract_loudness: list[float] (RMS в dB по окнам
hop_s=0.5 сек). Энергия = насколько окно клипа громче МЕДИАНЫ всего видео, нормировано на размах
``(v - med)/(hi - med)`` и заклампано в [0,1]. Громкое окно → ~1.0, тихое → ~0.0. Нет кривой /
нет сэмплов в окне → None (явная ветка «нет аудио-сигнала», не тихий 0).
"""

from __future__ import annotations

from app.pipeline.clip_energy import clip_energy

# 0–5 сек тихо (−40 dB), 5–10 сек громко (−10 dB); hop 0.5 сек → 20 сэмплов.
# median = −25 dB, hi = −10 dB, размах = 15 dB.
_QUIET_DB = -40.0
_LOUD_DB = -10.0
_CURVE = [_QUIET_DB] * 10 + [_LOUD_DB] * 10


def test_loud_window_is_near_one() -> None:
    e = clip_energy(_CURVE, 5.0, 9.5)
    assert e is not None
    assert e >= 0.95


def test_quiet_window_is_near_zero() -> None:
    e = clip_energy(_CURVE, 0.0, 4.5)
    assert e is not None
    assert e <= 0.05


def test_result_always_within_unit_interval() -> None:
    for start, end in [(0.0, 4.5), (5.0, 9.5), (0.0, 9.5), (2.0, 7.0)]:
        e = clip_energy(_CURVE, start, end)
        assert e is not None
        assert 0.0 <= e <= 1.0


def test_none_curve_returns_none() -> None:
    assert clip_energy(None, 0.0, 5.0) is None


def test_empty_curve_returns_none() -> None:
    assert clip_energy([], 0.0, 5.0) is None


def test_window_with_no_samples_returns_none() -> None:
    # окно целиком ЗА пределами кривой (клип позже конца RMS-семплов) → нет сэмплов → None
    assert clip_energy(_CURVE, 100.0, 110.0) is None


def test_flat_curve_returns_none() -> None:
    # ровная громкость → нет динамического размаха (hi == med) → нормировать нечем → None
    assert clip_energy([-20.0] * 12, 0.0, 5.0) is None
