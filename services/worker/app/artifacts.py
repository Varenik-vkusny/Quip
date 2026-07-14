"""Единое чтение артефактов пайплайна (meta/segments/transcript) + исходника — dual-mode.

**disk-first, cloud-fallback, по каждому файлу отдельно.** Если конкретный артефакт лежит на
локальном диске (контейнер ``run_job`` на Modal, либо локальный dev) — читаем его. Иначе, в
облачном режиме, тянем из Supabase ``job_artifacts`` и скачиваем ``source.mp4`` из R2 в scratch.

Так один и тот же код корректен в ТРЁХ местах: (1) ``run_job`` (всё на диске), (2) отдельный
web/render-контейнер на Modal (диск пуст → из облака), (3) локальный dev (всё на диске). Гранулярно
по файлу: ``load_transcript`` не требует наличия meta/segments (как было в старом store).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app import db
from app.cloud_state import cloud_enabled
from app.errors import JobError
from app.models import Segment, Transcript, Word
from app.pipeline.stage0_import import SourceMeta


def job_dir(job_id: str) -> Path:
    """Рабочая папка джоба (scratch на Modal, data/<job> локально).

    DATA_ROOT читаем из ``store`` в рантайме (ленивый импорт) → один источник правды и
    уважение monkeypatch ``store.DATA_ROOT`` в тестах. store→artifacts на верхнем уровне,
    artifacts→store лениво — цикла нет.
    """
    from app.editor import store

    return store.DATA_ROOT / job_id


def _cloud_row(job_id: str, columns: Sequence[str]) -> dict[str, Any]:
    """job_artifacts из Postgres (cloud), ТОЛЬКО ``columns``. JobError, если не облако / нет строки.

    ⚠️ ``columns`` — это бюджет памяти, а не косметика: одна строка ``job_artifacts`` несёт
    meta+segments+ВЕСЬ ТРАНСКРИПТ (86-мин видео = 2.18 МБ на проводе / ~33 МБ пиковой RAM на
    парсинг; 6-часовое ≈ 113 МБ). Читатель обязан просить РОВНО свою колонку — иначе
    ``load_meta()`` ради ``meta.fps`` (fast-path ``GET /reframe``, горячий) тащит весь транскрипт.
    """
    if not cloud_enabled():
        raise JobError("artifacts", f"no artifacts for {job_id} (no disk and not cloud)")
    row = db.get_job_artifacts(job_id, columns=columns)
    if row is None:
        raise JobError("artifacts", f"no artifacts in Supabase for {job_id}")
    return row


def _disk_or_cloud(job_id: str, filename: str, cloud_key: str) -> Any:
    """JSON-артефакт: с диска (если файл есть), иначе из Postgres job_artifacts[cloud_key].

    Из облака тянем РОВНО ``cloud_key`` (``?select=<cloud_key>``) — соседние колонки той же
    строки (в первую очередь транскрипт) по проводу не едут и в RAM не парсятся.
    """
    p = job_dir(job_id) / filename
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    val = _cloud_row(job_id, (cloud_key,)).get(cloud_key)
    if val is None:
        raise JobError("artifacts", f"no {cloud_key} for {job_id}")
    return val


def has_artifact(job_id: str, filename: str, cloud_key: str) -> bool:
    """Существует ли артефакт (файл на диске ИЛИ не-null колонка в Postgres) — БЕЗ его чтения.

    Для 404-гейтов («а транскрипт вообще есть?»): парсить мегабайтный блоб ради булева — чистая
    потеря. Disk-first (как ``_disk_or_cloud``); не облако и нет файла → False.
    """
    if (job_dir(job_id) / filename).exists():
        return True
    if not cloud_enabled():
        return False
    return db.has_job_artifact(job_id, cloud_key)


def load_meta(job_id: str) -> SourceMeta:
    """SourceMeta джоба (fps/размер/длительность). Из облака тянет ТОЛЬКО колонку ``meta``."""
    return SourceMeta.model_validate(_disk_or_cloud(job_id, "meta.json", "meta"))


def load_segments(job_id: str) -> list[Segment]:
    """Сегменты (кандидаты клипов). Из облака тянет ТОЛЬКО колонку ``segments``."""
    return [Segment.model_validate(s) for s in _disk_or_cloud(job_id, "segments.json", "segments")]


def load_segments_raw(job_id: str) -> list[Any]:
    """Сырые сегменты (list[dict]) — для ensure_edit (индексирование по clip_id)."""
    raw = _disk_or_cloud(job_id, "segments.json", "segments")
    return list(raw)


def load_transcript(job_id: str) -> Transcript:
    """Полный транскрипт. ДОРОГО (мегабайты): нужна лишь проверка наличия → ``has_transcript``."""
    return Transcript.model_validate(_disk_or_cloud(job_id, "transcript.json", "transcript"))


def load_transcript_words(job_id: str) -> list[Word]:
    return load_transcript(job_id).words


def has_transcript(job_id: str) -> bool:
    """Есть ли у джоба транскрипт — БЕЗ его выкачивания и парсинга (для 404-гейтов)."""
    return has_artifact(job_id, "transcript.json", "transcript")


def ensure_source(job_id: str) -> Path:
    """Путь к source.mp4. Локально/на run_job — с диска; на web/render — скачиваем из R2.

    Возвращает путь к существующему файлу. JobError, если источника нет нигде.
    """
    out = job_dir(job_id)
    src = out / "source.mp4"
    if src.exists():
        print(f"[artifacts] source cached: {job_id}")
        return src
    if cloud_enabled():
        from app.storage import download_source

        print(f"[artifacts] source missing -> downloading: {job_id}")
        out.mkdir(parents=True, exist_ok=True)
        download_source(job_id, src)
        return src
    raise JobError("artifacts", f"no source.mp4 for {job_id}")
