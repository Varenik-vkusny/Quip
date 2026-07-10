"""Stage 2 (Select): transcript.json → segments.json (list[Segment]). ГЛАВНЫЙ GATE КАЧЕСТВА.

LLM (Gemini, structured output) возвращает ИНДЕКСЫ СЛОВ (не секунды — снимает класс
ошибок «придуманный таймкод»). Точные секунды берём детерминированно из words[idx].

Границы: вся баг-опасная математика (snap-to-sentence, маппинг индексов→секунды,
длительность-гейт, анти-overlap, клиппинг score) — pure-функции с unit-тестами.
Сетевой вызов Gemini — тонкая обёртка (ленивый импорт SDK), JobError при сбое (правило №8).
"""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.config import get_settings
from app.errors import JobError
from app.models import ClipType, Segment, Transcript, Word
from app.pipeline.cut_boundary import _is_sentence_end, snap_start_off_banlist

_STAGE = "select"
# Потолок попыток на primary. Долбимся МНОГО (Google регулярно отдаёт транзиентные 429/503 —
# джоба не должна падать от одного чиха). Ретраятся ТОЛЬКО транзиентные исключения самого
# вызова generate_content (сеть/HTTP-5xx/429) — это дёшево (запрос отвергнут до биллинга
# токенов); парсинг ответа идёт ПОСЛЕ цикла, так что «плохой контент» сюда не попадает и
# деньги не жжёт. backoff equal-jitter min(2^n, 30)с → 20 попыток ≈ покрывают ~5-7 мин
# сбоя Google (главный пайплайн на Modal timeout=3h — с запасом).
_MAX_ATTEMPTS = 20
# При устойчивых сбоях primary пробуем lite (дешевле/другая нагрузка). gemini-2.0-flash УБРАНА
# (мертва с 2026-06-01). primary тут НЕ дублируем (он = s.llm_model = 2.5-flash).
_FALLBACK_MODELS = ("gemini-2.5-flash-lite",)

# ─────────────────────────── pure-постобработка (unit-тесты) ───────────────────────────

# A2: слова-паразиты (filler). Deepgram `filler_words=true` (stage1_transcribe) отдаёт им реальные
# таймстемпы, но inline в транскрипте — их ничто детерминированно не срезает у краёв клипа. Набор
# язык-нейтральный (EN + RU + KK), сверяемся по нормализованному тексту (см. _is_filler). Держим
# ТОЛЬКО однозначные филлеры — режем их лишь на самом КРАЮ клипа (start/end), не внутри, поэтому
# риск задеть омоним-союз («а»/«и») ограничен одним словом у границы.
#
# ⚠️ Провайдеры: Deepgram (EN/RU/…) реально ВЫДЕЛЯЕТ филлеры в отдельные токены (filler_words=true).
# Groq/Whisper (казахский kk-путь, stage1_transcribe авто-роут) filler_words НЕ поддерживает и
# обычно НЕ транскрибирует «э-э/мм» словами → filler-обрезка чаще всего no-op (безопасно, без ложных
# срабатываний). Зато аудио-снап конца к вдоху (по RMS-кривой loudness) работает НА ЛЮБОМ провайдере
# — это и есть главный аудио-рычаг чистых границ для kk.
_FILLERS: frozenset[str] = frozenset(
    {
        # English (Deepgram filler set + распространённые)
        "uh", "uhh", "um", "umm", "uhm", "er", "err", "ah", "ahh", "eh", "hmm", "hm",
        "mm", "mmm", "mhmm", "mhm", "uh-huh", "uh-uh",
        # Russian (вкл. одиночные «э»/«а» — симптом фаундера; «а» омонимичен союзу, но режем
        # ТОЛЬКО у самого края клипа и лишь при аудио-слое → риск ограничен одним словом)
        "э", "ээ", "эм", "эмм", "а", "а-а", "аа", "ну", "мда", "эх",
        # Kazakh (вокализованные хезитации; редкость в Whisper-выводе, но на всякий)
        "ә", "әә", "әм", "мм-м", "əм",
    }
)  # fmt: skip


def _is_filler(text: str) -> bool:
    """Слово-паразит ли (после нормализации: trim, lower, снятие окаймляющей пунктуации). PURE.

    ASR часто пунктуирует филлер («Uh,» / «um.») — сравниваем очищенную форму с _FILLERS.
    """
    t = text.strip().lower().strip(".,!?;:—–-\"'»«…()").strip()
    return t in _FILLERS


def clamp_score(x: float) -> float:
    """Клип score в [0,1] (JSON-schema не гарантирует диапазон — клиппим сами)."""
    return max(0.0, min(1.0, x))


# ─────────────────────── B6: tone whitelist (совпадает с UI ClipCard) ───────────────────────
# UI (ClipCard) рисует emoji-бейдж, цветной glow и энергетический fallback-бар по ТОЧНОМУ совпадению
# tone с этим закрытым набором. Модель регулярно уходит из него («infuriating», «deeply moving»,
# «emotional») → карточка молча теряла emoji/цвет. Нормализуем tone СЕРВЕРНО в postprocess: канон
# как есть, синонимы → канон, неизвестное → дефолт (никогда не доверяем LLM оставаться in-vocab).
_TONE_CANONICAL: frozenset[str] = frozenset(
    {"shocking", "funny", "controversial", "inspiring", "relatable", "insightful", "touching"}
)

# Неизвестный тон → дефолт: «insightful» (самый нейтральный из набора — карточка всегда получает
# осмысленный тон, а не пустой). Задокументировано + проверено тестом (test_tone_normalize).
_TONE_DEFAULT = "insightful"

_TONE_SYNONYMS: dict[str, str] = {
    # controversial
    "infuriating": "controversial", "enraging": "controversial", "angry": "controversial",
    "divisive": "controversial", "provocative": "controversial", "outrageous": "controversial",
    "polarizing": "controversial",
    # touching
    "moving": "touching", "emotional": "touching", "heartfelt": "touching", "sad": "touching",
    "tearjerker": "touching", "poignant": "touching", "wholesome": "touching", "sweet": "touching",
    # inspiring
    "inspirational": "inspiring", "motivating": "inspiring", "motivational": "inspiring",
    "uplifting": "inspiring", "empowering": "inspiring", "hopeful": "inspiring",
    # insightful
    "informative": "insightful", "educational": "insightful", "profound": "insightful",
    "eye-opening": "insightful", "smart": "insightful", "thought-provoking": "insightful",
    "enlightening": "insightful",
    # shocking
    "shock": "shocking", "surprising": "shocking", "jaw-dropping": "shocking", "wild": "shocking",
    "unbelievable": "shocking", "crazy": "shocking", "startling": "shocking",
    # funny
    "hilarious": "funny", "comedic": "funny", "humorous": "funny", "amusing": "funny",
    "lighthearted": "funny", "witty": "funny",
    # relatable
    "real": "relatable", "authentic": "relatable", "honest": "relatable", "candid": "relatable",
}  # fmt: skip


