"""I/O-сужение чтения артефактов: каждый ридер тянет ТОЛЬКО СВОЮ колонку ``job_artifacts``.

ЗАЧЕМ (замер, аудит 2026-07-14): строка ``job_artifacts`` несёт meta+segments+transcript ОДНИМ
рядом. На api-контейнере (локального диска нет → всегда cloud-путь) ``load_meta()`` ради
``meta.fps`` тянул по проводу ВЕСЬ транскрипт и парсил его: 138 Б/слово на проводе, ~2.1 КБ/слово
пиковой RAM. 86-минутное видео = 15 795 слов = 2.18 МБ / 33 МБ пик; 6-часовое (MAX_VIDEO_MINUTES
=360) ≈ 113 МБ пик. Горячий путь редактора — fast-path ``GET /reframe`` — платил это на КАЖДЫЙ
запрос, а ``/chapters`` и ``/video-map`` парсили транскрипт ЦЕЛИКОМ ради проверки «а он есть?».

Тесты фиксируют ИСХОДЯЩИЙ PostgREST-запрос (``?select=…``), а не только возвращаемое значение:
регрессия «опять select=meta,segments,transcript» по возврату НЕ ВИДНА, но стоит мегабайты.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app import artifacts, db, http_client
from app import cloud_state as cs
from app.editor import store
from app.errors import JobError
from app.models import Segment, Transcript, Word

_META = {
    "job_id": "job1",
    "source": "upload",
    "url": None,
    "title": "t",
    "duration": 30.0,
    "fps": 29.97,  # дробный fps — инвариант кадровой сетки reframe (Δ обязана быть 0)
    "width": 1920,
    "height": 1080,
}
_SEGMENTS = [Segment(start=0.0, end=3.0, reason="r", score=0.5, type="hook").model_dump()]
_TRANSCRIPT = json.loads(
    Transcript(
        language="ru",
        duration=30.0,
        words=[Word(text="a", start=0.0, end=0.4), Word(text="b", start=0.5, end=0.9)],
    ).model_dump_json()
)
_ROW: dict[str, Any] = {
    "job_id": "job1",
    "meta": _META,
    "segments": _SEGMENTS,
    "transcript": _TRANSCRIPT,
}


class _Recorder:
    """Мини-PostgREST для ``/job_artifacts``: честно уважает ``?select=`` и ``<col>=not.is.null``.

    Отдаёт РОВНО запрошенные колонки (как настоящий PostgREST) → ридер, попросивший лишнего,
    получит лишнее, а попросивший только своё — только своё. ``selects`` копит клаузы запросов.
    """

    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.row = dict(_ROW) if row is None else row
        self.selects: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", f"unexpected {request.method} {request.url}"
        params = request.url.params
        select = params.get("select") or ""
        self.selects.append(select)
        for key, raw in params.multi_items():  # серверный фильтр «колонка НЕ null»
            if raw == "not.is.null" and self.row.get(key) is None:
                return httpx.Response(200, json=[])
        cols = [c for c in select.split(",") if c]
        return httpx.Response(200, json=[{c: self.row.get(c) for c in cols}])

    @property
    def pulled_transcript(self) -> bool:
        return any("transcript" in s.split(",") for s in self.selects)


def _cloud(monkeypatch: Any, tmp_path: Any, row: dict[str, Any] | None = None) -> _Recorder:
    """Облачный режим + ПУСТОЙ DATA_ROOT (диска нет) → все ридеры идут в PostgREST через мок."""
    monkeypatch.setenv("STORAGE_BACKEND", "r2")
    monkeypatch.setenv("SUPABASE_URL", "https://ref.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
    monkeypatch.setattr(store, "DATA_ROOT", tmp_path / "data")
    rec = _Recorder(row)
    client = httpx.Client(transport=httpx.MockTransport(rec))
    monkeypatch.setattr(http_client, "client", lambda: client)
    return rec


# ─────────────────────────── PURE: select_clause ───────────────────────────


def test_select_clause_joins_columns() -> None:
    assert cs.select_clause(("meta",)) == "meta"
    assert cs.select_clause(("meta", "segments")) == "meta,segments"


def test_select_clause_rejects_empty_and_bad_names() -> None:
    # Пустой список ≠ «все колонки» (это баг вызывателя) → ValueError, не тихий select=* (№8).
    with pytest.raises(ValueError):
        cs.select_clause(())
    with pytest.raises(ValueError):
        cs.select_clause(("meta", ""))
    with pytest.raises(ValueError):
        cs.select_clause(("meta,transcript",))  # склейка мимо валидации


# ─────────────────────────── ридеры тянут ТОЛЬКО свою колонку ───────────────────────────


def test_load_meta_selects_only_meta(monkeypatch: Any, tmp_path: Any) -> None:
    rec = _cloud(monkeypatch, tmp_path)
    meta = artifacts.load_meta("job1")
    assert rec.selects == ["meta"]
    assert rec.pulled_transcript is False  # ← транскрипт по проводу НЕ поехал
    assert meta.fps == 29.97  # Δ=0: кадровая сетка reframe читает ТУ ЖЕ величину
    assert meta.duration == 30.0


def test_load_segments_selects_only_segments(monkeypatch: Any, tmp_path: Any) -> None:
    rec = _cloud(monkeypatch, tmp_path)
    segs = artifacts.load_segments("job1")
    assert rec.selects == ["segments"]
    assert rec.pulled_transcript is False
    assert len(segs) == 1 and segs[0].end == 3.0


def test_load_segments_raw_selects_only_segments(monkeypatch: Any, tmp_path: Any) -> None:
    rec = _cloud(monkeypatch, tmp_path)
    raw = artifacts.load_segments_raw("job1")
    assert rec.selects == ["segments"]
    assert rec.pulled_transcript is False
    assert raw[0]["reason"] == "r"


def test_load_transcript_selects_only_transcript(monkeypatch: Any, tmp_path: Any) -> None:
    rec = _cloud(monkeypatch, tmp_path)
    tr = artifacts.load_transcript("job1")
    assert rec.selects == ["transcript"]  # meta/segments НЕ тянем
    assert tr.language == "ru" and len(tr.words) == 2


def test_load_transcript_words_selects_only_transcript(monkeypatch: Any, tmp_path: Any) -> None:
    rec = _cloud(monkeypatch, tmp_path)
    words = artifacts.load_transcript_words("job1")
    assert rec.selects == ["transcript"]
    assert [w.text for w in words] == ["a", "b"]


def test_missing_column_still_raises_job_error(monkeypatch: Any, tmp_path: Any) -> None:
    # Правило №8: колонки нет → JobError (404 у вызывателя), а не тихий None/пустой список.
    row = dict(_ROW)
    row["transcript"] = None
    _cloud(monkeypatch, tmp_path, row)
    with pytest.raises(JobError):
        artifacts.load_transcript("job1")


def test_get_job_artifacts_still_defaults_to_all_three(monkeypatch: Any, tmp_path: Any) -> None:
    # Дефолт публичного API не менялся (совместимость): без columns — все три колонки.
    rec = _cloud(monkeypatch, tmp_path)
    row = cs.get_job_artifacts("job1")
    assert rec.selects == ["meta,segments,transcript"]
    assert row is not None and set(row) == {"meta", "segments", "transcript"}


# ─────────────────────────── дешёвая проверка существования ───────────────────────────


def test_has_transcript_does_not_pull_the_blob(monkeypatch: Any, tmp_path: Any) -> None:
    rec = _cloud(monkeypatch, tmp_path)
    assert artifacts.has_transcript("job1") is True
    assert rec.selects == ["job_id"]  # по проводу — один id, а не 2.2 МБ слов
    assert rec.pulled_transcript is False


def test_has_transcript_false_when_column_null(monkeypatch: Any, tmp_path: Any) -> None:
    row = dict(_ROW)
    row["transcript"] = None
    rec = _cloud(monkeypatch, tmp_path, row)
    assert artifacts.has_transcript("job1") is False
    assert rec.pulled_transcript is False


def test_has_transcript_true_from_disk_without_any_request(monkeypatch: Any, tmp_path: Any) -> None:
    rec = _cloud(monkeypatch, tmp_path)
    d = tmp_path / "data" / "job1"
    d.mkdir(parents=True)
    (d / "transcript.json").write_text(json.dumps(_TRANSCRIPT), encoding="utf-8")
    assert artifacts.has_transcript("job1") is True
    assert rec.selects == []  # disk-first: сети вообще нет


# ─────────────────────── 404-гейты /chapters и /video-map (disk-режим) ───────────────────────
# Auth выключен (SUPABASE_URL не задан) → ownership-гейт pass-through, как в остальных API-тестах.


def _disk_client(monkeypatch: Any, tmp_path: Any, *, with_transcript: bool = True) -> Any:
    from fastapi.testclient import TestClient

    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(store, "DATA_ROOT", tmp_path / "data")
    d = tmp_path / "data" / "job1"
    d.mkdir(parents=True)
    if with_transcript:
        (d / "transcript.json").write_text(json.dumps(_TRANSCRIPT), encoding="utf-8")
    db.init_db()
    # Транскрипт НЕЛЬЗЯ парсить ради «существует?» — любой вызов = провал теста.
    monkeypatch.setattr(
        artifacts,
        "load_transcript",
        lambda _j: pytest.fail("404-check must NOT parse the whole transcript"),
    )
    from app.main import app

    return TestClient(app)


def test_chapters_existence_check_does_not_parse_transcript(
    monkeypatch: Any, tmp_path: Any
) -> None:
    import app.tasks as tasks_mod

    monkeypatch.setattr(tasks_mod, "generate_chapters_job", lambda _j: None)
    r = _disk_client(monkeypatch, tmp_path).get("/jobs/job1/chapters")
    assert r.status_code == 200 and r.json()["status"] == "pending"


def test_chapters_404_when_no_transcript(monkeypatch: Any, tmp_path: Any) -> None:
    r = _disk_client(monkeypatch, tmp_path, with_transcript=False).get("/jobs/job1/chapters")
    assert r.status_code == 404


def test_video_map_existence_check_does_not_parse_transcript(
    monkeypatch: Any, tmp_path: Any
) -> None:
    import app.tasks as tasks_mod

    monkeypatch.setattr(tasks_mod, "generate_video_map_job", lambda _j: None)
    r = _disk_client(monkeypatch, tmp_path).get("/jobs/job1/video-map")
    assert r.status_code == 200 and r.json()["status"] == "pending"


def test_video_map_404_when_no_transcript(monkeypatch: Any, tmp_path: Any) -> None:
    r = _disk_client(monkeypatch, tmp_path, with_transcript=False).get("/jobs/job1/video-map")
    assert r.status_code == 404


# ─────────── bulk-вызыватель (export handoff): артефакты грузятся ОДИН раз на запрос ───────────


def _no_artifacts(monkeypatch: Any, tmp_path: Any) -> None:
    """Ни диска, ни облака → ЛЮБОЕ чтение артефактов падает JobError (детектор лишнего I/O)."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(store, "DATA_ROOT", tmp_path / "data")
    db.init_db()


