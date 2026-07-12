"""Speaker Focus: tight-follow с cut-переключением между спикерами («виртуальная вторая камера»).

PURE-логика (unit-тесты test_speaker_switch), БЕЗ I/O:
  build_turns   — диаризация Word.speaker → реплики (турны, clip-относительные секунды);
  bind_lanes    — привязка кластер-спикер ↔ лейн-лицо голосованием per-turn ASD (speak_frames);
  fill_crop_xrange / crop_iou_x — геометрия 9:16-кропа для IoU-гейта (анти «фотку подвинули»);
  plan_switches — greedy scheduler синтетических cut'ов: lookahead min-hold + hysteresis +
                  IoU-гейт + wide-фолбэк, границы frame-snapped (Δ=0).

Спека: docs/superpowers/specs/2026-07-13-speaker-focus-reframe-design.md.
Инвариант кадровой сетки: docs/REFRAME_FPS_GRID_INVARIANT.md (каждая граница = round(t*fps)/fps).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # только для тайп-хинтов — рантайм duck-typed (нет циклического импорта)
    from app.models import Word
    from app.pipeline.stage3_reframe import SpeakerTrack

WIDE = -1  # SubRegion.target: wide/fit фолбэк (не индекс лейна)
# Порог «этот лейн реально говорит в реплике» для голоса при binding. Light-ASD: >0 ≈ говорит.
# Ниже → голос не отдаём (off-screen спикер: ни одно лицо не говорит → кластер не привязан → None).
_VOTE_MIN = 0.0


@dataclass(frozen=True)
class SpeakerTurn:
    """Одна реплика: непрерывный отрезок слова одного диар-кластера. Clip-относительные секунды."""

    speaker: int
    t_start: float
    t_end: float
    n_words: int


@dataclass(frozen=True)
class SubRegion:
    """Под-регион шота: [t0,t1) с таргетом = индекс лейна (в active-дорожках шота) ИЛИ WIDE."""

    t0: float
    t1: float
    target: int


@dataclass(frozen=True)
class SwitchConfig:
    """Кнобы scheduler'а (см. config.py switch_*). min_hold — главный анти-пинг-понг рычаг."""

    min_hold_sec: float = 1.7
    iou_alpha: float = 0.15
    iou_beta: float = 0.30
    establish_sec: float = 0.0  # зарезервировано под establishing-wide (P2); v1 = 0
    bind_conf_min: float = 0.6


def build_turns(words: Sequence[Word], clip_start: float, clip_end: float) -> list[SpeakerTurn]:
    """Диар-слова → реплики (clip-относительные секунды). PURE.

    Подряд идущие слова ОДНОГО .speaker = одна реплика (турн кончается ТОЛЬКО на смене спикера —
    пауза внутри речи одного человека его слово не отбирает). speaker=None наследует текущего
    (ведущие None-слова без носителя пропускаются). Нет ни одной метки → []."""
    dur = clip_end - clip_start
    turns: list[list[float]] = []  # [speaker, t_start, t_end, n_words]
    cur_spk: int | None = None
    for w in words:
        spk = w.speaker if w.speaker is not None else cur_spk
        if spk is None:
            continue
        ws = max(0.0, w.start - clip_start)
        we = min(dur, w.end - clip_start)
        if turns and int(turns[-1][0]) == spk:
            turns[-1][2] = we
            turns[-1][3] += 1
        else:
            turns.append([float(spk), ws, we, 1.0])
        cur_spk = spk
    return [SpeakerTurn(int(s), t0, t1, int(n)) for s, t0, t1, n in turns if t1 > t0]


def bind_lanes(
    turns: Sequence[SpeakerTurn],
    tracks: Sequence[SpeakerTrack],
    fps: float,
    *,
    conf_min: float = 0.6,
) -> dict[int, int | None]:
    """Кластер-спикер → индекс лейна-лица (в списке ``tracks``) голосованием per-turn ASD. PURE.

    На каждый турн: лейн с МАКС средним speak_frames на кадрах турна (и > _VOTE_MIN — реально
    говорит) отдаёт один голос за turn.speaker. По кластеру winner = argmax голосов (тай →
    меньший индекс); confidence = голоса_winner / всего; < conf_min → None (ambiguous). Кластер
    без единого голоса (все реплики без говорящего on-screen лица) → None (off-screen). tracks
    несут speak_frames @native fps (выровнены по cx). Робастно: один шумный турн не переворачивает
    majority по всему клипу."""
    votes: dict[int, dict[int, int]] = {}
    for turn in turns:
        tf0 = round(turn.t_start * fps)
        tf1 = round(turn.t_end * fps)
        best_lane: int | None = None
        best_score: float | None = None
        for lane, tr in enumerate(tracks):
            if not tr.speak_frames:
                continue
            a, b = max(tf0, tr.f0), min(tf1, tr.f1)
            n_sf = len(tr.speak_frames)
            vals = [tr.speak_frames[i - tr.f0] for i in range(a, b) if 0 <= i - tr.f0 < n_sf]
            if not vals:
                continue
            score = sum(vals) / len(vals)
            if best_score is None or score > best_score:
                best_score, best_lane = score, lane
        if best_lane is not None and best_score is not None and best_score > _VOTE_MIN:
            votes.setdefault(turn.speaker, {})
            votes[turn.speaker][best_lane] = votes[turn.speaker].get(best_lane, 0) + 1

    binding: dict[int, int | None] = {}
    for spk, lane_votes in votes.items():
        total = sum(lane_votes.values())
        winner = max(lane_votes, key=lambda ln: (lane_votes[ln], -ln))
        binding[spk] = winner if lane_votes[winner] / total >= conf_min else None
    for turn in turns:  # спикеры без голосов → явно None (off-screen), не отсутствуют в словаре
        binding.setdefault(turn.speaker, None)
    return binding


