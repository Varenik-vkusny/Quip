"""Stage 3 (Reframe): source.mp4 + сегмент → reframe_<clip_id>.json (per-shot, cut-aligned).

Главный путь (ASD): detect_scene_cuts → build_shots_frames → score_tracks_in_segment
→ plan_regions → merge_short_regions → [TrackRegion] (frame-accurate, no flashes).

Редактор-путь (reframe_cache.py): sample_faces_continuous + detect_cuts → build_shots
→ build_regions_from_shots (секунды, V2-legacy).

Каждый шот = стабильный mode (fill/fit) + центр говорящего (plan_regions). Короткие шоты
(<1.5с) поглощаются соседним (merge_short_regions). Per-shot границы = реальные склейки.

Границы: PURE-математика изолирована (unit-тесты); I/O (ffmpeg/MediaPipe) — обёртки, JobError.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.errors import JobError
from app.models import CropWindow
from app.pipeline.speaker_switch import (
    NO_ASD,
    WIDE,
    SourceBinding,
    SpeakerTurn,
    SwitchConfig,
    bind_lanes,
    build_turns,
    clip_binding_from_source,
    collect_votes,
    match_identities,
    plan_switches,
    resolve_binding,
)

if TYPE_CHECKING:
    from app.models import Word
    from app.pipeline.stage2b_vision_review import VisionShot

_STAGE = "reframe"
_ASPECT_W, _ASPECT_H = 9, 16
# Минимальная ширина лица (доля кадра), при которой кроп в 9:16 оправдан без явной речи.
# Лицо уже этого И молчащее = неуверенный субъект → не кропим, держим горизонталь (fit).
# Кноба нет намеренно (blast radius = один файл); тюним правкой константы.
_MIN_FACE_FRAC = 0.08
# Порог «широкого» плана = размах центров лиц > _WIDE_SPREAD_RATIO * ширины 9:16-кропа.
# Раньше брали ПОЛНУЮ ширину кропа (crop_w_frac ≈ 0.317 на 1080p) — слишком строго: типичный
# 2-shot интервью даёт размах лиц ~0.26–0.29 (репро на реальном видео Tom Holland×Jay Shetty:
# 0.258/0.272/0.284/0.288) — ВСЕ ниже 0.317 → ИИ кропил одного, терял второго. Половина ширины
# кропа (0.5) = граница, за которой второе лицо физически вне центрированного кропа; 0.70 — с
# запасом против дрожания детектора: ловит 0.26+ как wide, отвергает кластер/одно лицо (~0.13).
_WIDE_SPREAD_RATIO = 0.55
# Одиночное лицо со средним ASD speak НИЖЕ этого = уверенно НЕ говорит (B-roll / реакция /
# широкий establishing-план: реклайн) → fit (wide), а НЕ вертикальный strip по телу/ногам.
# Репро Tom Holland: реклайн-планы молчат (speak ≈ −1.7..−2.0), говорящие головы ≥ ~0 → −0.6
# разделяет «широкий план» и «говорящую голову». Тюнинг качества, кадровую сетку не трогает.
_SILENCE_WIDE_MAX = -0.6
# Молчание уводит в wide ТОЛЬКО мелкое/среднее лицо (< этого). Крупный план (≥) = говорящая/
# слушающая ГОЛОВА → fill, даже молча (фаундер: «в кадре один человек — наводись на лицо, даже если
# молчит»). Настоящий широкий/establishing/legs-план имеет МЕЛКОЕ лицо (уходит в fit по размеру);
# молчащая крупная РЕАКЦИЯ (интервьюер слушает, лицо ~0.15) прежде ошибочно уходила в wide. 0.12 =
# уверенный close/medium (лица Cardi B 0.14–0.18, далёкие < 0.10). Кадровую сетку не трогает.
_SILENCE_MIN_FILL_FACE = 0.12
# Hybrid-наведение: если новый говорящий БЛИЗКО к центру прошлого fill-шота (|Δcx| ≤ этого) — EMA
# ДОЕЗЖАЕТ от прошлого центра (плавный пан); если ДАЛЬШЕ (другая камера/спикер) — СНАП на лицо
# (иначе кроп откроется «пол-лица»). ≈ доля, при которой лицо выходит за 9:16-кроп.
_GLIDE_MAX_DCX = 0.10
# ASD пропускается при ОДНОЙ дорожке в сегменте (perf, asd_reframe._SILENT=-9.0) → speak=-9 = «скор
# НЕ считался», а НЕ «молчит»: один однозначный говорящий → это сильнейший сигнал для FILL. Поэтому
# silence-gate (→ wide) НЕ срабатывает на сентинеле: требуем РЕАЛЬНЫЙ отрицательный скор (> этого).
_ASD_NODATA = -5.0
# Мин. покрытие шота дорожкой, чтобы лицо считалось СУБЪЕКТОМ для fill-кропа. Ниже — лица по сути
# нет (хвост дорожки заполз за склейку / мелькнувшее лицо в establishing-плане) → fit (wide), а НЕ
# tight-кроп в «ноги»/торс. Репро CARDI B: говорящий бьётся 3 кадра (1.3%) в 7.9с B-roll → кроп ног.
# Совпадает с coverage_min гейта _is_wide_shot (чистый разрыв 1–6% ↔ 35–100% покрытия).
_FILL_MIN_COVERAGE = 0.25
# B2 (exploratory, флаг REFRAME_VISION_SUBJECT): максимум |mean_cx(track) − subject_cx|, при котором
# vision-подсказка считается «про этот трек». Gemini-box груб/дрожит (per-clip окно, не per-frame) →
# берём его ТОЛЬКО чтобы ВЫБРАТЬ трек (тай-брейкер speak/width), НЕ чтобы двигать кроп. Если ни один
# трек не ближе этого к подсказке — субъект, вероятно, НЕ-лицо (вне контракта face-track) → честный
# фолбэк на speak/width (не тянем кроп к «ничему»). ~0.20 = запас против дрожания бокса.
_VISION_SUBJECT_MAX_DIST = 0.20


# ─────────────────── Фазовые тайминги (observability, [reframe]-строка) ───────────────────

# Per-thread копилка длительностей фаз reframe: наполняют detect_scene_cuts (scene_detect/
# refine) и asd_reframe.score_tracks_in_segment (sample/detect/asd), печатает ОДНОЙ строкой
# reframe_segment. thread-local ОБЯЗАТЕЛЕН: клипы рендерятся параллельными потоками
# (RENDER_CONCURRENCY) — модульный dict перемешал бы фазы разных клипов.
_PHASE_TIMINGS = threading.local()


def _phase_reset() -> None:
    """Начать сбор фазовых таймингов reframe для ТЕКУЩЕГО потока (зовёт reframe_segment)."""
    _PHASE_TIMINGS.d = {}


def _phase_add(name: str, seconds: float) -> None:
    """Прибавить длительность фазы (сек) в копилку текущего потока.

    Копилки нет (детектор/ASD позваны ВНЕ reframe_segment: main.py:_tight_lock_cx,
    reframe_cache) → замер некуда сводить, пропускаем. Это штатный режим сбора, не
    фолбэк-на-ошибке (правило «no silent fallbacks» не про это).
    """
    d: dict[str, float] | None = getattr(_PHASE_TIMINGS, "d", None)
    if d is not None:
        d[name] = d.get(name, 0.0) + seconds


def _phase_get(name: str) -> float:
    """Суммарная длительность фазы (сек) в текущем потоке; фаза не бегала → 0.0."""
    d: dict[str, float] = getattr(_PHASE_TIMINGS, "d", {})
    return d.get(name, 0.0)


# ─────────────────────────── Dataclasses ───────────────────────────


@dataclass(frozen=True)
class TrackPoint:
    """Одна точка сглаженной траектории (V2). t — клип-относительные секунды (0-based)."""

    t: float
    mode: str  # "fill" | "fit"
    cx: float | None  # fill: сглаженный центр X (доля кадра); fit: None


@dataclass(frozen=True)
class TrackRegion:
    """Непрерывный регион с одним режимом (V2).

    fill: points — сглаженная траектория cx (build_fill_crop_expr → ffmpeg expr);
    fit: points=() (весь кадр + блюр-рамки, нет траектории);
    split (v3): points = траектория ВЕРХНЕГО спикера (меньший cx),
    points_b = траектория НИЖНЕГО (два кропа 1080×960 + vstack).
    """

    t0: float
    t1: float
    mode: str  # "fill" | "fit" | "split"
    points: tuple[TrackPoint, ...]  # fill/split: значимые cx; fit: пустой tuple
    points_b: tuple[TrackPoint, ...] = ()  # только split: второй спикер
    fit_style: str = "blur_zoom"


@dataclass(frozen=True)
class SpeakerTrack:
    """Дорожка лица с ASD-скором (выход Pass-1 анализа, вход plan_regions).

    f0/f1 — клип-относительные КАДРЫ (полуинтервал [f0, f1)). cx — per-frame X-центр (доля
    кадра), len == f1-f0. width — средняя ширина лица (доля; для largest-face фолбэка и wide).
    speak — средний ASD speak-score (>0 ≈ говорит).
    """

    f0: int
    f1: int
    cx: tuple[float, ...]
    width: float
    speak: float
    # Per-frame ASD speak-score @track fps (turn-level binding спикер↔лицо, speaker_switch).
    # Выровнен по ПЕРВЫМ len(speak_frames) кадрам дорожки (score_track усекает хвост до length*fps).
    # () = ASD не считался (single-track skip) ИЛИ back-compat конструкция без per-frame. Скаляр
    # `speak` = mean этого массива (совместимость). Ресемплится в resample_track вместе с cx.
    speak_frames: tuple[float, ...] = ()
    # Face-reID отпечаток ЛИЧНОСТИ (512-d L2-норм, mean по кропам дорожки) для build_identities:
    # кластеризация треков по ВНЕШНОСТИ, устойчиво к смене камер/позиций. () = не считался
    # (не speaker-режим). НЕ per-frame → resample_track переносит как есть.
    embedding: tuple[float, ...] = ()


# MediaPipe Tasks FaceDetector
_FACE_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_detector/"
    "blaze_face_short_range/float16/1/blaze_face_short_range.tflite"
)
# stage3_reframe.py → parents[1] = app/
_FACE_MODEL_PATH = Path(__file__).resolve().parents[1] / "assets" / "blaze_face_short_range.tflite"


# ─────────────────────────── pure-математика (unit-тесты) ───────────────────────────


def compute_crop_window(src_w: int, src_h: int, center_x_frac: float, *, t: float) -> CropWindow:
    """9:16 static-окно по центру лица. x клипуется в [0, src_w-w]; y=0; h=src_h.

    JobError при нулевых размерах или если source уже 9:16-окна (кроп невозможен).
    """
    if src_w <= 0 or src_h <= 0:
        raise JobError(_STAGE, f"invalid source dimensions: {src_w}x{src_h}")
    w = round(src_h * _ASPECT_W / _ASPECT_H)
    h = src_h
    if w > src_w:
        raise JobError(_STAGE, f"source {src_w}x{src_h} is narrower than the 9:16 window ({w}px)")
    cx = min(1.0, max(0.0, center_x_frac))
    x = round(cx * src_w - w / 2)
    x = max(0, min(x, src_w - w))
    return CropWindow(t=t, x=x, y=0, w=w, h=h)


def shot_is_wide(
    frame_centers: list[list[float]], *, crop_w_frac: float, wide_ratio: float = 0.5
) -> bool:
    """Шот «широкий»: 2+ разнесённых лица (размах > ширины 9:16-кропа) → fit обоих.

    frame_centers — x-центры лиц по сэмпл-кадрам (только кадры С лицами). Кадр широкий =
    ≥2 лица И размах > crop_w_frac. Шот широкий, если таких кадров ≥ wide_ratio. Пусто → False.
    """
    if not frame_centers:
        return False
    wide = sum(1 for cxs in frame_centers if len(cxs) >= 2 and (max(cxs) - min(cxs)) > crop_w_frac)
    return wide >= wide_ratio * len(frame_centers)


def smooth_centers(
    samples: list[float | None], smoothing: float = 0.15, *, init: float = 0.5
) -> list[float]:
    """Exponential smoothing по X-центрам лиц (как в прототипе). None=нет лица → держим.

    cx_smooth[i] = cx_smooth[i-1] + smoothing * (cx_raw[i] - cx_smooth[i-1]).
    Нет лица → держим последний сглаженный (или init если ни одного ещё не было).
    init=0.5 по умолчанию; передай первый cx лица, чтобы избежать пана от центра.
    """
    result: list[float] = []
    last = init
    for cx in samples:
        if cx is not None:
            last = last + smoothing * (cx - last)
        result.append(last)
    return result


# Follower smoothing: a CENTERED (non-causal) moving average kills sub-second head-sway jitter
# WITHOUT lag — the shot is processed offline (batch), so we look ahead + behind. Cheap O(n) array
# math, no model → zero extra load. Window (sec) + keyframe time-resolution for tracing the curve.
_FOLLOW_SMOOTH_SEC = 0.9
# 0.15 (было 0.5): кейфреймы трассируют плавную camera-кривую в ~3× ЧАЩЕ. Рендер линейно
# интерполирует между кейфреймами (build_fill_crop_expr / _interp_cx), поэтому редкая выборка (0.5с)
# на медленном пане давала СКАЧКИ СКОРОСТИ каждые 0.5с = «маленькими прыжками» (founder QA clip_07).
# Holds не раздувает: min_delta схлопывает плоские участки → лишние кейфреймы только на РЕАЛЬНОМ
# движении (2-3с пан ≈ 14 кейфреймов, ок для piecewise-expr). Кадровую сетку/границы НЕ трогает.
_FOLLOW_SAMPLE_SEC = 0.15
# Dead-zone virtual camera: the face may drift ±_FOLLOW_DEADZONE (frame-width) from the held crop
# centre WITHOUT the camera moving; only when it leaves the band does the crop ease toward it. → the
# crop moves ONLY when the face really needs it, and always smoothly (founder: "track only if the
# face doesn't fit — that prevents jerking"). _FOLLOW_EASE = per-frame catch-up once outside.
_FOLLOW_DEADZONE = 0.06
_FOLLOW_EASE = 0.04
# 2nd-order (velocity-damped) ease: instead of snapping the POSITION toward the catch-up target
# each frame (exponential — velocity jumps 0→peak on frame 1 = the "jerky start" founder felt), we
# ramp the VELOCITY toward it. The camera accelerates smoothly INTO a pan and decelerates OUT
# (cinematic slow-in / slow-out). Lower = gentler ramp / longer to reach speed. Grid-safe: this
# only shapes cx VALUES inside a fill region; region/mode boundaries + fps grid are untouched.
_FOLLOW_ACCEL = 0.12


def _ease_dead_zone_camera(sm: list[float], init_cx: float | None) -> list[float]:
    """PURE. 2nd-order dead-zone virtual camera over a (pre-smoothed) cx signal `sm`.

    HOLD while the face stays within ±_FOLLOW_DEADZONE of the crop centre; once it leaves the band,
    ramp the VELOCITY toward the exponential catch-up target (`_FOLLOW_EASE*(sm[i]-cam)`) by
    `_FOLLOW_ACCEL` per frame, then integrate position. Ramping velocity (not position) removes the
    first-frame lurch of a plain exponential ease → smooth slow-in/slow-out. `init_cx` (legacy
    glide seed) sets the start centre; None → start on the face (sm[0]). Empty → empty.

    Меняет ТОЛЬКО значения cx ВНУТРИ fill-региона — границы режима / кадровая сетка НЕ тронуты
    (инвариант docs/REFRAME_FPS_GRID_INVARIANT.md цел).
    """
    if not sm:
        return []
    cam = init_cx if init_cx is not None else sm[0]
    vel = 0.0
    cams = [cam]
    for i in range(1, len(sm)):
        target_vel = _FOLLOW_EASE * (sm[i] - cam) if abs(sm[i] - cam) > _FOLLOW_DEADZONE else 0.0
        vel += _FOLLOW_ACCEL * (target_vel - vel)
        cam += vel
        cams.append(cam)
    return cams


def _smooth_cx_centered(values: list[float], win: int) -> list[float]:
    """Centered moving average over `win` frames (non-causal → ZERO lag). PURE.

    Unlike causal EMA (smooth_centers), a centered window smooths head-sway without trailing the
    face: the crop tracks the true low-frequency motion → holds on a still face, eases gently on a
    real move (no oscillation, no lag). Edges clamp the window.
    """
    n = len(values)
    if n == 0 or win <= 1:
        return list(values)
    half = win // 2
    out: list[float] = []
    for i in range(n):
        a, b = max(0, i - half), min(n, i + half + 1)
        out.append(sum(values[a:b]) / (b - a))
    return out


# Safety margin on the locked-crop coverage gate: hold a STATIC per-shot crop only if the face
# stays within this fraction of the crop width across the WHOLE shot; otherwise return None so the
# caller falls back to the follower — the face then never drifts to the crop edge. Fixes "half-face"
# on a moving subject inside a long fill shot (founder QA on Cardi B clip_04, where a 21 s shot
# locked at cx=0.37 let the face touch the frame edge). 1.0 = old edge-touching behaviour.
_LOCK_COVERAGE_MARGIN = 0.65


def compute_locked_cx(centers: list[float], face_width: float, crop_w_frac: float) -> float | None:
    """ОДИН статичный cx на шот, при котором ВСЁ лицо влезает в 9:16-кроп с запасом. PURE.

    Размах центров лица за шот [min, max] плюс ширина лица должен влезть в ширину кропа
    (crop_w_frac): тогда ставим кроп на СЕРЕДИНУ размаха — лицо целиком на каждом кадре,
    статикой, без слежения («лок на шот», фокус на конкретном месте после склейки). Если НЕ
    влезает (субъект реально проходит через кадр дальше, чем кроп может удержать) → None →
    вызывающий фолбэчит на слежение (_track_trajectory), чтобы не потерять движущегося.

    Меняет ТОЛЬКО значение cx ВНУТРИ fill-региона (одна точка вместо траектории) — границы
    режима fill/fit и кадровая сетка НЕ тронуты (инвариант docs/REFRAME_FPS_GRID_INVARIANT.md
    цел: рендер схлопывает одно-точечную траекторию в постоянный кроп, флеши невозможны).
    """
    if not centers:
        return None
    lo, hi = min(centers), max(centers)
    if (hi - lo) + face_width <= crop_w_frac * _LOCK_COVERAGE_MARGIN:
        return (lo + hi) / 2
    return None


def classify_frame(all_faces: list[tuple[float, float]], crop_w_frac: float) -> str:
    """fit/fill на один кадр по геометрии лиц (ВСЕ лица кадра).

    Нет лиц → fit; 2+ разнесённых (размах > crop_w_frac) → fit; одно/кластер → fill.
    """
    if not all_faces:
        return "fit"
    if shot_is_wide([[cx for cx, _ in all_faces]], crop_w_frac=crop_w_frac):
        return "fit"
    return "fill"


# Ниже этого короткую перебивку НЕ спасаем от проглатывания даже при промахе кропа: суб-0.4с
# обрезок (напр. хвост шота на границе клипа) как отдельный wide-регион = блик/микро-зум-аут; ~5
# кадров чужого кропа незаметны, а лишний fit-флеш — заметен. 0.4с = как reframe_min_scene_sec.
_MIN_CUTAWAY_SEC = 0.4
# Проглатывание короткого fill в предыдущий fill промахивается, если центры лиц дальше этой доли
# ширины кропа. НЕ пол-кропа (0.5): лицо reg имеет свою ширину (~0.5 кропа), поэтому уже при |Δcx| ≈
# треть кропа его КРАЙ выходит за окно prev = срезано (баг Cardi B clip_06: интервьюер 0.59 vs Карди
# 0.44, Δ=0.15 — при 0.5 глоталось и резало пол-лица). 0.33 → разные люди держат СВОЙ кроп, а мелкий
# джиттер того же лица (Δ < ~0.1) глотается (лишний регион не плодим).
_CUTAWAY_MISFRAME_FRAC = 0.33


def _crop_would_misframe(prev: TrackRegion, reg: TrackRegion, crop_w_frac: float | None) -> bool:
    """True, если проглотить короткий reg в prev = держать ТАЙТ-кроп prev поверх ДРУГОГО шота reg,
    промахиваясь мимо его субъекта. PURE.

    Срабатывает только когда prev — fill (узкий 9:16-кроп на конкретном лице). reg — другой шот:
      • reg fit (wide/молчащий слушатель → silence-гейт): тайт-кроп prev держится на позиции лица
        prev поверх ШИРОКОГО контента reg → лицо/субъект reg вне окна → плечо (баг Cardi B clip_02:
        0.9с молчащего интервьюера cx 0.59, gate→fit, проглочен в кроп Карди 0.36).
      • reg fill, но центр дальше _CUTAWAY_MISFRAME_FRAC ширины кропа (другой человек): край лица
        reg выходит за окно prev → срезано (баг Cardi B clip_04 0.64 vs 0.42; clip_06 0.59 vs 0.44).
    prev fit → полный кадр, кропом не промахнёшься → глотаем как прежде (анти-строб цел).
    crop_w_frac None → выключено (всегда глотаем). Близкий fill (тот же человек) → глотаем (не наш).
    """
    if crop_w_frac is None or prev.mode != "fill" or not prev.points:
        return False
    if reg.t1 - reg.t0 < _MIN_CUTAWAY_SEC:
        return False  # суб-0.4с обрезок → глотаем (блик хуже незаметного микро-плеча)
    pcx = prev.points[-1].cx
    if pcx is None:
        return False
    if reg.mode != "fill":
        return True  # тайт-кроп поверх другого (широкого) шота → субъект вне окна
    if not reg.points or reg.points[0].cx is None:
        return False
    return abs(pcx - reg.points[0].cx) > crop_w_frac * _CUTAWAY_MISFRAME_FRAC


def merge_short_regions(
    regions: list[TrackRegion],
    min_hold_sec: float,
    *,
    crop_w_frac: float | None = None,
) -> list[TrackRegion]:
    """Анти-флеш V2: регион < min_hold_sec поглощается предыдущим (держим его mode+points).

    Гасит рапидное чередование fill↔fit на коротких (<1.5с) планах — «флеши».
    Первый регион не глотаем (нет предыдущего).

    crop_w_frac (не None) — ширина 9:16-кропа: короткий fill-регион, чей субъект ДАЛЬШЕ пол-кропа от
    удерживаемого кропа предыдущего fill-региона, НЕ проглатываем (иначе на экране плечо чужого
    человека — баг Cardi B clip_04: 0.9с перебивки интервьюера на cx 0.64 держали кроп Карди 0.42).
    Такой короткий шот — РЕАЛЬНАЯ склейка (границы на контент-стыке → жёсткий cut невидим,
    REFRAME_FPS_GRID_INVARIANT), анти-строб тут не нужен. None → прежнее поведение (всегда глотаем).
    """
    if not regions:
        return []
    out = [regions[0]]
    for reg in regions[1:]:
        short = reg.t1 - reg.t0 < min_hold_sec
        if short and not _crop_would_misframe(out[-1], reg, crop_w_frac):
            prev = out[-1]
            out[-1] = TrackRegion(
                t0=prev.t0, t1=reg.t1, mode=prev.mode, points=prev.points, points_b=prev.points_b
            )
        else:
            out.append(reg)
    return out


# ─────────────────────── Legacy ASD helpers (сохранены для asd_reframe.py) ───────────────────────


def build_shots(cuts: list[float], duration: float) -> list[tuple[float, float]]:
    """Тайминги склеек (клип-относительные) → интервалы планов [(start, end), …].

    Используется ASD speaker-путём (asd_reframe.py). Склейки на 0 и в конце игнорируем.
    """
    if duration <= 0:
        return []
    pts = sorted({round(c, 3) for c in cuts if 0 < c < duration})
    bounds = [0.0, *pts, duration]
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1) if bounds[i + 1] > bounds[i]]


def build_shots_frames(cuts: list[int], total_frames: int) -> list[tuple[int, int]]:
    """Номера кадров склеек (клип-относительные) → интервалы шотов [(f0, f1), …] в КАДРАХ. PURE.

    Frame-accurate замена build_shots (тот в секундах). Склейки на 0 и в конце игнорируем,
    дубликаты схлопываем. Пустой/нулевой total → []. Единица — КАДР (не float-секунда),
    чтобы граница шота попала ровно на кадр реальной склейки (нет рассинхрона в рендере).
    """
    if total_frames <= 0:
        return []
    pts = sorted({c for c in cuts if 0 < c < total_frames})
    bounds = [0, *pts, total_frames]
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1) if bounds[i + 1] > bounds[i]]


def resample_track(track: SpeakerTrack, src_fps: float, dst_fps: float) -> SpeakerTrack:
    """Пересэмплировать дорожку из сетки src_fps в сетку dst_fps (frame-grid conversion). PURE.

    ASD сэмплит лица @25fps (модель LR-ASD требует 4:1 audio/video), но геометрия
    (склейки/шоты/регионы) и рендер работают в НАТИВНОМ fps источника. Без этой конвертации
    границы регионов (кадры 25fps) не попадают на кадры рендера (нативный fps) → флеш-кадр.
    cx пересэмплируем nearest-neighbor по времени; width/speak — скаляры. f0/f1 — по времени.
    """
    if src_fps == dst_fps or not track.cx:
        return track
    nf0 = round(track.f0 / src_fps * dst_fps)
    nf1 = max(nf0 + 1, round(track.f1 / src_fps * dst_fps))
    n_src = len(track.cx)
    n_sf = len(track.speak_frames)
    cx_new: list[float] = []
    sf_new: list[float] = []
    for nf in range(nf0, nf1):
        idx = round(nf / dst_fps * src_fps) - track.f0  # время кадра dst → индекс в src-сетке
        cx_new.append(track.cx[max(0, min(n_src - 1, idx))])
        if (
            n_sf
        ):  # speak_frames выровнен по тем же кадрам (короче cx на хвост — клипуем в свою длину)
            sf_new.append(track.speak_frames[max(0, min(n_sf - 1, idx))])
    return SpeakerTrack(
        f0=nf0,
        f1=nf1,
        cx=tuple(cx_new),
        width=track.width,
        speak=track.speak,
        speak_frames=tuple(sf_new),
        embedding=track.embedding,  # личность не зависит от сетки fps — переносим как есть
    )


def _track_cx_in_shot(t: SpeakerTrack, f0: int, f1: int) -> list[float]:
    """cx дорожки t для кадров пересечения с шотом [f0, f1). PURE."""
    lo, hi = max(t.f0, f0), min(t.f1, f1)
    return [t.cx[f - t.f0] for f in range(lo, hi)]


def _is_wide_shot(
    active: list[SpeakerTrack],
    f0: int,
    f1: int,
    spread_min: float,
    *,
    coverage_min: float = 0.25,
    min_width: float = _MIN_FACE_FRAC,
) -> bool:
    """2+ СТАБИЛЬНЫХ лица (покрытие ≥ coverage_min кадров шота И реальная ширина), разнесённых
    по X сильнее spread_min → широкий план (fit/split). PURE.

    ⚠️ Coverage-гейт ОБЯЗАТЕЛЕН (репро на реальном видео Tom Holland×Jay Shetty): трекер почти на
    КАЖДОМ одиночном плане плодит КОРОТКИЙ спурьез-трек (отражение/фон/мелькнувшее лицо/разрыв IOU
    одного лица на два) с покрытием 0–6%, тогда как настоящие лица покрывают 35–100%. Без гейта
    спурьез подделывал 2-shot → ЛОЖНЫЙ wide на ОДНОМ человеке (жалоба фаундера). Width-гейт
    отсекает крошечные фоновые лица. Чистый разрыв 6%↔35% → coverage_min=0.25 разделяет надёжно.
    """
    shot_len = f1 - f0
    if shot_len <= 0:
        return False
    reps: list[float] = []
    for t in active:
        cxs = _track_cx_in_shot(t, f0, f1)
        if len(cxs) >= coverage_min * shot_len and t.width >= min_width:
            reps.append(sum(cxs) / len(cxs))
    return len(reps) >= 2 and (max(reps) - min(reps)) > spread_min


def _split_pair(
    active: list[SpeakerTrack],
    f0: int,
    f1: int,
    spread_min: float,
    coverage_min: float = 0.6,
) -> tuple[SpeakerTrack, SpeakerTrack] | None:
    """Пара для split: РОВНО 2 устойчивых трека (покрытие ≥ coverage_min шота),
    разнесённых сильнее spread_min. Возвращает (левый, правый) по среднему cx. PURE.

    3+ устойчивых лица или нестабильные треки → None (остаётся fit — требование
    фаундера «оставить горизонтальный вид для других ситуаций»).
    """
    shot_len = f1 - f0
    if shot_len <= 0:
        return None
    stable: list[tuple[float, SpeakerTrack]] = []
    for t in active:
        cxs = _track_cx_in_shot(t, f0, f1)
        if len(cxs) >= coverage_min * shot_len:
            stable.append((sum(cxs) / len(cxs), t))
    if len(stable) != 2:
        return None
    stable.sort(key=lambda p: p[0])
    (cx_a, ta), (cx_b, tb) = stable
    if cx_b - cx_a <= spread_min:
        return None  # кластер — это fill-кейс
    return ta, tb


def _track_mean_cx(t: SpeakerTrack) -> float:
    """PURE. Средний cx дорожки за её кадры (для сравнения с vision subject-x). Пусто → 0.5."""
    return sum(t.cx) / len(t.cx) if t.cx else 0.5


def _pick_target(
    active: list[SpeakerTrack], speak_threshold: float, *, subject_cx: float | None = None
) -> SpeakerTrack | None:
    """Выбрать дорожку в кадр: макс. speak; если ниже порога → макс. width (largest-face). PURE.

    B2 (тай-брейкер, вход subject_cx НЕ None): если есть УВЕРЕННАЯ vision-подсказка центра субъекта
    (Gemini-box → centre-x, проводится ТОЛЬКО когда флаг REFRAME_VISION_SUBJECT включён у вызывателя
    — эта функция чистая, конфиг не читает), берём дорожку с mean cx БЛИЖАЙШИМ к subject_cx —
    ПЕРЕОПРЕДЕЛЯЯ speak/width. Это чинит «выбрали не того человека» в multi-face шоте. Если ни одна
    дорожка не ближе _VISION_SUBJECT_MAX_DIST (субъект, вероятно, НЕ-лицо / бокс мимо) → ЯВНЫЙ
    фолбэк на speak/width (не тихий: подсказка просто не подошла). subject_cx=None → байт-в-байт
    прежнее поведение. Кроп-координаты подсказка НЕ двигает (Δ=0 сетка цела) — только ВЫБОР трека.
    """
    if not active:
        return None
    if subject_cx is not None:
        nearest = min(active, key=lambda t: abs(_track_mean_cx(t) - subject_cx))
        if abs(_track_mean_cx(nearest) - subject_cx) <= _VISION_SUBJECT_MAX_DIST:
            return nearest
        # уверенная подсказка есть, но ни один трек к ней не близок → фолбэк на speak/width
    best = max(active, key=lambda t: t.speak)
    if best.speak < speak_threshold:
        best = max(active, key=lambda t: t.width)
    return best


def locked_cx_for_tracks(
    tracks: list[SpeakerTrack],
    src_w: int,
    src_h: int,
    *,
    speak_threshold: float,
    smoothing: float,
) -> float | None:
    """Лок-центр лица для РУЧНОГО tight-override (таб «Кадр»): cx говорящего/крупнейшего лица в
    диапазоне, вмещающий лицо в 9:16 (compute_locked_cx); не влезает (субъект панорамирует шире
    кропа) → медиана сглаженных центров. Нет лиц/дорожек → None (вызывающий оставит честный
    0.5-фолбэк). PURE (вход — уже посчитанные дорожки). Меняет ТОЛЬКО cx ВНУТРИ fill-региона —
    границы режима / кадровая сетка НЕ тронуты (инвариант docs/REFRAME_FPS_GRID_INVARIANT.md цел).
    """
    target = _pick_target(tracks, speak_threshold)
    if target is None or not target.cx:
        return None
    crop_w_frac = round(src_h * _ASPECT_W / _ASPECT_H) / src_w
    sm = smooth_centers(list(target.cx), smoothing, init=target.cx[0])
    locked = compute_locked_cx(sm, target.width, crop_w_frac)
    if locked is not None:
        return locked
    ordered = sorted(sm)
    return ordered[len(ordered) // 2]


def _track_trajectory(
    t: SpeakerTrack,
    f0: int,
    f1: int,
    fps: float,
    smoothing: float,
    min_delta: float = 0.003,
    *,
    init_cx: float | None = None,
) -> tuple[TrackPoint, ...]:
    """Сглаженная cx-траектория дорожки внутри шота → TrackPoint'ы (клип-время = кадр/fps). PURE.

    init_cx (НЕ None) = форсированный стартовый центр EMA (опц. knob). None (дефолт, и так
    передаёт plan_regions) → init = первый реальный cx ЦЕЛИ → кроп ОТКРЫВАЕТСЯ на лице с
    первого кадра шота (нет «пол-лица» в начале). Раньше plan_regions гнал сюда
    prev_fill_end_cx (continuity), но на склейке к ДРУГОМУ спикеру это открывало кроп между
    людьми = пол-лица — убрано (жёсткий cut и так маскирует снап центра).
    Dead-zone: новый кейфрейм только при сдвиге ≥ min_delta от последнего — убирает
    per-frame дрожание детектора, сохраняет следование реальному движению человека. min_delta 0.003
    (было 0.012): кейфреймы плотнее по cx → линейная интерполяция рендера повторяет плавную ease-
    кривую без скачков скорости («прыжков») на медленном пане; статику dead-zone всё равно держит
    (camera-путь плоский → кейфреймы схлопываются), поэтому мельче min_delta шум НЕ пропускает.
    ⚠️ Границы режима (fill/fit) НЕ трогаем — инвариант docs/REFRAME_FPS_GRID_INVARIANT.md цел:
    меняется только стартовое значение cx-выражения внутри fill-региона (рендер не меняется).
    """
    lo, hi = max(t.f0, f0), min(t.f1, f1)
    raw = [t.cx[f - t.f0] for f in range(lo, hi)]
    if not raw:
        return (TrackPoint(t=f0 / fps, mode="fill", cx=0.5 if init_cx is None else init_cx),)
    # Centered low-pass (zero lag) removes head-sway jitter before the dead-zone logic.
    sm = _smooth_cx_centered(raw, max(1, round(_FOLLOW_SMOOTH_SEC * fps)))
    # Dead-zone virtual camera (2nd-order velocity-damped ease): HOLD while the face stays within
    # ±_FOLLOW_DEADZONE of the crop centre; once it leaves the band, ACCELERATE smoothly toward it
    # and decelerate back into a hold → moves only when the face needs it, no jerky start.
    cams = _ease_dead_zone_camera(sm, init_cx)
    # Keyframes from the CAMERA path: keep endpoints + the frame before a move (anchors the hold) +
    # the move; collapse the long flat holds. Faithful → no false linear drift (the clip_04 bug).
    step = max(1, round(_FOLLOW_SAMPLE_SEC * fps))
    idxs = list(range(0, len(cams), step))
    if idxs[-1] != len(cams) - 1:
        idxs.append(len(cams) - 1)
    pts: list[TrackPoint] = [TrackPoint(t=(lo + idxs[0]) / fps, mode="fill", cx=cams[idxs[0]])]
    last_cx = cams[idxs[0]]
    for k in range(1, len(idxs)):
        c = cams[idxs[k]]
        is_last = k == len(idxs) - 1
        moved = abs(c - last_cx) >= min_delta
        next_moves = (not is_last) and abs(cams[idxs[k + 1]] - c) >= min_delta
        if moved or next_moves or is_last:
            pts.append(TrackPoint(t=(lo + idxs[k]) / fps, mode="fill", cx=c))
            last_cx = c
    return tuple(pts)


def vision_shot_for_scene_shot(
    vision_shots: Sequence[VisionShot], f0: int, f1: int, fps: float
) -> VisionShot | None:
    """PURE. Map a native-frame scene shot [f0,f1) to the vision shot covering its MIDPOINT time.

    Vision shots are window-relative, ordered by start_sec; shot k spans
    [start_sec[k], start_sec[k+1]) (the last one → +∞). Return the shot whose interval contains the
    scene-shot midpoint ((f0+f1)/2)/fps; if the midpoint precedes the first vision shot, return the
    FIRST (nearest). Empty → None.

    Используется ТОЛЬКО чтобы выбрать per-shot MODE + субъект — scene-cut границы (f0,f1) и
    кадровую сетку НЕ трогаем (инвариант docs/REFRAME_FPS_GRID_INVARIANT.md, Δ=0 цел).
    """
    if not vision_shots:
        return None
    mid = ((f0 + f1) / 2) / fps
    ordered = sorted(vision_shots, key=lambda s: s.start_sec)
    chosen = ordered[0]
    for sh in ordered:
        if sh.start_sec <= mid:
            chosen = sh
        else:
            break
    return chosen


def _fill_points_for_target(
    target: SpeakerTrack | None,
    f0: int,
    f1: int,
    fps: float,
    *,
    crop_w_frac: float,
    smoothing: float,
    lock_per_shot: bool,
    prev_fill_end_cx: float | None,
) -> tuple[TrackPoint, ...]:
    """PURE. cx-точки fill-региона для `target` внутри шота [f0,f1). Извлечено ДОСЛОВНО из fill-
    ветки plan_regions, чтобы vision-форс-fill (Layer A) и эвристический fill давали БАЙТ-В-БАЙТ
    одинаковые точки.

    lock_per_shot=True (дефолт): одна статичная locked-cx, если ВСЁ лицо влезает в бюджет кропа
    за весь шот (compute_locked_cx), иначе снап+слежение (_track_trajectory). lock_per_shot=False:
    legacy hybrid-глайд/снап по prev_fill_end_cx. Меняет ТОЛЬКО cx ВНУТРИ fill-региона — границы
    режима (= кадры шота, ставит вызыватель) и сетка НЕ тронуты (инвариант REFRAME_FPS_GRID цел).
    """
    t0 = f0 / fps
    if lock_per_shot:
        raw = _track_cx_in_shot(target, f0, f1) if target is not None else []
        locked = (
            compute_locked_cx(
                smooth_centers(list(raw), smoothing, init=raw[0]), target.width, crop_w_frac
            )
            if (target is not None and raw)
            else None
        )
        if locked is not None:
            return (TrackPoint(t=t0, mode="fill", cx=locked),)
        if target is not None:
            return _track_trajectory(target, f0, f1, fps, smoothing)  # снап + слежение
        return (TrackPoint(t=t0, mode="fill", cx=0.5),)
    # Legacy hybrid-наведение (глайд на близких склейках, снап на дальних). Сохранён ПОД ФЛАГОМ
    # для отката/совместимости. ⚠️ EMA ВНУТРИ шота = «следование», от которого фаундер просил уйти.
    target_first = None
    if target is not None:
        cxs = _track_cx_in_shot(target, f0, f1)
        target_first = cxs[0] if cxs else None
    init = prev_fill_end_cx
    if init is not None and (target_first is None or abs(target_first - init) > _GLIDE_MAX_DCX):
        init = None
    if target is not None:
        return _track_trajectory(target, f0, f1, fps, smoothing, init_cx=init)
    return (TrackPoint(t=t0, mode="fill", cx=0.5 if init is None else init),)


_SEAT_MERGE_CX = 0.12  # треки с mean cx ближе этого → одно «место» (seat) на локированной камере


def _seat_mean_cx(t: SpeakerTrack) -> float:
    return sum(t.cx) / len(t.cx) if t.cx else 0.5


def build_seats(tracks: list[SpeakerTrack]) -> list[SpeakerTrack]:
    """Слить фрагментированные IOU-треки в позиционные «места» (seats) по cx. PURE.

    На локированной камере люди сидят на стабильных X-позициях, но IOU-трекер рвёт КАЖДОГО на
    много коротких треков (репро: 2 человека → 15 треков за 60с, ни один не покрывает 25% шота →
    coverage-гейт и per-track binding ломаются). Кластеризуем треки по mean cx (порог
    _SEAT_MERGE_CX) и сливаем в ОДНУ дорожку на место: на каждый кадр cx/speak берём у члена с МАКС
    ASD; кадр без члена → NO_ASD-гэп (в coverage/binding не считается). Так bind_lanes/plan_switches
    работают на СТАБИЛЬНЫХ лейнах, а не на осколках. Seats отсортированы по cx (лево→право).
    """
    if not tracks:
        return []
    ordered = sorted(tracks, key=_seat_mean_cx)
    clusters: list[list[SpeakerTrack]] = [[ordered[0]]]
    for t in ordered[1:]:
        if _seat_mean_cx(t) - _seat_mean_cx(clusters[-1][-1]) <= _SEAT_MERGE_CX:
            clusters[-1].append(t)
        else:
            clusters.append([t])
    return [_aggregate_group(cl) for cl in clusters]


def _aggregate_group(cl: list[SpeakerTrack]) -> SpeakerTrack:
    """Слить группу треков (одно МЕСТО или одна ЛИЧНОСТЬ) в ОДНУ дорожку. PURE.

    На каждый кадр cx/speak берём у члена с МАКС ASD (он и есть «лицо этого человека сейчас»);
    кадр без члена → NO_ASD-гэп (в coverage/binding не считается). Эмбеддинг группы = средний по
    членам (L2-норм) — отпечаток личности. cx per-frame СЛЕДУЕТ за человеком, поэтому при смене
    камеры (личность в другой позиции) кроп всё равно попадает на него.
    """
    f0 = min(t.f0 for t in cl)
    f1 = max(t.f1 for t in cl)
    cxs: list[float] = []
    sfs: list[float] = []
    last_cx = _seat_mean_cx(cl[0])
    for f in range(f0, f1):
        best_sp: float | None = None
        best_cx: float | None = None
        for t in cl:
            if t.f0 <= f < t.f1:
                i = f - t.f0
                cx = t.cx[i] if i < len(t.cx) else None
                sp = t.speak_frames[i] if i < len(t.speak_frames) else None
                if sp is not None and (best_sp is None or sp > best_sp):
                    best_sp, best_cx = sp, cx
                elif best_cx is None and cx is not None:
                    best_cx = cx  # детект есть, но ASD-скор отсутствует (хвост score_track)
        if best_cx is not None:
            last_cx = best_cx
        cxs.append(last_cx)
        sfs.append(best_sp if best_sp is not None else NO_ASD)
    real = [v for v in sfs if v > NO_ASD]
    return SpeakerTrack(
        f0=f0,
        f1=f1,
        cx=tuple(cxs),
        width=sum(t.width for t in cl) / len(cl),
        speak=(sum(real) / len(real)) if real else NO_ASD,
        speak_frames=tuple(sfs),
        embedding=_mean_norm([t.embedding for t in cl if t.embedding]),
    )


def _mean_norm(embs: list[tuple[float, ...]]) -> tuple[float, ...]:
    """Средний L2-нормированный эмбеддинг группы (центроид личности). PURE. [] → ()."""
    if not embs:
        return ()
    n = len(embs[0])
    mean = [sum(e[i] for e in embs) / len(embs) for i in range(n)]
    norm = sum(x * x for x in mean) ** 0.5
    return tuple(x / norm for x in mean) if norm else ()


def _cos(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Косинус между L2-нормированными эмбеддингами (= dot). PURE."""
    return sum(x * y for x, y in zip(a, b, strict=False))


