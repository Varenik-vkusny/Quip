"""Stage 2b (B1, multimodal): Gemini-vision review НАД КАЖДЫМ кандидат-окном клипа.

После текстового отбора (stage2_select) прогоняем МУЛЬТИМОДАЛЬНЫЙ проход по видео+звуку
каждого выбранного окна, чтобы:
  1) оценить, насколько клип ВИРАЛЕН по картинке И звуку (`hook_strength`) — ре-ранк клипов;
  2) проверить/уточнить ЧИСТЫЙ рез краёв (полная мысль, нет филлера/мёртвого воздуха в самом
     начале/конце) — маленький trim, ЖЁСТКО клампится к ±1 слову детерминированной границы.

Экономная форма (см. spec 2026-07-06 §Phase B): НЕ грузим весь 86-мин/532МБ источник — на
КАЖДОЕ окно вырезаем ffmpeg'ом маленький клип (~30с, 480p, со звуком) и шлём ЕГО (inline, если
< ~15МБ, иначе через File API). `media_resolution=low`, звук включён, один structured-вызов на окно.

Границы (правило TDD + чистые функции): вся баг-опасная логика — PURE, под unit-тестами:
  • parse_vision_response  — JSON ответа → VisionReview (кламп hook_strength, nullable-края);
  • clamp_edges_to_words   — защита от дрейфа таймкодов модели (снап края к ±1 слову);
  • apply_review / apply_vision_reviews / rerank_and_cap — применение + ре-ранк.
Сетевой/ffmpeg слой (review_*_with_video) изолирован обёрткой: ретраи транзиентных ошибок
Gemini (как call_gemini_structured), а per-window сбой → НЕЙТРАЛЬНЫЙ review (no-op, логируем) —
батч не падает (правило №8: явная ветка + лог, джоб доходит до done на детерминированном отборе).

B2 (exploratory, флаг REFRAME_VISION_SUBJECT, ДЕФОЛТ OFF): парсим Gemini-box → `subject_cx`
(centre-x субъекта, 0..1) и проводим его в reframe как ТАЙ-БРЕЙКЕР `_pick_target` (какой face-track
вести в multi-face шоте). Границы регионов / fill-vs-fit / scene-cuts / Δ=0 сетку НЕ трогаем.
`subject_hint`/`fill_vs_fit_hint` (текст) по-прежнему НИКУДА не проводятся.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.errors import JobError
from app.models import Segment, Word
from app.pipeline.stage0_import import SourceMeta
from app.pipeline.stage2_select import (
    _backoff_delay,
    clamp_score,
    is_transient_gemini_error,
)

_STAGE = "vision_review"

# Порог inline↔File API: маленький клип (< ~15 МБ) шлём сырыми байтами в запросе (быстрее, без
# отдельной загрузки/поллинга ACTIVE); крупнее — через client.files.upload (File API).
_INLINE_MAX_BYTES = 15 * 1024 * 1024

# Ретраев на окно МЕНЬШЕ, чем у select (_MAX_ATTEMPTS=20): select — GATE качества (бьёмся долго),
# а vision per-window сбой НЕ фатален (→ neutral review, no-op). 6 попыток с capped-бэкоффом ≈
# до ~минуты на окно — потолок латентности не взрываем на N окнах.
_VISION_MAX_ATTEMPTS = 6

# Высота вырезанного окна (px). 480p + media_resolution=low — модели хватает для оценки хука и
# чистоты краёв; байты малы → чаще всего inline (без File API round-trip).
_WINDOW_HEIGHT = 480


@dataclass(frozen=True)
class VisionReview:
    """Результат мультимодального ревью ОДНОГО окна. `ok=False` → нейтральный/сбойный review:
    вызыватели (apply_review) его НЕ применяют — сегмент остаётся детерминированным.

    Времена trim'ов ОТНОСИТЕЛЬНЫ старта окна (0 = seg.start), null = «край не менять».
    subject_hint / fill_vs_fit_hint — текстовые B2-хинты (пока не проводятся в reframe).
    subject_cx — ЧИСЛОВОЙ B2-сигнал (centre-x субъекта из Gemini-box, 0..1); проводится в
    reframe как тай-брейкер `_pick_target` (какой трек вести); флаг REFRAME_VISION_SUBJECT.
    """

    hook_strength: float  # [0,1] — вирусность хука по видео+звуку
    hook_reason: str
    is_complete_thought: bool
    edge_issue_start: bool
    edge_issue_end: bool
    trimmed_start_sec: float | None  # window-relative; None = без изменений
    trimmed_end_sec: float | None  # window-relative; None = без изменений
    subject_hint: str | None  # B2-hint (текст): кого держать в кадре
    fill_vs_fit_hint: str | None  # B2-hint (текст): пер-шот режим
    subject_cx: float | None = None  # B2: centre-x субъекта из Gemini-box [0,1]; None=нет
    ok: bool = True  # False = не применять (нейтральный/сбойный)


def neutral_review() -> VisionReview:
    """Нейтральный review (ok=False) — «нет мультимодального сигнала» для этого окна.

    apply_review вернёт сегмент БЕЗ изменений (детерминированный score/края). НЕ тихий фолбэк:
    это явная ветка «vision недоступен/сбой на окне», всегда сопровождается логом у вызывателя.
    """
    return VisionReview(
        hook_strength=0.0, hook_reason="", is_complete_thought=True,
        edge_issue_start=False, edge_issue_end=False,
        trimmed_start_sec=None, trimmed_end_sec=None,
        subject_hint=None, fill_vs_fit_hint=None, subject_cx=None, ok=False,
    )  # fmt: skip


# ─────────────────────────── pure: parse ───────────────────────────


def subject_cx_from_box(box: Any) -> float | None:
    """PURE. Gemini 2D box `[ymin, xmin, ymax, xmax]` (normalized 0..1000) → subject centre-x [0,1].

    Gemini returns boxes in the [ymin, xmin, ymax, xmax] / 0..1000 convention. We keep ONLY the
    horizontal centre (the reframe crop is a vertical 9:16 slice — only x matters). Returns None on
    absent / malformed / degenerate (zero-or-negative width, e.g. an all-zero "no subject") box —
    an EXPLICIT "no confident subject" signal, never a silent 0.0. Centre clamped into [0,1].
    """
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    try:
        _ymin, xmin, _ymax, xmax = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    if xmax <= xmin:  # zero/negative width → not a real box (degenerate / all-zero)
        return None
    cx = (xmin + xmax) / 2.0 / 1000.0
    return min(1.0, max(0.0, cx))


def parse_vision_response(text: str) -> VisionReview:
    """PURE. JSON structured-ответа Gemini → VisionReview. JobError на битый JSON / без hook_str.

    hook_strength клампится в [0,1] (JSON-schema диапазон не гарантирует). Опциональные поля
    (trim'ы, hint'ы) отсутствуют → None; is_complete_thought по умолчанию True (нейтрально).
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise JobError(_STAGE, f"Gemini returned non-JSON: {e}") from e
    if not isinstance(data, dict) or "hook_strength" not in data:
        raise JobError(_STAGE, "vision response missing hook_strength")
    try:
        hook_strength = clamp_score(float(data["hook_strength"]))
    except (TypeError, ValueError) as e:
        raise JobError(_STAGE, f"vision hook_strength not a number: {e}") from e

    def _opt_float(key: str) -> float | None:
        v = data.get(key)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _opt_str(key: str) -> str | None:
        v = data.get(key)
        s = str(v).strip() if v is not None else ""
        return s or None

    return VisionReview(
        hook_strength=hook_strength,
        hook_reason=str(data.get("hook_reason", "")).strip(),
        is_complete_thought=bool(data.get("is_complete_thought", True)),
        edge_issue_start=bool(data.get("edge_issue_start", False)),
        edge_issue_end=bool(data.get("edge_issue_end", False)),
        trimmed_start_sec=_opt_float("trimmed_start_sec"),
        trimmed_end_sec=_opt_float("trimmed_end_sec"),
        subject_hint=_opt_str("subject_hint"),
        fill_vs_fit_hint=_opt_str("fill_vs_fit_hint"),
        subject_cx=subject_cx_from_box(data.get("subject_box")),
        ok=True,
    )


