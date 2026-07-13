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
_IDENTITY_SIM_MIN_DEFAULT = 0.35  # см. stage3_reframe._IDENTITY_SIM_MIN (тот же порог косинуса)
# Порог «этот лейн реально говорит в реплике» для голоса при binding. Light-ASD: >0 ≈ говорит.
# Ниже → голос не отдаём (off-screen спикер: ни одно лицо не говорит → кластер не привязан → None).
_VOTE_MIN = 0.0
# Насколько лучший вариант НАЗНАЧЕНИЯ «голоса↔лица» должен обгонять следующий (в долях, на голос),
# чтобы мы вообще кому-то поверили. Это НЕ «уверенность голоса» (её ASD поза-биасом занижает у
# профильного спикера — см. resolve_binding), а различимость ВАРИАНТОВ. Замер (кейс 04): верный
# вариант обгонял ошибочный на 0.20/голос; равные варианты (0.0) = мы не различаем говорящих → wide.
_BIND_MARGIN_MIN = 0.05
# Сентинель «на этом кадре лица нет / ASD не считался» в speak_frames места (seat). Кадры-гэпы
# при склейке фрагментов в seat (build_seats) НЕ считаются в binding (иначе занижают средний ASD).
NO_ASD = -99.0


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
class SourceBinding:
    """Привязка ГОЛОС↔ЛИЧНОСТЬ, посчитанная ОДИН раз по ИСХОДНИКУ (не по клипу).

    В исходнике реплик много → голосование надёжно; личности стабильны (reID, не позиция) → привязку
    можно применить к ЛЮБОМУ клипу, включая чисто-монологный (где внутри клипа чередования нет и
    привязку не вычислить). Это и есть фикс монолога + устойчивость к смене камер.
    """

    embeddings: tuple[tuple[float, ...], ...]  # отпечаток каждой личности исходника
    cluster_to_identity: dict[int, int | None]  # диар-кластер → индекс личности исходника


@dataclass(frozen=True)
class SwitchConfig:
    """Кнобы scheduler'а (см. config.py switch_*). min_hold — главный анти-пинг-понг рычаг."""

    min_hold_sec: float = 1.7
    iou_alpha: float = 0.15
    iou_beta: float = 0.30
    establish_sec: float = 0.0  # зарезервировано под establishing-wide (P2); v1 = 0
    bind_margin_min: float = _BIND_MARGIN_MIN


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
    margin_min: float = _BIND_MARGIN_MIN,
) -> dict[int, int | None]:
    """Кластер-спикер → индекс лейна-лица (в списке ``tracks``) голосованием per-turn ASD. PURE.

    На каждый турн: лейн с МАКС средним speak_frames на кадрах турна (и > _VOTE_MIN — реально
    говорит) отдаёт голос (вес = n_words) за turn.speaker. Дальше resolve_binding решает ЗАДАЧУ
    НАЗНАЧЕНИЯ (один голос = одно лицо) — см. её docstring: независимый argmax тут ломается на
    поза-биасе ASD. Кластер без единого голоса (все реплики без говорящего on-screen лица) → None
    (off-screen). tracks несут speak_frames @native fps (выровнены по cx).

    ⚠️ БАЗЛАЙН-НОРМАЛИЗАЦИЯ (важно для интервью лицом-к-лицу): вычитаем средний ASD дорожки за весь
    шот. На профиль/tilt-съёмке (спикеры смотрят друг на друга) ASD отражает ПОЗУ (насколько лицо
    фронтально к камере), а не речь → одно лицо всегда выше по АБСОЛЮТУ (репро: правое лицо
    выигрывало все 7 реплик независимо от говорящего). Сигнал речи = ОТКЛОНЕНИЕ ASD дорожки ВВЕРХ от
    ЕЁ базлайна (её поза — константа, сокращается). С нормализацией per-turn winner стал 6/7 верным.
    """
    votes = collect_votes(turns, tracks, fps)
    binding: dict[int, int | None] = resolve_binding(votes, margin_min=margin_min)
    for turn in turns:  # спикеры без голосов → явно None (off-screen), не отсутствуют в словаре
        binding.setdefault(turn.speaker, None)
    return binding


