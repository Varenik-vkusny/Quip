"""Посегментная нарезка source для контейнерного фан-аута рендера.

Проблема: сегодня КАЖДЫЙ quip-render контейнер качает ВЕСЬ source (до десятков ГБ) на свой
эфемерный диск, чтобы отрендерить ОДИН клип. Размер контейнера ∝ размеру source → под региональной
квотой диска влезает мало контейнеров → параллельность зажата (см. docs про ephemeral-disk quota).

Решение: координатор (у него source уже локально) режет КАЖДОМУ клипу маленький сегмент
**stream-copy `-c copy`** (кадры БАЙТ-идентичны источнику, НУЛЬ переэнкода) от ближайшего кейфрейма
≤ seg.start; контейнер качает только свой сегмент (десятки МБ, НЕЗАВИСИМО от длины source).

⚠️ ИНВАРИАНТ КАДРОВОЙ СЕТКИ (docs/REFRAME_FPS_GRID_INVARIANT.md). Извлечение сегмента СДВИГАЕТ
origin времени — ровно то, что защищает инвариант. Кадр 0 сегмента ЕСТЬ кадр K источника (K —
ЦЕЛЫЙ индекс, source — CFR с ТОЧНЫМ fps). shift_segment пере-якорит клип покадрово-точно; stage3/
stage5 НЕ трогаем (только вход: файл + сдвинутый Segment). См. shift_segment про banker's-rounding
gotcha и test_segment_extract.py (grid-доказательство). ⚠️ бит-идентичность НЕ доказуема одним
анализом (см. shift_segment «остаточный риск») → ОБЯЗАТЕЛЬНА покадровая render-проверка.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

from app.errors import JobError
from app.models import Segment

_STAGE = "segment"

# Хвостовой запас сегмента: _refine_cut_native читает до ~window=4 кадров ПОСЛЕ склейки-кандидата,
# снапая её на реальный контент-край. На полном source за seg.end всегда есть кадры; на извлечённом
# сегменте, обрывающемся ровно на seg.end, край мог бы снапнуться иначе. 2с >> window/fps (≈0.13с)
# на любом fps флота → окно снапа полное. seg.end в reframe/render передаём БЕЗ этого запаса —
# сегмент просто несёт лишний хвост (как слак полного source сегодня).
TRAILING_MARGIN_SEC = 2.0
_DEFAULT_LOOKBACK_SEC = 30.0
_MAX_LOOKBACK_SEC = 300.0


# ─────────────────────────── ЧИСТАЯ математика (Δ=0) ───────────────────────────
def parse_keyframe_pts(stdout: str) -> list[float]:
    """PURE. ffprobe `-show_entries packet=pts_time,flags -of csv=p=0` → отсортированные pts_time
    ТОЛЬКО кейфрейм-пакетов (flags содержит 'K'). N/A pts и мусор пропускаем."""
    times: list[float] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 2 or "K" not in parts[1]:
            continue
        try:
            times.append(float(parts[0]))
        except ValueError:
            continue  # pts_time = "N/A" и т.п.
    return sorted(times)


def preceding_keyframe(keyframe_times: Sequence[float], t: float) -> float:
    """PURE. Наибольший кейфрейм ≤ t. Нет такого / пусто → 0.0 (кадр 0 всегда кейфрейм; безопасно:
    0 ≤ seg.start). НИКОГДА не возвращаем кейфрейм ПОСЛЕ t (иначе потеряем контент клипа)."""
    before = [k for k in keyframe_times if k <= t + 1e-9]
    return max(before) if before else 0.0


def snap_to_frame(t: float, fps: float) -> float:
    """PURE. Снап времени на ТОЧНУЮ кадровую границу нативной сетки: round(t·fps)/fps. ffprobe pts
    округлён до микросекунд; снап возвращает точное K/fps → сдвиг сегмента = целое число кадров."""
    return round(t * fps) / fps


def probe_window(seg_start: float, *, lookback_sec: float, duration: float) -> tuple[float, float]:
    """PURE. Окно ffprobe-скана кейфреймов: [max(0,seg_start−lookback), min(duration,seg_start)]."""
    return (max(0.0, seg_start - lookback_sec), min(duration, seg_start))


def extraction_end(seg_end: float, *, trailing_margin_sec: float, duration: float) -> float:
    """PURE. Правый край извлечения = seg.end + хвостовой запас, зажат длиной source."""
    return min(seg_end + trailing_margin_sec, duration)


def shift_segment(seg: Segment, keyframe_time: float, fps: float) -> Segment:
    """PURE. Пере-якорить Segment на сегмент-origin (кейфрейм) ПОКАДРОВО-ТОЧНО.

    ⚠️ Наивный `seg.start − keyframe_time` НЕ Δ=0: Python `round()` — БАНКОВСКОЕ (half-to-even),
    НЕ трансляционно-инвариантное → `round(x−N) ≠ round(x)−N` на ТОЧНЫХ полукадрах (напр.
    17.35·30=520.5→520, но (17.35−499/30)·30=21.5→22; 520−499=21≠22 → ±1 кадр). Документировано
    test_naive_shift_breaks_at_half_frame.

    Фикс: (1) старт СНАПАЕМ на кадр `(a−k)/fps` → `round(new_start·fps)=a−k` ТОЧНО (a−k целое);
    reframe и так снапает старт (aligned_start), клип тот же. (2) конец несёт ТОТ ЖЕ span-float
    `seg.end − a/fps`, которым reframe считает `(end − aligned_start)` → total_frames бит-в-бит.
    k=round(keyframe_time·fps) (кейфрейм = целый кадр), a=round(seg.start·fps).
    ⚠️ ОСТАТОЧНЫЙ риск: render `-t = seg.start+clip_dur−aligned_start` несёт суб-кадровый остаток
    сырого старта → бит-идентичность НЕ доказуема одним анализом; ОБЯЗАТЕЛЬНА покадровая
    render-проверка (tmp/verify_segment_extraction.py — TODO)."""
    k = round(keyframe_time * fps)
    a = round(seg.start * fps)
    new_start = (a - k) / fps
    span = seg.end - a / fps  # то же float-выражение, что reframe (end − aligned_start)
    return seg.model_copy(update={"start": new_start, "end": new_start + span})


# ─────────────────────────── I/O (ffprobe / ffmpeg) ───────────────────────────
def _ffprobe_keyframes(source: Path, lo: float, hi: float) -> str:
    """Список pts_time,flags видеопакетов в окне [lo, hi]. JobError при сбое (№8)."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-read_intervals", f"{lo}%{hi}",
        "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", str(source),
    ]  # fmt: skip
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise JobError(_STAGE, f"ffprobe not found: {e}") from e
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-300:]
        raise JobError(_STAGE, f"ffprobe keyframes exit {proc.returncode}: {tail}")
    return proc.stdout