def fill_crop_xrange(cx: float, crop_w_frac: float) -> tuple[float, float]:
    """9:16 fill-окно (нормированный x-диапазон), клипуется в [0,1]. PURE."""
    half = crop_w_frac / 2
    lo, hi = cx - half, cx + half
    if lo < 0:
        return (0.0, crop_w_frac)
    if hi > 1:
        return (1.0 - crop_w_frac, 1.0)
    return (lo, hi)


def crop_iou_x(a: tuple[float, float], b: tuple[float, float]) -> float:
    """1-D IoU двух fill-окон (оба full-height → пересечение только по X). PURE."""
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    union = (a[1] - a[0]) + (b[1] - b[0]) - inter
    return inter / union if union > 0 else 0.0


def _snap(t: float, fps: float) -> float:
    """Снап времени на нативный кадр (Δ=0 инвариант)."""
    return round(t * fps) / fps


def _lane_xrange(
    tracks: Sequence[SpeakerTrack], lane: int, crop_w_frac: float
) -> tuple[float, float]:
    tr = tracks[lane]
    cx = sum(tr.cx) / len(tr.cx) if tr.cx else 0.5
    return fill_crop_xrange(cx, crop_w_frac)


def plan_switches(
    turns: Sequence[SpeakerTurn],
    binding: dict[int, int | None],
    tracks: Sequence[SpeakerTrack],
    crop_w_frac: float,
    shot_t0: float,
    shot_t1: float,
    fps: float,
    cfg: SwitchConfig,
) -> list[SubRegion]:
    """Greedy scheduler синтетических cut'ов внутри ОДНОГО шота [shot_t0, shot_t1). PURE.

    Правила (спека §6.5–6.6): переключаемся на нового говорящего ТОЛЬКО если (a) его реплика ≥
    min_hold (короткая/бэкченел → линжер на текущем, L-cut) И (b) текущий шот прожил ≥ min_hold
    (hysteresis). IoU-гейт: cut fill→fill между слишком похожими кропами (IoU > iou_beta, сидят
    близко / почти тот же кадр) → вместо лейна уходим в WIDE (иначе «фотку подвинули»). wide↔fill
    из гейта исключён. Непривязанный кластер (binding None) → WIDE. Границы frame-snapped (Δ=0).
    Границы шота тоже снапим (в проде f0/f1 уже на сетке — no-op; защищает от off-grid входа)."""
    shot_t0, shot_t1 = _snap(shot_t0, fps), _snap(shot_t1, fps)
    if not turns:
        return [SubRegion(shot_t0, shot_t1, WIDE)]

    def target_of(turn: SpeakerTurn) -> int:
        lane = binding.get(turn.speaker)
        return lane if lane is not None else WIDE

    committed: list[tuple[float, int]] = [(shot_t0, target_of(turns[0]))]
    cur_target = committed[0][1]
    cur_start = shot_t0
    for turn in turns[1:]:
        desired = target_of(turn)
        if desired == cur_target:
            continue
        if (turn.t_end - turn.t_start) < cfg.min_hold_sec:  # lookahead: входящий держит слово?
            continue
        sw = _snap(turn.t_start, fps)
        if (sw - cur_start) < cfg.min_hold_sec:  # hysteresis: текущий шот ещё молод
            continue
        if (
            shot_t1 - sw
        ) < cfg.min_hold_sec:  # switch слишком поздно → короткий хвост, линжер до конца
            continue
        if cur_target != WIDE and desired != WIDE:  # IoU-гейт только на fill→fill
            iou = crop_iou_x(
                _lane_xrange(tracks, cur_target, crop_w_frac),
                _lane_xrange(tracks, desired, crop_w_frac),
            )
            if iou > cfg.iou_beta:
                desired = WIDE
        if desired == cur_target or sw <= cur_start or sw >= shot_t1:
            continue
        committed.append((sw, desired))
        cur_target, cur_start = desired, sw

    regs: list[SubRegion] = []
    for i, (st, tg) in enumerate(committed):
        end = committed[i + 1][0] if i + 1 < len(committed) else shot_t1
        if end > st:
            regs.append(SubRegion(st, end, tg))
    return regs