def normalize_tone(raw: str | None) -> str | None:
    """Свести свободный tone LLM к закрытому UI-набору (или None). PURE.

    None/пусто → None (тон не определён). Канон как есть; известный синоним → канон; иначе пробуем
    по ОТДЕЛЬНЫМ токенам («deeply moving» → «moving» → «touching»); неизвестное → ``_TONE_DEFAULT``.
    """
    if raw is None:
        return None
    t = raw.strip().lower()
    if not t:
        return None
    if t in _TONE_CANONICAL:
        return t
    if t in _TONE_SYNONYMS:
        return _TONE_SYNONYMS[t]
    # мультисловный/пунктуированный tone — матчим по токенам (первый совпавший канон/синоним)
    for tok in t.replace("/", " ").replace(",", " ").split():
        if tok in _TONE_CANONICAL:
            return tok
        if tok in _TONE_SYNONYMS:
            return _TONE_SYNONYMS[tok]
    return _TONE_DEFAULT


def resolve_max_clips(requested: int | None, default: int, *, lo: int = 1, hi: int = 30) -> int:
    """Сколько кандидатов отдавать: запрос юзера (степпер) > дефолт; кламп в [lo,hi].

    hi=30 — продуктовый потолок «как найдётся, максимум 30» (UI Auto-режим шлёт 30).
    """
    n = default if requested is None else requested
    return max(lo, min(hi, n))


def _primary_speaker(words: list[Word], start_idx: int, end_idx: int) -> int | None:
    """Спикер БОЛЬШИНСТВА слов клипа [start_idx, end_idx]. None, если меток спикера нет. PURE.

    Детерминированно: макс по числу слов, при равенстве — меньший индекс спикера.
    """
    counts: dict[int, int] = {}
    for w in words[start_idx : end_idx + 1]:
        if w.speaker is not None:
            counts[w.speaker] = counts.get(w.speaker, 0) + 1
    if not counts:
        return None
    return min(counts, key=lambda s: (-counts[s], s))


# How far BACK snap_end_index may look for a sentence end when the clip would otherwise end
# mid-sentence on a mere breath-pause. Wider than max_extend because on long clips / long sentences
# the nearest .?! is often >8 words away in BOTH directions (founder QA: clip_05 ended on "…face"
# because "myself." was 9 words back — one past the ±8 window — and "mean." ~17 words forward).
_SENTENCE_BACK_WINDOW = 20


def snap_end_index(
    words: list[Word],
    end_idx: int,
    max_extend: int = 8,
    min_pause: float = 0.35,
    *,
    start_idx: int | None = None,
    min_dur_sec: float | None = None,
) -> int:
    """Конец клипа → чистая граница (W1 + D2). Приоритет:

    0) D2 (есть speaker-метки И передан start_idx): отрезаем ХВОСТ слов ДРУГОГО спикера (не
       «основного» = большинство клипа) — это «рандомный выкрик другого человека в конце»
       (жалоба фаундера). Без меток (speaker=None) / без start_idx — шаг пропущен → legacy;
    1) слово уже завершает предложение (.?!) → не трогаем;
    2) ближайшее .?! в окне +max_extend → тянем туда; D2: НЕ пересекаем смену спикера
       (иначе .?! следующего спикера утянул бы его реплику в конец клипа);
    3) нет .?! → снап к НАИБОЛЬШЕЙ паузе ≥ min_pause (чистый вдох); D2: пауза на стыке со
       сменой спикера = идеальная граница, дальше неё в чужую речь не идём;
    4) D2: основного перебили и конец не на .?! → назад к его последнему .?! в окне (не
       кончаем клип на полумысли);
    5) иначе без изменений (хвостовой паддинг сгладит луп).
    """
    primary = _primary_speaker(words, start_idx, end_idx) if start_idx is not None else None
    lo = start_idx if start_idx is not None else 0
    # 0) D2: отрезаем хвост другого спикера (до последнего слова основного спикера)
    if primary is not None:
        while end_idx > lo and words[end_idx].speaker not in (None, primary):
            end_idx -= 1
    # 1) уже чистый конец предложения
    if _is_sentence_end(words[end_idx].text):
        return end_idx
    # 2) тянем вперёд к .?!, не пересекая смену спикера
    for k in range(1, max_extend + 1):
        j = end_idx + k
        if j >= len(words):
            break
        if primary is not None and words[j].speaker not in (None, primary):
            break
        if _is_sentence_end(words[j].text):
            return j
    # 3) снап к наибольшей паузе ≥ min_pause (не уходя в чужую речь)
    best_j = end_idx
    best_gap = 0.0
    for k in range(max_extend + 1):
        j = end_idx + k
        if j + 1 >= len(words):
            break
        gap = words[j + 1].start - words[j].end
        if primary is not None and words[j + 1].speaker not in (None, primary):
            # стык со сменой спикера: пауза тут — чистая граница, дальше в чужую речь не идём
            if gap >= min_pause and gap > best_gap:
                best_gap = gap
                best_j = j
            break
        if gap >= min_pause and gap > best_gap:
            best_gap = gap
            best_j = j
    # 4) конец НЕ на .?! → тянем НАЗАД к ближайшему концу предложения (завершённая мысль вместо
    #    вдоха посреди фразы). Широкое окно _SENTENCE_BACK_WINDOW: на длинных клипах ближайший .?!
    #    часто дальше max_extend в ОБЕ стороны (founder QA: clip_05 сел на "…face", т.к. "myself."
    #    в 9 словах назад — на 1 за окном ±8). Работает и БЕЗ диаризации. Не пересекаем смену
    #    спикера / начало клипа и не уходим короче min_dur_sec (иначе клип выкинет гейт длины).
    if not _is_sentence_end(words[best_j].text):
        for k in range(1, _SENTENCE_BACK_WINDOW + 1):
            p = best_j - k
            if p <= lo:
                break
            if not _is_sentence_end(words[p].text):
                continue
            if primary is not None and words[p].speaker not in (None, primary):
                continue
            if (
                start_idx is not None
                and min_dur_sec is not None
                and words[p].end - words[start_idx].start < min_dur_sec
            ):
                break  # дальше назад только короче → сохраняем паузу как границу
            return p
    return best_j