# ─────────────────────────── pure: edge clamp (drift guard) ───────────────────────────


def _nearest_index(values: list[float], target: float) -> int:
    """PURE. argmin |values[k] - target| (при равенстве — меньший индекс)."""
    return min(range(len(values)), key=lambda k: (abs(values[k] - target), k))


def clamp_edges_to_words(
    words: list[Word],
    seg_start: float,
    seg_end: float,
    new_start_abs: float | None,
    new_end_abs: float | None,
    *,
    max_word_shift: int = 1,
) -> tuple[float, float]:
    """PURE. Кламп модель-предложенных АБСОЛЮТНЫХ краёв к ±max_word_shift слова от детерминированной
    границы seg_start/seg_end → снап на РЕАЛЬНУЮ границу слова (≈ ближайшая пауза) в этом окне.

    Защита от дрейфа таймкодов модели (spec Phase B: «Gemini может подвинуть край ТОЛЬКО в пределах
    ±1 слова/паузы детерминированной границы»). Логика:
      • None-предложение → край НЕ трогаем (оставляем детерминированный, с его -150мс паддингом);
      • иначе среди слов [i−shift .. i+shift] (i = слово, ближайшее к детерминированной границе)
        берём то, чья граница БЛИЖЕ к предложению модели; ушла модель далеко — всё равно не дальше
        shift слов;
      • если победило САМО детерминированное слово (i) → возвращаем ИСХОДНЫЙ seg_start/seg_end
        (сохраняем детерминизм и микро-паддинг, не теряем на снапе к words[i].start);
      • инверсия/схлопывание (start ≥ end) → откат к детерминированным границам (не режем клип).
    Пустой words → детерминированные границы как есть.
    """
    start, end = seg_start, seg_end
    n = len(words)
    if n == 0:
        return start, end
    if new_start_abs is not None:
        si = _nearest_index([w.start for w in words], seg_start)
        cand = range(max(0, si - max_word_shift), min(n, si + max_word_shift + 1))
        j = min(cand, key=lambda k: (abs(words[k].start - new_start_abs), abs(k - si)))
        start = seg_start if j == si else words[j].start
    if new_end_abs is not None:
        ei = _nearest_index([w.end for w in words], seg_end)
        cand = range(max(0, ei - max_word_shift), min(n, ei + max_word_shift + 1))
        j = min(cand, key=lambda k: (abs(words[k].end - new_end_abs), abs(k - ei)))
        end = seg_end if j == ei else words[j].end
    if start >= end:  # патология → не схлопываем клип, откат к детерминированным границам
        return seg_start, seg_end
    return start, end