def collect_votes(
    turns: Sequence[SpeakerTurn], tracks: Sequence[SpeakerTrack], fps: float
) -> dict[int, dict[int, int]]:
    """Голоса «диар-кластер → лейн/личность» по репликам. PURE. См. bind_lanes (доктрина там).

    Вынесено отдельно, чтобы НАКАПЛИВАТЬ голоса по НЕСКОЛЬКИМ окнам исходника (source-level
    привязка): личности глобальные (reID), а ASD-свидетельства собираются из разных мест видео.
    Индекс лейна ДОЛЖЕН быть согласован между окнами (глобальный индекс личности).
    """
    baseline: list[float] = []
    for tr in tracks:
        real = [v for v in tr.speak_frames if v > NO_ASD]
        baseline.append(sum(real) / len(real) if real else 0.0)
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
            vals = [
                tr.speak_frames[i - tr.f0]
                for i in range(a, b)
                if 0 <= i - tr.f0 < n_sf and tr.speak_frames[i - tr.f0] > NO_ASD
            ]
            if not vals:
                continue
            score = sum(vals) / len(vals) - baseline[lane]  # отклонение от собственного базлайна
            if best_score is None or score > best_score:
                best_score, best_lane = score, lane
        if best_lane is not None and best_score is not None and best_score > _VOTE_MIN:
            # Голос взвешен ДЛИНОЙ реплики (n_words): длинный турн = больше надёжного ASD-сигнала,
            # короткие обрезки («да», 2 слова) не перевешивают 50-словный ответ. Репро: без веса s0
            # давал ничью 2:2 из-за коротких шумных турнов (conf 0.5 → None); с весом → conf 0.74.
            w = max(1, turn.n_words)
            votes.setdefault(turn.speaker, {})
            votes[turn.speaker][best_lane] = votes[turn.speaker].get(best_lane, 0) + w

    return votes


_MAX_ASSIGN = 6  # больше голосов/лиц → перебор биекций дорог, падаем в жадный режим


def resolve_binding(
    votes: dict[int, dict[int, int]], *, margin_min: float = _BIND_MARGIN_MIN
) -> dict[int, int | None]:
    """Накопленные голоса → привязка ГОЛОС↔ЛИЦО. ОДИН ГОЛОС = ОДНО ЛИЦО (биекция). PURE.

    ⚠️ ПОЧЕМУ НЕ НЕЗАВИСИМЫЙ argmax ПО КАЖДОМУ ГОЛОСУ (так было — ЛОМАЛОСЬ на реале, кейс 04):
    ASD поза-биасен — более фронтальное лицо «выигрывает речь» у ОБОИХ голосов. Замер:
        s0 (женщина): лейн0=34 (44%), лейн1=44 (56%)
        s1 (мужчина): лейн0=14 (24%), лейн1=44 (76%)
    Независимый argmax: мужчина забирает лейн1, женщина тоже тянется туда (56%), не добирает до
    порога уверенности → None → ВСЕ ЕЁ РЕПЛИКИ В WIDE, хотя она ГОВОРИТ и её лицо отлично видно.
    34 голоса за ЕЁ СОБСТВЕННОЕ лицо просто выбрасывались. Это ломает спеку продукта («несколько
    людей → наводимся на ГОВОРЯЩЕГО; wide — только когда непонятно»).

    Физика: один голос принадлежит одному лицу. Значит это ЗАДАЧА НАЗНАЧЕНИЯ, а не N независимых
    argmax'ов. Максимизируем сумму ДОЛЕЙ (доля = вес лейна / все голоса этого голоса — так «громкий»
    спикер с кучей слов не перевешивает тихого) при условии биекции:
        вариант A: s1→лейн1 (0.76) + s0→лейн0 (0.44) = 1.20   ✓ побеждает
        вариант B: s0→лейн1 (0.56) + s1→лейн0 (0.24) = 0.80
    Верная пара находится сама, БЕЗ порога уверенности.

    Мера надёжности — не абсолютная уверенность голоса, а ОТРЫВ лучшего варианта назначения от
    следующего (на голос). Варианты равны → мы реально не различаем говорящих → все None → WIDE
    (правило «непонятно → широкий»). Голос без голосов (спикер ЗА КАДРОМ) → None: не приклеиваем
    его к чужому лицу. Голосов больше, чем лиц → лишние остаются без лица (а не крадут чужое).
    """
    from itertools import permutations  # noqa: PLC0415

    voiced = sorted(s for s, lv in votes.items() if sum(lv.values()) > 0)
    lanes = sorted({ln for lv in votes.values() for ln in lv})
    out: dict[int, int | None] = dict.fromkeys(votes)
    if not voiced or not lanes:
        return out

    share = {
        s: {ln: w / sum(votes[s].values()) for ln, w in votes[s].items()} for s in voiced
    }  # доля голоса за лейн: нормируем, чтобы многословный спикер не давил малословного

    def score(assign: dict[int, int | None]) -> float:
        return sum(share[s].get(ln, 0.0) for s, ln in assign.items() if ln is not None)

    if len(voiced) > _MAX_ASSIGN or len(lanes) > _MAX_ASSIGN:  # деградируем ЯВНО, не молча
        best_assign: dict[int, int | None] = {}
        used: set[int] = set()
        for s in sorted(voiced, key=lambda s: -max(share[s].values())):
            free = [ln for ln in lanes if ln not in used]
            if not free:
                break
            ln = max(free, key=lambda ln: (share[s].get(ln, 0.0), -ln))
            best_assign[s] = ln
            used.add(ln)
        out.update(best_assign)
        return out

    slots = [*lanes, *([None] * max(0, len(voiced) - len(lanes)))]
    best = second = -1.0
    best_assign = {}
    for perm in permutations(slots, len(voiced)):
        cand = dict(zip(voiced, perm, strict=True))
        sc = score(cand)
        if sc > best:
            best, second, best_assign = sc, best, cand
        elif sc > second:
            second = sc
    # отрыв ЛУЧШЕГО варианта от следующего (на голос) — вот это и есть уверенность привязки
    if (best - max(second, 0.0)) / len(voiced) < margin_min:
        return out  # варианты неразличимы → никого не привязываем → WIDE
    out.update(best_assign)
    return out