def snap_start_index(
    words: list[Word], start_idx: int, max_extend: int = 5, max_fwd: int = 4
) -> int:
    """Начало клипа → к ГРАНИЦЕ предложения (clean start). Три уровня, backward-first:

    1) предыдущее слово уже завершило предложение → старт чистый, не трогаем
       (так СОХРАНЯЕМ короткие предложения-хуки);
    2) иначе назад к началу текущего предложения (после ближайшего .?! в окне max_extend) —
       включаем зачин мысли;
    3) если начало предложения недостижимо назад → старт сидит в ХВОСТЕ предложения
       (баг «Антимошенника»: первое слово — конец прошлой мысли) → уходим вперёд к началу
       следующего предложения (ближайший .?! в окне max_fwd).
    """
    if start_idx <= 0:
        return 0
    if _is_sentence_end(words[start_idx - 1].text):
        return start_idx
    for k in range(1, max_extend + 1):
        p = start_idx - k
        if p < 0:
            return 0
        if _is_sentence_end(words[p].text):
            return p + 1
    for k in range(max_fwd + 1):
        j = start_idx + k
        if j >= len(words) - 1:
            break
        if _is_sentence_end(words[j].text):
            return j + 1
    return start_idx


def _loudness_at(loudness: list[float], t: float, hop_s: float) -> float | None:
    """Значение RMS-кривой (dB) в момент t сек (окно hop_s). None, если кривая пуста. PURE."""
    if not loudness:
        return None
    idx = int(t / hop_s) if hop_s > 0 else 0
    idx = max(0, min(len(loudness) - 1, idx))
    return loudness[idx]


def refine_boundaries_audio(
    words: list[Word],
    loudness: list[float] | None,
    start_idx: int,
    end_idx: int,
    *,
    hop_s: float = 0.5,
    min_pause: float = 0.35,
    trough_window: int = 4,
    trough_margin_db: float = 3.0,
    max_filler_trim: int = 2,
) -> tuple[int, int]:
    """A2: аудио-осознанное уточнение уже-снапнутых границ (start_idx, end_idx) → (si, ei). PURE.

    Работает ПОВЕРХ детерминированных snap_start_index/snap_end_index — только когда доступна
    RMS-кривая громкости `loudness` (dB по окнам hop_s сек, из preview_moments.extract_loudness):

      1) обрезка слов-паразитов у КРАЁВ: хвостовой филлер («…real. uh») → назад к настоящему
         слову; ведущий филлер («uh, real…») → вперёд;
      2) снап конца к «вдоху»: если конец НЕ на .?! и после него НЕТ паузы ≥ min_pause (снапперы
         уже это исчерпали), тянем ei к БЛИЖАЙШЕЙ глубокой низко-энергетической впадине кривой
         (тихий вдох между фразами) в окне ±trough_window слов, глубиной ≥ trough_margin_db ниже
         текущего уровня. Нет такой впадины → конец не двигаем.

    Деградирует в СТРОГИЙ no-op, когда `loudness` is None/пусто (правило №8: это явная ветка
    «нет аудио-сигнала → оставляем детерминированный результат», НЕ тихий фолбэк). Границы
    остаются в [0, len(words)); si ≤ ei гарантируется (не схлопываем клип).
    """
    si, ei = start_idx, end_idx
    # Нет аудио-сигнала → строгий no-op (оставляем детерминированный снап как есть).
    if not loudness:
        return si, ei
    # Защита от битых индексов (снап в патологии мог инвертировать) — не падаем, отдаём как есть.
    n = len(words)
    if not (0 <= si <= ei < n):
        return si, ei

    # 1a) хвостовой филлер у самого конца → отрезаем к предыдущему настоящему слову
    trimmed = 0
    while ei > si and trimmed < max_filler_trim and _is_filler(words[ei].text):
        ei -= 1
        trimmed += 1
    # 1b) ведущий филлер у самого начала → сдвигаем вперёд к настоящему слову
    trimmed = 0
    while si < ei and trimmed < max_filler_trim and _is_filler(words[si].text):
        si += 1
        trimmed += 1

    # 2) снап конца к вдоху — ТОЛЬКО если конец не чист (не .?! и после него нет паузы).
    if _is_sentence_end(words[ei].text):
        return si, ei
    if ei + 1 < n and (words[ei + 1].start - words[ei].end) >= min_pause:
        return si, ei

    cur_db = _loudness_at(loudness, words[ei].end, hop_s)
    if cur_db is None:
        return si, ei
    lo = max(si, ei - trough_window)
    hi = min(n - 1, ei + trough_window)
    best_j, best_db = ei, cur_db
    # обходим кандидатов от БЛИЖАЙШЕГО к ei → при равной глубине побеждает ближний край.
    for j in sorted(range(lo, hi + 1), key=lambda x: (abs(x - ei), x)):
        db = _loudness_at(loudness, words[j].end, hop_s)
        if db is not None and db < best_db - trough_margin_db:
            best_db, best_j = db, j
    return si, best_j


def indices_to_times(words: list[Word], start_idx: int, end_idx: int) -> tuple[float, float]:
    """Индексы слов → (start_sec, end_sec) с безопасным сдвигом начала назад на 150мс.
    JobError при выходе за диапазон или start>end.
    """
    n = len(words)
    if not (0 <= start_idx < n and 0 <= end_idx < n):
        raise JobError(_STAGE, f"index out of range: start={start_idx}, end={end_idx}, n={n}")
    if start_idx > end_idx:
        raise JobError(_STAGE, f"start_word_index > end_word_index: {start_idx} > {end_idx}")

    start = words[start_idx].start
    # Микро-паддинг 150мс назад для сохранения первой согласной,
    # не залезая на аудио предыдущего слова
    if start_idx > 0:
        start = max(words[start_idx - 1].end, start - 0.15)
    else:
        start = max(0.0, start - 0.15)

    return start, words[end_idx].end


def pad_clip_end(
    end_sec: float, words: list[Word], end_idx: int, duration: float, pad: float = 0.3
) -> float:
    """W1: добить конец клипа тишиной для чистого лупа (вертикальный шортс зацикливается мгновенно —
    резкий обрыв на последнем слове звучит обрублено).

    Расширяем end на `pad` секунд, но НЕ дальше: (а) старта следующего слова (не залезть в чужую
    речь), (б) длительности source. pad<=0 → no-op.
    """
    if pad <= 0:
        return end_sec
    upper = duration
    if end_idx + 1 < len(words):
        upper = min(upper, words[end_idx + 1].start)
    # max(end_sec, …): ASR иногда отдаёт перекрытие (next.start < слово.end) → кламп по
    # next_start не должен уйти РАНЬШЕ конца слова (иначе паддинг режет конечное слово).
    return max(end_sec, min(end_sec + pad, upper))


