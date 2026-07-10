"""Composite-ASS fast download path: pure gate + ffmpeg command builder.

Готовый чистый клип ``clips/<id>.mp4`` — это УЖЕ reframe+watermark+clip-time база (1080×1920,
та же, что показывает грид/превью), БЕЗ субтитров. Когда правка клипа caption/hook-only (один
дефолтный интервал, без кропа/аспекта/трима — геометрия НЕ меняется), «download с субтитрами»
можно сделать ОДНИМ коротким энкодом: прожечь ASS поверх baked-клипа. Это пропускает дорогой
полный путь (скачать весь source → CV reframe → reframe-фильтрграф) ради секунд вместо минут.

Здесь — ТОЛЬКО pure-логика (предикат гейта + сборка команды), под unit-тестами. I/O (скачать
baked-клип, запустить ffmpeg) живёт в ``tasks.render_edit_to_file`` (оркестратор).
"""

from __future__ import annotations

from typing import Any

from app.models import ClipEdit

# Baked-клип всегда рендерится в 9:16 (run.render_one_clip: clamp_output_dims(1080,1920,...)).
# Любой другой аспект edit'а → геометрия не совпадает с baked → fast-path НЕЛЬЗЯ.
_BAKED_ASPECT = "9:16"


def edit_matches_baked(
    edit: ClipEdit, *, seg_start: float, seg_end: float, tol: float = 0.05
) -> bool:
    """PURE. True ⇔ edit — нетронутая-по-геометрии caption/hook-only правка, чьи кадры совпадают
    с baked-клипом ``clips/<id>.mp4`` (можно прожечь субтитры поверх него, без пересчёта reframe).

    Условия (ВСЕ обязательны — иначе геометрия baked-клипа уже не та):
      • ``aspect == "9:16"`` — baked всегда 9:16; другой аспект = другой кадр.
      • нет ``reframe_overrides`` — ручной кроп меняет регионы → нужен пересчёт.
      • ``lock_per_shot is True`` — baked рендерится LOCKED (дефолт батча); follow (пан) = пиксели.
      • РОВНО один source-интервал — мульти-интервал = пере-монтаж (concat), не один клип.
      • границы интервала совпадают с сегментом (в пределах ``tol``) — клип не тримили/не сдвигали
        (baked рендерился именно из [seg_start, seg_end]; default_clip_edit сидит этот интервал).
    Субтитры/хук НЕ влияют на геометрию — их и накладываем поверх baked. Любое НЕсовпадение →
    False → вызыватель идёт полным путём (корректность важнее скорости).
    """
    if edit.aspect != _BAKED_ASPECT:
        return False
    if edit.reframe_overrides:
        return False
    if not edit.lock_per_shot:
        # baked рендерится LOCKED (дефолт); follow = пан вместо статики = другие пиксели → пересчёт.
        return False
    if len(edit.source_intervals) != 1:
        return False
    iv = edit.source_intervals[0]
    return abs(iv.source_start - seg_start) <= tol and abs(iv.source_end - seg_end) <= tol


def can_composite_captions(
    edit: ClipEdit,
    *,
    seg_start: float,
    seg_end: float,
    baked_clip_exists: bool,
    tol: float = 0.05,
) -> bool:
    """PURE. Полный гейт fast-vs-full: baked-клип ЕСТЬ И edit совпадает по геометрии (см.
    ``edit_matches_baked``). True → composite-ASS fast-path; False → полный render_timeline.

    Существование baked-клипа — отдельный аргумент (его проверяет/скачивает I/O-слой), чтобы
    решение целиком оставалось чистым и тестируемым.
    """
    if not baked_clip_exists:
        return False
    return edit_matches_baked(edit, seg_start=seg_start, seg_end=seg_end, tol=tol)


def should_serve_cached_captioned(edit_row: dict[str, Any] | None) -> bool:
    """PURE. True ⇔ a baked captioned object exists (``render_status == "done"`` + ``render_url``),
    so ``/export/captioned.mp4`` 302-redirects to the CDN instead of re-rendering.

    We DELIBERATELY do NOT gate on ``version == 1`` (2026-07-11). The on-demand re-render of an
    EDITED clip is NOT viable on the api box for big-source jobs: the endpoint downloads the whole
    source (measured a ~10GB upload) into Cloud Run's RAM-backed FS → OOM/503, and the render runs
    >300s > the request timeout. So the re-render path is a GUARANTEED failure (503 + "expired"
    media-token) for those clips — serving the last baked is strictly better. Trade-off: for a clip
    edited AFTER its last render this serves the last-RENDERED version (re-rendering in the editor
    refreshes the baked). A truly un-baked clip (``render_status != "done"``) still falls through to
    the on-demand render below. Retention never deletes clip keys → ``status == "done"`` ⇒ present.
    """
    if not edit_row:
        return False
    return edit_row.get("render_status") == "done" and bool(edit_row.get("render_url"))


def build_composite_ass_cmd(
    clip_in: str, ass_filter: str, out_name: str, *, crf: int = 20, preset: str = "veryfast"
) -> list[str]:
    """PURE. ffmpeg-команда composite-ASS: прожечь субтитры поверх baked-клипа, аудио — копией.

    ``clip_in`` (baked ``clips/<id>.mp4``), ``ass_filter`` (готовый ``subtitles=...:fontsdir=...``
    — тот же, что у полного рендера, см. stage5_render._subtitles_filter) и ``out_name`` —
    ОТНОСИТЕЛЬНЫЕ пути от cwd ffmpeg (= job_dir), как в полном пути. Видео ре-энкодим (прожиг
    субтитров требует энкода) с crf/preset из RenderPolicy владельца; аудио ``-c:a copy`` (в
    baked уже aac — НЕ ре-энкодим зря). ``+faststart`` — moov вперёд (мгновенный старт в браузере).
    """
    return [
        "ffmpeg", "-y", "-i", clip_in,
        "-vf", ass_filter,
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
        "-c:a", "copy", "-movflags", "+faststart",
        out_name,
    ]  # fmt: skip
