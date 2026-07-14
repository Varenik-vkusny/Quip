"""Прод-OOM quip-api (2026-07-14, ревью двух аудитов): ТЯЖЁЛАЯ РАБОТА В HTTP-ЗАПРОСЕ.

Контейнер `quip-api` (8 GiB, 2 vCPU, containerConcurrency=8) убивали не «утечки», а честные
аллокации внутри запроса. Замерено:
  • ``extract_loudness``           — 0.288 ГБ на КАЖДЫЙ ЧАС источника (весь waveform в RAM);
  • ASD (``score_tracks_in_segment``) — 3.76 МБ на СЕКУНДУ анализируемой дорожки (×2–3 на копии);
  • ``render_edit_to_file``       — 0.7–1.0 ГБ на 60с клип, 2.0–2.9 ГБ на 180с (+ RSS ffmpeg).
Семафор ``_cv_semaphore`` (общий бюджет контейнера) стоял ТОЛЬКО на /reframe. Три пути были
БЕЗ него: POST /jobs/{id}/clips (loudness), POST /edit/crop (ASD), GET /export/*.mp4 (рендер).
Последний — самый вероятный в проде: глобальный тумблер «убирать паузы» ставит
``silence_config.enabled`` КАЖДОМУ клипу джоба → оба fast-path'а (302/composite) закрыты →
КАЖДАЯ скачка = полный CV+энкод в web-контейнере.

Плюс ДВА одно-запросных убийцы: ``_tight_lock_cx`` гоняет ASD по ДИАПАЗОНУ ИЗ ТЕЛА ЗАПРОСА (без
границ: 10 минут = 4.5–6.8 ГБ, 6 часов ≈ 81 ГБ) и ``package.zip?include_clips=true`` зипует
клипы С ДИСКА в BytesIO (+ вторая копия в Response) — сегодня пусто, но тёплый контейнер,
только что отрендеривший клипы, даст ~2.4 ГБ × 2.

Пиннится: 503 + Retry-After на исчерпанном семафоре (и НИ ОДНОГО тяжёлого вызова), возврат
пермита на исключении, кламп CV-окна, 400 на инвертированном диапазоне, 413 на переростке-zip.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import artifacts, db
from app.config import get_settings
from app.editor import store
from app.errors import JobError
from app.models import Segment, Transcript, Word
from app.pipeline.stage0_import import SourceMeta

SEG_START, SEG_END = 0.0, 3.0


@pytest.fixture(autouse=True)
def _settings_cache() -> Any:
    """Тесты крутят REFRAME_API_* / EXPORT_* env → кэш Settings не течёт в соседние модули."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _meta(job: str) -> SourceMeta:
    return SourceMeta(
        job_id=job,
        source="upload",
        url=None,
        title="t",
        duration=30.0,
        fps=30.0,
        width=1920,
        height=1080,
    )


def _client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[TestClient, str]:
    """TestClient + джоб с одним сегментом [0,3] на диске (как test_reframe_dispatch._client)."""
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(store, "DATA_ROOT", tmp_path / "data")
    db.init_db()
    job = "jobOOM"
    d = tmp_path / "data" / job
    d.mkdir(parents=True)
    (d / "segments.json").write_text(
        json.dumps(
            [Segment(start=SEG_START, end=SEG_END, reason="r", score=0.5, type="hook").model_dump()]
        ),
        encoding="utf-8",
    )
    words = [Word(text="a", start=0.0, end=0.4), Word(text="b", start=1.0, end=1.4)]
    (d / "transcript.json").write_text(
        Transcript(language="ru", duration=3.0, words=words).model_dump_json(), encoding="utf-8"
    )
    (d / "meta.json").write_text(_meta(job).model_dump_json(), encoding="utf-8")
    db.insert_job(job, "upload", "x")
    import app.main as main

    monkeypatch.setattr(main, "_CV_SEMAPHORE", None, raising=False)
    return TestClient(main.app), job


def _exhaust_semaphore(monkeypatch: pytest.MonkeyPatch) -> None:
    """Семафор занят целиком (все пермиты выданы) → тяжёлый путь ОБЯЗАН отбить 503, не ждать."""
    import app.main as main

    monkeypatch.setattr(main, "_CV_SEMAPHORE", threading.Semaphore(0))