def resolve_overlaps(segments: list[Segment]) -> list[Segment]:
    """Жадно по убыванию score берём непересекающиеся сегменты; результат сортируем по start."""
    chosen: list[Segment] = []
    for seg in sorted(segments, key=lambda s: s.score, reverse=True):
        if all(seg.end <= c.start or seg.start >= c.end for c in chosen):
            chosen.append(seg)
    chosen.sort(key=lambda s: s.start)
    return chosen


def select_spread(chosen: list[Segment], max_clips: int) -> list[Segment]:
    """Обрезать `chosen` (уже непересекающиеся) до `max_clips`, отдавая приоритет РАСПРЕДЕЛЕНИЮ
    по таймлайну, а не чистому топ-N по score (который может СКУЧИВАТЬ клипы в одном участке). PURE.

    Chunked-отбор поднимает много сильных непересекающихся кандидатов из РАЗНЫХ окон; голый топ-N
    по score мог бы собрать их в первой трети (обратная сторона той же болезни, что мы лечим). Тут:
    делим таймспан кандидатов на `max_clips` равных ВРЕМЕННЫ́Х корзин; каждая корзина оставляет свой
    ОДИН максимум по score; пустые корзины добиваем следующими по score «остатками». Результат —
    max_clips клипов, размазанных по всему видео (а не в начале), отсортированных по start.

    len(chosen) <= max_clips → просто сортировка по start (нечего резать).
    """
    if len(chosen) <= max_clips:
        return sorted(chosen, key=lambda s: s.start)
    lo = min(s.start for s in chosen)
    hi = max(s.end for s in chosen)
    span = (hi - lo) or 1.0
    by_score = sorted(chosen, key=lambda s: s.score, reverse=True)
    picked: dict[int, Segment] = {}
    for seg in by_score:
        mid = (seg.start + seg.end) / 2.0
        bucket = min(max_clips - 1, int((mid - lo) / span * max_clips))
        picked.setdefault(bucket, seg)  # первый (=макс. score) в корзине побеждает
    result = list(picked.values())
    if len(result) < max_clips:
        chosen_ids = {id(s) for s in result}
        for seg in by_score:  # добиваем пустые корзины лучшими по score остатками
            if id(seg) not in chosen_ids:
                result.append(seg)
                if len(result) >= max_clips:
                    break
    result.sort(key=lambda s: s.start)
    return result


def postprocess(
    raw: list[dict[str, Any]],
    words: list[Word],
    *,
    min_sec: float = 20.0,
    max_sec: float = 60.0,
    max_clips: int = 8,
    duration: float | None = None,
    tail_pad: float = 0.0,
    loudness: list[float] | None = None,
    spread: bool = False,
) -> list[Segment]:
    """Сырые сегменты LLM → валидные Segment: snap, маппинг в секунды, длительность-гейт,
    клиппинг score, валидация type, анти-overlap, обрезка до max_clips (топ по score).
    Битый/невалидный сегмент пропускаем, а не роняем весь прогон.

    spread (chunked-отбор): при обрезке до max_clips использовать распределение по таймлайну
    (`select_spread`) вместо голого топ-N по score — чтобы кандидаты из разных окон не скучивались
    в первой трети. По умолчанию False → прежнее поведение (одиночный вызов байт-в-байт).

    A1: границы ДЕТЕРМИНИРОВАННО снапаются к предложениям (snap_start_index/snap_end_index) ДО
    маппинга в секунды — иначе клип кончается на фрагменте / начинается с хвоста мысли (регресс
    18abe24 их молча удалил). A2: при наличии `loudness` (RMS-кривая) поверх снапа работает
    refine_boundaries_audio (обрезка филлеров у краёв + снап конца к вдоху); None → чистый снап.

    W1: длительность-гейт считается по РЕЧИ (до паддинга) — клип ровно на max_sec не дропается;
    `tail_pad`>0 → конец добивается тишиной (`pad_clip_end`) для чистого лупа (нужен `duration`).
    """
    valid_types = {t.value for t in ClipType}
    candidates: list[Segment] = []
    for item in raw:
        try:
            si = int(item["start_word_index"])
            ei = int(item["end_word_index"])
            typ = str(item["type"])
            reason = str(item.get("reason", "")).strip()
            score = clamp_score(float(item.get("score", 0.0)))
        except (KeyError, TypeError, ValueError):
            continue
        if typ not in valid_types:
            continue
        # T1/T2: объяснимость как продукт — топ-хук + разбор «почему». Опциональны
        # (старый raw без них → None; не роняем сегмент).
        hook_raw = item.get("hook")
        why_raw = item.get("why_works")
        style_raw = item.get("hook_style")
        hook = str(hook_raw).strip() or None if hook_raw else None
        why_works = str(why_raw).strip() or None if why_raw else None
        # W2: нормализуем стиль (trim+lower); словарь стилей — в промпте, тут не валидируем
        # жёстко (свободная строка), но пустое → None (обратная совместимость со старым raw).
        hook_style = str(style_raw).strip().lower() or None if style_raw else None
        tone_raw = item.get("tone")
        key_quote_raw = item.get("key_quote")
        # B6: сводим свободный tone LLM к ЗАКРЫТОМУ UI-набору (emoji/glow/energy-fallback в ClipCard
        # ломались на «infuriating»/«deeply moving»/…). Пусто → None (обратная совместимость).
        tone = normalize_tone(str(tone_raw)) if tone_raw else None
        key_quote = str(key_quote_raw).strip() or None if key_quote_raw else None
        try:
            # A1: детерминированный снап к границам предложений (start первым — от него зависит
            # speaker-aware логика конца). A2: аудио-слой поверх (no-op без loudness).
            si = snap_start_index(words, si)
            # Машина B (спека §6): реснэп с ЗАПРЕЩЁННОГО зачина (союз/дейктик/филлер) — клип не
            # открывается словом-связкой (прямой кейс clip_10 «Тут я сначала…»). snap_start_index
            # даёт лишь ГРАНИЦУ предложения; это гарантирует, что предложение — не «вода». No-op на
            # чистом зачине. ei ещё сырой (LLM) — служит верхней границей, дальше него не уходим.
            si = snap_start_off_banlist(words, si, ei)
            ei = snap_end_index(words, ei, start_idx=si, min_dur_sec=min_sec)
            if loudness:
                si, ei = refine_boundaries_audio(words, loudness, si, ei)
            start, end = indices_to_times(words, si, ei)
        except (JobError, IndexError):
            continue
        # Гейт по РЕЧИ (до паддинга): иначе клип у границы max_sec дропнется из-за тишины.
        # si/ei — уже СНАПНУТЫЕ индексы (гейт считаем по реальной длине выбранной речи).
        raw_dur = words[ei].end - words[si].start
        if not (min_sec <= raw_dur <= max_sec):
            continue
        if tail_pad > 0 and duration is not None:
            end = pad_clip_end(end, words, ei, duration, tail_pad)
        candidates.append(
            Segment(
                start=start,
                end=end,
                reason=reason,
                score=score,
                type=ClipType(typ),
                hook=hook,
                why_works=why_works,
                hook_style=hook_style,
                tone=tone,
                key_quote=key_quote,
            )  # fmt: skip
        )
    chosen = resolve_overlaps(candidates)
    if len(chosen) > max_clips:
        if spread:
            chosen = select_spread(chosen, max_clips)
        else:
            top = sorted(chosen, key=lambda s: s.score, reverse=True)[:max_clips]
            chosen = sorted(top, key=lambda s: s.start)
    return chosen