def test_ensure_edit_accepts_preloaded_artifacts(monkeypatch: Any, tmp_path: Any) -> None:
    """``ensure_edit`` с уже загруженными segments/words НЕ читает артефакты вообще.

    Экспорт-хендофф (``/export.edl`` и Ко) зовёт ensure_edit на КАЖДЫЙ клип: на 30-клиповой
    джобе это было 30 полных транскриптов по проводу. Инъекция ≠ кэш: данные передаются вниз
    ЯВНО в рамках ОДНОГО запроса → протухнуть между вызовами физически не могут.
    """
    _no_artifacts(monkeypatch, tmp_path)
    segs = [Segment.model_validate(s) for s in _SEGMENTS]
    words = [Word(text="a", start=0.0, end=0.4)]
    edit = store.ensure_edit("job1", "clip_01", segments=segs, words=words)
    assert edit.version == 1 and len(edit.source_intervals) == 1


def test_ensure_edit_without_preload_still_loads_artifacts(monkeypatch: Any, tmp_path: Any) -> None:
    # Контроль: без инъекции ensure_edit по-прежнему сам идёт за артефактами (их нет → JobError).
    _no_artifacts(monkeypatch, tmp_path)
    with pytest.raises(JobError):
        store.ensure_edit("job1", "clip_01")
