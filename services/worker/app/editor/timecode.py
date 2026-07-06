"""Pure таймкод-конверсия для editor-handoff экспорта (EDL / FCPXML / markers).

Единственная точка кадровой/таймкод-математики экспорта. Всё чистое (вход→выход, без I/O),
покрыто unit-тестами (`tests/unit/test_timecode.py`) — CLAUDE.md «TDD на pure-логике».

Три источника коварства, ради которых этот модуль существует отдельно:
  1. **Drop-frame.** NTSC-семейства 29.97 (30000/1001) и 59.94 (60000/1001) считают таймкод в
     drop-frame: каждую минуту пропускают 2 (для 30) / 4 (для 60) НОМЕРА кадров, КРОМЕ каждой
     10-й минуты — так таймкод-«часы» не уезжают от реального времени. 23.976/24/25/30/50/60 —
     non-drop. Разделитель кадров в TC: ';' у drop-frame, ':' у non-drop (SMPTE-конвенция, её
     читают Premiere/Resolve/FCP).
  2. **Точный fps.** meta.json хранит fps БЕЗ округления (30000/1001 = 29.97002997…, см.
     stage0_import.parse_fps) — округление накопило бы ошибку в кадр на длинном видео. Здесь мы
     тоже НЕ округляем вход; `nominal_rate` (30/25/…) берём как round(fps) только для РАЗМЕТКИ
     таймкода (число кадро-номеров в секунде).
  3. **floor-in / ceil-out.** Чтобы слово НИКОГДА не обрезалось на границе кадра: IN-точку
     округляем ВНИЗ (клип стартует НЕ ПОЗЖЕ слова), OUT-точку — ВВЕРХ (клип кончается НЕ РАНЬШЕ
     слова). См. `seconds_to_frames`.
"""

from __future__ import annotations

import math

# Номинальные целые рейты, чьи 1000/1001-варианты считаются drop-frame (только 30- и 60-семейства).
_DF_NOMINALS = (30.0, 60.0)
# Допуск сравнения дробных рейтов (29.97002997 vs 30000/1001) — с запасом от float-джиттера, но
# уже разрыва до соседнего рейта (29.97 vs ровно 30 = 0.03).
_RATE_TOL = 0.01
# Эпсилон для floor/ceil на РОВНЫХ кадровых границах (t*fps = 60.0000001 или 59.9999999 из-за
# float) — чтобы ceil(60.0) не дал 61, а floor(60.0) не дал 59.
_FRAME_EPS = 1e-6


def is_drop_frame(fps: float) -> bool:
    """True ⇔ fps принадлежит drop-frame семейству (29.97 или 59.94). PURE.

    Только 30000/1001 и 60000/1001 используют drop-frame таймкод. Ровно 30/60, 25, 24 и
    23.976 (24000/1001) — non-drop.
    """
    return any(abs(fps - n * 1000.0 / 1001.0) < _RATE_TOL for n in _DF_NOMINALS)


def nominal_rate(fps: float) -> int:
    """Число кадро-НОМЕРОВ в таймкод-секунде (30 для 29.97, 25→25, 24 для 23.976). PURE."""
    return int(round(fps))


def seconds_to_frames(t: float, fps: float, *, rounding: str = "round") -> int:
    """Секунды → индекс кадра при `fps`. PURE.

    Политика округления (чтобы слово НЕ обрезалось на границе кадра):
      - ``"floor"`` — IN-точки: стартуем НЕ ПОЗЖЕ слова (смещение назад по времени);
      - ``"ceil"``  — OUT-точки: кончаем НЕ РАНЬШЕ слова (смещение вперёд);
      - ``"round"`` — ближайший кадр (дефолт; для отображаемых таймкодов). Round-half-up
        (``floor(x+0.5)``) — детерминированно, без banker's-rounding сюрпризов в тестах.

    Эпсилон гасит float-джиттер на РОВНЫХ границах (2.0с @30 = ровно 60 кадров при любой политике).
    """
    x = t * fps
    if rounding == "floor":
        return int(math.floor(x + _FRAME_EPS))
    if rounding == "ceil":
        return int(math.ceil(x - _FRAME_EPS))
    if rounding == "round":
        return int(math.floor(x + 0.5))
    raise ValueError(f"unknown rounding policy: {rounding!r}")