# ─────────────────────────── chunked selection (pure, unit-тесты) ───────────────────────────


def should_chunk(
    transcript: Transcript, *, min_duration_sec: float = 900.0, min_words: int = 3000
) -> bool:
    """Резать ли транскрипт на окна для отбора моментов (chunked selection). PURE.

    True только для ДЛИННЫХ входов (по умолчанию > 15 мин ИЛИ > 3000 слов): там одиночный
    Gemini-вызов теряет recall — на 86-мин интервью (15795 слов) вернулось 2 клипа, оба в первых
    25 мин. Короткий вход → False → одиночный вызов, прежний путь БАЙТ-В-БАЙТ.
    """
    return transcript.duration > min_duration_sec or len(transcript.words) > min_words


def make_windows(
    words: list[Word], *, target_sec: float = 720.0, overlap_sec: float = 75.0
) -> list[tuple[int, int]]:
    """Разбить `words` на перекрывающиеся ВРЕМЕННЫ́Е окна → список АБСОЛЮТНЫХ (start_idx, end_idx),
    обе границы ВКЛЮЧИТЕЛЬНЫ, покрывающих ВЕСЬ транскрипт БЕЗ пропусков с нахлёстом overlap_sec
    (момент на стыке окон виден целиком хотя бы в одном окне). PURE.

    Каждое окно ≈ target_sec речи по word-таймингам; следующее окно стартует через
    (target_sec − overlap_sec) секунд → соседи делят ~overlap_sec контента. Инварианты:
      • старт след. окна ≤ (конец текущего + 1) → НЕТ дыр в покрытии;
      • продвижение ≥ 1 слово за итерацию → нет зацикливания;
      • первое окно стартует с 0, последнее кончается на n−1.
    total ≤ target_sec → одно окно (0, n−1). Пустой вход → []. overlap_sec ≥ target_sec —
    конфиг-ошибка (ValueError, не тихий фолбэк): окно не сдвинулось бы вперёд.
    """
    n = len(words)
    if n == 0:
        return []
    if overlap_sec >= target_sec:
        raise ValueError(f"overlap_sec ({overlap_sec}) must be < target_sec ({target_sec})")
    total = words[-1].end - words[0].start
    if total <= target_sec:
        return [(0, n - 1)]
    stride = target_sec - overlap_sec
    windows: list[tuple[int, int]] = []
    start_i = 0
    while True:
        start_t = words[start_i].start
        end_limit = start_t + target_sec
        end_i = start_i
        while end_i + 1 < n and words[end_i + 1].start < end_limit:
            end_i += 1
        windows.append((start_i, end_i))
        if end_i >= n - 1:  # окно достало до конца транскрипта → готово
            break
        next_start_t = start_t + stride
        new_start = start_i + 1  # гарантируем продвижение минимум на 1 слово
        while new_start < n and words[new_start].start < next_start_t:
            new_start += 1
        start_i = new_start
    return windows