def find_preceding_keyframe(
    source: Path,
    seg_start: float,
    duration: float,
    fps: float,
    *,
    lookback_sec: float = _DEFAULT_LOOKBACK_SEC,
    max_lookback_sec: float = _MAX_LOOKBACK_SEC,
) -> float:
    """Точное время (K/fps) ближайшего кейфрейма ≤ seg_start. Скан окна расширяется вдвое при
    промахе (большой GOP), пока не найдём или не упрёмся в max_lookback → JobError (№8: не гадаем).
    Возвращает СНАПНУТОЕ на кадр значение — точный origin для сдвига (см. shift_segment)."""
    lb = lookback_sec
    while True:
        lo, hi = probe_window(seg_start, lookback_sec=lb, duration=duration)
        kts = parse_keyframe_pts(_ffprobe_keyframes(source, lo, hi))
        if kts:
            return snap_to_frame(preceding_keyframe(kts, seg_start), fps)
        if lo <= 0.0 or lb >= max_lookback_sec:
            raise JobError(
                _STAGE,
                f"no keyframe within {max_lookback_sec}s before {seg_start:.3f}s in {source.name}",
            )
        lb *= 2


def extract_segment(source: Path, dest: Path, keyframe_time: float, end: float) -> None:
    """Lossless stream-copy сегмента [keyframe_time, end] source→dest. `-ss` на КЕЙФРЕЙМЕ +
    `-c copy` → кадры байт-идентичны, ноль переэнкода. `-avoid_negative_ts make_zero` → PTS
    сегмента с 0 (кадр 0 = кадр keyframe·fps источника). JobError при сбое / нет выхода (№8)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-ss", f"{keyframe_time:.6f}", "-i", str(source),
        "-t", f"{max(0.0, end - keyframe_time):.6f}",
        "-c", "copy", "-avoid_negative_ts", "make_zero", "-map", "0", str(dest),
    ]  # fmt: skip
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise JobError(_STAGE, f"ffmpeg not found: {e}") from e
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-400:]
        raise JobError(_STAGE, f"segment extract exit {proc.returncode}: {tail}")
    if not dest.exists() or dest.stat().st_size == 0:
        raise JobError(_STAGE, f"segment extract produced no output: {dest}")