def _source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, job: str) -> Path:
    src = tmp_path / "data" / job / "source.mp4"
    src.write_bytes(b"\x00")
    monkeypatch.setattr(artifacts, "ensure_source", lambda _jid: src)
    return src


# ══════════════ 1. POST /jobs/{id}/clips — extract_loudness под семафором ══════════════


def test_extract_clip_returns_503_when_cv_semaphore_exhausted(monkeypatch, tmp_path) -> None:
    """0.288 ГБ/час × 8 одновременных = OOM на ≥4ч источнике. Занят → 503, loudness НЕ считаем."""
    monkeypatch.setenv("REFRAME_API_CV_WAIT_SEC", "0.05")
    client, job = _client(monkeypatch, tmp_path)
    _source(monkeypatch, tmp_path, job)
    from app.pipeline import preview_moments

    called: list[str] = []
    monkeypatch.setattr(preview_moments, "extract_loudness", lambda p: called.append(str(p)) or [])
    _exhaust_semaphore(monkeypatch)

    r = client.post(f"/jobs/{job}/clips", json={"start": 0.5, "end": 2.5})

    assert r.status_code == 503
    assert r.headers.get("Retry-After")
    assert called == []  # ← суть: waveform НЕ материализован (RAM не выросла)


def test_extract_clip_runs_loudness_when_semaphore_free(monkeypatch, tmp_path) -> None:
    """Регрессия: свободный семафор → путь работает как раньше (клип извлекается)."""
    client, job = _client(monkeypatch, tmp_path)
    _source(monkeypatch, tmp_path, job)
    import app.main as main
    from app.pipeline import preview_moments

    called: list[str] = []
    monkeypatch.setattr(preview_moments, "extract_loudness", lambda p: called.append(str(p)) or [])
    monkeypatch.setattr(main, "render_extracted_clip_job", lambda *a, **k: None)

    r = client.post(f"/jobs/{job}/clips", json={"start": 0.5, "end": 2.5})

    assert r.status_code == 200
    assert len(called) == 1


# ══════════════ 2. POST /edit/crop — ASD (_tight_lock_cx) под семафором + кламп окна ══════════════


def test_crop_returns_503_when_cv_semaphore_exhausted(monkeypatch, tmp_path) -> None:
    """Ручной tight-кроп гоняет ASD (3.76 МБ/с дорожки) — тоже из бюджета контейнера."""
    monkeypatch.setenv("REFRAME_API_CV_WAIT_SEC", "0.05")
    client, job = _client(monkeypatch, tmp_path)
    import app.main as main

    called: list[Any] = []
    monkeypatch.setattr(main, "_tight_lock_cx", lambda *a: called.append(a) or 0.5)
    v = client.get(f"/jobs/{job}/clips/clip_01/edit").json()["version"]
    _exhaust_semaphore(monkeypatch)

    r = client.post(
        f"/jobs/{job}/clips/clip_01/edit/crop",
        json={"version": v, "source_start": 0.0, "source_end": 3.0, "mode": "fill"},
    )

    assert r.status_code == 503
    assert r.headers.get("Retry-After")
    assert called == []  # ASD не запускался


def test_crop_with_explicit_center_needs_no_permit(monkeypatch, tmp_path) -> None:
    """Центр задан руками → CV не нужен → семафор НЕ трогаем (не отбиваем 503 зря)."""
    monkeypatch.setenv("REFRAME_API_CV_WAIT_SEC", "0.05")
    client, job = _client(monkeypatch, tmp_path)
    v = client.get(f"/jobs/{job}/clips/clip_01/edit").json()["version"]
    _exhaust_semaphore(monkeypatch)

    r = client.post(
        f"/jobs/{job}/clips/clip_01/edit/crop",
        json={
            "version": v,
            "source_start": 0.0,
            "source_end": 3.0,
            "mode": "fill",
            "center": 0.42,
        },
    )

    assert r.status_code == 200
    assert r.json()["reframe_overrides"][0]["center"] == 0.42