# ─────────────────────────── pure: apply + rerank ───────────────────────────


def apply_review(seg: Segment, review: VisionReview, words: list[Word]) -> Segment:
    """PURE. Наложить один VisionReview на сегмент → новый Segment.

    ok=False (нейтральный/сбойный) → сегмент БЕЗ изменений. Иначе: score := hook_strength (REUSE
    поля score — оно и сортирует, и видно в карточке; НЕ добавляем модель-поле → без codegen);
    края = clamp_edges_to_words(...) от window-relative trim'ов (0 = seg.start). Прочие поля
    (reason/type/hook/...) сохраняются.
    """
    if not review.ok:
        return seg
    new_start_abs = (
        seg.start + review.trimmed_start_sec if review.trimmed_start_sec is not None else None
    )
    new_end_abs = seg.start + review.trimmed_end_sec if review.trimmed_end_sec is not None else None
    start, end = clamp_edges_to_words(words, seg.start, seg.end, new_start_abs, new_end_abs)
    return seg.model_copy(
        update={"start": start, "end": end, "score": clamp_score(review.hook_strength)}
    )


def rerank_and_cap(segments: list[Segment], max_clips: int) -> list[Segment]:
    """PURE. Стабильная сортировка по score УБЫВ. + обрезка до max_clips (best-first).

    Стабильность (Python sort) сохраняет исходный порядок при равных score.
    """
    return sorted(segments, key=lambda s: s.score, reverse=True)[:max_clips]


def apply_vision_reviews_indexed(
    segments: list[Segment],
    reviews: list[VisionReview],
    words: list[Word],
    *,
    max_clips: int,
) -> tuple[list[Segment], list[float | None]]:
    """PURE. Как apply_vision_reviews, но ДОПОЛНИТЕЛЬНО возвращает subject_cx, ВЫРОВНЕННЫЙ по
    ре-ранкнутым сегментам (B2: центр субъекта должен ехать ВМЕСТЕ со «своим» клипом сквозь
    сортировку по score, т.к. Segment — read-only контракт, поле subject_cx туда не добавить).

    len(reviews) короче segments (тех.сбой) → недостающие = нейтральные (no-op, subject_cx=None).
    Сортировка идентична apply_vision_reviews (стабильный sort по score убыв. + обрезка).
    """
    applied: list[tuple[Segment, float | None]] = [
        (
            apply_review(seg, reviews[i] if i < len(reviews) else neutral_review(), words),
            reviews[i].subject_cx if i < len(reviews) else None,
        )
        for i, seg in enumerate(segments)
    ]
    ordered = sorted(applied, key=lambda p: p[0].score, reverse=True)[:max_clips]
    return [seg for seg, _ in ordered], [cx for _, cx in ordered]


