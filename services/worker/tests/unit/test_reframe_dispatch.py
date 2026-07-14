"""GET /reframe: тяжёлый CV уезжает в Cloud Run Job, web-контейнер его НЕ считает.

Прод-инцидент 2026-07-13: /reframe на промахе кэша качал source.mp4 в RAM-FS web-Service'а и гонял
PySceneDetect+MediaPipe+ASD ВНУТРИ HTTP-запроса (80–195с) → `Memory limit of 8192 MiB exceeded` →
контейнер убит ВМЕСТЕ со всеми in-flight запросами (массовые 503). Новый контракт:
  • 200 {"regions": [...]} — fast-path (запечённые регионы) ИЛИ in-process CV (edited-кейс);
  • 202 {"status": "computing"} — промах кэша на ДЕФОЛТНОМ интервале → спавним "rebake_regions";
  • 204 — compute=false и регионов нет (neighbour-prefetch редактора не платит за CV);
  • 503 — CV упал ИЛИ семафор in-process CV исчерпан (+ Retry-After).
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import artifacts, db, dispatch
from app.config import get_settings
from app.editor import reframe_cache, store
from app.models import Segment, Transcript, Word
from app.pipeline.stage0_import import SourceMeta
from app.pipeline.stage3_reframe import TrackPoint, TrackRegion


@pytest.fixture(autouse=True)
def _settings_cache() -> Any:
    """Тесты крутят REFRAME_API_* env → кэш Settings не должен течь в соседние модули."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _meta(job: str) -> SourceMeta:
    return SourceMeta(
        job_id=job,
        source="youtube",
        url=None,
        title="t",
        duration=30.0,
        fps=30.0,
        width=1920,
        height=1080,
    )


def _client(monkeypatch, tmp_path) -> tuple[TestClient, str]:
    """TestClient + джоб с одним сегментом [0,3] на диске (как test_editor_api._client)."""
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(store, "DATA_ROOT", tmp_path / "data")
    db.init_db()
    job = "jobRB"
    d = tmp_path / "data" / job
    d.mkdir(parents=True)
    (d / "segments.json").write_text(
        json.dumps([Segment(start=0.0, end=3.0, reason="r", score=0.5, type="hook").model_dump()]),
        encoding="utf-8",
    )
    words = [Word(text="a", start=0.0, end=0.4), Word(text="b", start=1.0, end=1.4)]
    (d / "transcript.json").write_text(
        Transcript(language="ru", duration=3.0, words=words).model_dump_json(), encoding="utf-8"
    )
    (d / "meta.json").write_text(_meta(job).model_dump_json(), encoding="utf-8")
    import app.main as main

    # Гвардия ре-спавна живёт в процессе → чистим между тестами (иначе порядок тестов решает).
    monkeypatch.setattr(main, "_REBAKE_SPAWNED", {})
    monkeypatch.setattr(main, "_CV_SEMAPHORE", None, raising=False)
    return TestClient(main.app), job


def _no_cv(monkeypatch) -> list[str]:
    """Запретить тяжёлый CV: любой вызов пишется в список (тест ассертит ПУСТОТУ)."""
    called: list[str] = []

    def _src(job_id: str) -> Any:
        called.append(f"ensure_source:{job_id}")
        raise AssertionError("ensure_source must not run inside the API request")

    def _cv(*a: Any, **k: Any) -> Any:
        called.append("resolve_regions_accurate")
        raise AssertionError("resolve_regions_accurate must not run inside the API request")

    monkeypatch.setattr(artifacts, "ensure_source", _src)
    monkeypatch.setattr(reframe_cache, "resolve_regions_accurate", _cv)
    return called


def _spawns(monkeypatch) -> list[tuple[Any, ...]]:
    """CLOUDRUN_JOB задан → dispatch.spawn_enabled(); перехватываем сам spawn."""
    monkeypatch.setenv("CLOUDRUN_JOB", "quip-pipeline")
    calls: list[tuple[Any, ...]] = []

    def fake_spawn(fn_name: str, *args: Any) -> str:
        calls.append((fn_name, *args))
        return "projects/p/locations/l/jobs/j/executions/x"

    monkeypatch.setattr(dispatch, "spawn", fake_spawn)
    return calls


def _payload(fps: float = 30.0) -> dict[str, Any]:
    """Канонический persist-payload дефолтного интервала [0,3] (как печёт batch/rebake)."""
    r = TrackRegion(t0=0.0, t1=3.0, mode="fill", points=(TrackPoint(t=0.0, mode="fill", cx=0.4),))
    return reframe_cache.build_persist_payload([r], 0.0, 3.0, fps=fps)


# ── 1. промах кэша на ДЕФОЛТНОМ интервале → 202 + spawn("rebake_regions"), CV в API НЕ гоняем ──