def dedup_raw_segments(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Слить сырые сегменты LLM из перекрывающихся окон: убрать ТОЧНЫЕ дубли по диапазону
    (start_word_index, end_word_index) — один и тот же момент, увиденный в нахлёсте двух соседних
    окон, — оставляя копию с БÓЛЬШИМ score. Порядок = первое появление уникального диапазона.
    Элементы без валидных индексов сохраняем как есть (их отбраковывает postprocess). PURE.
    """
    seen: dict[tuple[int, int], int] = {}  # диапазон → позиция в out
    out: list[dict[str, Any]] = []
    for item in raw:
        try:
            key = (int(item["start_word_index"]), int(item["end_word_index"]))
        except (KeyError, TypeError, ValueError):
            out.append(item)  # битый → пусть postprocess решает (тут не роняем дедуп)
            continue
        if key not in seen:
            seen[key] = len(out)
            out.append(item)
        else:
            pos = seen[key]
            try:
                if float(item.get("score", 0.0)) > float(out[pos].get("score", 0.0)):
                    out[pos] = item
            except (TypeError, ValueError):
                pass
    return out


# Перманентные HTTP-коды Gemini: ключ/доступ/модель/запрос — ретрай их не вылечит.
_PERMANENT_HTTP_CODES = frozenset({400, 401, 403, 404, 422})


def is_transient_gemini_error(exc: BaseException) -> bool:
    """Транзиентная ли ошибка Gemini → стоит ли ретраить.

    Транзиентные (ретраим, free-tier норма): 429 (rate limit), 5xx (server/unavailable),
    сетевые таймауты/обрывы (httpx). Перманентные (роняем СРАЗУ, не маскируя бэкоффом):
    400/401/403/404/422 — битый ключ, нет доступа, несуществующая модель, ошибка схемы.
    Неизвестный тип → консервативно True (лучше лишний ретрай, чем падение на классификации).
    """
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code not in _PERMANENT_HTTP_CODES
    return True


def _backoff_delay(attempt: int, *, cap: float = 30.0) -> float:
    """Задержка перед ретраем: equal-jitter поверх экспоненты с потолком.

    Без jitter все параллельные джобы под provider-wide 503 ретраят В ЛОКСТЕП → каждая волна
    заново спайкает Gemini синхронно. Equal-jitter рассинхронизирует волны, сохраняя осмысленный
    минимум ожидания (половина детерминированного бэкоффа): delay ∈ [0.5·c, c], c = min(2^n, cap).
    """
    ceil = min(2.0**attempt, cap)
    return ceil * (0.5 + 0.5 * random.random())


# ─────────────────────────── промпт + structured output (Gemini) ───────────────────────────

_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "select_moments.v2.txt"


def load_system_prompt() -> str:
    """Системный промпт из prompts/select_moments.v2.txt (его и крутим). Fallback — дефолт ниже."""
    if _PROMPT_PATH.exists():
        return _PROMPT_PATH.read_text(encoding="utf-8")
    return DEFAULT_SYSTEM_PROMPT


DEFAULT_SYSTEM_PROMPT = """\
You are an expert short-form video editor. You receive a word-indexed transcript of a \
video and must select the BEST standalone moments to cut into vertical clips.

Return moments as word index ranges. For each moment choose a `type`:
- hook: a strong opening — a question, bold claim, conflict, or number in the first seconds.
- emotional_peak: a moment of high emotion, tension, or surprise.
- complete_thought: a self-contained idea that makes sense without surrounding context.
- strong_quote: a quotable, punchy line.

For each moment ALSO produce (in this order — classify the feeling BEFORE writing the hook):
- `tone`: the emotion of the moment. Pick EXACTLY ONE word from this closed set:
  shocking / funny / controversial / inspiring / relatable / insightful / touching. Name the
  feeling first — a generic title comes from skipping this.
- `hook_style`: one of story | insight | question | bold_claim | number (story = the speaker's own
  narrative, use their name when known; insight = a generalizable truth framed for the viewer).
- `hook`: a SPECIFIC on-screen TOP title (≤10 words, transcript language, no ending period) written
  IN that style — grounded in WHO is speaking + WHAT the clip reveals, never POV/empty clickbait.
- `why_works`: ONE short sentence (transcript language) on WHY it performs standalone.

HARD RULES:
- The transcript contains `<pause: Xs>` tokens indicating pauses/silences between words,
  and `<Speaker X>:` headers indicating speaker turns. Use these tags to find natural
  sentence/speech boundaries.
- Each word in the transcript is marked with its absolute index `[index]word` (e.g. `[12]think`).
  Use these exact indices for `start_word_index` and `end_word_index` of your selections.
- Each moment MUST be a complete thought: clean start (begin a sentence), clean ending.
- Choose the word indices EXACTLY so that the speaker finishes their thought. Never cut in the
  middle of a sentence, during a dramatic tone/pitch change, or in the middle of a
  rising intonation. The final clip must feel logically complete and satisfying.
  Prefer cutting at `<pause: Xs>` gaps or speaker transitions.
- Respect the HARD length limit stated in the request. Aim for a sweet spot of 30-60 seconds
  (but at least 20 seconds).
- Moments MUST NOT overlap.
- `reason` must be CONCRETE and specific to THIS moment (why it works), not generic.
- `tone`, `hook_style`, `hook`, `why_works` are REQUIRED and specific to each moment.
- High bar, but surface ALL genuinely strong standalone moments — never pad to hit a number.
- `score` in [0,1] = your confidence this clip will perform standalone.
- Use ONLY word indices that exist in the transcript.
"""


class _LlmSegment(BaseModel):
    start_word_index: int
    end_word_index: int
    reason: str
    score: float
    type: ClipType
    # W2: двухступенчатая генерация хука — модель СНАЧАЛА читает тон момента, ПОТОМ под него
    # выбирает стиль, и только затем пишет хук в этом стиле (structured CoT повышает качество).
    tone: str | None = None  # эмоциональный «прочит» момента (shocking/funny/touching/…)
    hook_style: str  # выбранный стиль: pov | relatable | informative | shock | …
    hook: str  # T1: цепляющий топ-заголовок (≤~6 слов) В ВЫБРАННОМ СТИЛЕ, язык транскрипта
    why_works: str  # T2: 1 фраза — почему момент работает как шортс
    key_quote: str | None = None  # самая сильная дословная цитата из клипа (≤180 chars)


class _LlmSelection(BaseModel):
    segments: list[_LlmSegment]


def build_indexed_transcript(words: list[Word], per_line: int = 15, *, start_index: int = 0) -> str:
    """Пронумерованный по словам транскрипт с разметкой пауз и спикеров.
    Каждое слово идет в формате '[индекс]слово'.

    start_index (chunked selection): АБСОЛЮТНЫЙ индекс первого слова слайса `words` в ПОЛНОМ
    транскрипте. Печатаем '[i + start_index]слово', поэтому при подаче ОКНА `words[a:b+1]` с
    start_index=a возвращаемые LLM start_word_index/end_word_index по-прежнему адресуют ГЛОБАЛЬНЫЙ
    words[idx] (postprocess работает над полным списком слов) — это КРИТИЧНО для мержа окон.
    Пауза-детекция внутри слайса (words[i-1]) корректна: слайс непрерывен. Default 0 → прежнее
    поведение байт-в-байт (полный транскрипт, индексы с 0).
    """
    lines: list[str] = []
    current_speaker: int | None = -999  # Сентинел
    current_line: list[str] = []

    for i, w in enumerate(words):
        # 1) Смена спикера
        if w.speaker is not None and w.speaker != current_speaker:
            if current_line:
                lines.append(" ".join(current_line))
                current_line = []
            lines.append(f"\n<Speaker {w.speaker}>:")
            current_speaker = w.speaker

        # 2) Паузы
        if i > 0:
            pause = w.start - words[i - 1].end
            if pause >= 0.4:
                current_line.append(f"<pause: {pause:.1f}s>")
                if current_line:
                    lines.append(" ".join(current_line))
                    current_line = []

        # 3) Слово с АБСОЛЮТНЫМ индексом (i + start_index): при подаче окна индексы остаются
        # глобальными → LLM-выбор адресует полный words[idx] после мержа окон.
        current_line.append(f"[{i + start_index}]{w.text}")

        # Перенос длинных строк
        if len(current_line) >= per_line:
            lines.append(" ".join(current_line))
            current_line = []

    if current_line:
        lines.append(" ".join(current_line))

    return "\n".join(lines).strip()


def build_user_prompt(
    title: str,
    transcript: Transcript,
    indexed: str,
    max_clips: int = 8,
    *,
    min_sec: float = 20.0,
    max_sec: float = 60.0,
) -> str:
    """W1: реальные min/max длины уходят в промпт как ЖЁСТКИЙ лимит (раньше хардкод «15-60s» в
    системном промпте — модель не знала фактического потолка и брала длиннее)."""
    lang = transcript.language or ""
    lang_reminder = (
        f"\n⚠️  LANGUAGE REMINDER: transcript is {lang!r}. "
        f"Write hook / why_works / key_quote in {lang!r} — NEVER in English.\n"
        if lang.lower() not in ("en", "english", "")
        else ""
    )
    return (
        f"Video title: {title}\n"
        f"Language: {lang}  Duration: {transcript.duration:.0f}s  "
        f"Words: {len(transcript.words)}\n"
        f"{lang_reminder}\n"
        f"Word-indexed transcript (each line starts with the index of its first word):\n"
        f"{indexed}\n\n"
        f"LENGTH: the ARC decides the clip length — pick the SHORTEST cut that keeps the hook AND "
        f"the full payoff (completion % beats raw seconds). Every clip MUST be between "
        f"{min_sec:.0f} and {max_sec:.0f} seconds: a clip over {max_sec:.0f}s is rejected (hard "
        f"cap), so if a moment runs longer pick a tighter self-contained sub-window (clean hook "
        f"start AND clean payoff end) — but NEVER truncate the payoff, and NEVER pad a thin moment "
        f"to reach {min_sec:.0f} seconds.\n"
        f"Surface up to {max_clips} of the strongest non-overlapping moments as word index ranges, "
        f"spread across the WHOLE video — include every genuinely strong standalone moment (the "
        f"user will choose among them), but never pad with weak ones."
    )


def call_gemini_structured(
    user_prompt: str,
    *,
    system_prompt: str,
    response_schema: type[BaseModel],
    stage: str = _STAGE,
    usage_sink: dict[str, int] | None = None,
    thinking_budget: int = 0,
) -> str:
    """Вызов Gemini structured output с ретраями → сырой JSON-текст ответа.

    Общая обвязка для select_segments и generate_chapters (editor v3):
    ретраи транзиентных ошибок (free-tier 429/503), backoff capped 30с;
    primary устойчиво падает → fallback-модели (разная нагрузка).
    usage_sink (опц.) заполняется токенами prompt/output/thoughts для лога стоимости.
    """
    from google import genai
    from google.genai import types

    s = get_settings()
    key = s.gemini_api_key
    if key is None:
        raise JobError(stage, "GEMINI_API_KEY is not set (LLM_PROVIDER=gemini)")

    client = genai.Client(api_key=key)
    cfg = types.GenerateContentConfig(
        system_instruction=system_prompt,
        response_mime_type="application/json",
        response_schema=response_schema,
        max_output_tokens=s.llm_max_output_tokens,
        # thinking_budget управляет «thinking» у Gemini 2.5 Flash. 0 = ВЫКЛ (узкие вызовы
        # hook-regen/chapters — CoT не нужен). Стадия ОТБОРА передаёт s.select_thinking_budget>0:
        # «какой момент виральный» — задача на РАССУЖДЕНИЕ (спека §5.1). ВАЖНО: thinking НЕ должен
        # съедать max_output_tokens и обрывать JSON («Unterminated string») — 16000 с запасом на
        # structured-ответ (~n_clips сегментов); проверяем на реальном прогоне.
        thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget),
    )

    resp: Any = None
    last_err: Exception | None = None

    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = client.models.generate_content(
                model=s.llm_model, contents=user_prompt, config=cfg
            )
            break
        except Exception as e:
            last_err = e
            # Перманентная ошибка (ключ/доступ/модель/схема) → роняем сразу с корнем,
            # не маскируя 60с бэкоффа и не дёргая fallback (правило №8 — явная причина).
            if not is_transient_gemini_error(e):
                raise JobError(stage, f"Gemini: non-retryable error: {e}") from e
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_backoff_delay(attempt))

    if resp is None:
        for fb_model in _FALLBACK_MODELS:
            for attempt in range(3):
                try:
                    resp = client.models.generate_content(
                        model=fb_model, contents=user_prompt, config=cfg
                    )
                    break
                except Exception as e:
                    last_err = e
                    if not is_transient_gemini_error(e):
                        raise JobError(stage, f"Gemini: non-retryable error: {e}") from e
                    if attempt < 2:
                        time.sleep(_backoff_delay(attempt))
            if resp is not None:
                break

    if resp is None:
        raise JobError(stage, f"Gemini unavailable after all attempts: {last_err}")

    if usage_sink is not None and resp.usage_metadata is not None:
        um = resp.usage_metadata
        usage_sink["prompt"] = um.prompt_token_count or 0
        usage_sink["output"] = um.candidates_token_count or 0
        usage_sink["thoughts"] = getattr(um, "thoughts_token_count", 0) or 0

    text = resp.text
    if not text:
        raise JobError(stage, "Gemini returned an empty response")
    return str(text)


def select_segments(
    transcript: Transcript,
    title: str,
    *,
    max_clips: int | None = None,
    loudness: list[float] | None = None,
    usage_sink: dict[str, int] | None = None,
    clip_min_sec: int | None = None,
    clip_max_sec: int | None = None,
) -> list[Segment]:
    """Gemini structured output → сырые сегменты → постобработка → list[Segment].

    clip_min_sec/clip_max_sec (опц.) — настройки длины клипов от юзера; None → дефолт конфига.
    max_clips (опц.) — запрошенное юзером число клипов (UI-степпер); None → дефолт из настроек.
    loudness (опц., A3) — RMS-кривая громкости источника (dB, из preview_moments.extract_loudness):
      скармливается refine_boundaries_audio в postprocess для аудио-осознанной чистки границ.
      None / выключенный SELECT_AUDIO_AWARE → чисто детерминированный снап (провайдер-агностик:
      работает и на Deepgram, и на Groq/kk — filler-набор язык-нейтральный, вдох-снап по аудио).
    usage_sink (опц.) заполняется токенами (prompt/output/thoughts) для лога стоимости.

    Chunked (длинное видео, SELECT_CHUNK_ENABLED + should_chunk): транскрипт режется на
    перекрывающиеся временны́е окна (make_windows), КАЖДОЕ окно отбирается отдельным Gemini-вызовом
    по тому же промпту (индексы АБСОЛЮТНЫ через build_indexed_transcript(start_index=…)), кандидаты
    мержатся (dedup_raw_segments) и ОДИН раз прогоняются через postprocess (глобальный анти-overlap
    + spread top-N). Лечит коллапс recall одиночного вызова на гигантском входе. Короткое видео —
    прежний одиночный путь БАЙТ-В-БАЙТ.
    """
    s = get_settings()
    min_sec = clip_min_sec if clip_min_sec is not None else s.clip_min_sec
    max_sec = clip_max_sec if clip_max_sec is not None else s.clip_max_sec
    if min_sec > max_sec:
        raise JobError(_STAGE, "min_sec > max_sec: clip length range is inverted")
    if min_sec > transcript.duration:
        raise JobError(
            _STAGE,
            f"This video is only {transcript.duration:.0f}s long, shorter than the minimum "
            f"clip length of {min_sec:.0f}s. Upload a longer video (Quip is built for "
            "podcasts, interviews and talks) or lower the minimum clip length.",
        )
    n_clips = resolve_max_clips(max_clips, s.max_clips)
    words = transcript.words
    system_prompt = load_system_prompt()
    audio = loudness if s.select_audio_aware else None

    if not (s.select_chunk_enabled and should_chunk(transcript)):
        indexed = build_indexed_transcript(words)
        user_prompt = build_user_prompt(
            title, transcript, indexed, n_clips, min_sec=min_sec, max_sec=max_sec
        )
        text = call_gemini_structured(
            user_prompt,
            system_prompt=system_prompt,
            response_schema=_LlmSelection,
            stage=_STAGE,
            usage_sink=usage_sink,
            thinking_budget=s.select_thinking_budget,
        )
        try:
            raw = json.loads(text).get("segments", [])
        except json.JSONDecodeError as e:
            raise JobError(_STAGE, f"Gemini returned non-JSON: {e}") from e
        return postprocess(
            raw,
            words,
            min_sec=min_sec,
            max_sec=max_sec,
            max_clips=n_clips,
            duration=transcript.duration,
            tail_pad=s.clip_tail_pad_sec,
            loudness=audio,
        )

    windows = make_windows(
        words,
        target_sec=float(s.select_chunk_target_sec),
        overlap_sec=float(s.select_chunk_overlap_sec),
    )
    combined_raw: list[dict[str, Any]] = []
    failures = 0
    for a, b in windows:
        w_slice = words[a : b + 1]
        indexed = build_indexed_transcript(w_slice, start_index=a)
        win_tr = Transcript(
            language=transcript.language,
            duration=w_slice[-1].end - w_slice[0].start,
            words=w_slice,
        )
        user_prompt = build_user_prompt(
            title, win_tr, indexed, n_clips, min_sec=min_sec, max_sec=max_sec
        )
        win_usage: dict[str, int] | None = {} if usage_sink is not None else None
        try:
            text = call_gemini_structured(
                user_prompt,
                system_prompt=system_prompt,
                response_schema=_LlmSelection,
                stage=_STAGE,
                usage_sink=win_usage,
                thinking_budget=s.select_thinking_budget,
            )
            raw = json.loads(text).get("segments", [])
        except (JobError, json.JSONDecodeError) as e:
            failures += 1
            print(f"[2] select: window words[{a}:{b}] failed, skipping: {e}", file=sys.stderr)
            continue
        combined_raw.extend(raw)
        if usage_sink is not None and win_usage:
            for k, v in win_usage.items():
                usage_sink[k] = usage_sink.get(k, 0) + v

    if failures == len(windows):
        raise JobError(_STAGE, f"all {len(windows)} selection windows failed (see logs above)")

    combined_raw = dedup_raw_segments(combined_raw)
    return postprocess(
        combined_raw,
        words,
        min_sec=min_sec,
        max_sec=max_sec,
        max_clips=n_clips,
        duration=transcript.duration,
        tail_pad=s.clip_tail_pad_sec,
        loudness=audio,
        spread=True,
    )


def select_to_file(transcript: Transcript, title: str, out_path: Any) -> list[Segment]:
    """select_segments + запись segments.json. Возвращает list[Segment]."""
    from pathlib import Path

    segments = select_segments(transcript, title)
    payload = [s.model_dump() for s in segments]
    Path(out_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return segments


# ─────────────────────── W4: одноразовый ре-ген хука под новый интервал ───────────────────────
# Хук — хранимый текст; при сдвиге клипа на таймлайне он устаревает. Это УЗКИЙ Gemini-вызов
# (НЕ чат W3) по транскрипту ТОЛЬКО этого клипа, в стиле W2. Цена sub-cent → НЕ метрим (как и
# прочие правки редактора пост-генерации). Триггер — явная кнопка (opt-in, не перетираем молча).

_HOOK_REGEN_STAGE = "hook_regen"
_HOOK_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "regenerate_hook.v1.txt"

DEFAULT_HOOK_PROMPT = """\
You are a viral short-form hook copywriter. You receive the transcript of ONE short video clip
and must write a single scroll-stopping on-screen TOP hook for it. Work in THREE steps:
1) `tone`: the emotion of the clip (funny / shocking / touching / controversial / insightful /
   relatable …) — name the feeling first.