def _frames_to_df_tc(frame: int, fps: float) -> str:
    """Номер кадра → drop-frame таймкод ``HH:MM:SS;FF``. Классический SMPTE-алгоритм.

    Каждую минуту «выкидываем» `drop` НОМЕРОВ кадров (2 для 30, 4 для 60), кроме каждой 10-й —
    так номер кадра догоняет реальное время. `frame` здесь — счётчик РЕАЛЬНЫХ кадров (0-based).
    """
    r = nominal_rate(fps)  # 30 или 60
    drop = int(round(fps * 0.066666))  # 2 (для 29.97) или 4 (для 59.94)
    frames_per_min = r * 60 - drop  # кадро-номеров в «дропнутой» минуте (1798 / 3596)
    frames_per_10min = frames_per_min * 9 + r * 60  # 9 дропнутых + 1 полная (17982 / 35964)
    frames_per_24h = frames_per_10min * 6 * 24
    frame %= frames_per_24h  # wrap суток (TC цикличен)
    d = frame // frames_per_10min
    m = frame % frames_per_10min
    if m > drop:
        frame += drop * 9 * d + drop * ((m - drop) // frames_per_min)
    else:
        frame += drop * 9 * d
    ff = frame % r
    ss = (frame // r) % 60
    mm = (frame // (r * 60)) % 60
    hh = frame // (r * 3600)
    return f"{hh:02d}:{mm:02d}:{ss:02d};{ff:02d}"


def frames_to_tc(frame: int, fps: float) -> str:
    """Номер кадра → SMPTE-таймкод. ``HH:MM:SS:FF`` (NDF) / ``HH:MM:SS;FF`` (DF). PURE.

    Отрицательный кадр клэмпится в 0 (защита от битого ввода — правило №8: не тихий отрицательный
    таймкод, а явный ноль).
    """
    frame = max(0, int(frame))
    if is_drop_frame(fps):
        return _frames_to_df_tc(frame, fps)
    r = nominal_rate(fps)
    ff = frame % r
    total_s = frame // r
    ss = total_s % 60
    mm = (total_s // 60) % 60
    hh = total_s // 3600
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def seconds_to_tc(t: float, fps: float) -> str:
    """Секунды → SMPTE-таймкод (DF/NDF по `fps`). PURE. Округление — ближайший кадр."""
    return frames_to_tc(seconds_to_frames(t, fps, rounding="round"), fps)


def frame_duration_rational(fps: float) -> str:
    """fps → FCPXML ``frameDuration`` (рациональная длительность кадра). PURE.

    NTSC → ``1001/<nominal*1000>s`` (напр. ``1001/30000s`` для 29.97); целочисленный →
    ``1/<rate>s`` (напр. ``1/25s``). FCPXML требует рациональную кадровую длительность, чтобы
    времена ложились на кадровую сетку без дрейфа.
    """
    for nominal in (24, 30, 60):
        if abs(fps - nominal * 1000.0 / 1001.0) < _RATE_TOL:
            return f"1001/{nominal * 1000}s"
    r = nominal_rate(fps)
    if abs(fps - r) < _RATE_TOL:
        return f"1/{r}s"
    # Нестандартный дробный рейт — общий рациональный fallback (миллисекундный знаменатель).
    return f"1000/{round(fps * 1000)}s"


def frame_time_rational(frames: int, fps: float) -> str:
    """Число кадров → FCPXML рациональное время, выровненное по кадровой сетке. PURE.

    Кратно `frame_duration_rational`, чтобы время НЕ уезжало с сетки:
    NTSC → ``<frames*1001>/<nominal*1000>s``; целочисленный → ``<frames>/<rate>s``; ноль → ``0s``.
    """
    if frames <= 0:
        return "0s"
    for nominal in (24, 30, 60):
        if abs(fps - nominal * 1000.0 / 1001.0) < _RATE_TOL:
            return f"{frames * 1001}/{nominal * 1000}s"
    r = nominal_rate(fps)
    if abs(fps - r) < _RATE_TOL:
        return f"{frames}/{r}s"
    return f"{frames * 1000}/{round(fps * 1000)}s"