# Порог AVERAGE-linkage: сливаем ДВЕ ГРУППЫ треков в одну личность, если СРЕДНЯЯ похожесть между
# ними ≥ этого. Замеры на реальном диван-видео (12 треков, emb_matrix):
#   свой-свой (мужчина) mean +0.73 (min +0.49), свой-свой (женщина) mean +0.93 (min +0.79),
#   ЧУЖОЙ-чужой mean +0.26, но MAX +0.52 (!).
# ⚠️ Порог по ОТДЕЛЬНЫМ ПАРАМ невозможен: распределения ПЕРЕСЕКАЮТСЯ (чужой max 0.52 > свой min 0.49)
# — у человека с сильной сменой позы отдельные треки «дрейфуют» к другому лицу. А вот ГРУППОВЫЕ
# средние расходятся с запасом (0.73/0.93 vs 0.26) → 0.45 делит их надёжно.
_IDENTITY_SIM_MIN = 0.45
# Матчинг личность-КЛИПА → личность-ИСХОДНИКА: сравниваем уже УСРЕДНЁННЫЕ (стабильные) отпечатки, и
# берётся argmax — порог тут лишь пол «это вообще тот же человек?» (защита от нового лица).
_MATCH_SIM_MIN = 0.35


def build_identities(tracks: list[SpeakerTrack]) -> list[SpeakerTrack]:
    """Кластеризовать треки в ЛИЧНОСТИ по face-эмбеддингу (v2). PURE.

    Замена build_seats (по позиции): группируем по ВНЕШНОСТИ, а не по месту в кадре. Поэтому
    устойчиво к СМЕНЕ КАМЕР / движению камеры / «мафии» — один и тот же человек узнаётся в любой
    позиции и на любом плане (позиционная привязка тут разваливается). Жадная агломерация: длинные
    треки первыми (у них надёжнее отпечаток) сеют личности; остальные липнут к ближайшему центроиду,
    если косинус ≥ _IDENTITY_SIM_MIN, иначе заводят новую личность.

    Нет эмбеддингов (не speaker-режим / reID не считался) → ЧЕСТНЫЙ фолбэк на build_seats (позиция),
    а не молчаливая деградация. Порядок выхода — по cx (детерминизм).
    """
    if not tracks:
        return []
    if not all(t.embedding for t in tracks):
        return build_seats(tracks)
    groups = cluster_by_embedding(tracks)
    return sorted((_aggregate_group([tracks[i] for i in g]) for g in groups), key=_seat_mean_cx)