def test_tight_lock_cx_clamps_the_analysed_window(monkeypatch, tmp_path) -> None:
    """ОДИН запрос с 1-часовым диапазоном = ~14 ГБ ASD-кропов → смерть контейнера.

    Окно анализа режем до ``reframe_api_cv_max_window_sec`` (деф. 180 = clip_max_sec):
    ЛОК ЛИЦА берётся с начала диапазона (там и стоит спикер) — override всё равно применяется ко
    ВСЕМУ диапазону, меняется лишь СКОЛЬКО секунд мы декодируем ради cx.
    """
    _client(monkeypatch, tmp_path)
    import app.main as main
    from app.pipeline import asd_reframe, stage3_reframe

    _source(monkeypatch, tmp_path, "jobOOM")
    seen: list[tuple[float, float]] = []

    def fake_asd(video, w, h, start, end, fps, **k: Any) -> list[Any]:
        seen.append((start, end))
        return []

    monkeypatch.setattr(asd_reframe, "score_tracks_in_segment", fake_asd)
    monkeypatch.setattr(stage3_reframe, "locked_cx_for_tracks", lambda *a, **k: 0.6)

    cx = main._tight_lock_cx("jobOOM", 0.0, 3600.0)

    assert cx == 0.6
    assert len(seen) == 1
    start, end = seen[0]
    assert end - start == pytest.approx(180.0)  # 3600с запросили — 180с проанализировали


def test_tight_lock_cx_keeps_short_window_intact(monkeypatch, tmp_path) -> None:
    """Регрессия: нормальный диапазон (< потолка) НЕ трогаем — cx считается по всему шоту."""
    _client(monkeypatch, tmp_path)
    import app.main as main
    from app.pipeline import asd_reframe, stage3_reframe

    _source(monkeypatch, tmp_path, "jobOOM")
    seen: list[tuple[float, float]] = []
    monkeypatch.setattr(
        asd_reframe,
        "score_tracks_in_segment",
        lambda video, w, h, start, end, fps, **k: seen.append((start, end)) or [],
    )
    monkeypatch.setattr(stage3_reframe, "locked_cx_for_tracks", lambda *a, **k: 0.7)

    assert main._tight_lock_cx("jobOOM", 10.0, 40.0) == 0.7
    assert seen == [(10.0, 40.0)]


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (5.0, 5.0),  # пустой
        (9.0, 1.0),  # инвертированный
        (-3.0, 10.0),  # отрицательное начало
    ],
)
def test_crop_rejects_absurd_range_with_400(monkeypatch, tmp_path, start, end) -> None:
    """Невалидный диапазон — ЯВНАЯ ошибка клиента (400), а не молчаливый кламп/500 (правило №8)."""
    client, job = _client(monkeypatch, tmp_path)
    import app.main as main

    called: list[Any] = []
    monkeypatch.setattr(main, "_tight_lock_cx", lambda *a: called.append(a) or 0.5)
    v = client.get(f"/jobs/{job}/clips/clip_01/edit").json()["version"]

    r = client.post(
        f"/jobs/{job}/clips/clip_01/edit/crop",
        json={"version": v, "source_start": start, "source_end": end, "mode": "fill"},
    )

    assert r.status_code == 400
    assert called == []


# ══════════════ 3. GET /export/*.mp4 — полный рендер под семафором + возврат пермита ══════════════


def _wire_render(monkeypatch, main: Any, out: Path, *, boom: bool = False) -> list[str]:
    """Подменяем render_edit_to_file: пишет файл (успех) или роняет JobError (провал)."""
    calls: list[str] = []

    def _fake(job_id: str, clip_id: str, *, with_subtitles: bool, out_rel: str) -> None:
        calls.append(out_rel)
        if boom:
            raise JobError("render", "ffmpeg exploded")
        dst = out / out_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"\x00" * 16)

    monkeypatch.setattr(main, "render_edit_to_file", _fake)
    return calls