def apply_vision_reviews(
    segments: list[Segment],
    reviews: list[VisionReview],
    words: list[Word],
    *,
    max_clips: int,
) -> list[Segment]:
    """PURE. Наложить reviews на segments (по индексу) и ре-ранкнуть best-first до max_clips.

    len(reviews) короче segments (тех.сбой) → недостающие трактуются как нейтральные (no-op).
    Тонкая обёртка над apply_vision_reviews_indexed (единый источник порядка) — отбрасывает
    subject_cx для вызывателей, которым нужен только ре-ранк сегментов.
    """
    segs, _ = apply_vision_reviews_indexed(segments, reviews, words, max_clips=max_clips)
    return segs


# ─────────────────────────── prompt + structured schema (Gemini) ───────────────────────────

_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "vision_review.v1.txt"

DEFAULT_VISION_PROMPT = """\
You are an expert short-form (vertical Reels/TikTok/Shorts) video editor. You are given ONE
candidate clip — a short video WITH AUDIO, already cut from a longer source. Judge it as a
STANDALONE vertical short and return a single JSON object.

Score BOTH picture AND sound:
- hook_strength [0..1]: how strongly the FIRST ~2 seconds grab attention and how well the clip
  pays off — on the VISUAL (face, expression, motion, on-screen action) AND the AUDIO (a bold
  claim, a question, tension, emotion, a punchy line). A slow, contextless or low-energy opening
  scores LOW; a scroll-stopping hook with a satisfying payoff scores HIGH.
- hook_reason: ONE short sentence — WHY it does or does not hook (name the concrete signal).
- is_complete_thought: true if the clip is a self-contained, finished thought (no dangling setup,
  no cut-off conclusion).

Clean-cut edges (be CONSERVATIVE — only flag REAL problems at the very edges):
- edge_issue_start / edge_issue_end: true if there is a filler word ("uh/um/э/а"), a partial/
  cut-off word, or dead air at the very START / very END of the clip.
- trimmed_start_sec / trimmed_end_sec: if (and ONLY if) there is such an edge problem, give a
  SMALL refined in/out time IN SECONDS RELATIVE TO THE START OF THIS CLIP (0 = first frame).
  Trim at most a fraction of a second — a word or two. If the edge is already clean, return null.

Also return (used later for vertical reframing, does not affect this decision — best effort):
- subject_box: the 2D bounding box of the ONE person/subject the viewer should watch, as
  [ymin, xmin, ymax, xmax] normalized to 0-1000 (Gemini box convention). null if unsure or if
  there is no single clear subject (e.g. a wide shot of several equally-important people).
- subject_hint: a short label for that same subject (e.g. "left speaker", "the woman").
- fill_vs_fit_hint: "fill" (single tight subject → crop) or "fit" (wide / multiple people / text).

Return ONLY the JSON object with these exact keys.
"""


def load_vision_prompt() -> str:
    """Промпт ревью из prompts/vision_review.v1.txt (крутим без передеплоя); иначе — дефолт ниже."""
    if _PROMPT_PATH.exists():
        return _PROMPT_PATH.read_text(encoding="utf-8")
    return DEFAULT_VISION_PROMPT


class _VisionSchema(BaseModel):
    """JSON-схема structured output Gemini (порядок полей = порядок «рассуждения» модели)."""

    hook_strength: float
    hook_reason: str
    is_complete_thought: bool
    edge_issue_start: bool
    edge_issue_end: bool
    trimmed_start_sec: float | None = None
    trimmed_end_sec: float | None = None
    subject_box: list[int] | None = None  # B2: [ymin,xmin,ymax,xmax] 0..1000 → subject centre-x
    subject_hint: str | None = None
    fill_vs_fit_hint: str | None = None