def cluster_by_embedding(tracks: Sequence[SpeakerTrack]) -> list[list[int]]:
    """Индексы треков → группы по ОТПЕЧАТКУ лица. Агломерация с AVERAGE linkage. PURE.

    Сливаем две ближайшие ГРУППЫ, пока их СРЕДНЯЯ взаимная похожесть ≥ _IDENTITY_SIM_MIN.

    ⚠️ ПОЧЕМУ НЕ «жадно прилипнуть к ближайшему центроиду» (так было — РАЗВАЛИВАЛОСЬ на реале):
    у человека с сильной сменой позы (профиль/наклон головы) ОТДЕЛЬНЫЕ треки «дрейфуют» к другому
    лицу — на реальном диван-видео трек мужчины имел cos 0.44–0.52 с треками женщины, хотя его
    собственная минимальная self-похожесть всего 0.49. Распределения ПЕРЕСЕКАЮТСЯ → порога по парам
    не существует. Один такой merge каскадом склеивал ВСЕХ в одну личность (репро: 12 треков → 1
    «личность» с cx=0.50 = среднее между двумя людьми → фича не включалась).
    Average-linkage смотрит на СРЕДНЮЮ похожесть между ГРУППАМИ (свой 0.73/0.93 vs чужой 0.26 —
    расходятся с запасом), поэтому единичный выброс кластеризацию не ломает.

    Вынесено из build_identities: source-level привязке надо кластеризовать треки из НЕСКОЛЬКИХ окон
    исходника в ГЛОБАЛЬНЫЕ личности (агрегировать по кадрам нельзя — кадры разных окон). Треки без
    эмбеддинга пропускаются (в группы не попадают).
    """
    idx = [i for i, t in enumerate(tracks) if t.embedding]
    if not idx:
        return []
    sim = {
        (i, j): _cos(tracks[i].embedding, tracks[j].embedding) for i in idx for j in idx if i < j
    }
    groups: list[list[int]] = [[i] for i in idx]

    def link(a: list[int], b: list[int]) -> float:
        vals = [sim[(min(i, j), max(i, j))] for i in a for j in b]
        return sum(vals) / len(vals)

    while len(groups) > 1:
        best_s, bx, by = -2.0, -1, -1
        for x in range(len(groups)):
            for y in range(x + 1, len(groups)):
                s = link(groups[x], groups[y])
                if s > best_s:
                    best_s, bx, by = s, x, y
        if best_s < _IDENTITY_SIM_MIN:
            break
        groups[bx] = groups[bx] + groups[by]
        groups.pop(by)
    return groups