def test_clean_export_full_render_returns_503_when_semaphore_exhausted(
    monkeypatch, tmp_path
) -> None:
    """Полный рендер (0.7–2.9 ГБ) — самый жирный путь; глобальный «убирать паузы» гонит СЮДА
    КАЖДУЮ скачку джоба (оба fast-path'а закрыты) → без семафора 8 параллельных = гарантированный
    OOM."""
    monkeypatch.setenv("REFRAME_API_CV_WAIT_SEC", "0.05")
    client, job = _client(monkeypatch, tmp_path)
    import app.main as main

    calls = _wire_render(monkeypatch, main, tmp_path / "data" / job)
    _exhaust_semaphore(monkeypatch)

    r = client.get(f"/jobs/{job}/clips/clip_01/export/clean.mp4", follow_redirects=False)

    assert r.status_code == 503
    assert r.headers.get("Retry-After")
    assert calls == []  # рендер даже не начался


def test_captioned_export_full_render_returns_503_when_semaphore_exhausted(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("REFRAME_API_CV_WAIT_SEC", "0.05")
    client, job = _client(monkeypatch, tmp_path)
    import app.main as main

    calls = _wire_render(monkeypatch, main, tmp_path / "data" / job)
    _exhaust_semaphore(monkeypatch)

    r = client.get(f"/jobs/{job}/clips/clip_01/export/captioned.mp4", follow_redirects=False)

    assert r.status_code == 503
    assert r.headers.get("Retry-After")
    assert calls == []


def test_permit_is_released_when_the_guarded_render_raises(monkeypatch, tmp_path) -> None:
    """ТЕЧЬ ПЕРМИТА = медленная смерть: один упавший рендер навсегда съедал бы слот, и через
    N провалов контейнер отвечал бы 503 ВСЕМ. Семафор на 1 → первый запрос падает (500), второй
    ОБЯЗАН пройти."""
    monkeypatch.setenv("REFRAME_API_CV_CONCURRENCY", "1")
    monkeypatch.setenv("REFRAME_API_CV_WAIT_SEC", "0.05")
    client, job = _client(monkeypatch, tmp_path)
    import app.main as main

    out = tmp_path / "data" / job
    _wire_render(monkeypatch, main, out, boom=True)

    first = client.get(f"/jobs/{job}/clips/clip_01/export/captioned.mp4", follow_redirects=False)
    assert first.status_code == 500  # JobError → 500 (правило №8: видно)

    ok = _wire_render(monkeypatch, main, out)  # тот же процесс, тот же семафор
    second = client.get(f"/jobs/{job}/clips/clip_01/export/captioned.mp4", follow_redirects=False)

    assert second.status_code == 200  # ← пермит вернулся (иначе 503 навсегда)
    assert ok == ["clips/clip_01_captioned.mp4"]


# ══════════════ 4. package.zip — жёсткий потолок байтов (латентная бомба) ══════════════


def test_package_zip_over_cap_returns_413_and_does_not_buffer(monkeypatch, tmp_path) -> None:
    """``include_clips=true`` (owner-authenticated, URL руками) зипует клипы С ДИСКА в BytesIO, и
    Response буферизует ВТОРУЮ копию. На тёплом контейнере, только что отрендерившем клипы, это
    ~2.4 ГБ × 2. Потолок → ЯВНЫЙ 413, а не тихая усечка и не OOM."""
    monkeypatch.setenv("EXPORT_PACKAGE_MAX_MB", "1")
    client, job = _client(monkeypatch, tmp_path)
    clips = tmp_path / "data" / job / "clips"
    clips.mkdir(parents=True)
    (clips / "clip_01.mp4").write_bytes(b"\x00" * (2 * 1024 * 1024))  # 2 МБ > 1 МБ потолка

    r = client.get(f"/jobs/{job}/export/package.zip", params={"include_clips": "true"})

    assert r.status_code == 413
    assert "package" in json.dumps(r.json()).lower()


def test_package_zip_under_cap_still_works(monkeypatch, tmp_path) -> None:
    """Регрессия: обычный пакет (манифест+EDL+субтитры, 1–5 МБ) отдаётся как раньше."""
    client, job = _client(monkeypatch, tmp_path)

    r = client.get(f"/jobs/{job}/export/package.zip")

    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert r.content[:2] == b"PK"