_COOCCUR_MIN_FRAMES = 5  # столько общих кадров уже достаточно, чтобы считать лица одновременными
_COOCCUR_CX_APART = 0.12  # разнос центров, при котором это ТОЧНО два разных лица (= _SEAT_MERGE_CX)


def _mean_cx_over(t: SpeakerTrack, lo: int, hi: int) -> float | None:
    """Средний cx дорожки на кадрах [lo,hi). PURE. Нет валидных кадров → None."""
    vals = [v for f in range(max(lo, t.f0), min(hi, t.f1)) if (v := t.cx[f - t.f0]) is not None]
    return sum(vals) / len(vals) if vals else None


def _same_frame_conflict(
    a: Sequence[int], b: Sequence[int], tracks: Sequence[SpeakerTrack]
) -> bool:
    """Есть ли кадр, где дорожки групп ВИДНЫ ОДНОВРЕМЕННО и стоят ДАЛЕКО друг от друга? PURE.

    Если да — это ФИЗИЧЕСКИ разные люди (одного человека не бывает дважды в одном кадре), и
    склеивать группы нельзя, что бы ни говорили голоса и эмбеддинги.
    Одновременность на ОДНОМ месте (cx рядом) конфликтом НЕ считается — это дубли трекера на одном
    лице (IOU-трекер регулярно плодит параллельные дорожки одного лица).
    """
    for i in a:
        for j in b:
            ta, tb = tracks[i], tracks[j]
            lo, hi = max(ta.f0, tb.f0), min(ta.f1, tb.f1)
            if hi - lo < _COOCCUR_MIN_FRAMES:
                continue
            ca, cb = _mean_cx_over(ta, lo, hi), _mean_cx_over(tb, lo, hi)
            if ca is not None and cb is not None and abs(ca - cb) > _COOCCUR_CX_APART:
                return True
    return False