def test_cache_miss_dispatches_rebake_job_and_never_runs_cv(monkeypatch, tmp_path):
    client, job = _client(monkeypatch, tmp_path)
    cv = _no_cv(monkeypatch)
    spawned = _spawns(monkeypatch)

    r = client.get(f"/jobs/{job}/clips/clip_01/reframe")

    assert r.status_code == 202
    assert r.json() == {"status": "computing"}
    assert spawned == [("rebake_regions", job, "clip_01")]
    assert cv == []  # ← суть фикса: ни ensure_source, ни CV внутри HTTP-запроса


# ── 2. гвардия от стампида: поллинг НЕ спавнит job на каждый запрос ──


def test_repeated_polls_do_not_respawn_the_job(monkeypatch, tmp_path):
    client, job = _client(monkeypatch, tmp_path)
    _no_cv(monkeypatch)
    spawned = _spawns(monkeypatch)

    first = client.get(f"/jobs/{job}/clips/clip_01/reframe")
    second = client.get(f"/jobs/{job}/clips/clip_01/reframe")
    third = client.get(f"/jobs/{job}/clips/clip_01/reframe")

    assert [first.status_code, second.status_code, third.status_code] == [202, 202, 202]
    assert len(spawned) == 1  # один job на клип в пределах TTL


# ── 3. compute=false (neighbour-prefetch) → 204, ни спавна, ни CV ──


def test_compute_false_without_regions_returns_204(monkeypatch, tmp_path):
    client, job = _client(monkeypatch, tmp_path)
    cv = _no_cv(monkeypatch)
    spawned = _spawns(monkeypatch)

    r = client.get(f"/jobs/{job}/clips/clip_01/reframe", params={"compute": "false"})

    assert r.status_code == 204
    assert r.content == b""
    assert spawned == [] and cv == []


# ── 4. fast-path (persisted + нетронутые интервалы) → 200 без CV (регрессия) ──


def test_fast_path_still_returns_regions_without_cv(monkeypatch, tmp_path):
    client, job = _client(monkeypatch, tmp_path)
    cv = _no_cv(monkeypatch)
    spawned = _spawns(monkeypatch)
    monkeypatch.setattr(db, "get_reframe_regions", lambda j, c: _payload())

    r = client.get(f"/jobs/{job}/clips/clip_01/reframe")

    assert r.status_code == 200
    regions = r.json()["regions"]
    assert len(regions) == 1 and regions[0]["mode"] == "fill"
    assert regions[0]["points"][0]["cx"] == 0.4
    assert spawned == [] and cv == []


# ── 5. edited-интервал (трим) → честный in-process CV под семафором: занят → 503 + Retry-After ──


def test_edited_interval_returns_503_when_cv_semaphore_exhausted(monkeypatch, tmp_path):
    monkeypatch.setenv("REFRAME_API_CV_WAIT_SEC", "0.05")  # не висим на тесте
    client, job = _client(monkeypatch, tmp_path)
    _spawns(monkeypatch)
    v = client.get(f"/jobs/{job}/clips/clip_01/edit").json()["version"]
    trim = client.post(
        f"/jobs/{job}/clips/clip_01/edit/trim", json={"version": v, "word_indices": [1]}
    )
    assert trim.status_code == 200  # интервалы ≠ дефолт → путь честного CV
    cv = _no_cv(monkeypatch)
    import app.main as main

    monkeypatch.setattr(main, "_CV_SEMAPHORE", threading.Semaphore(0))

    r = client.get(f"/jobs/{job}/clips/clip_01/reframe")

    assert r.status_code == 503
    assert r.headers.get("Retry-After")
    assert cv == []  # семафор отбил ДО скачивания source (RAM не растёт)


def test_edited_interval_runs_cv_in_process_when_semaphore_free(monkeypatch, tmp_path):
    client, job = _client(monkeypatch, tmp_path)
    spawned = _spawns(monkeypatch)
    v = client.get(f"/jobs/{job}/clips/clip_01/edit").json()["version"]
    client.post(f"/jobs/{job}/clips/clip_01/edit/trim", json={"version": v, "word_indices": [1]})
    src = tmp_path / "data" / job / "source.mp4"
    src.write_bytes(b"\x00")
    monkeypatch.setattr(artifacts, "ensure_source", lambda jid: src)
    pt = TrackPoint(t=0.0, mode="fill", cx=0.5)
    monkeypatch.setattr(
        reframe_cache,
        "resolve_regions_accurate",
        lambda *a, **k: [
            [TrackRegion(t0=0.0, t1=0.4, mode="fill", points=(pt,))],
            [TrackRegion(t0=0.0, t1=1.6, mode="fill", points=(pt,))],
        ],
    )

    r = client.get(f"/jobs/{job}/clips/clip_01/reframe")

    assert r.status_code == 200
    assert len(r.json()["regions"]) == 2  # два интервала после трима
    assert spawned == []  # edited-кейс НЕ спавнит rebake (payload его не обслужит)