def _build_user_prompt(seg: Segment, window_dur: float) -> str:
    """PURE. Короткий user-промпт окна: длина клипа + напоминание про относительные секунды."""
    return (
        f"This clip is {window_dur:.1f} seconds long. Its transcript-derived reason for selection "
        f"was: {seg.reason!r}. Judge the hook (picture + sound) and the clean-cut edges. "
        f"Any trimmed_start_sec / trimmed_end_sec MUST be in seconds relative to the start of THIS "
        f"clip (0 = first frame, {window_dur:.1f} = last frame)."
    )


# ─────────────────────────── ffmpeg window extraction ───────────────────────────


def _extract_window(source: Path, start: float, end: float, out_path: Path) -> None:
    """Вырезать окно [start,end] источника в маленький 480p mp4 СО ЗВУКОМ (модели нужен звук для
    хука/чистого реза). JobError при отсутствии ffmpeg или ненулевом коде (правило №8).

    `-ss <start>` до `-i` (быстрый input-seek), `-t <dur>` на выходе (однозначная длительность —
    без `-to` перед `-i`, у которого семантика «относительно -ss» неочевидна). scale=-2:480 —
    высота 480, ширина авто-чётная. Звук перекодируем в aac (окно — короткий independent клип).
    """
    dur = max(0.0, end - start)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{dur:.3f}",
        "-vf", f"scale=-2:{_WINDOW_HEIGHT}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        str(out_path),
    ]  # fmt: skip
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise JobError(_STAGE, f"ffmpeg not found: {e}") from e
    if proc.returncode != 0:
        raise JobError(_STAGE, f"ffmpeg exit code {proc.returncode}: {(proc.stderr or '')[-400:]}")
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise JobError(_STAGE, f"ffmpeg produced no window file for [{start:.1f},{end:.1f}]")


def _media_resolution(name: str, types: Any) -> Any:
    """`"low"|"medium"|"high"` → types.MediaResolution.*; неизвестное → LOW (дешёвый дефолт)."""
    table = {
        "low": types.MediaResolution.MEDIA_RESOLUTION_LOW,
        "medium": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
        "high": types.MediaResolution.MEDIA_RESOLUTION_HIGH,
    }
    return table.get(name.strip().lower(), types.MediaResolution.MEDIA_RESOLUTION_LOW)


def _prepare_contents(
    client: Any, types: Any, temp: Path, prompt: str
) -> tuple[list[Any], str | None]:
    """Собрать `contents` для generate_content: маленький файл → inline Part.from_bytes; крупный →
    File API (upload + поллинг до ACTIVE). Возвращает (contents, имя_загруженного_файла|None).
    """
    size = temp.stat().st_size
    if size <= _INLINE_MAX_BYTES:
        part = types.Part.from_bytes(data=temp.read_bytes(), mime_type="video/mp4")
        return [prompt, part], None
    # File API: > ~15МБ — грузим и ждём готовности (PROCESSING → ACTIVE) перед генерацией.
    f = client.files.upload(file=str(temp))
    for _ in range(60):  # ~60с потолок ожидания обработки видео
        if f.state == types.FileState.ACTIVE:
            break
        if f.state == types.FileState.FAILED:
            raise JobError(_STAGE, "File API processing failed")
        time.sleep(1.0)
        f = client.files.get(name=f.name)
    if f.state != types.FileState.ACTIVE:
        raise JobError(_STAGE, f"File API not ACTIVE after wait (state={f.state})")
    return [prompt, f], f.name


def _call_gemini_vision(
    client: Any,
    model: str,
    contents: list[Any],
    cfg: Any,
    *,
    usage_sink: dict[str, int] | None,
) -> str:
    """generate_content с ретраями транзиентных ошибок (как call_gemini_structured, но контент —
    мультимодальный). Контент готовится ОДИН раз вызывателем (upload не повторяем на каждый ретрай).
    JobError на перманентной ошибке (ключ/доступ/схема) или после всех попыток. Отдаёт JSON-текст.
    """
    resp: Any = None
    last_err: Exception | None = None
    for attempt in range(_VISION_MAX_ATTEMPTS):
        try:
            resp = client.models.generate_content(model=model, contents=contents, config=cfg)
            break
        except Exception as e:  # noqa: BLE001 — классифицируем транзиент/перманент ниже
            last_err = e
            if not is_transient_gemini_error(e):
                raise JobError(_STAGE, f"Gemini: non-retryable error: {e}") from e
            if attempt < _VISION_MAX_ATTEMPTS - 1:
                time.sleep(_backoff_delay(attempt))
    if resp is None:
        raise JobError(_STAGE, f"Gemini vision unavailable after all attempts: {last_err}")
    if usage_sink is not None and resp.usage_metadata is not None:
        um = resp.usage_metadata
        usage_sink["prompt"] = usage_sink.get("prompt", 0) + (um.prompt_token_count or 0)
        usage_sink["output"] = usage_sink.get("output", 0) + (um.candidates_token_count or 0)
        usage_sink["thoughts"] = usage_sink.get("thoughts", 0) + (
            getattr(um, "thoughts_token_count", 0) or 0
        )
    text = resp.text
    if not text:
        raise JobError(_STAGE, "Gemini returned an empty response")
    return str(text)


