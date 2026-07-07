"""CLI E2E: Stage 0→5 для одного источника → клипы + job.json + runs.jsonl.

Запуск:
  uv run python -m app.run <job_id> [<youtube_url>]
  - есть data/<job_id>/source.mp4 → import пропускается (кэш);
  - есть transcript.json/segments.json → стадии 1/2 берутся из кэша (не платим повторно).

Это склейка (план §4А F): вся логика — в pure-функциях стадий, здесь только оркестрация
+ запись статуса/телеметрии. Артефакты — в data/<job_id>/ (см. README).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from app import billing, db, dispatch, storage, ytdlp_cookies, ytdlp_proxy_pool
from app.config import get_settings
from app.editor.reframe_cache import build_persist_payload
from app.errors import JobError, YoutubeBotGateError
from app.models import ClipOut, Job, JobStatus, Metrics, Segment, Transcript, Word
from app.pipeline.preview_moments import (
    detect_energy_moments,
    detect_preview_moments,
    extract_loudness,
    merge_moments,
)
from app.pipeline.stage0_import import SourceMeta, build_preview_proxy, import_youtube
from app.pipeline.stage1_transcribe import (
    DEEPGRAM_NOVA_USD_PER_MIN,
    DEEPGRAM_UNSUPPORTED,
    GROQ_TURBO_USD_PER_MIN,
    transcribe_to_file,
)
from app.pipeline.stage2_select import select_segments
from app.pipeline.stage2b_vision_review import (
    VisionShot,
    apply_vision_reviews_full,
    review_segments_with_video,
)
from app.pipeline.stage3_reframe import reframe_segment
from app.pipeline.stage4_captions import words_in_segment
from app.pipeline.stage5_render import clamp_output_dims, render_clip
from app.transcript_cache import audio_sha, cache_key, evict, get_cached, put_cached

DATA_ROOT = Path(__file__).resolve().parents[1] / "data"  # services/worker/data

# Gemini flash (прибл.): input $0.30/M, output (вкл. thoughts) $2.50/M.
_GEMINI_IN_USD_PER_TOK = 0.30 / 1_000_000
_GEMINI_OUT_USD_PER_TOK = 2.50 / 1_000_000


def _snippet(words: list[Word], start: float, end: float, limit: int = 240) -> str:
    return " ".join(w.text for w in words_in_segment(words, start, end))[:limit]


def transcript_cache_model(settings: Any) -> str:
    """PURE. Имя модели для ключа кэша транскрипта, ЗАВИСЯЩЕЕ от провайдера.

    Раньше run.py клал в ключ всегда ``deepgram_model`` — для assemblyai это мусорный слот
    (транскрипт помечался deepgram-моделью, смена assemblyai-модели не инвалидировала кэш).
    Берём модель выбранного провайдера; для assemblyai читаем ``assemblyai_model`` через
    getattr (поле опционально в config — провайдер ещё не реализован, дефолт безопасный).
    """
    provider = settings.transcription_provider
    if provider == "assemblyai":
        return str(getattr(settings, "assemblyai_model", None) or "assemblyai-default")
    return str(settings.deepgram_model)


def _gemini_cost(usage: dict[str, int]) -> float:
    if not usage:
        return 0.0
    out_tok = usage.get("output", 0) + usage.get("thoughts", 0)
    return round(
        usage.get("prompt", 0) * _GEMINI_IN_USD_PER_TOK + out_tok * _GEMINI_OUT_USD_PER_TOK, 4
    )


def clip_spawn_args(
    job_id: str, segments: list[Segment], meta: SourceMeta, user_id: str | None
) -> list[tuple[str, int, dict[str, Any], dict[str, Any], str | None]]:
    """Аргументы фан-аута: кортеж на сегмент ``(job_id, clip_index, seg, meta, user_id)``. PURE.

    ``clip_index`` 1-based (совпадает с ``clip_id`` ``clip_{i:02d}``). seg/meta — ``model_dump``
    для переноса через границу Modal (cloudpickle-дружелюбные dict'ы). ``user_id`` — владелец
    джоба: на фан-аут-контейнере по нему резолвится план (вотермарка/разрешение) СЕРВЕРНО.
    """
    md = meta.model_dump()
    return [(job_id, i, seg.model_dump(), md, user_id) for i, seg in enumerate(segments, start=1)]


def build_clip_out(
    clip_id: str, seg: Segment, transcript_words: list[Word], video_url: str
) -> ClipOut:
    """Сегмент + слова транскрипта + готовый ``video_url`` → ClipOut (wire). PURE.

    Сниппет/слова считаются по окну сегмента — НЕ зависят от того, где рендерился клип
    (локально или на фан-аут-контейнере).
    """
    return ClipOut(
        id=clip_id,
        start=seg.start,
        end=seg.end,
        duration=round(seg.end - seg.start, 2),
        reason=seg.reason,
        type=seg.type,
        score=seg.score,
        video_url=video_url,
        thumbnail_url=None,
        transcript=_snippet(transcript_words, seg.start, seg.end),
        words=words_in_segment(transcript_words, seg.start, seg.end),
        hook=seg.hook,
        why_works=seg.why_works,
        hook_style=seg.hook_style,
        tone=seg.tone,
        key_quote=seg.key_quote,
    )


def resolve_clip_render_policy(user_id: str | None) -> billing.RenderPolicy:
    """Владелец джоба → политика рендера (вотермарка/разрешение), резолвится СЕРВЕРНО. PURE-ish.

    План берётся из ``profiles.plan`` по ``user_id`` (db.get_user_plan, dual-mode Postgres/
    SQLite) — НИКОГДА из клиентского флага → обойти с фронта нельзя. Нет ``user_id`` (локальный
    dev на диске/SQLite) → local_dev-политика (без вотермарки, полное разрешение). В облаке
    (Modal) у джоба ВСЕГДА есть ``user_id``, поэтому free-юзер ВСЕГДА получает вотермарку.
    """
    if not user_id:
        return billing.resolve_render_policy(None, local_dev=True)
    plan_id = db.get_user_plan(user_id)
    return billing.resolve_render_policy(plan_id, local_dev=False)


def render_one_clip(
    out: Path,
    source_name: str,
    clip_index: int,
    seg: Segment,
    meta: SourceMeta,
    user_id: str | None = None,
    subject_cx: float | None = None,
    vision_shots: Sequence[VisionShot] | None = None,
) -> dict[str, Any]:
    """Stages 3–5 для ОДНОГО клипа: reframe → render → upload. Возвращает picklable result.

    subject_cx (B2, exploratory, флаг REFRAME_VISION_SUBJECT): vision-подсказка центра субъекта
    [0,1] → тай-брейкер выбора face-track в reframe_segment. None (дефолт) → прежнее поведение.
    vision_shots (Layer A, exploratory, флаг REFRAME_VISION_LAYOUT): per-shot fill/fit + субъект
    от Gemini → семантический override режима в reframe_segment. None (дефолт) → прежнее поведение.

    Общее ядро (DRY): локальный цикл И Modal-фан-аут зовут ЭТУ функцию. Настройки берём из
    ``get_settings()`` ВНУТРИ (на фан-аут-контейнере свой процесс/env). ``source_name`` —
    относительно ``out``. НЕ трогает stage3/stage5 — только вызывает (инвариант кадровой
    сетки docs/REFRAME_FPS_GRID_INVARIANT.md цел).

    ``user_id`` (владелец джоба) → политика рендера (вотермарка + потолок разрешения)
    резолвится СЕРВЕРНО из плана (см. resolve_clip_render_policy). Вотермарка/cap прожигаются
    в render_clip — обойти с клиента невозможно (план не приходит с фронта).
    """
    s = get_settings()
    policy = resolve_clip_render_policy(user_id)
    clip_id = f"clip_{clip_index:02d}"
    t0 = time.perf_counter()
    regions, face_found = reframe_segment(
        out / source_name, meta.width, meta.height, seg.start, seg.end,
        clip_id=clip_id, out_dir=out, fps=meta.fps, mode_setting=s.reframe_mode,
        speaker_crop_scale=s.reframe_speaker_crop_scale,
        face_fps=s.reframe_face_fps, smoothing=s.reframe_smoothing,
        min_hold_sec=s.reframe_min_hold_sec,
        speak_threshold=s.reframe_speak_threshold,
        scene_threshold=s.reframe_scene_threshold,
        min_scene_sec=s.reframe_min_scene_sec,
        split_enabled=s.reframe_split_enabled,
        wide_speak_min=s.reframe_wide_speak_min,
        subject_cx=subject_cx if s.reframe_vision_subject else None,
        vision_shots=vision_shots if s.reframe_vision_layout else None,
        keep_distant_cutaways=s.reframe_keep_cutaways,
    )  # fmt: skip
    reframe_lat = round(time.perf_counter() - t0, 2)
    # Домен 1: персистим РЕАЛЬНЫЕ границы шотов (PySceneDetect) дефолтного интервала клипа, чтобы
    # /reframe в редакторе отдавал их мгновенно (без пересчёта тяжёлого CV на холодном контейнере).
    # Best-effort (cloud-only) — провал персиста НЕ должен валить рендер клипа (фолбэк = on-demand).
    try:
        db.put_reframe_regions(
            out.name, clip_id, build_persist_payload(regions, seg.start, seg.end)
        )
    except Exception as e:  # noqa: BLE001 — персист не критичен; рендер важнее, ошибку логируем
        print(f"  {clip_id}: WARN reframe-regions persist failed: {e}")
    # Базовый клип-аспект 9:16; free капится по меньшей стороне (1080→720). Кадровая сетка
    # (trim по SOURCE-кадрам) от out_w/out_h НЕ зависит → Δ=0 инвариант цел.
    out_w, out_h = clamp_output_dims(1080, 1920, policy.max_resolution)
    render_lat = render_clip(
        out, source_name, seg.start, f"clips/{clip_id}.mp4",
        regions=regions, src_w=meta.width, src_h=meta.height, fps=meta.fps,
        engine=s.reframe_engine, out_w=out_w, out_h=out_h, watermark=policy.watermark,
        crf=policy.video_crf, preset=policy.video_preset,
    )  # fmt: skip
    video_url = storage.upload_clip(out / "clips" / f"{clip_id}.mp4", out.name, clip_id)
    # Инкрементальная выдача: атомарно проставляем video_url ЭТОГО клипа в строку джоба сразу
    # после заливки → GET /jobs отдаёт его готовым, пока остальные ещё рендерятся (status
    # остаётся "rendering" до set_done). out.name == job_id (на фан-аут-контейнере и локально).
    db.set_clip_ready(out.name, clip_index, video_url)
    n_fit = sum(1 for r in regions if r.mode == "fit")
    print(
        f"  {clip_id}: {seg.start:.1f}-{seg.end:.1f} face={face_found} "
        f"regions={len(regions)} fit={n_fit} render={render_lat}s"
    )
    return {
        "clip_id": clip_id,
        "clip_index": clip_index,
        "video_url": video_url,
        "reframe_lat": reframe_lat,
        "render_lat": render_lat,
        "face_found": face_found,
    }


def failed_clip_result(clip_index: int, reason: str) -> dict[str, Any]:
    """PURE. Sentinel-результат для УПАВШЕГО клипа: пустой ``video_url`` (= still/failed по
    контракту инкрементальной выдачи), нулевые латентности, флаг ``failed`` + текст причины.

    Контейнирование per-clip провала (правило №8 — НЕ тихо): один сбойный клип НЕ валит весь
    джоб. Координатор собирает ClipOut с пустым video_url (карточка остаётся «pending/failed»,
    как персистнули после select), остальные клипы рендерятся, джоб ДОХОДИТ до done. Если ВСЕ
    клипы упали — вызыватель (_render_all_clips) поднимет JobError (тотальный провал не прячем).
    """
    return {
        "clip_id": f"clip_{clip_index:02d}",
        "clip_index": clip_index,
        "video_url": "",  # пусто = клип не отрендерился (still/failed по streaming-контракту)
        "reframe_lat": 0.0,
        "render_lat": 0.0,
        "face_found": False,
        "failed": True,
        "error": reason,
    }


def render_one_clip_contained(
    out: Path,
    source_name: str,
    clip_index: int,
    seg: Segment,
    meta: SourceMeta,
    user_id: str | None = None,
    subject_cx: float | None = None,
    vision_shots: Sequence[VisionShot] | None = None,
) -> dict[str, Any]:
    """render_one_clip с КОНТЕЙНИРОВАНИЕМ per-clip провала (локальный цикл).

    Один клип упал (JobError из stage3/5 ИЛИ любое неожиданное) → ловим, ЯВНО логируем
    (не молча, правило №8) и возвращаем ``failed_clip_result`` (пустой video_url). Остальные
    клипы рендерятся, джоб доходит до done. Так зеркалит контейнирование Modal-фан-аута в
    worker.reframe_render_clip — единый контракт «провал клипа = пустой url, не падение джоба».
    ``subject_cx`` (B2) / ``vision_shots`` (Layer A) прокидываются в render_one_clip → reframe.
    """
    try:
        return render_one_clip(
            out, source_name, clip_index, seg, meta, user_id, subject_cx, vision_shots
        )
    except JobError as e:
        print(f"  clip_{clip_index:02d}: RENDER FAILED (contained, clip stays empty): {e}")
        return failed_clip_result(clip_index, str(e))
    except Exception as e:  # noqa: BLE001 — per-clip изоляция: один клип не валит весь джоб
        print(f"  clip_{clip_index:02d}: RENDER FAILED (contained, unexpected): {e}")
        return failed_clip_result(clip_index, f"unexpected: {e}")


def _render_one_clip_star(a: tuple[Any, ...]) -> dict[str, Any]:
    """Обёртка для executor.map (распаковка кортежа аргументов)."""
    return render_one_clip_contained(*a)


def _subject_cx_for(subject_cxs: list[float | None] | None, clip_index: int) -> float | None:
    """PURE. subject_cx для 1-based clip_index (позиция в списке сегментов); нет данных → None."""
    if not subject_cxs:
        return None
    i = clip_index - 1
    return subject_cxs[i] if 0 <= i < len(subject_cxs) else None


def _vision_shots_for(
    vision_shots_list: list[tuple[VisionShot, ...]] | None, clip_index: int
) -> tuple[VisionShot, ...] | None:
    """PURE. per-clip vision shots (Layer A) для 1-based clip_index; нет данных → None."""
    if not vision_shots_list:
        return None
    i = clip_index - 1
    return vision_shots_list[i] if 0 <= i < len(vision_shots_list) else None


def _render_clips_sequential(
    out: Path,
    source_name: str,
    segments: list[Segment],
    meta: SourceMeta,
    user_id: str | None,
    subject_cxs: list[float | None] | None = None,
    vision_shots_list: list[tuple[VisionShot, ...]] | None = None,
) -> list[dict[str, Any]]:
    return [
        render_one_clip_contained(
            out,
            source_name,
            i,
            seg,
            meta,
            user_id,
            _subject_cx_for(subject_cxs, i),
            _vision_shots_for(vision_shots_list, i),
        )  # fmt: skip
        for i, seg in enumerate(segments, start=1)
    ]


def _render_clips_parallel(
    out: Path,
    source_name: str,
    segments: list[Segment],
    meta: SourceMeta,
    user_id: str | None,
    workers: int,
    subject_cxs: list[float | None] | None = None,
    vision_shots_list: list[tuple[VisionShot, ...]] | None = None,
) -> list[dict[str, Any]]:
    """Параллельный per-clip рендер в ПОТОКАХ (движок B). Тяжёлое — нативный cv2/MediaPipe и
    ffmpeg-СУБПРОЦЕСС (stage5) — отпускает GIL, поэтому потоки дают реальный параллелизм по vCPU
    контейнера БЕЗ spawn-оверхеда (re-import cv2/mediapipe/numpy на каждый воркер) и ×N памяти.
    Детекторы MediaPipe создаются per-call (sample_faces_continuous/detect_scene_cuts) → нет
    разделяемого небезопасного состояния. Порядок сегментов сохраняется (executor.map). Каждый
    клип пишет СВОЙ файл и СВОЙ ключ R2; set_clip_ready сериализуется row-lock'ом Postgres → без
    потери апдейтов. Так восстанавливаем параллелизм Modal-фан-аута ВНУТРИ одного job-контейнера."""
    import concurrent.futures as cf

    args = [
        (
            out,
            source_name,
            i,
            seg,
            meta,
            user_id,
            _subject_cx_for(subject_cxs, i),
            _vision_shots_for(vision_shots_list, i),
        )  # fmt: skip
        for i, seg in enumerate(segments, start=1)
    ]
    with cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="render") as ex:
        return list(ex.map(_render_one_clip_star, args))


def _render_clips_in_process(
    out: Path,
    source_name: str,
    segments: list[Segment],
    meta: SourceMeta,
    user_id: str | None,
    subject_cxs: list[float | None] | None = None,
    vision_shots_list: list[tuple[VisionShot, ...]] | None = None,
) -> list[dict[str, Any]]:
    """Рендер клипов В ПРОЦЕССЕ job-контейнера (нет контейнерного фан-аута): параллелим ПОТОКАМИ на
    vCPU (``RENDER_CONCURRENCY``, деф.4). Пул сломался → sequential (НЕ падаем, правило №8). Это и
    фолбэк контейнерного фан-аута, и локальный/dev-путь. NB: несколько тяжёлых клипов на ОДНОМ
    контейнере переподписывают ядра (каждый клип уже cv2/ffmpeg-многопоточный) → контейнерный
    фан-аут (execution-на-клип) быстрее; этот путь оставлен как надёжный дефолт/фолбэк.
    ``subject_cxs`` (B2) / ``vision_shots_list`` (Layer A) — vision-подсказки на клип; проводятся
    ТОЛЬКО этим путём (контейнерный фан-аут их не несёт — прототип)."""
    n = len(segments)
    workers = min(n, max(1, int(os.environ.get("RENDER_CONCURRENCY", "4"))))
    if workers <= 1:
        return _render_clips_sequential(
            out, source_name, segments, meta, user_id, subject_cxs, vision_shots_list
        )
    try:
        return _render_clips_parallel(
            out, source_name, segments, meta, user_id, workers, subject_cxs, vision_shots_list
        )
    except Exception as e:  # noqa: BLE001 — пул сломался → sequential-фолбэк, не валим джоб
        print(f"[render] parallel pool failed ({e}); sequential fallback")
        return _render_clips_sequential(
            out, source_name, segments, meta, user_id, subject_cxs, vision_shots_list
        )


def _render_all_clips(
    job_id: str,
    out: Path,
    source_name: str,
    segments: list[Segment],
    meta: SourceMeta,
    user_id: str | None,
    subject_cxs: list[float | None] | None = None,
    vision_shots_list: list[tuple[VisionShot, ...]] | None = None,
) -> list[dict[str, Any]]:
    """Stages 3–5 для ВСЕХ клипов. Modal → фан-аут (контейнер на клип, ПАРАЛЛЕЛЬНО);
    локально → последовательный цикл (идентично прежнему поведению). Результаты — в порядке
    сегментов (стабильный ``clip_index`` для ассемблинга ClipOut). ``user_id`` несётся в каждый
    клип-контейнер → план владельца (вотермарка/разрешение) резолвится там СЕРВЕРНО.

    ``subject_cxs`` (B2, exploratory, флаг REFRAME_VISION_SUBJECT): vision subject-x на клип →
    тай-брейкер выбора face-track. ⚠️ Проводится ТОЛЬКО in-process путём (локально/dev + фолбэк
    фан-аута); контейнерный фан-аут (Modal/Cloud Run) НЕ несёт его через границу (clip_spawn_args
    не расширяли — вне скоупа прототипа) → на фан-ауте subject_cx=None (прежний reframe).

    КОНТЕЙНИРОВАНИЕ per-clip провала: один сбойный клип (edge-case reframe/render на конкретном
    видео — ffmpeg 0 видео-кадров) НЕ валит весь джоб. Локально — render_one_clip_contained;
    на Modal контейнирует сам дочерний reframe_render_clip (возвращает failed_clip_result, не
    кидает — иначе starmap ре-рейзит в координаторе). ЕСЛИ ВСЕ клипы упали → JobError (тотальный
    провал НЕ прячем: джоб честно failed, правило №8).
    """
    if dispatch.modal_spawn_enabled():
        results = dispatch.map_render_clips(clip_spawn_args(job_id, segments, meta, user_id))
    elif dispatch.cloudrun_fanout_enabled():
        # Cloud Run: КОНТЕЙНЕР НА КЛИП (как Modal) — клипы рендерятся ПАРАЛЛЕЛЬНО, каждый на своих
        # vCPU (in-process пул на одном 8-vCPU контейнере переподписывает ядра: каждый клип уже
        # cv2/ffmpeg-многопоточный). Фан-аут сломался (квота/API) → деградация в in-process рендер
        # главного контейнера (НЕ валим джоб, правило №8).
        try:
            results = dispatch.map_render_clips_cloudrun(
                job_id, clip_spawn_args(job_id, segments, meta, user_id)
            )
        except Exception as e:  # noqa: BLE001 — фан-аут упал → in-process рендер, не валим джоб
            print(f"[render] container fan-out failed ({e}); in-process render fallback")
            results = _render_clips_in_process(
                out, source_name, segments, meta, user_id, subject_cxs, vision_shots_list
            )
    else:
        results = _render_clips_in_process(
            out, source_name, segments, meta, user_id, subject_cxs, vision_shots_list
        )
    if results and all(r.get("failed") for r in results):
        reasons = "; ".join(str(r.get("error", "")) for r in results)
        raise JobError("render", f"all {len(results)} clips failed to render: {reasons}")
    return results


def _preview_and_video_map(
    job_id: str, out: Path, preview_height: int, preview_crf: int, stages: dict[str, float]
) -> None:
    """Preview-прокси + VideoMap pre-warm для НЕ-Modal пути (Cloud Run / локально). Тот же код,
    что прежний inline-«хвост» после клипов, — только теперь его гонит фоновый ПОТОК
    ПАРАЛЛЕЛЬНО рендеру (см. run_pipeline: старт после заливки source в R2, join перед
    финализацией). ffmpeg — субпроцесс, Gemini — HTTP: оба отпускают GIL (та же логика, что
    у поточного рендера _render_clips_parallel). Требует ЛОКАЛЬНЫЙ source.mp4 (материализован
    и проверен ДО старта потока; рендер его только читает — не двигает/не удаляет)."""
    t0 = time.perf_counter()
    build_preview_proxy(
        out / "source.mp4", out / "preview.mp4", height=preview_height, crf=preview_crf
    )
    stages["preview_proxy"] = round(time.perf_counter() - t0, 2)
    storage.upload_preview(out / "preview.mp4", job_id)
    # VideoMap: нужны только transcript/segments из job_artifacts (персистнуты ДО старта потока)
    # — рендер клипов ей не нужен. Ранний результат безопасен: фронт поллит /video-map когда угодно.
    from app.tasks import generate_video_map_job

    generate_video_map_job(job_id)


def run_pipeline(
    job_id: str,
    source_url: str | None = None,
    on_status: Callable[[JobStatus, int], None] | None = None,
    *,
    max_clips: int | None = None,
    on_meta: Callable[[SourceMeta], None] | None = None,
    on_cancellable: Callable[[bool], None] | None = None,
    user_id: str | None = None,
    language: str | None = None,
    normalize_source: Callable[[], None] | None = None,
    on_ready: Callable[[Job], None] | None = None,
) -> Job:
    """Прогнать весь конвейер для job_id. Возвращает Job (также пишет job.json/runs.jsonl).

    on_status(status, progress) (опц.) вызывается на границах стадий — для статуса в БД (J1).
    max_clips (опц.) — сколько клипов запросил юзер (UI-степпер); None → дефолт из настроек.
    on_meta(meta) (опц.) — вызывается СРАЗУ после импорта (известна реальная длина), ДО
    транскрипции. Поднимет JobError → джоб падает до оплаты Deepgram (гейт квоты по длине).
    on_cancellable(value) (опц.) — Stop-кнопка: вызывается с ``False`` ПРЯМО перед началом
    транскрипции (граница FREE→PAID: download/probe бесплатны, транскрипция — первый платный
    шаг). Джоб стартует cancellable=True (insert_job), True эмитить не нужно.
    user_id (опц.) — владелец джоба: несётся в per-clip рендер → план (вотермарка/разрешение)
    резолвится СЕРВЕРНО из profiles.plan (free прожигает вотермарку, обойти с клиента нельзя).
    normalize_source(опц.) — ОТЛОЖЕННАЯ видео-нормализация аплоада в seekable source.mp4 (см.
    stage0_import.normalize_upload_source). Аплоад-путь даёт аудио+meta из СЫРОГО файла сразу
    (транскрипция стартует без ожидания ре-энкода), а source.mp4 материализуется ЗДЕСЬ — ПОСЛЕ
    select, ПЕРЕД заливкой в R2/фан-аутом (где впервые нужен seekable mp4). YouTube/кэш: source.mp4
    уже есть → None (no-op). Параметры _ensure_mp4 не меняются — двигается только момент запуска.
    on_ready(job) (опц.) — done-early шов для tasks.py (владелец set_done/_meter): вызывается
    СРАЗУ по готовности клипов (Job собран, job.json записан), ДО join'а preview/video_map-потока
    → done не ждёт энкод прокси (на длинных источниках хвост стоил минуты wall-clock). Ошибка
    хвоста ПОСЛЕ on_ready НЕ валит джоб — громкий лог «[preview] FAILED after done» (это
    документированная деградация storage.preview_read_url: редактор фолбэчит на source URL,
    пока preview.mp4 не появится в R2; video_map retry-able через /video-map?retry=true).
    Без on_ready (CLI/main) — старая семантика: join ДО финализации, ошибка потока валит джоб.
    """
    s = get_settings()  # fail-fast на отсутствии ключей; также берём reframe_mode
    out = DATA_ROOT / job_id
    out.mkdir(parents=True, exist_ok=True)
    stages: dict[str, float] = {}
    t_start = time.perf_counter()

    def emit(status: JobStatus, progress: int) -> None:
        if on_status is not None:
            on_status(status, progress)

    # ── Stage 0: Import (кэш по наличию source.mp4 + meta.json) ──
    emit(JobStatus.downloading, 10)
    t0 = time.perf_counter()
    meta_path = out / "meta.json"
    have_mp4 = (out / "source.mp4").exists()
    if meta_path.exists() and (have_mp4 or (out / "source.wav").exists()):
        # Stage 0 уже выполнен: либо ПОЛНЫЙ кэш (source.mp4 + meta — повторный прогон / после yt),
        # либо БЫСТРАЯ подготовка аплоада (meta + source.wav готовы, source.mp4 ОТЛОЖЕН до
        # normalize_source). meta из СЫРОГО исходника совпадает с будущим source.mp4 (ре-энкод не
        # масштабирует) → fps/размеры/длина консистентны для гейта квоты и reframe.
        meta = SourceMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
        if have_mp4:
            print(f"[0] import: cached ({meta.duration:.0f}s {meta.width}x{meta.height})")
        else:
            print(
                f"[0] import: prepared, source.mp4 deferred "
                f"({meta.duration:.0f}s {meta.width}x{meta.height})"
            )
    elif source_url:
        # ── Multi-cookie + proxy-pool YouTube fallback (free bot-gate bypass, BEST-EFFORT).
        #    YouTube bot-gates DC-IPs; we fight on TWO fronts:
        #    1. Cookie pool: N jars → one passing is enough (P ≈ 1-(1-p)^N).
        #    2. Proxy pool: free HTTP proxies from proxyscrape.com → different IPs per attempt.
        #    Outer loop: proxies (when ALL cookies fail on this IP → try next proxy IP).
        #    Inner loop: cookies (when one jar is stale → try next jar on same IP).
        #    for...else: inner else fires only when the loop exhausted WITHOUT break (all gated).
        #    Local dev (no R2): no proxy pool, single config jar (behavior unchanged). ──
        candidates: list[tuple[str, str]] = []  # (label, cookies_file path; "" = no cookies)
        if ytdlp_cookies.cookies_enabled():
            r2_tmp = ytdlp_cookies.cookies_temp_path(out)
            if ytdlp_cookies.pull_cookies(r2_tmp):
                candidates.append(("r2-rotating", str(r2_tmp)))
        for jar in ytdlp_cookies.baked_jars():
            writable = out / f"cookies_{jar.stem}.txt"  # baked is read-only → copy to writable temp
            shutil.copyfile(jar, writable)
            candidates.append((jar.name, str(writable)))
        if not candidates:  # local dev / no pool → single config jar (may be "")
            candidates.append(("config", s.ytdlp_cookies_file))

        # Load proxy pool from R2 (fast, ~0.5s). If pool is low, kick off a background refresh
        # that runs CONCURRENTLY with the first download attempt — yt-dlp takes ~10-15s to
        # surface a bot-gate; by then the ~5s refresh (3s proxy timeout) is already done.
        proxy_pool: list[str] = []
        _bg_thread: threading.Thread | None = None
        _bg_result: list[list[str]] = [[]]  # mutable container for the background thread result

        if ytdlp_proxy_pool.pool_enabled():
            proxy_pool = ytdlp_proxy_pool.load_proxy_pool()
            if len(proxy_pool) < s.ytdlp_proxy_pool_min_size:

                def _run_refresh() -> None:
                    _bg_result[0] = ytdlp_proxy_pool.refresh_proxy_pool()

                _bg_thread = threading.Thread(
                    target=_run_refresh, daemon=True, name="proxy-refresh"
                )
                _bg_thread.start()
                print("[0] import: proxy pool low — refresh running in background")

        # No-proxy first: ~40-70% of downloads succeed on Modal's IP with no proxy overhead.
        # Proxy pool (from R2 or background refresh) is the fallback when the IP is bot-gated.
        proxies_to_try: list[str] = [s.ytdlp_proxy] + proxy_pool
        _bg_appended = False

        meta_or_none: SourceMeta | None = None
        last_gate: YoutubeBotGateError | None = None
        winning_proxy: str = ""

        for proxy_idx, proxy in enumerate(proxies_to_try):
            proxy_label = proxy or "no-proxy"
            if len(proxies_to_try) > 1:
                print(f"[0] import: proxy {proxy_idx + 1}/{len(proxies_to_try)} ({proxy_label})")
            for i, (label, cfile) in enumerate(candidates, 1):
                print(f"[0] import: cookie attempt {i}/{len(candidates)} ({label})")
                try:
                    meta_or_none = import_youtube(
                        source_url, out, job_id=job_id,
                        cookies_browser=s.ytdlp_cookies_browser,
                        cookies_file=cfile,
                        proxy=proxy,
                        pot_server_home=s.ytdlp_pot_server_home,  # bgutil PO-token (script mode)
                        player_client=s.ytdlp_player_client,  # tv,android_vr pass DC-IP w/o POT
                    )  # fmt: skip
                except YoutubeBotGateError as e:
                    last_gate = e
                    print(f"[0] import: cookie {i} ({label}) bot-gated on proxy {proxy_label}")
                    continue
                # Success → persist winning rotated jar to R2 as new current-best (cloud only).
                winning_proxy = proxy
                if ytdlp_cookies.cookies_enabled() and cfile:
                    ytdlp_cookies.push_cookies(Path(cfile))
                break  # inner: stop trying cookies
            else:
                # Inner loop exhausted without break → ALL cookies bot-gated on this proxy IP.
                # Join background refresh thread (likely already done in ~5s); add fresh proxies.
                if _bg_thread is not None:
                    _bg_thread.join(timeout=35)
                    _bg_thread = None
                if _bg_result[0] and not _bg_appended:
                    ytdlp_proxy_pool.save_proxy_pool(_bg_result[0])
                    proxies_to_try.extend(_bg_result[0])
                    _bg_appended = True
                    print(
                        f"[0] import: +{len(_bg_result[0])} fresh proxies from background refresh"
                    )
                print(f"[0] import: all cookies bot-gated on {proxy_label} — trying next proxy")
                continue  # outer: try next proxy
            break  # inner had a break (success) → stop trying proxies

        # Rotate winning proxy to front so next job starts from the last known-good IP.
        if proxy_pool and winning_proxy and ytdlp_proxy_pool.pool_enabled():
            updated = [winning_proxy] + [p for p in proxy_pool if p != winning_proxy]
            ytdlp_proxy_pool.save_proxy_pool(updated)

        if meta_or_none is None:  # every proxy+jar combo bot-gated
            raise last_gate or JobError(
                "import", "all YouTube cookie jars and proxies failed (bot-gated)"
            )
        meta = meta_or_none  # narrowed → SourceMeta (matches the cached branch's type below)
        print(f"[0] import: {meta.duration:.0f}s {meta.width}x{meta.height}")
    else:
        raise JobError("import", f"no data/{job_id}/source.mp4 and no URL provided")
    stages["download"] = round(time.perf_counter() - t0, 2)
    db.set_progress_detail(job_id, source_minutes=round(meta.duration / 60, 1))  # live narration
    # Энергетические co-watch-маркеры (Part 4) — БЕСТ-ЭФФОРТ, доступны РАНО (до transcribe), потому
    # покрывают самый длинный кусок ожидания. [] при любом сбое extract_loudness. Источник звука:
    # source.mp4 (yt/кэш), а для аплоада до отложенной нормализации mp4 ещё нет → берём source.wav
    # (тот же звук, что в mp4; loudness re-decode'ит в 8kHz mono в любом случае).
    # ⚠️ ИНВАРИАНТ (уточнён Phase A, 2026-07-06): косметические МАРКЕРЫ (preview_moments)
    # по-прежнему НЕ идут в select_segments — LLM-отбор моментов от них НЕ зависит. Но СЫРАЯ
    # RMS-кривая `loudness` ТЕПЕРЬ дополнительно кормит аудио-осознанную чистку границ
    # (refine_boundaries_audio) — это НЕ выбор моментов, а детерминированное уточнение уже-выбранных
    # краёв (обрезка филлеров + снап к вдоху). Кривая провайдер-агностична (Deepgram и Groq/kk).
    _loud_src = out / "source.mp4" if (out / "source.mp4").exists() else out / "source.wav"
    loudness = extract_loudness(str(_loud_src))
    energy_moments = detect_energy_moments(loudness)
    if energy_moments:
        db.put_job_artifact(job_id, "preview_moments", [m.model_dump() for m in energy_moments])

    # ── Гейт квоты по РЕАЛЬНОЙ длине (до оплаты транскрипции). Поднимет JobError → failed,
    #    БЕЗ списания (record_usage идёт только после set_done). ──
    if on_meta is not None:
        on_meta(meta)

    # ── FREE→PAID граница: транскрипция — первый платный шаг. Гасим cancellable ДО неё, чтобы
    #    Stop больше не предлагался (отмена платной стадии = частичный заряд, недопустимо). ──
    if on_cancellable is not None:
        on_cancellable(False)

    # ── Stage 1: Transcribe (уровень 1: job-local transcript.json; уровень 2: hash-кэш) ──
    emit(JobStatus.transcribing, 35)
    t0 = time.perf_counter()
    tr_path = out / "transcript.json"
    transcribe_cost = 0.0
    cache_dir = DATA_ROOT / "_cache" / "transcripts"

    if tr_path.exists():
        # Level 1: job-local cache (same job_id re-run)
        transcript = Transcript.model_validate_json(tr_path.read_text(encoding="utf-8"))
        print(f"[1] transcribe: cached/local ({len(transcript.words)} words)")
    else:
        wav_path = out / "source.wav"
        # Level 2: content-addressed cache (same audio, different job_id).
        # cloud (Postgres transcript_cache) durable между Modal-контейнерами; локально диск.
        cached_tr: Transcript | None = None
        sha: str | None = None
        ck: str | None = None
        tr_model = transcript_cache_model(s)
        if s.transcript_cache_enabled:
            sha = audio_sha(wav_path)
            ck = cache_key(sha, s.transcription_provider, tr_model)
            cloud_tr = db.get_cached_transcript(sha, s.transcription_provider, tr_model)
            cached_tr = (
                Transcript.model_validate(cloud_tr)
                if cloud_tr is not None
                else get_cached(cache_dir, ck)
            )

        if cached_tr is not None:
            transcript = cached_tr
            tr_path.write_text(transcript.model_dump_json(indent=2), encoding="utf-8")
            print(f"[1] transcribe: cached/hash ({len(transcript.words)} words, $0)")
        else:
            transcript = transcribe_to_file(wav_path, tr_path, language=language)
            # Cost depends on effective provider (Groq for kk, Deepgram otherwise).
            rate = (
                GROQ_TURBO_USD_PER_MIN
                if language in DEEPGRAM_UNSUPPORTED and s.groq_api_key
                else DEEPGRAM_NOVA_USD_PER_MIN
            )
            transcribe_cost = round(transcript.duration / 60 * rate, 4)
            print(f"[1] transcribe: {len(transcript.words)} words (${transcribe_cost})")
            if s.transcript_cache_enabled and ck is not None:
                put_cached(cache_dir, ck, transcript)
                evict(
                    cache_dir,
                    max_entries=s.transcript_cache_max_entries,
                    max_age_days=s.transcript_cache_max_age_days,
                )
            if s.transcript_cache_enabled and sha is not None:
                db.put_cached_transcript(
                    sha, s.transcription_provider, tr_model, transcript.model_dump()
                )
    stages["transcription"] = round(time.perf_counter() - t0, 2)
    db.set_progress_detail(job_id, transcript_words=len(transcript.words))  # live narration
    # Дополняем энергетические маркеры (с download) транскриптными (вопрос/нажим/цифра/пауза) и
    # перезаписываем preview_moments объединённым набором. ⚠️ ИНВАРИАНТ: НЕ передаём в
    # select_segments ниже — LLM-отбор от них НЕ зависит (качество нарезки не меняется).
    _merged = merge_moments(energy_moments, detect_preview_moments(transcript.words))
    db.put_job_artifact(job_id, "preview_moments", [m.model_dump() for m in _merged])

    # ── Stage 2: Select (кэш по segments.json) ──
    emit(JobStatus.selecting, 60)
    t0 = time.perf_counter()
    seg_path = out / "segments.json"
    usage: dict[str, int] = {}
    vision_cost = 0.0
    # B2 (exploratory): vision subject-x на клип (выровнен по ре-ранкнутым сегментам) → тай-брейкер
    # выбора face-track в reframe. Заполняется в блоке 2b ТОЛЬКО при REFRAME_VISION_SUBJECT; иначе
    # (и на кэш-пути segments.json — subject_cx не персистится) остаётся None → прежний reframe.
    subject_cxs: list[float | None] | None = None
    # Layer A (exploratory): per-shot fill/fit + субъект на клип (выровнены по ре-ранку) → per-shot
    # override режима в reframe. Заполняется в 2b ТОЛЬКО при REFRAME_VISION_LAYOUT; иначе None.
    vision_shots_list: list[tuple[VisionShot, ...]] | None = None
    if seg_path.exists():
        segments = [
            Segment.model_validate(x) for x in json.loads(seg_path.read_text(encoding="utf-8"))
        ]
        print(f"[2] select: cached ({len(segments)} segments)")
    else:
        segments = select_segments(
            transcript,
            meta.title,
            max_clips=max_clips,
            # A3: сырая RMS-кривая → аудио-осознанная чистка границ. [] (сбой extract_loudness) →
            # None (явная ветка «нет сигнала», не тихий фолбэк); гейт SELECT_AUDIO_AWARE — в select.
            loudness=loudness or None,
            usage_sink=usage,
        )
        print(f"[2] select: {len(segments)} segments")
        # ── Stage 2b (B1): Gemini-vision ревью каждого окна — ре-ранк по мультимодальному
        #    hook_strength (видео+звук) + уточнение чистого реза краёв (trim клампится к ±1 слову
        #    детерминированной границы). Нужен source ВИДЕО: гоняем ТОЛЬКО когда source.mp4 уже
        #    материализован (YouTube/кэш). Upload-путь откладывает source.mp4 за select → там
        #    ревью пропускаем с логом (правило №8: явная ветка, детерминированный отбор цел).
        #    Kill-switch VISION_REVIEW_ENABLED. Мутируем segments ДО записи segments.json → кэш
        #    хранит УЖЕ отревьюенные сегменты (повторный прогон не дёргает Gemini снова). ──
        if s.vision_review_enabled and segments and (out / "source.mp4").exists():
            t_vis = time.perf_counter()
            vis_usage: dict[str, int] = {}
            try:
                reviews = review_segments_with_video(
                    out / "source.mp4", segments, meta, settings=s, usage_sink=vis_usage
                )
                segments, reviewed_subject_cxs, reviewed_shots = apply_vision_reviews_full(
                    segments, reviews, transcript.words, max_clips=len(segments)
                )
                # B2: subject_cx в reframe ТОЛЬКО под флагом (иначе byte-identical).
                if s.reframe_vision_subject:
                    subject_cxs = reviewed_subject_cxs
                    n_subj = sum(1 for cx in reviewed_subject_cxs if cx is not None)
                    print(f"[2b] vision: {n_subj} subject hints → reframe")
                # Layer A: per-shot fill/fit override в reframe ТОЛЬКО под флагом.
                if s.reframe_vision_layout:
                    vision_shots_list = reviewed_shots
                    n_layout = sum(1 for sh in reviewed_shots if sh)
                    print(f"[2b] vision: {n_layout} clips with per-shot layout → reframe")
                n_ok = sum(1 for r in reviews if r.ok)
                print(f"[2b] vision: reviewed {n_ok}/{len(reviews)} windows, re-ranked")
            except Exception as e:  # noqa: BLE001 — весь проход упал → детерминированный отбор (№8)
                print(f"[2b] vision: FAILED, keeping deterministic selection: {e}")
            vision_cost = _gemini_cost(vis_usage)
            stages["vision_review"] = round(time.perf_counter() - t_vis, 2)
        elif s.vision_review_enabled and not (out / "source.mp4").exists():
            print("[2b] vision: skipped (source.mp4 deferred — upload path, not yet materialized)")
        seg_path.write_text(
            json.dumps([s_.model_dump() for s_ in segments], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    # NB: llm_select — время ВСЕГО Stage 2 блока (вкл. под-проход 2b, если он был); отдельная
    # латентность ревью — в stages["vision_review"].
    stages["llm_select"] = round(time.perf_counter() - t0, 2)
    select_cost = _gemini_cost(usage)
    db.set_progress_detail(job_id, moments_found=len(segments))  # live narration

    # ── Артефакты (meta/segments/transcript) в Postgres job_artifacts ДО фан-аута: preview_job
    #    (и любой будущий клип-контейнер) читает meta из облака. Локально — no-op. ──
    db.put_job_artifacts(
        job_id,
        meta.model_dump(),
        [seg.model_dump() for seg in segments],
        transcript.model_dump(),
    )

    # ── Инкрементальная выдача СРАЗУ после select (а не на границе рендера): персистим ВСЕ клипы
    #    (метаданные хук/why/score/интервал + ПУСТОЙ video_url) в строку джоба ТЕПЕРЬ → GET /jobs
    #    отдаёт богатые карточки на ~60%, за МИНУТЫ до рендера (фронт встаёт на грид по наличию
    #    клипов). status="selecting" (честно), progress=60. Каждый per-clip контейнер позже
    #    атомарно проставит свой video_url (db.set_clip_ready). clip_id 1-based (clip_{i:02d})
    #    совпадает с порядком segments → idx фан-аута выровнен. ──
    pending_clips: list[ClipOut] = [
        build_clip_out(f"clip_{i:02d}", seg, transcript.words, "")
        for i, seg in enumerate(segments, start=1)
    ]
    db.set_clips_pending(job_id, pending_clips, progress=60, status=JobStatus.selecting)

    # ── ОТЛОЖЕННАЯ видео-нормализация аплоада: транскрипция/селект уже прошли по аудио из СЫРОГО
    #    файла; ТЕПЕРЬ, ПЕРЕД заливкой в R2 и фан-аутом (которым нужен seekable mp4), выполняем
    #    _ensure_mp4 (remux h264 / полный ре-энкод AV1/HEVC — параметры ИДЕНТИЧНЫ, двигается лишь
    #    момент). YouTube/кэш: source.mp4 уже есть → normalize_source=None. Идемпотентно. ──
    if normalize_source is not None:
        t_norm = time.perf_counter()
        normalize_source()
        stages["normalize"] = round(time.perf_counter() - t_norm, 2)
    # Контракт (правило №8 — без тихого брака): к этому моменту source.mp4 ОБЯЗАН существовать
    # (seekable mp4 для R2/reframe/render). Нет → явный JobError, а не битый фан-аут ниже.
    if not (out / "source.mp4").exists():
        raise JobError("import", f"source.mp4 missing after normalization for {job_id}")

    # ── Источник в R2 ДО фан-аута: каждый клип-контейнер скачивает source из R2
    #    (artifacts.ensure_source — тот же проверенный путь, что у editor-render). Локально
    #    upload_source = no-op (исходник остаётся на диске). ──
    storage.upload_source(out / "source.mp4", job_id)

    # ── #3 Preview-прокси СНЯТ с критического пути: на Modal — отдельная функция preview_job,
    #    спавним СЕЙЧАС → она строит прокси ПАРАЛЛЕЛЬНО с клипами и не держит set_done. Редактор
    #    фолбэчит на source, пока прокси не готов (storage.preview_read_url). Без Modal (Cloud
    #    Run / локально) — фоновый ПОТОК ниже, тоже ПАРАЛЛЕЛЬНО с клипами. ──
    if dispatch.modal_spawn_enabled():
        dispatch.spawn("preview_job", job_id)

    # ── VideoMap pre-warm: на Modal спавним СЕЙЧАС (отдельный контейнер, durable в Postgres) →
    #    карта обычно готова, когда юзер открыл результаты. НЕ держит set_done и не влияет
    #    на нарезку. ──
    if dispatch.modal_spawn_enabled():
        dispatch.spawn("generate_video_map_job", job_id)

    # ── БЕЗ Modal-спавна (Cloud Run / локально): preview + VideoMap — в фоновом ПОТОКЕ, который
    #    стартует СЕЙЧАС (source.mp4 материализован, проверен и залит выше) и работает
    #    ПАРАЛЛЕЛЬНО с рендером клипов (координатор фан-аута всё равно простаивает в поллинге);
    #    join — ПЕРЕД финализацией (ниже). Раньше это был ПОСЛЕДОВАТЕЛЬНЫЙ хвост ПОСЛЕ всех
    #    клипов (+40–150с+ к джобу). Ошибка потока НЕ глотается (правило №8): ре-рейз на join →
    #    джоб падает так же, как при inline-запуске. ──
    preview_thread: threading.Thread | None = None
    preview_errors: list[BaseException] = []
    if not dispatch.modal_spawn_enabled():
        _preview_h = min(s.preview_height, meta.height)

        def _preview_worker() -> None:
            try:
                _preview_and_video_map(job_id, out, _preview_h, s.preview_crf, stages)
            except BaseException as e:  # noqa: BLE001 — НЕ тихий фолбэк: ре-рейзится на join
                preview_errors.append(e)

        preview_thread = threading.Thread(target=_preview_worker, name="preview-map", daemon=True)
        preview_thread.start()

    # ── Рендер реально стартует здесь: статус → rendering, progress 80. Клипы уже персистнуты
    #    выше (после select); тут только апдейт статуса/прогресса (клипы не переписываем). ──
    emit(JobStatus.rendering, 80)

    # ── Stages 3–5: #1 фан-аут per-clip по контейнерам Modal (параллельно) ЛИБО последовательный
    #    цикл локально. Результаты приходят в порядке сегментов → стабильный ClipOut. ──
    results = _render_all_clips(
        job_id, out, "source.mp4", segments, meta, user_id, subject_cxs, vision_shots_list
    )
    results.sort(key=lambda r: r["clip_index"])
    clips: list[ClipOut] = [
        build_clip_out(r["clip_id"], seg, transcript.words, r["video_url"])
        for seg, r in zip(segments, results, strict=True)
    ]
    stages["reframe"] = round(sum(r["reframe_lat"] for r in results), 2)
    stages["render"] = round(sum(r["render_lat"] for r in results), 2)

    # БЕЗ on_ready (CLI/main): join preview/VideoMap-потока ПЕРЕД финализацией — прежняя
    # семантика 1:1 с inline-хвостом: исключение потока ре-рейзим ЗДЕСЬ (до job.json) →
    # провал preview/map = failed-джоб, НЕ тихо. (С on_ready join уезжает НИЖЕ, ПОСЛЕ done.)
    if on_ready is None and preview_thread is not None:
        preview_thread.join()
        if preview_errors:
            raise preview_errors[0]

    # ── job.json (wire-контракт) ──
    total_sec = round(time.perf_counter() - t_start, 2)
    total_usd = round(transcribe_cost + select_cost + vision_cost, 4)
    job = Job(
        id=job_id,
        status=JobStatus.done,
        stage=JobStatus.done,
        progress=100,
        source_kind=meta.source,
        clips=clips,
        metrics=Metrics(cost_usd=total_usd, duration_sec=meta.duration, elapsed_sec=total_sec),
    )
    (out / "job.json").write_text(job.model_dump_json(indent=2), encoding="utf-8")

    # ── done-early (хвост-киллер Cloud Run): on_ready = set_done+_meter у вызывателя (tasks.py)
    #    — джоб виден done СРАЗУ по готовности клипов, а preview/video_map-поток джойнится
    #    ПОСЛЕ, но строго ДО возврата (Cloud Run Job убивает контейнер на выходе процесса —
    #    вернуться с живым потоком = оборванная заливка preview). Ошибка хвоста ПОСЛЕ done
    #    СОЗНАТЕЛЬНО не валит джоб (клипы валидны и уже отданы) — это ДОКУМЕНТИРОВАННАЯ
    #    деградация storage.preview_read_url (редактор фолбэчит на source URL, пока preview.mp4
    #    нет в R2; video_map retry-able через /video-map?retry=true). НЕ тихий фолбэк
    #    (правило №8): громкий лог ниже. set_done ничего из preview-стейта не читает, а
    #    video_map пишется независимым upsert'ом job_artifacts (db.put_job_artifact) —
    #    порядок done → артефакт хвоста безопасен. ──
    if on_ready is not None:
        on_ready(job)
        if preview_thread is not None:
            preview_thread.join()
            if preview_errors:
                print(f"[preview] FAILED after done: {preview_errors[0]}")

    # ── runs.jsonl (телеметрия экономики) ──
    run_line = {
        "run_id": job_id,
        "source_minutes": round(meta.duration / 60, 2),
        "stages": stages,
        "total_sec": total_sec,
        "total_usd": total_usd,
        "n_clips": len(clips),
        # При параллельном фан-ауте per-clip TTFC с координатора не имеет смысла (клипы
        # рендерятся одновременно на отдельных контейнерах) → None. В локальном цикле тоже None
        # (упрощение: телеметрия экономики живёт в stages/total_sec).
        "time_to_first_clip_sec": None,
    }
    with (DATA_ROOT / "runs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(run_line, ensure_ascii=False) + "\n")

    print(f"\nDONE: {len(clips)} clips, total ${total_usd}, {total_sec}s -> data/{job_id}/")
    return job


def main() -> None:
    args = sys.argv[1:]
    job_id = args[0] if args else "sample01"
    source_url = args[1] if len(args) > 1 else None
    run_pipeline(job_id, source_url)


if __name__ == "__main__":
    main()