# ── 6. rebake-job печёт payload, который читает fast-path (round-trip) ──


def test_rebake_job_persists_payload_the_fast_path_can_read(monkeypatch, tmp_path):
    client, job = _client(monkeypatch, tmp_path)
    from app import tasks

    baked: dict[tuple[str, str], dict[str, Any]] = {}
    monkeypatch.setattr(db, "put_reframe_regions", lambda j, c, v: baked.__setitem__((j, c), v))
    monkeypatch.setattr(db, "get_reframe_regions", lambda j, c: baked.get((j, c)))
    src = tmp_path / "data" / job / "source.mp4"
    src.write_bytes(b"\x00")
    monkeypatch.setattr(artifacts, "ensure_source", lambda jid: src)
    seen: list[Any] = []

    def fake_cv(video, intervals, overrides, **k: Any) -> Any:
        seen.append((intervals, overrides, k.get("lock_per_shot")))
        pt = TrackPoint(t=0.0, mode="fill", cx=0.7)
        return [[TrackRegion(t0=0.0, t1=3.0, mode="fill", points=(pt,))]]

    monkeypatch.setattr(reframe_cache, "resolve_regions_accurate", fake_cv)

    tasks.rebake_reframe_regions_job(job, "clip_01")

    # печём ДЕФОЛТНЫЙ интервал сегмента (не пользовательский трим), locked, без override'ов
    intervals, overrides, locked = seen[0]
    assert [(i.source_start, i.source_end) for i in intervals] == [(0.0, 3.0)]
    assert overrides == [] and locked is True
    payload = baked[(job, "clip_01")]
    assert payload["default_start"] == 0.0 and payload["default_end"] == 3.0
    assert payload["fps"] == 30.0

    # …и fast-path его читает: 200 без CV
    cv = _no_cv(monkeypatch)
    r = client.get(f"/jobs/{job}/clips/clip_01/reframe")
    assert r.status_code == 200
    assert r.json()["regions"][0]["points"][0]["cx"] == 0.7
    assert cv == []


def test_rebake_job_all_clips_when_clip_id_is_none(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    from app import tasks

    job = "jobRB"
    d = tmp_path / "data" / job
    (d / "segments.json").write_text(
        json.dumps(
            [
                Segment(start=0.0, end=3.0, reason="r", score=0.5, type="hook").model_dump(),
                Segment(start=5.0, end=9.0, reason="r", score=0.5, type="hook").model_dump(),
            ]
        ),
        encoding="utf-8",
    )
    baked: dict[tuple[str, str], dict[str, Any]] = {}
    monkeypatch.setattr(db, "put_reframe_regions", lambda j, c, v: baked.__setitem__((j, c), v))
    src = d / "source.mp4"
    src.write_bytes(b"\x00")
    monkeypatch.setattr(artifacts, "ensure_source", lambda jid: src)
    monkeypatch.setattr(
        reframe_cache,
        "resolve_regions_accurate",
        lambda video, intervals, overrides, **k: [
            [
                TrackRegion(
                    t0=0.0,
                    t1=intervals[0].source_end - intervals[0].source_start,
                    mode="fill",
                    points=(TrackPoint(t=0.0, mode="fill", cx=0.5),),
                )
            ]
        ],
    )

    tasks.rebake_reframe_regions_job(job)

    assert sorted(baked) == [(job, "clip_01"), (job, "clip_02")]
    assert baked[(job, "clip_02")]["default_start"] == 5.0
    assert baked[(job, "clip_02")]["default_end"] == 9.0


def test_rebake_job_raises_on_missing_clip(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    from app import tasks
    from app.errors import JobError

    with pytest.raises(JobError):
        tasks.rebake_reframe_regions_job("jobRB", "clip_09")  # сегмента нет → НЕ молча


def test_rebake_registered_as_cloudrun_job_kind() -> None:
    """JOB_KIND "rebake_regions" должен резолвиться в entrypoint'е Cloud Run Job."""
    import app.cloudrun_job as cj
    from app import tasks

    calls: list[tuple[Any, ...]] = []
    os_env = {"JOB_KIND": "rebake_regions", "JOB_ARGS": json.dumps(["job1", "clip_01"])}
    with pytest.MonkeyPatch.context() as mp:
        for k, v in os_env.items():
            mp.setenv(k, v)
        mp.setattr(tasks, "rebake_reframe_regions_job", lambda *a: calls.append(a))
        assert cj.main() == 0
    assert calls == [("job1", "clip_01")]