2) `hook_style`: pick what fits — story | insight | question | bold_claim | number.
3) `hook`: write the hook in that style. ≤10 words, SAME language as the transcript, NO ending
   period, grounded in WHO+WHAT — a scroll-stopper, never POV/clickbait or a lie.
Return exactly one tone, one hook_style and one hook.
"""


class _LlmHook(BaseModel):
    tone: str
    hook_style: str
    hook: str


def load_hook_prompt() -> str:
    """Системный промпт ре-гена хука из файла (версионируем без передеплоя). Fallback — дефолт."""
    if _HOOK_PROMPT_PATH.exists():
        return _HOOK_PROMPT_PATH.read_text(encoding="utf-8")
    return DEFAULT_HOOK_PROMPT


def build_hook_regen_prompt(
    clip_text: str, *, language: str, duration: float, style_hint: str | None = None
) -> str:
    """PURE. User-промпт ре-гена: транскрипт клипа + его длина + язык.

    style_hint (опц., W3-агент): пожелание стиля от юзера («шок», «pov», «покороче») — мягкая
    директива, модель всё равно сама финально решает (но учитывает просьбу)."""
    hint = (
        f"\nPreferred style/direction from the user: {style_hint.strip()}"
        if style_hint and style_hint.strip()
        else ""
    )
    return (
        f"Clip language: {language}  Clip length: {duration:.0f}s\n\n"
        f"Clip transcript:\n{clip_text}\n{hint}\n"
        f"Write ONE hook for THIS clip (fill tone, hook_style, hook)."
    )


def parse_hook_response(text: str) -> tuple[str, str | None]:
    """PURE. JSON ответа Gemini → (hook_text, hook_style|None). JobError на битый/пустой."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise JobError(_HOOK_REGEN_STAGE, f"Gemini returned non-JSON: {e}") from e
    hook = str(data.get("hook", "")).strip()
    if not hook:
        raise JobError(_HOOK_REGEN_STAGE, "Gemini did not return hook text")
    style = str(data.get("hook_style", "")).strip().lower() or None
    return hook, style


def regenerate_hook(
    clip_text: str,
    *,
    language: str,
    duration: float,
    style_hint: str | None = None,
    usage_sink: dict[str, int] | None = None,
) -> tuple[str, str | None]:
    """Ре-генерировать ОДИН хук под клип (W4). Возвращает (hook_text, hook_style|None).

    style_hint (опц., W3-агент) — пожелание стиля от юзера, мягко уходит в промпт."""
    prompt = build_hook_regen_prompt(
        clip_text, language=language, duration=duration, style_hint=style_hint
    )
    text = call_gemini_structured(
        prompt,
        system_prompt=load_hook_prompt(),
        response_schema=_LlmHook,
        stage=_HOOK_REGEN_STAGE,
        usage_sink=usage_sink,
    )
    return parse_hook_response(text)
