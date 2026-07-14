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


def _payload(fps: float = 30.0, cx: float = 0.4) -> dict[str, Any]:
    """Канонический persist-payload дефолтного интервала [0,3] (как печёт batch/rebake)."""
    r = TrackRegion(t0=0.0, t1=3.0, mode="fill", points=(TrackPoint(t=0.0, mode="fill", cx=cx),))
    return reframe_cache.build_persist_payload([r], 0.0, 3.0, fps=fps)


def _baked_store(monkeypatch) -> dict[tuple[str, str, bool], dict[str, Any]]:
    """Фейк-хранилище персиста, РАЗЛИЧАЮЩЕЕ вариант (job, clip, lock_per_shot) — как реальный
    key-aware слой (locked → clip_id, follow → clip_id#follow в том же jsonb)."""
    baked: dict[tuple[str, str, bool], dict[str, Any]] = {}

    def _put(j: str, c: str, v: dict[str, Any], *, lock_per_shot: bool = True) -> None:
        baked[(j, c, lock_per_shot)] = v

    def _get(j: str, c: str, *, lock_per_shot: bool = True) -> dict[str, Any] | None:
        return baked.get((j, c, lock_per_shot))

    monkeypatch.setattr(db, "put_reframe_regions", _put)
    monkeypatch.setattr(db, "get_reframe_regions", _get)
    return baked


def _set_follow(client: TestClient, job: str, clip_id: str = "clip_01") -> None:
    """Переключить клип в follow-режим (lock_per_shot=False) через продуктовый эндпоинт."""
    v = client.get(f"/jobs/{job}/clips/{clip_id}/edit").json()["version"]
    r = client.post(
        f"/jobs/{job}/clips/{clip_id}/edit/motion", json={"version": v, "lock_per_shot": False}
    )
    assert r.status_code == 200 and r.json()["lock_per_shot"] is False


# ── 1. промах кэша на ДЕФОЛТНОМ интервале → 202 + spawn("rebake_regions"), CV в API НЕ гоняем ──


def test_cache_miss_dispatches_rebake_job_and_never_runs_cv(monkeypatch, tmp_path):
    client, job = _client(monkeypatch, tmp_path)
    cv = _no_cv(monkeypatch)
    spawned = _spawns(monkeypatch)

    r = client.get(f"/jobs/{job}/clips/clip_01/reframe")

    assert r.status_code == 202
    assert r.json() == {"status": "computing"}
    assert spawned == [("rebake_regions", job, "clip_01", True)]  # locked-вариант
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
    baked = _baked_store(monkeypatch)
    baked[(job, "clip_01", True)] = _payload()

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

    baked = _baked_store(monkeypatch)
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
    payload = baked[(job, "clip_01", True)]
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
    baked = _baked_store(monkeypatch)
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

    assert sorted(baked) == [(job, "clip_01", True), (job, "clip_02", True)]
    assert baked[(job, "clip_02", True)]["default_start"] == 5.0
    assert baked[(job, "clip_02", True)]["default_end"] == 9.0


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


# ═══════════════ FOLLOW-режим: свой ключ персиста, тот же кэш (2026-07-14) ═══════════════
# Прод-данные: КАЖДЫЙ клип, круживший CV в API (clip_29/12/02/06/09), был lock_per_shot=False.
# Персист хранил ТОЛЬКО locked-план → follow НИКОГДА не попадал в fast-path и НИКОГДА не мог
# забэкфилиться → полный CV на КАЖДОЕ открытие редактора, вечно. Фикс: follow-план живёт в ТОЙ ЖЕ
# jsonb-колонке под ОТДЕЛЬНЫМ ключом "<clip_id>#follow" (RPC merge пишет тот ключ, что передан).


def test_regions_key_locked_is_clip_id_and_follow_is_suffixed() -> None:
    """Ключ хранения: locked = clip_id (БАЙТ-В-БАЙТ как сегодня), follow = clip_id#follow."""
    from app.cloud_state import reframe_regions_key

    assert reframe_regions_key("clip_01") == "clip_01"
    assert reframe_regions_key("clip_01", lock_per_shot=True) == "clip_01"
    assert reframe_regions_key("clip_01", lock_per_shot=False) == "clip_01#follow"


