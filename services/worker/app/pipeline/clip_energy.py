"""B4: реальная энергия клипа из RMS-кривой громкости источника. PURE.

Пайплайн уже считает RMS-кривагу громкости всего видео (``preview_moments.extract_loudness`` →
``list[float]`` в dB по окнам ``hop_s`` сек). ``clip_energy`` берёт окно ОДНОГО клипа и отвечает,
насколько оно ГРОМЧЕ типичного уровня видео — нормируя относительно СОБСТВЕННОГО распределения
громкости источника (как ``detect_energy_moments``: intensity = ``(v - med)/(hi - med)``,
preview_moments.py:116), заклампано в [0,1]. Громкое окно → ~1.0, тихое → ~0.0.

Значение уходит в ``ClipOut.energy`` и рисует «энергетический» бар в карточке клипа (ClipCard).
``None`` — когда кривой нет (напр. провайдер без RMS) или в окне клипа нет ни одного сэмпла: это
ЯВНАЯ ветка «нет аудио-сигнала», НЕ тихий 0 (правило №8) — фронт откатывается на tone-эвристику.
"""

from __future__ import annotations

import statistics


def clip_energy(
    loudness: list[float] | None, start: float, end: float, *, hop_s: float = 0.5
) -> float | None:
    """RMS-кривая громкости + окно клипа [start, end] сек → энергия 0..1 (или None). PURE.

    ``loudness`` — dB по непересекающимся окнам ``hop_s`` сек (индекс i ≈ момент ``i*hop_s`` сек),
    как в ``preview_moments.extract_loudness``. Энергия = средняя громкость окна клипа, нормиро-
    ванная относительно РАСПРЕДЕЛЕНИЯ громкости всего видео: ``(mean_window - median_all)/(max_all -
    median_all)``, заклампано в [0,1]. Возвращает:
      • ``None`` — кривая пуста/None (нет RMS-сигнала);
      • ``None`` — в окне клипа нет ни одного сэмпла (клип за пределами кривой);
      • ``None`` — кривая ровная (max == median): нормировать нечем, динамического размаха нет;
      • иначе float в [0,1].
    """
    if not loudness:
        return None
    n = len(loudness)
    lo = max(0, int(start / hop_s)) if hop_s > 0 else 0
    hi = min(n - 1, int(end / hop_s)) if hop_s > 0 else n - 1
    if lo > hi or lo >= n:
        return None
    window = loudness[lo : hi + 1]
    if not window:
        return None
    med = statistics.median(loudness)
    top = max(loudness)
    denom = top - med
    if denom <= 0:  # ровная громкость → нет размаха для нормировки (не делим на ноль)
        return None
    v = statistics.mean(window)
    return round(min(1.0, max(0.0, (v - med) / denom)), 3)