def _speaker_switch_regions(
    active: list[SpeakerTrack],
    turns: Sequence[SpeakerTurn],
    f0: int,
    f1: int,
    fps: float,
    *,
    crop_w_frac: float,
    smoothing: float,
    lock_per_shot: bool,
    cfg: SwitchConfig,
    source: SourceBinding | None = None,
) -> list[TrackRegion] | None:
    """Speaker-Focus: суб-деление шота [f0,f1) на fill(лейн)/fit регионы по сменам говорящего.

    Реплики шота (turns) → binding кластер↔лейн (bind_lanes, per-turn ASD) → plan_switches (greedy
    min-hold/hysteresis/IoU-гейт) → TrackRegion'ы (fill на лейне через _fill_points_for_target, WIDE
    → fit). Возвращает None → вызыватель падает в ОБЫЧНУЮ single-region логику, если в шоте нет 2+
    привязанных спикеров (не диалог) — так одиночный talking-head/wide-план не трогается. Каждая
    суб-граница frame-snapped (round(t*fps)/fps) → Δ=0 (docs/REFRAME_FPS_GRID_INVARIANT.md). PURE.
    """
    t0, t1 = f0 / fps, f1 / fps
    shot_turns = [tn for tn in turns if tn.t_end > t0 and tn.t_start < t1]
    if not shot_turns:
        return None
    # Склеиваем осколки IOU-трекера в ЛИЧНОСТИ (по face-эмбеддингу; без эмбеддингов — фолбэк на
    # позиционные места), затем берём только СТАБИЛЬНЫЕ: РЕАЛЬНОЕ покрытие (не-gap кадры, где лицо
    # реально детектилось) ≥ _FILL_MIN_COVERAGE + ширина. Спурьез (краткое отражение/фон) → низкое
    # real-покрытие → отсекается (как в _is_wide_shot).
    shot_len = f1 - f0
    real = [
        s
        for s in build_identities(active)
        if sum(1 for v in s.speak_frames if v > NO_ASD) >= _FILL_MIN_COVERAGE * shot_len
        and s.width >= _MIN_FACE_FRAC
    ]
    if len(real) < 2:
        return None  # 0/1 лицо → обычная логика (она и делает «1 крупное → tight на нём»)

    if source is not None:
        # SOURCE-привязка (надёжна: посчитана по всему видео, где есть диалог; личности стабильны
        # благодаря reID) → достаточно ОДНОГО говорящего в шоте. Это ФИКС МОНОЛОГА: в кадре двое,
        # говорит один, чередования нет — но мы ЗНАЕМ его лицо из исходника → tight именно на него.
        clip_to_src = match_identities(real, source.embeddings, sim_min=_MATCH_SIM_MIN)
        binding = clip_binding_from_source(source.cluster_to_identity, clip_to_src)
        min_speakers = 1
    else:
        # Без source-привязки надёжность берём из ЧЕРЕДОВАНИЯ в клипе → нужен диалог (2 спикера).
        if len(shot_turns) < 2:
            return None
        binding = bind_lanes(shot_turns, real, fps, conf_min=cfg.bind_conf_min)
        min_speakers = 2
    # Кто из говорящих В ЭТОМ шоте реально привязан к видимому лицу (остальные — за кадром).
    active_bound = {
        ln for tn in shot_turns if (ln := binding.get(tn.speaker)) is not None and ln < len(real)
    }
    if len(active_bound) < min_speakers:
        return None  # не поняли, кто говорит / говорящий за кадром → обычная логика → wide
    regions: list[TrackRegion] = []
    for sub in plan_switches(shot_turns, binding, real, crop_w_frac, t0, t1, fps, cfg):
        sf0, sf1 = round(sub.t0 * fps), round(sub.t1 * fps)
        if sf1 <= sf0:
            continue
        if sub.target == WIDE:
            regions.append(TrackRegion(t0=sf0 / fps, t1=sf1 / fps, mode="fit", points=()))
        else:
            pts = _fill_points_for_target(
                real[sub.target], sf0, sf1, fps,
                crop_w_frac=crop_w_frac, smoothing=smoothing,
                lock_per_shot=lock_per_shot, prev_fill_end_cx=None,
            )  # fmt: skip
            regions.append(TrackRegion(t0=sf0 / fps, t1=sf1 / fps, mode="fill", points=pts))
    return regions or None