def test_follow_clip_with_baked_follow_plan_hits_fast_path_without_cv(monkeypatch, tmp_path):
    """Суть фикса: follow-клип с испечённым follow-планом → 200 из FAST-PATH, БЕЗ CV и спавна."""
    client, job = _client(monkeypatch, tmp_path)
    spawned = _spawns(monkeypatch)
    baked = _baked_store(monkeypatch)
    baked[(job, "clip_01", False)] = _payload(cx=0.9)  # follow-вариант испечён
    _set_follow(client, job)
    cv = _no_cv(monkeypatch)

    r = client.get(f"/jobs/{job}/clips/clip_01/reframe")

    assert r.status_code == 200
    assert r.json()["regions"][0]["points"][0]["cx"] == 0.9  # именно follow-план
    assert cv == []  # ← ни ensure_source, ни resolve_regions_accurate в HTTP-запросе
    assert spawned == []


def test_follow_clip_never_serves_the_locked_plan(monkeypatch, tmp_path):
    """Locked-план НЕ подмешивается в follow (разные ключи) → follow без своего плана = промах."""
    client, job = _client(monkeypatch, tmp_path)
    _spawns(monkeypatch)
    baked = _baked_store(monkeypatch)
    baked[(job, "clip_01", True)] = _payload(cx=0.4)  # есть ТОЛЬКО locked
    _set_follow(client, job)
    cv = _no_cv(monkeypatch)

    r = client.get(f"/jobs/{job}/clips/clip_01/reframe")

    assert r.status_code == 202  # промах follow-ключа → печём follow, а не отдаём locked
    assert cv == []


def test_follow_cache_miss_dispatches_rebake_with_lock_false_and_no_cv(monkeypatch, tmp_path):
    """Follow-промах на ДЕФОЛТНОМ интервале → 202 + Job с lock_per_shot=False (CV в API нет)."""
    client, job = _client(monkeypatch, tmp_path)
    spawned = _spawns(monkeypatch)
    _baked_store(monkeypatch)
    _set_follow(client, job)
    cv = _no_cv(monkeypatch)

    r = client.get(f"/jobs/{job}/clips/clip_01/reframe")

    assert r.status_code == 202
    assert r.json() == {"status": "computing"}
    assert spawned == [("rebake_regions", job, "clip_01", False)]
    assert cv == []


def test_locked_and_follow_spawns_are_claimed_independently(monkeypatch, tmp_path):
    """Гвардия от стампида — ПО ВАРИАНТУ: locked-заявка не блокирует спавн follow того же клипа."""
    client, job = _client(monkeypatch, tmp_path)
    spawned = _spawns(monkeypatch)
    _baked_store(monkeypatch)
    _no_cv(monkeypatch)

    assert client.get(f"/jobs/{job}/clips/clip_01/reframe").status_code == 202  # locked
    _set_follow(client, job)
    assert client.get(f"/jobs/{job}/clips/clip_01/reframe").status_code == 202  # follow

    assert spawned == [
        ("rebake_regions", job, "clip_01", True),
        ("rebake_regions", job, "clip_01", False),
    ]


def test_rebake_job_bakes_the_requested_variant_only(monkeypatch, tmp_path):
    """Job печёт ИМЕННО запрошенный вариант (follow), под follow-ключом; locked НЕ трогает."""
    _client(monkeypatch, tmp_path)
    from app import tasks

    job = "jobRB"
    baked = _baked_store(monkeypatch)
    src = tmp_path / "data" / job / "source.mp4"
    src.write_bytes(b"\x00")
    monkeypatch.setattr(artifacts, "ensure_source", lambda jid: src)
    seen: list[Any] = []

    def fake_cv(video, intervals, overrides, **k: Any) -> Any:
        seen.append(k.get("lock_per_shot"))
        pt = TrackPoint(t=0.0, mode="fill", cx=0.9)
        return [[TrackRegion(t0=0.0, t1=3.0, mode="fill", points=(pt,))]]

    monkeypatch.setattr(reframe_cache, "resolve_regions_accurate", fake_cv)

    tasks.rebake_reframe_regions_job(job, "clip_01", lock_per_shot=False)

    assert seen == [False]  # follow — ДРУГОЙ CV-прогон; ОДИН, не оба (2× стоимость)
    assert list(baked) == [(job, "clip_01", False)]