def merge_groups_by_voice(
    groups: Sequence[Sequence[int]],
    votes: dict[int, dict[int, int]],
    tracks: Sequence[SpeakerTrack],
) -> tuple[list[list[int]], dict[int, dict[int, int]]]:
    """Слить лица, обслуживающие ОДИН голос → (новые группы, пересчитанные голоса). PURE.

    ⚠️ ЗАЧЕМ (репро на реальном видео). reID разбивает ОДНОГО человека на несколько кластеров,
    когда поза/свет гуляют: замер — две личности ОДНОГО мужчины имели похожесть +0.48, СТОЛЬКО ЖЕ,
    сколько между РАЗНЫМИ людьми (+0.48). Порогом это не лечится — распределения пересекаются (та
    же ловушка, что и везде в этом файле). Итог: голос привязывался к «осколочной» личности, а в
    клипе её отсеивал гейт покрытия (99 кадров из 1800), хотя НАСТОЯЩЕЕ лицо было на месте → голос
    указывал в никуда → весь монолог уходил в WIDE.

    ГОЛОС — сигнал СИЛЬНЕЕ внешности: два лицевых кластера, «загорающихся» на одном и том же голосе,
    принадлежат одному человеку, какими бы ни были их эмбеддинги. Диаризация тут надёжнее reID.

    Лицо БЕЗ голосов (молчащий в кадре) остаётся ОТДЕЛЬНОЙ личностью — не приклеиваем его к чужому
    голосу (иначе кроп уедет на молчуна, когда заговорит другой).

    ⚠️⚠️ ЖЁСТКИЙ ЗАПРЕТ (_same_frame_conflict): НИКОГДА не сливаем группы, чьи лица видны В ОДНОМ
    КАДРЕ на РАЗНЫХ местах. Репро (подкаст Юлия, 4K): поза-биас ASD отдал ОБА лица одному голосу →
    два РАЗНЫХ человека слились в «среднего» (оба матчились к одной source-личности) → кроп уехал бы
    на ЧУЖОГО. Голос — сигнал сильный, но НЕ абсолютный; одновременность в кадре — физика, она
    сильнее. Кейс «мафия» это не ломает: там человек меняет место МЕЖДУ шотами, а не соседствует
    сам с собой.
    """
    # каждому лейну — голос, который он обслуживает (по максимуму веса)
    dominant: dict[int, int] = {}
    best_w: dict[int, int] = {}
    for spk, lane_votes in votes.items():
        for lane, w in lane_votes.items():
            if w > best_w.get(lane, 0):
                dominant[lane], best_w[lane] = spk, w

    merged: list[list[int]] = []
    voice_slot: dict[int, int] = {}
    old_to_new: dict[int, int] = {}
    for li, g in enumerate(groups):
        voice = dominant.get(li)
        slot = voice_slot.get(voice) if voice is not None else None
        # физика важнее голоса: одновременно в кадре на разных местах = разные люди
        if slot is not None and _same_frame_conflict(merged[slot], g, tracks):
            slot = None
        if slot is not None:
            merged[slot].extend(g)
        else:
            slot = len(merged)
            merged.append(list(g))
            if voice is not None and voice not in voice_slot:
                voice_slot[voice] = slot
        old_to_new[li] = slot

    remapped: dict[int, dict[int, int]] = {}
    for spk, lane_votes in votes.items():
        acc = remapped.setdefault(spk, {})
        for lane, w in lane_votes.items():
            slot = old_to_new[lane]
            acc[slot] = acc.get(slot, 0) + w
    return merged, remapped


def clip_binding_from_source(
    source_binding: dict[int, int | None],
    clip_to_source: dict[int, int],
) -> dict[int, int | None]:
    """Source-привязка (голос→личность ИСХОДНИКА) + лейны клипа → привязка ДЛЯ КЛИПА. PURE.

    source_binding: диар-кластер → индекс личности исходника (посчитан ОДИН раз по всему видео, где
    есть диалог → надёжно). clip_to_source: индекс ЛЕЙНА клипа → индекс личности исходника (даёт
    stage3_reframe.identities_from_source — прижимает КАЖДЫЙ трек к личности исходника напрямую).
    Возврат: диар-кластер → индекс лейна клипа.

    Спикер, чьего лица НЕТ в этом клипе (говорит за кадром) → None → вызыватель уйдёт в wide /
    удержит текущего, а не начнёт кропить случайного. Это и есть фикс МОНОЛОГА: даже если в клипе
    ОДИН говорящий (нет чередования, привязку внутри клипа не посчитать), мы знаем его личность из
    исходника → наводимся именно на него.
    """
    src_to_clip = {sj: ci for ci, sj in clip_to_source.items()}
    return {
        cluster: (src_to_clip.get(sj) if sj is not None else None)
        for cluster, sj in source_binding.items()
    }


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