def plan_regions(
    shots: list[tuple[int, int]],
    tracks: list[SpeakerTrack],
    fps: float,
    *,
    crop_w_frac: float,
    smoothing: float = 0.15,
    lock_per_shot: bool = True,
    speak_threshold: float = 0.0,
    wide_spread_min: float | None = None,
    mode_setting: str = "auto",
    split_enabled: bool = False,
    wide_speak_min: float = 0.3,
    subject_cx: float | None = None,
    vision_shots: Sequence[VisionShot] | None = None,
    turns: Sequence[SpeakerTurn] | None = None,
    reframe_mode: str = "auto",
    switch_cfg: SwitchConfig | None = None,
    source_binding: SourceBinding | None = None,
) -> list[TrackRegion]:
    """Cut-aligned планировщик: на КАЖДЫЙ шот один режим + траектория. Сердце Pass-1. PURE.

    shots — интервалы [(f0,f1)] в КАДРАХ (build_shots_frames). На шот:
      широкий (2+ разнесённых дорожек) → split_enabled И ровно 2 устойчивых трека →
      split (верх/низ, оба full-bleed); иначе fit (пейзаж/толпа/3+ лиц — «горизонтальный
      вид» остаётся); не широкий → fill на говорящем (макс. speak), при молчании ASD
      (< speak_threshold) → фолбэк на крупнейшее лицо; нет дорожек → fit.
    mode_setting "fill"/"fit" — глобальный оверрайд. wide_spread_min дефолт = crop_w_frac.
    Границы регионов = границы шотов (= кадры склеек) → смена режима только на склейке
    (инвариант docs/REFRAME_FPS_GRID_INVARIANT.md не трогаем: split не двигает границы).
    subject_cx (B2, exploratory, флаг REFRAME_VISION_SUBJECT у вызывателя): vision-подсказка центра
    субъекта [0,1] → ТАЙ-БРЕЙКЕР `_pick_target` (какой face-track вести на FILL-шоте). НЕ влияет на
    выбор режима fill/fit, границы и сетку — только на ВЫБОР трека. None → прежнее поведение.
    vision_shots (Layer A, exploratory, флаг REFRAME_VISION_LAYOUT у вызывателя): per-shot fill/fit
    + субъект, увиденные Gemini. Уверенный vision-шот, попавший на scene-шот, СЕМАНТИЧЕСКИ РЕШАЕТ
    режим (fill/fit) + субъект, ПЕРЕОПРЕДЕЛЯЯ silence/coverage-гейты (которые ошибочно расширяют
    молчащий close-up — баг Cardi B). Нет vision-шота → падаем в сегодняшние эвристики (явно).
    Меняет ТОЛЬКО mode + субъект (cx-кривую) — scene-cut границы / кадровую сетку НЕ трогаем
    (инвариант docs/REFRAME_FPS_GRID_INVARIANT.md, Δ=0 цел). None (дефолт) → байт-в-байт прежнее.
    """
    spread_min = crop_w_frac * _WIDE_SPREAD_RATIO if wide_spread_min is None else wide_spread_min
    regions: list[TrackRegion] = []
    # Центр конца предыдущего fill-шота → hybrid-наведение (плавный пан между БЛИЗКИМИ планами,
    # снап на ДАЛЁКИХ). Сбрасывается на fit/split (реальная смена плана).
    prev_fill_end_cx: float | None = None
    for f0, f1 in shots:
        active = [t for t in tracks if t.f0 < f1 and t.f1 > f0]
        t0, t1 = f0 / fps, f1 / fps
        if mode_setting == "fit":
            regions.append(TrackRegion(t0=t0, t1=t1, mode="fit", points=()))
            prev_fill_end_cx = None
            continue

        # ── Speaker Focus (reframe_mode="speaker"): tight-follow с cut-переключением между ──
        # спикерами. Если в шоте 2+ привязанных спикера — суб-делим шот на fill(говорящий)/fit-wide
        # регионы по сменам реплик (синтетические cut'ы = «вторая камера»). Иначе (одиночный/wide)
        # → None → падаем в обычную эвристику ниже (байт-в-байт). Только в auto (явный mode_setting
        # юзера приоритетнее). Границы суб-регионов на реальных кадрах (Δ=0). Спека 2026-07-13.
        if reframe_mode == "speaker" and turns is not None and mode_setting == "auto":
            sub = _speaker_switch_regions(
                active, turns, f0, f1, fps,
                crop_w_frac=crop_w_frac, smoothing=smoothing,
                lock_per_shot=lock_per_shot, cfg=switch_cfg or SwitchConfig(),
                source=source_binding,
            )  # fmt: skip
            if sub is not None:
                regions.extend(sub)
                prev_fill_end_cx = None
                continue

        # ── Layer A: vision-driven per-shot fill/fit override (флаг REFRAME_VISION_LAYOUT). ──
        # Уверенный vision-шот, попавший на ЭТОТ scene-шот, РЕШАЕТ режим (семантика картинки),
        # ПЕРЕОПРЕДЕЛЯЯ silence/coverage-гейты. Нет vision-шота → падаем в эвристики (ЯВНО, ниже).
        # Только в auto-режиме (явный mode_setting юзера — приоритетнее). Границы/сетка не тронуты.
        vshot = (
            vision_shot_for_scene_shot(vision_shots, f0, f1, fps)
            if (vision_shots and mode_setting == "auto")
            else None
        )
        if vshot is not None:
            if vshot.mode == "fit":
                regions.append(TrackRegion(t0=t0, t1=t1, mode="fit", points=()))
                prev_fill_end_cx = None
                continue
            # vision mode == "fill": ФОРСИМ fill — обходим silence/coverage-гейты (широко бы
            # ошиблись). Субъект-трек выбираем vision subject_cx тай-брейкером среди ВСЕХ active
            # (coverage-гейт обойдён). Нет ни одной face-track → кропить не на что → ЯВНО падаем
            # в эвристики ниже (дадут fit). Не тихий 0-регион (правило №8).
            vtarget = _pick_target(active, speak_threshold, subject_cx=vshot.subject_cx)
            if vtarget is not None:
                pts = _fill_points_for_target(
                    vtarget, f0, f1, fps,
                    crop_w_frac=crop_w_frac, smoothing=smoothing,
                    lock_per_shot=lock_per_shot, prev_fill_end_cx=prev_fill_end_cx,
                )  # fmt: skip
                regions.append(TrackRegion(t0=t0, t1=t1, mode="fill", points=pts))
                prev_fill_end_cx = pts[-1].cx
                continue
        if mode_setting != "fill" and (not active or _is_wide_shot(active, f0, f1, spread_min)):
            # WIDE shot (2+ horizontally-spread faces) or no faces → go WIDE; do NOT tight-crop one
            # speaker. Cropping a real 2-shot hides the other person and routinely frames a torso/
            # legs — founder: "the AI must go wide when it sees more than one person." Exactly-2
            # stable spread tracks + split enabled → split (both full-bleed); otherwise → fit (full
            # frame + blur bars). This restores the documented behaviour (above): it reverts the
            # 7e7d754 "clear-speaker → crop tight on dialogue" hybrid, which kept every 2-shot
            # cropped because in a podcast someone is almost always speaking (wide_speak_min=0.3).
            pair = _split_pair(active, f0, f1, spread_min) if split_enabled else None
            if pair is not None:
                ta, tb = pair
                regions.append(
                    TrackRegion(
                        t0=t0,
                        t1=t1,
                        mode="split",
                        points=_track_trajectory(ta, f0, f1, fps, smoothing),
                        points_b=_track_trajectory(tb, f0, f1, fps, smoothing),
                    )
                )
                prev_fill_end_cx = None
                continue
            regions.append(TrackRegion(t0=t0, t1=t1, mode="fit", points=()))
            prev_fill_end_cx = None
            continue
        # FILL-кандидаты: дорожки с покрытием ≥ _FILL_MIN_COVERAGE кадров шота. Хвост дорожки,
        # заползший за склейку в face-sparse план (покрытие ~1%), НЕ субъект → не кропим широкий
        # план в «ноги»/торс (репро CARDI B: 3 кадра в 7.9с B-roll). Нет покрытой дорожки → fit.
        covered = [
            t for t in active if len(_track_cx_in_shot(t, f0, f1)) >= _FILL_MIN_COVERAGE * (f1 - f0)
        ]
        target = _pick_target(covered, speak_threshold, subject_cx=subject_cx)
        if mode_setting != "fill" and target is None:
            regions.append(TrackRegion(t0=t0, t1=t1, mode="fit", points=()))
            prev_fill_end_cx = None
            continue
        # Одиночное лицо → fit (wide) ТОЛЬКО когда это НЕ уверенный крупный план:
        # (а) лицо МОЛЧИТ (speak < _SILENCE_WIDE_MAX) И НЕ крупное (width < _SILENCE_MIN_FILL_FACE)
        #     = B-roll/establishing (реклайн/legs) — мелкое лицо, не режем strip по телу; ЛИБО
        # (б) лицо мелкое И ниже порога речи (старое правило «неуверенный субъект»).
        # Крупное лицо (≥ _SILENCE_MIN_FILL_FACE), пусть и молчит, → fill: в кадре один человек,
        # наводимся на лицо (говорящая/слушающая голова, а НЕ широкий план). "fill" — оверрайд.
        if (
            mode_setting != "fill"
            and target is not None
            and (
                (
                    _ASD_NODATA < target.speak < _SILENCE_WIDE_MAX
                    and target.width < _SILENCE_MIN_FILL_FACE
                )
                or (target.speak < speak_threshold and target.width < _MIN_FACE_FRAC)
            )
        ):
            regions.append(TrackRegion(t0=t0, t1=t1, mode="fit", points=()))
            prev_fill_end_cx = None
            continue
        # FILL: фокус ЗАФИКСИРОВАН на шот (lock_per_shot, дефолт) — один статичный центр,
        # вмещающий ВСЁ лицо за весь шот → камера НЕ следует за лицом, после склейки сразу стоит
        # на месте (запрос фаундера: «фиксировать фокус, а не следовать»). Субъект, реально
        # проходящий через кадр дальше бюджета кропа → compute_locked_cx=None → фолбэк на слежение
        # (_track_trajectory, init=None → СНАП на лицо с первого кадра, без «пол-лица»).
        # Одна точка ⇒ рендер = постоянный кроп; границы режима / кадровая сетка не тронуты
        # (инвариант docs/REFRAME_FPS_GRID_INVARIANT.md цел). Построение cx-точек вынесено в
        # _fill_points_for_target (переиспользуется vision-форс-fill Layer A, байт-в-байт).
        pts = _fill_points_for_target(
            target, f0, f1, fps,
            crop_w_frac=crop_w_frac, smoothing=smoothing,
            lock_per_shot=lock_per_shot, prev_fill_end_cx=prev_fill_end_cx,
        )  # fmt: skip
        regions.append(TrackRegion(t0=t0, t1=t1, mode="fill", points=pts))
        prev_fill_end_cx = pts[-1].cx
    return regions


