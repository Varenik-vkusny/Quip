"""ffmpeg `silencedetect` → аудио-подтверждённые тихие интервалы (гибридный паузрез). Impure (I/O).

Гибрид (ресёрч 2026-07-12): word-gap решает ЧТО резать, а ЭТИ регионы уточняют ГДЕ (точный стык по
реальной громкости, не по ASR-таймингу) + отсекают ложные гэпы. Прогоняем только по спану клипа
(короткий), не по всему источнику. Сбой ffmpeg → [] (паузрез падает на чистый word-gap — не тихий
фолбэк: причина логируется числом регионов, поведение корректно-консервативное).
"""

from __future__ import annotations

import re
import subprocess

_SIL_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SIL_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


def detect_silence_regions(
    video_path: str,
    start: float,
    end: float,
    *,
    noise_db: float = -34.0,
    min_silence_sec: float = 0.12,
) -> list[tuple[float, float]]:
    """Тихие интервалы (АБС. source-сек) в [start, end] по ffmpeg silencedetect. [] при сбое/пусто.

    `-ss start -t dur` перед `-i` сбрасывает output-PTS в 0 → тайминги silencedetect относительны
    сегмента, прибавляем `start`. noise_db/min_silence — порог громкости и мин. длительность тишины.
    """
    dur = end - start
    if dur <= 0:
        return []
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
        "-i", video_path, "-map", "0:a:0",
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence_sec}",
        "-f", "null", "-",
    ]  # fmt: skip
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (subprocess.SubprocessError, OSError):
        return []
    log = proc.stderr or ""
    starts = [float(m) + start for m in _SIL_START.findall(log)]
    ends = [float(m) + start for m in _SIL_END.findall(log)]
    regions: list[tuple[float, float]] = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else end  # хвостовой silence_start без end → до конца спана
        if e > s:
            regions.append((s, e))
    return regions