def _review_one(
    client: Any,
    types: Any,
    source: Path,
    seg: Segment,
    *,
    model: str,
    system_prompt: str,
    media_res: str,
    tmpdir: Path,
    idx: int,
    usage_sink: dict[str, int] | None,
) -> VisionReview:
    """Ревью ОДНОГО окна. Любой сбой (ffmpeg/upload/Gemini/парс) → NEUTRAL review (логируем, не
    роняем батч; правило №8 — явная ветка «окно не отревьюено», не тихий фолбэк). Temp-файл чистим.
    """
    temp = tmpdir / f"win_{idx:02d}.mp4"
    uploaded_name: str | None = None
    try:
        _extract_window(source, seg.start, seg.end, temp)
        contents, uploaded_name = _prepare_contents(
            client, types, temp, _build_user_prompt(seg, seg.end - seg.start)
        )
        cfg = types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_schema=_VisionSchema,
            media_resolution=_media_resolution(media_res, types),
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        text = _call_gemini_vision(client, model, contents, cfg, usage_sink=usage_sink)
        return parse_vision_response(text)
    except Exception as e:  # noqa: BLE001 — per-window изоляция: neutral review, батч не падает
        print(f"[2b] vision: window {idx} ({seg.start:.1f}-{seg.end:.1f}) failed → neutral: {e}",
              file=sys.stderr)  # fmt: skip
        return neutral_review()
    finally:
        if uploaded_name is not None:
            try:
                client.files.delete(name=uploaded_name)
            except Exception as e:  # noqa: BLE001 — cleanup best-effort (File API TTL всё равно 48ч)
                print(f"[2b] vision: file cleanup failed ({uploaded_name}): {e}", file=sys.stderr)
        temp.unlink(missing_ok=True)


def review_segments_with_video(
    source_mp4: Path,
    segments: list[Segment],
    meta: SourceMeta,
    *,
    settings: Any,
    usage_sink: dict[str, int] | None = None,
) -> list[VisionReview]:
    """B1: мультимодальный проход по КАЖДОМУ окну segments → list[VisionReview] (той же длины).

    Нет gemini_api_key → ВЕСЬ проход пропускаем: логируем предупреждение и отдаём НЕЙТРАЛЬНЫЕ
    reviews (apply → no-op, детерминированный отбор сохраняется; правило №8 — явная ветка + лог,
    джоб доходит до done). Per-window сбой → neutral для ЭТОГО окна (внутри _review_one). Окна
    вырезаются во временную папку (авто-очистка). `meta` — для будущего B2 (сейчас не нужен).
    """
    key = getattr(settings, "gemini_api_key", None)
    if not key:
        print("[2b] vision: GEMINI_API_KEY not set — skipping vision review (deterministic only)",
              file=sys.stderr)  # fmt: skip
        return [neutral_review() for _ in segments]
    if not segments:
        return []

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=key)
    model = getattr(settings, "vision_review_model", "gemini-2.5-flash")
    media_res = getattr(settings, "vision_review_media_res", "low")
    system_prompt = load_vision_prompt()

    reviews: list[VisionReview] = []
    with tempfile.TemporaryDirectory(prefix="quip_vision_") as td:
        tmpdir = Path(td)
        for i, seg in enumerate(segments, start=1):
            reviews.append(
                _review_one(
                    client,
                    types,
                    source_mp4,
                    seg,
                    model=model,
                    system_prompt=system_prompt,
                    media_res=media_res,
                    tmpdir=tmpdir,
                    idx=i,
                    usage_sink=usage_sink,
                )  # fmt: skip
            )
    return reviews