def test_locked_and_follow_payloads_coexist_in_one_jsonb_column(monkeypatch):
    """db→cloud_state round-trip: follow-план НЕ перетирает канонический locked (и наоборот).

    Эмулируем серверный ``||``-merge RPC (migrations/0013): он пишет по ПЕРЕДАННОМУ p_clip_id →
    разные ключи = разные записи в ОДНОЙ jsonb-колонке (миграция не нужна).
    """
    from app import http_client as http

    monkeypatch.setenv("STORAGE_BACKEND", "r2")
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    blob: dict[str, Any] = {}

    class _Resp:
        def __init__(self, payload: Any) -> None:
            self._p = payload
            self.status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> Any:
            return self._p

    def fake_post(url: str, **kw: Any) -> Any:
        assert url.endswith("/rpc/merge_reframe_regions")
        body = kw["json"]
        blob[body["p_clip_id"]] = body["p_value"]  # jsonb ||-merge по переданному ключу
        return _Resp(1)

    def fake_get(url: str, **kw: Any) -> Any:
        return _Resp([{"reframe_regions": dict(blob)}])

    monkeypatch.setattr(http, "post", fake_post)
    monkeypatch.setattr(http, "get", fake_get)

    db.put_reframe_regions("j", "clip_01", _payload(cx=0.4))  # канонический locked
    db.put_reframe_regions("j", "clip_01", _payload(cx=0.9), lock_per_shot=False)  # follow

    assert sorted(blob) == ["clip_01", "clip_01#follow"]
    locked = db.get_reframe_regions("j", "clip_01")
    follow = db.get_reframe_regions("j", "clip_01", lock_per_shot=False)
    assert locked is not None and follow is not None
    assert locked["regions"][0]["points"][0]["cx"] == 0.4  # canonical цел
    assert follow["regions"][0]["points"][0]["cx"] == 0.9


# ── RENDER (зеркало превью): plan-read обязан быть mode-aware, иначе превью ≠ скачанный файл ──


def _render_with_plan(monkeypatch, tmp_path, *, follow: bool) -> tuple[list[Any], list[Any]]:
    """Прогнать render_edit_to_file (без субтитров) на клипе с испечённым планом ВАРИАНТА.
    Возвращает (вызовы CV, регионы, доехавшие до render_timeline)."""
    from app import tasks
    from app.pipeline import stage0_import, stage5_render

    client, job = _client(monkeypatch, tmp_path)
    baked = _baked_store(monkeypatch)
    baked[(job, "clip_01", follow is False)] = _payload(cx=0.9 if follow else 0.4)
    if follow:
        _set_follow(client, job)
    else:
        client.get(f"/jobs/{job}/clips/clip_01/edit")  # ensure_edit (locked по дефолту)

    src = tmp_path / "data" / job / "source.mp4"
    src.write_bytes(b"\x00")
    monkeypatch.setattr(artifacts, "ensure_source", lambda jid: src)
    monkeypatch.setattr(stage0_import, "probe_video", lambda p: {})
    monkeypatch.setattr(stage0_import, "_video_stream", lambda p: {"r_frame_rate": "30/1"})
    cv: list[Any] = []
    monkeypatch.setattr(
        reframe_cache, "resolve_regions_accurate", lambda *a, **k: cv.append(k) or [[]]
    )
    rendered: list[Any] = []
    monkeypatch.setattr(stage5_render, "render_timeline", lambda *a, **k: rendered.append((a, k)))
    tasks.render_edit_to_file(job, "clip_01", with_subtitles=False, out_rel="clips/x.mp4")
    return cv, rendered


def test_render_reads_baked_follow_plan_without_cv(monkeypatch, tmp_path):
    """Follow-клип с испечённым follow-планом → рендер берёт ПЛАН (0 CV). Зеркало превью."""
    cv, rendered = _render_with_plan(monkeypatch, tmp_path, follow=True)

    assert cv == []  # ← иначе рендер считал бы CV, а превью отдавало план → WYSIWYG-дрейф
    regions = rendered[0][0][3]  # render_timeline(out, src, intervals, regions, ...)
    assert regions[0][0].points[0].cx == 0.9  # именно follow-план


def test_render_locked_plan_read_still_hits(monkeypatch, tmp_path):
    """Регрессия locked-пути: канонический план по-прежнему читается без CV."""
    cv, rendered = _render_with_plan(monkeypatch, tmp_path, follow=False)

    assert cv == []
    assert rendered[0][0][3][0][0].points[0].cx == 0.4


def test_cloudrun_job_threads_lock_per_shot_arg() -> None:
    """JOB_ARGS (позиционный JSON-список) доносит вариант до rebake-функции в контейнере Job'а."""
    import app.cloudrun_job as cj
    from app import tasks

    calls: list[tuple[Any, ...]] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("JOB_KIND", "rebake_regions")
        mp.setenv("JOB_ARGS", json.dumps(["job1", "clip_01", False]))
        mp.setattr(tasks, "rebake_reframe_regions_job", lambda *a: calls.append(a))
        assert cj.main() == 0
    assert calls == [("job1", "clip_01", False)]