def samples_in_shot(
    raw_samples: list[tuple[float, list[tuple[float, float]]]], t0: float, t1: float
) -> list[tuple[float, list[tuple[float, float]]]]:
    """Сэмплы лиц (t, faces), попадающие в полуинтервал плана [t0, t1). PURE."""
    return [(t, faces) for (t, faces) in raw_samples if t0 <= t < t1]


def decide_shot_mode(
    shot_samples: list[tuple[float, list[tuple[float, float]]]],
    *,
    crop_w_frac: float,
    mode_setting: str = "auto",
    wide_ratio: float = 0.5,
) -> str:
    """Один режим ("fill"|"fit") на весь план по геометрии лиц. PURE.

    План = "fit", если доля кадров с широкой геометрией (classify_frame) >= wide_ratio.
    Нет сэмплов -> "fit". mode_setting "fit"/"fill" -- глобальный оверрайд.
    """
    if mode_setting in ("fill", "fit"):
        return mode_setting
    if not shot_samples:
        return "fit"
    fit_frames = sum(1 for _t, faces in shot_samples if classify_frame(faces, crop_w_frac) == "fit")
    return "fit" if fit_frames >= wide_ratio * len(shot_samples) else "fill"


def build_shot_trajectory(
    shot_samples: list[tuple[float, list[tuple[float, float]]]], smoothing: float
) -> tuple[TrackPoint, ...]:
    """Сглаженная cx-траектория ВНУТРИ одного fill-плана. PURE.

    smooth_centers сбрасывается на каждый план (стартует с 0.5) -- пан не "протекает"
    сквозь склейку. cx берётся у КРУПНЕЙШЕГО лица; нет лица -- держим последний.
    """
    cx_raws: list[float | None] = [
        max(faces, key=lambda f: f[1])[0] if faces else None for _t, faces in shot_samples
    ]
    init_cx = next((cx for cx in cx_raws if cx is not None), 0.5)
    cx_sm = smooth_centers(cx_raws, smoothing, init=init_cx)
    return tuple(
        TrackPoint(t=t, mode="fill", cx=cx)
        for (t, _faces), cx in zip(shot_samples, cx_sm, strict=False)
    )


def split_centers_for_shot(
    shot_samples: list[tuple[float, list[tuple[float, float]]]],
    crop_w_frac: float,
    coverage_min: float = 0.6,
) -> tuple[float, float] | None:
    """Пара центров для split-режима по сэмплам лиц плана (editor-путь). PURE.

    Кадр «split-пригоден»: РОВНО 2 лица, разнесённые сильнее ширины 9:16-кропа.
    Таких кадров ≥ coverage_min от всех сэмплов плана → (медиана левых, медиана
    правых). 3+ лиц / нестабильная пара / кластер → None (fit остаётся —
    «горизонтальный вид для других ситуаций», решение фаундера).
    """
    if not shot_samples:
        return None
    lefts: list[float] = []
    rights: list[float] = []
    for _t, faces in shot_samples:
        if len(faces) != 2:
            continue
        cx_a, cx_b = sorted(f[0] for f in faces)
        if cx_b - cx_a > crop_w_frac:
            lefts.append(cx_a)
            rights.append(cx_b)
    if len(lefts) < coverage_min * len(shot_samples):
        return None
    lefts.sort()
    rights.sort()
    return lefts[len(lefts) // 2], rights[len(rights) // 2]


def build_regions_from_shots(
    shots: list[tuple[float, float]],
    raw_samples: list[tuple[float, list[tuple[float, float]]]],
    crop_w_frac: float,
    smoothing: float,
    min_hold_sec: float,
    *,
    mode_setting: str = "auto",
    wide_ratio: float = 0.5,
    split_enabled: bool = False,
) -> list[TrackRegion]:
    """Cut-aligned регионы: ОДИН режим на план, пан внутри fill-плана. PURE.

    Заменяет grid-based build_trajectory+build_regions. Границы режима = границы планов
    (= реальные склейки) -> смена режима только на склейке -> нет флеша. merge_short_regions
    в конце гасит планы короче min_hold (рапид-монтаж).
    split_enabled (v3): «широкий» план с устойчивой парой разнесённых лиц → split
    (верх/низ, как plan_regions в batch-пути) вместо fit; границы НЕ двигаются.
    """
    regions: list[TrackRegion] = []
    for t0, t1 in shots:
        seg = samples_in_shot(raw_samples, t0, t1)
        mode = decide_shot_mode(
            seg, crop_w_frac=crop_w_frac, mode_setting=mode_setting, wide_ratio=wide_ratio
        )
        if mode == "fill":
            pts = build_shot_trajectory(seg, smoothing)
            if not pts:
                pts = (TrackPoint(t=t0, mode="fill", cx=0.5),)
            regions.append(TrackRegion(t0=t0, t1=t1, mode="fill", points=pts))
            continue
        pair = split_centers_for_shot(seg, crop_w_frac) if split_enabled else None
        if pair is not None:
            cx_a, cx_b = pair
            regions.append(
                TrackRegion(
                    t0=t0,
                    t1=t1,
                    mode="split",
                    points=(TrackPoint(t=t0, mode="split", cx=cx_a),),
                    points_b=(TrackPoint(t=t0, mode="split", cx=cx_b),),
                )
            )
        else:
            regions.append(TrackRegion(t0=t0, t1=t1, mode="fit", points=()))
    return merge_short_regions(regions, min_hold_sec)


def detect_cuts(video: Path, start: float, end: float, *, threshold: float = 0.3) -> list[float]:
    """Тайминги склеек источника (ffmpeg scene-detect), КЛИП-относительные.

    Используется ASD speaker-путём (asd_reframe.py). JobError при сбое (№8).
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-ss", str(start), "-to", str(end), "-i", str(video),
        "-vf", f"select='gt(scene,{threshold})',showinfo", "-an", "-f", "null", "-",
    ]  # fmt: skip
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise JobError(_STAGE, f"ffmpeg not found: {e}") from e
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-300:]
        raise JobError(_STAGE, f"ffmpeg scene exit code {proc.returncode}: {tail}")
    cuts: list[float] = []
    for m in re.finditer(r"pts_time:([0-9.]+)", proc.stderr or ""):
        cuts.append(float(m.group(1)))
    return cuts


def _min_scene_frames(fps: float, min_scene_sec: float) -> int:
    """min_scene_len для ContentDetector, привязанный к НАТИВНОМУ fps. PURE.

    PySceneDetect-дефолт min_scene_len=15 кадров = 0.5с@30fps + FlashFilter.MERGE дропает
    ЛЮБУЮ склейку короче 15 нативных кадров ДО того, как она станет границей сцены — так
    глотался быстрый реакционный рез на лицо. Привязываем порог к секундам (0.25с дефолт):
    короткий рез выживает, саб-четвертьсекундный строб всё ещё душится. Пол = 2 кадра
    (ContentDetector требует ≥1; 2 — минимальный осмысленный шот). Δ=0 инвариант не трогаем:
    это фильтр ДЕТЕКЦИИ, границы по-прежнему = реальные нативные кадры склеек.
    """
    return max(2, round(fps * min_scene_sec))


def _scene_cut_offset(fps: float) -> int:
    """Сдвиг кадра склейки PySceneDetect → сетка рендера. PURE.

    detect_scene_cuts реэнкодит сегмент с `-r fps`. На ДРОБНОМ fps (29.97/23.976/59.94…) это
    РЕСЭМПЛ в CFR → сетка детектора опережает нативный декод рендера на кадр, и PySceneDetect
    возвращает склейку на кадр ПОЗЖЕ → вычитаем 1. На ЦЕЛОМ fps (24/25/30/60) `-r` = no-op,
    ресэмпла нет → склейка точна → offset 0. Жёсткий `-1` (старый код) на 25fps уводил границу на
    кадр РАНЬШЕ контент-склейки → 1 кадр старого шота показывался в НОВОМ режиме = флеш на переходе
    tight↔wide (пойман глазами на реальном 25fps Tom Holland: контент-cut 402, PySceneDetect тоже
    402, но -1 → 401 → кадр 401 = старый шот уже в wide). Δ=0 цел (вычитаем ЦЕЛЫЙ кадр).
    """
    return 1 if abs(fps - round(fps)) > 1e-3 else 0


def _argmax_jump(diffs: list[tuple[int, float]], fallback: int) -> int:
    """Кадр с МАКСИМАЛЬНЫМ скачком контента (diff vs предыдущий). Пусто → fallback. PURE."""
    if not diffs:
        return fallback
    return max(diffs, key=lambda d: d[1])[0]


def _refine_cut_native(video: Path, start: float, candidate: int, *, window: int = 4) -> int:
    """Снап PySceneDetect-кандидата на ТОЧНЫЙ кадр контент-склейки в НАТИВНОМ декоде. Fail-safe.

    PySceneDetect гоняется на ПЕРЕ-сжатом сегменте; фиксированный fps-offset (_scene_cut_offset)
    пересаливал на видео, где реэнкод НЕ ресэмплил (репро CARDI B 29.97: PySceneDetect raw=322 =
    истинная склейка, а offset −1 уводил границу на 321 = ФЛЕШ на fill↔fit). Здесь декодим узкое
    окно НАТИВНЫХ кадров [candidate±window] от того же origin (start=aligned_start, как режет
    рендер) и снапим границу на кадр МАКС. скачка яркости → граница == реальная склейка независимо
    от fps/ресэмпла (ground-truth, не эвристика). Δ=0 цел (целые нативные кадры). Сбой ffmpeg / нет
    кадров → возвращаем candidate (без тихого вреда — хуже не делаем).

    ⚠️ Декод КАПИТСЯ `-frames:v (hi-lo+1)` — иначе ffmpeg после `-ss` декодит исходник до EOF
    на КАЖДЫЙ кандидат, оставляя ~9 PNG (на 86-мин сорсе ранние клипы ждали 491–776с в этой
    фазе). select пропускает ровно кадры n∈[lo,hi] → лимит выходных кадров достигается ТОЛЬКО
    после кадра hi → декод глохнет сразу за окном, остановиться РАНЬШЕ hi он не может →
    множество PNG байт-в-байт прежнее. Кап — СЧЁТЧИК кадров в той же n-сетке от -ss-origin,
    что и select (fps не нужен, округления времени нет); `-ss`/select-выражение НЕ трогать —
    сместится нумерация n = выберутся другие кадры. Запас (+margin) счётчику ПРОТИВОПОКАЗАН:
    select больше hi-lo+1 кадров не отдаст → лимит недостижим → декод снова уйдёт в EOF.
    """
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    lo = max(1, candidate - window)
    hi = candidate + window
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        cmd = [
            "ffmpeg", "-y", "-ss", str(start), "-i", str(video),
            "-vf", f"select='gte(n\\,{lo})*lte(n\\,{hi})'", "-vsync", "0",
            "-frames:v", str(hi - lo + 1),
            "-start_number", str(lo), str(out / "r_%06d.png"),
        ]  # fmt: skip
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            return candidate
        if proc.returncode != 0:
            return candidate
        grays: list[tuple[int, Any]] = []
        for p in sorted(out.glob("r_*.png")):
            img = cv2.imread(str(p))
            if img is None:
                continue
            grays.append(
                (int(p.stem.split("_")[1]), cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype("int16"))
            )
    diffs = [
        (grays[i][0], float(np.mean(np.abs(grays[i][1] - grays[i - 1][1]))))
        for i in range(1, len(grays))
    ]
    return _argmax_jump(diffs, candidate)


def detect_scene_cuts(
    video: Path,
    start: float,
    end: float,
    fps: float,
    *,
    threshold: float = 27.0,
    min_scene_sec: float = 0.4,
) -> list[int]:
    """Frame-accurate склейки сегмента (PySceneDetect ContentDetector), КЛИП-относительные КАДРЫ.

    Сегмент режется ffmpeg в temp h264 (декодит AV1, старт=0 → seek-точность), затем
    PySceneDetect на нём. Возвращает номера кадров склеек (0-based от старта сегмента).
    Нет склеек → []. JobError при сбое (№8).

    min_scene_sec: минимальная длина сцены (сек) → min_scene_len кадров в НАТИВНОЙ сетке.
    Явный, чтобы короткий рез не дропался библиотечным дефолтом 15 кадров (см.
    _min_scene_frames).
    """
    from scenedetect import ContentDetector, SceneManager, open_video  # noqa: PLC0415

    t0 = time.monotonic()
    with tempfile.TemporaryDirectory() as td:
        seg = Path(td) / "seg.mp4"
        cut_cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            str(start),
            "-to",
            str(end),
            "-i",
            str(video),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-r",
            str(fps),
            str(seg),
        ]
        try:
            proc = subprocess.run(cut_cmd, capture_output=True, text=True)
        except FileNotFoundError as e:
            raise JobError(_STAGE, f"ffmpeg not found: {e}") from e
        if proc.returncode != 0:
            tail = (proc.stderr or "")[-300:]
            raise JobError(_STAGE, f"ffmpeg seg exit code {proc.returncode}: {tail}")
        vid = None
        try:
            vid = open_video(str(seg))
            sm = SceneManager()
            sm.add_detector(
                ContentDetector(
                    threshold=threshold, min_scene_len=_min_scene_frames(fps, min_scene_sec)
                )
            )
            sm.detect_scenes(vid)
            scenes = sm.get_scene_list()
        except Exception as e:
            raise JobError(_STAGE, f"PySceneDetect failure: {e}") from e
        finally:
            # Закрываем дескриптор ДО выхода из TemporaryDirectory (Windows держит лок на файл).
            # ОБЯЗАТЕЛЬНО в finally: при сбое detect_scenes незакрытый VideoCapture держит лок →
            # cleanup temp роняет PermissionError, маскируя исходную JobError.
            # VideoStreamCv2 не имеет release() — закрываем вложенный cv2.VideoCapture напрямую.
            if vid is not None and hasattr(vid, "capture") and hasattr(vid.capture, "release"):
                vid.capture.release()
    # get_scene_list даёт [(start, end), …]; склейка = start КАДР каждой сцены, кроме первой (0).
    # ⚠️ Сдвиг детектор→рендер ЗАВИСИТ ОТ fps (_scene_cut_offset): дробный fps → -1 (как было на
    # 29.97: cut@261, PySceneDetect 262), ЦЕЛЫЙ fps → 0. Жёсткий -1 на 25fps уводил границу на кадр
    # РАНЬШЕ контент-склейки = 1 кадр старого шота в НОВОМ режиме = флеш на tight↔wide
    # (REFRAME_FPS_GRID_INVARIANT). Δ=0 цел: t0=(cut-off)/fps ТОЧНЫЙ кадр → round(t0*fps)=cut-off.
    # Снап КАЖДОЙ границы на РЕАЛЬНУЮ контент-склейку в нативном декоде (_refine_cut_native) =
    # ground-truth вместо хрупкого fps-offset (_scene_cut_offset вычитал 1 на любом дробном fps;
    # при ТОЧНОМ нативном fps ресэмпла нет → −1 уводил на кадр РАНЬШЕ склейки = ФЛЕШ fill↔fit,
    # репро CARDI B 29.97: raw=322=истина, −1→321). Снап на макс. скачок яркости верен на ЛЮБОМ fps.
    _phase_add("scene_detect", time.monotonic() - t0)
    t0 = time.monotonic()
    refined = [_refine_cut_native(video, start, s[0].get_frames()) for s in scenes[1:]]
    _phase_add("refine", time.monotonic() - t0)
    return refined


# ─────────────────────────── I/O: кадры (ffmpeg) + лица (MediaPipe) ───────────────────────────


def _extract_frames(
    video: Path, start: float, end: float, out_dir: Path, *, fps: float = 5.0
) -> list[Path]:
    """ffmpeg извлекает кадры сегмента в PNG (fps кадров/с). Возвращает пути отсортированно."""
    pattern = str(out_dir / "f_%05d.png")
    cmd = [
        "ffmpeg", "-y", "-ss", str(start), "-to", str(end), "-i", str(video),
        "-vf", f"fps={fps}", "-f", "image2", pattern,
    ]  # fmt: skip
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise JobError(_STAGE, f"ffmpeg not found: {e}") from e
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-300:]
        raise JobError(_STAGE, f"ffmpeg frames exit code {proc.returncode}: {tail}")
    return sorted(out_dir.glob("f_*.png"))


def _ensure_face_model() -> Path:
    """Вернуть путь к модели лица, скачав её в кэш при отсутствии. JobError при сбое."""
    if _FACE_MODEL_PATH.exists():
        return _FACE_MODEL_PATH
    import httpx  # noqa: PLC0415

    _FACE_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = httpx.get(_FACE_MODEL_URL, timeout=60.0)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise JobError(_STAGE, f"cannot download face model: {e}") from e
    _FACE_MODEL_PATH.write_bytes(r.content)
    return _FACE_MODEL_PATH


def sample_faces_continuous(
    video: Path, start: float, end: float, *, fps: float = 5.0
) -> list[tuple[float, list[tuple[float, float]]]]:
    """(клип-время t, [(cx, w_frac), …]) — ВСЕ лица кадра по сэмплам сегмента.

    Дефолт fps=5 (V2, непрерывное слежение; R1 был 2 fps). t клип-относительное (0-based,
    от -ss seek). ВСЕ лица нужны для classify_frame (геометрия широкого плана).
    MediaPipe Tasks API: bbox в ПИКСЕЛЯХ → делим на ширину кадра.
    """
    import cv2  # noqa: PLC0415
    import mediapipe as mp  # noqa: PLC0415
    from mediapipe.tasks import python as mp_python  # noqa: PLC0415
    from mediapipe.tasks.python import vision as mp_vision  # noqa: PLC0415

    model_path = _ensure_face_model()
    options = mp_vision.FaceDetectorOptions(
        base_options=mp_python.BaseOptions(model_asset_path=str(model_path)),
        min_detection_confidence=0.5,
    )
    samples: list[tuple[float, list[tuple[float, float]]]] = []
    with tempfile.TemporaryDirectory() as td:
        frames = _extract_frames(video, start, end, Path(td), fps=fps)
        with mp_vision.FaceDetector.create_from_options(options) as detector:
            for idx, png in enumerate(frames):
                img = cv2.imread(str(png))
                if img is None:
                    continue
                rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                frame_w = rgb.shape[1]
                mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                res = detector.detect(mp_img)
                faces: list[tuple[float, float]] = []
                for d in res.detections:
                    bb = d.bounding_box
                    cx = min(1.0, max(0.0, (bb.origin_x + bb.width / 2) / frame_w))
                    faces.append((cx, bb.width / frame_w))
                samples.append((idx / fps, faces))
    return samples


def _write_reframe_json(out_dir: Path, clip_id: str, regions: list[TrackRegion]) -> None:
    (out_dir / f"reframe_{clip_id}.json").write_text(
        json.dumps(
            {
                "regions": [
                    {
                        "t0": r.t0,
                        "t1": r.t1,
                        "mode": r.mode,
                        "points": [{"t": p.t, "mode": p.mode, "cx": p.cx} for p in r.points],
                        "points_b": [{"t": p.t, "mode": p.mode, "cx": p.cx} for p in r.points_b],
                    }
                    for r in regions
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def pick_binding_windows(
    words: Sequence[Word],
    duration: float,
    *,
    n: int = 2,
    window_sec: float = 60.0,
    min_turn_sec: float = 1.5,
) -> list[tuple[float, float]]:
    """Окна исходника с ДИАЛОГОМ — на них считаем source-привязку голос↔личность. PURE.

    Привязке нужен КОНТРАСТ «этот говорит / этот молчит» по каждому лицу → годятся окна, где реплики
    ЧЕРЕДУЮТСЯ между 2+ спикерами и достаточно длинные (короткие «ага» сигнала не дают). Скользим
    окном с полушагом, оцениваем «сколько слов сказали РАЗНЫЕ спикеры длинными репликами», берём
    top-n НЕПЕРЕСЕКАЮЩИХСЯ. Диалога нет нигде (весь исходник — монолог одного) → [] (привязывать
    нечего: второго голоса просто нет).
    """
    turns = build_turns(words, 0.0, duration)
    good = [t for t in turns if (t.t_end - t.t_start) >= min_turn_sec]
    if len({t.speaker for t in good}) < 2:
        return []
    cands: list[tuple[float, float, float]] = []
    t = 0.0
    while t + window_sec <= max(duration, window_sec):
        ws, we = t, t + window_sec
        inside = [g for g in good if g.t_start >= ws and g.t_end <= we]
        if len({g.speaker for g in inside}) >= 2:
            cands.append((float(sum(g.n_words for g in inside)), ws, we))
        t += window_sec / 2
    cands.sort(key=lambda c: (-c[0], c[1]))
    out: list[tuple[float, float]] = []
    for _, ws, we in cands:
        if all(we <= s or ws >= e for s, e in out):  # непересекающиеся
            out.append((ws, we))
        if len(out) >= n:
            break
    return out


def compute_source_binding(
    video: Path,
    src_w: int,
    src_h: int,
    fps: float,
    words: Sequence[Word],
    windows: Sequence[tuple[float, float]],
    *,
    face_fps: float = 25.0,
    speaker_crop_scale: float = 0.55,
    det_conf: float = 0.25,
    conf_min: float = 0.6,
) -> SourceBinding | None:
    """Привязка ГОЛОС↔ЛИЧНОСТЬ по ИСХОДНИКУ (считается ОДИН раз на видео). I/O.

    По каждому окну-сэмплу (pick_binding_windows — там, где есть диалог): детект + ASD + reID →
    треки. Треки со ВСЕХ окон кластеризуем в ГЛОБАЛЬНЫЕ личности по отпечатку лица (reID → устойчиво
    к смене камер/позиций, в отличие от позиции). Голоса «кластер→личность» собираем в КАЖДОМ окне
    ЕГО реплик и НАКАПЛИВАЕМ, резолвим один раз → надёжная привязка. Дальше ЛЮБОЙ клип (даже
    чисто-монологный, где чередования нет) матчит свои лица к этим личностям (match_identities) и
    знает, КТО говорит → наводится на него.

    Нет окон / <2 личностей / нет голосов → None (ЯВНО: вызыватель работает без source-привязки —
    тогда она считается внутри клипа и требует чередования; монолог уйдёт в wide).
    """
    from app.pipeline.asd_reframe import score_tracks_in_segment  # noqa: PLC0415

    if not windows:
        return None
    per_window_turns: list[list[SpeakerTurn]] = []
    all_tracks: list[SpeakerTrack] = []
    owner: list[int] = []
    for wi, (ws, we) in enumerate(windows):
        aligned = round(ws * fps) / fps
        native = score_tracks_in_segment(
            video, src_w, src_h, aligned, we, face_fps,
            crop_scale=speaker_crop_scale, cuts=(), det_conf=det_conf, compute_embeddings=True,
        )  # fmt: skip
        tracks = [resample_track(t, face_fps, fps) for t in native]
        per_window_turns.append(build_turns(words, aligned, we))
        for t in tracks:
            all_tracks.append(t)
            owner.append(wi)

    groups = cluster_by_embedding(all_tracks)
    if len(groups) < 2:
        return None
    embeddings = tuple(_mean_norm([all_tracks[i].embedding for i in g]) for g in groups)

    # Голоса копим ПО ОКНАМ: лейн = ГЛОБАЛЬНЫЙ индекс личности (окна имеют свои кадровые сетки, но
    # личность — сквозная). Личности нет в окне → пустая дорожка (collect_votes её пропустит).
    empty = SpeakerTrack(f0=0, f1=0, cx=(), width=0.0, speak=NO_ASD)
    votes: dict[int, dict[int, int]] = {}
    for wi, turns_w in enumerate(per_window_turns):
        lanes: list[SpeakerTrack] = []
        for g in groups:
            members = [all_tracks[i] for i in g if owner[i] == wi]
            lanes.append(_aggregate_group(members) if members else empty)
        for spk, lane_votes in collect_votes(turns_w, lanes, fps).items():
            votes.setdefault(spk, {})
            for lane, w in lane_votes.items():
                votes[spk][lane] = votes[spk].get(lane, 0) + w
    if not votes:
        return None
    return SourceBinding(
        embeddings=embeddings,
        cluster_to_identity=resolve_binding(votes, conf_min=conf_min),
    )


def reframe_segment(
    video: Path,
    src_w: int,
    src_h: int,
    start: float,
    end: float,
    *,
    clip_id: str,
    out_dir: Path,
    fps: float,
    mode_setting: str = "auto",
    speaker_crop_scale: float = 0.55,
    face_fps: float = 25.0,
    smoothing: float = 0.15,
    lock_per_shot: bool = True,
    min_hold_sec: float = 1.5,
    speak_threshold: float = 0.0,
    scene_threshold: float = 27.0,
    min_scene_sec: float = 0.4,
    split_enabled: bool = False,
    wide_speak_min: float = 0.3,
    subject_cx: float | None = None,
    vision_shots: Sequence[VisionShot] | None = None,
    keep_distant_cutaways: bool = True,
    words: Sequence[Word] | None = None,
    reframe_mode: str = "auto",
    switch_cfg: SwitchConfig | None = None,
    det_conf: float = 0.5,
    source_binding: SourceBinding | None = None,
) -> tuple[list[TrackRegion], bool]:
    """Сегмент → (cut-aligned регионы, face_found). Единый путь (ASD по дефолту).

    PySceneDetect → кадры склеек → build_shots_frames → score_tracks_in_segment (MediaPipe+ASD)
    → plan_regions → merge_short_regions. Пишет reframe_<clip_id>.json ({regions:[...]}).

    subject_cx (B2, exploratory): vision-подсказка центра субъекта [0,1] → тай-брейкер выбора
    face-track в plan_regions (какой человек в кадре на multi-face шоте). None (дефолт) → прежнее
    поведение байт-в-байт. Проводится ТОЛЬКО когда вызыватель включил REFRAME_VISION_SUBJECT.

    vision_shots (Layer A, exploratory, флаг REFRAME_VISION_LAYOUT у вызывателя): per-shot fill/fit
    + субъект от Gemini (window-relative) → СЕМАНТИЧЕСКИЙ override режима per-shot в plan_regions
    (переопределяет silence/coverage-гейты). НЕ трогает scene-cut границы / кадровую сетку (Δ=0).
    None (дефолт) → прежнее поведение байт-в-байт.
    """
    from app.pipeline.asd_reframe import score_tracks_in_segment  # noqa: PLC0415

    _phase_reset()  # копилка фазовых таймингов ЭТОГО клипа (см. [reframe]-строку ниже)
    t_total = time.monotonic()
    # Геометрия (склейки/шоты/регионы) И рендер работают в НАТИВНОЙ сетке fps источника.
    # origin = aligned_start (= round(start*fps)/fps) — ТА ЖЕ граница кадра, что в render_clip,
    # иначе t0 регионов уезжает на <1 кадр от filtergraph-часов → флеш. ASD остаётся @face_fps
    # (модель требует 25fps), дорожки потом ресемплим в нативную сетку (resample_track).
    aligned_start = round(start * fps) / fps
    duration = end - aligned_start
    total_frames = round(duration * fps)
    crop_w_frac = round(src_h * _ASPECT_W / _ASPECT_H) / src_w

    cuts = detect_scene_cuts(
        video, aligned_start, end, fps, threshold=scene_threshold, min_scene_sec=min_scene_sec
    )
    shots = build_shots_frames(cuts, total_frames)

    # Склейки (нативные кадры) → сетка face_fps: трекинг лиц НЕ тянется через шот-границу.
    # Иначе bbox лица линейно интерполируется старое→новое положение вокруг стыка → cx нового
    # шота стартует «между» лицами → кроп открывается не на лице и «доводится» за кадры (флеш).
    cuts_face = [round(c / fps * face_fps) for c in cuts]
    tracks_native = score_tracks_in_segment(
        video,
        src_w,
        src_h,
        aligned_start,
        end,
        face_fps,
        crop_scale=speaker_crop_scale,
        cuts=cuts_face,
        det_conf=det_conf,
        # reID-отпечатки нужны ТОЛЬКО speaker-режиму (кластеризация в личности) — в обычном пути
        # не тратим CPU на torch-эмбеддер.
        compute_embeddings=(reframe_mode == "speaker"),
    )
    face_found = bool(tracks_native)
    tracks = [resample_track(t, face_fps, fps) for t in tracks_native]

    # Speaker Focus: диар-реплики (clip-относительные) для cut-переключения. None → прежний путь.
    turns = (
        build_turns(words, aligned_start, end) if (words and reframe_mode == "speaker") else None
    )

    # crop_w_frac в merge: короткую перебивку (реальный отдельный шот, напр. 0.9с интервьюера) НЕ
    # проглатываем в предыдущий fill, если его кроп промахнётся мимо нового субъекта → иначе плечо
    # (баг Cardi B clip_04 @40.5с: шот 40.0–40.9с cx 0.64 держал кроп Карди 0.42). Флаг
    # keep_distant_cutaways=false → merge без crop_w_frac (прежнее: всегда глотаем короткие).
    regions = merge_short_regions(
        plan_regions(
            shots,
            tracks,
            fps,
            crop_w_frac=crop_w_frac,
            smoothing=smoothing,
            lock_per_shot=lock_per_shot,
            speak_threshold=speak_threshold,
            mode_setting=mode_setting,
            split_enabled=split_enabled,
            wide_speak_min=wide_speak_min,
            subject_cx=subject_cx,
            vision_shots=vision_shots,
            turns=turns,
            reframe_mode=reframe_mode,
            switch_cfg=switch_cfg,
            source_binding=source_binding,
        ),
        min_hold_sec,
        crop_w_frac=crop_w_frac if keep_distant_cutaways else None,
    )
    if not regions:
        regions = [TrackRegion(t0=0.0, t1=duration, mode="fit", points=())]

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_reframe_json(out_dir, clip_id, regions)
    # Одна компактная строка фазовых таймингов на клип: прод-лог между стартом контейнера и
    # финальной clip-строкой иначе ПУСТ — тюнить перф вслепую нельзя. Фазы копят
    # detect_scene_cuts (scene_detect/refine) и score_tracks_in_segment (sample/detect/asd).
    print(
        f"[reframe] clip={clip_id} "
        f"scene_detect={_phase_get('scene_detect'):.1f}s cuts={len(cuts)} "
        f"refine={_phase_get('refine'):.1f}s sample={_phase_get('sample'):.1f}s "
        f"detect={_phase_get('detect'):.1f}s asd={_phase_get('asd'):.1f}s "
        f"tracks={len(tracks_native)} total={time.monotonic() - t_total:.1f}s"
    )
    return regions, face_found
