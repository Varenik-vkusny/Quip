"""Интеграция POST /jobs/{job_id}/clips — on-demand экстракция клипа (TestClient).

Гейты СЕРВЕРНЫЕ (обойти с фронта нельзя): план владельца (pro/studio) → доступность источника
(60-дневное окно) → идемпотентность → per-clip пейволл за cap. На успех: клип добавляется pending
(пустой video_url) + рендер спавнится в фоне, публикуя video_url через set_clip_ready РОВНО как
авто-клип. Тяжёлый рендер замокан (без ffmpeg); artifacts/loudness — фикстуры.

⚠️ Persistent tmp jobs.db gotcha: _DB_PATH монки-патчится на СВЕЖИЙ tmp_path/jobs.db (уникален на
тест) + уникальные uid/job_id → нет утечки состояния между тестами.
"""

from __future__ import annotations

import types
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import artifacts, db
from app.editor import store
from app.errors import JobError
from app.models import Transcript, Word
from app.pipeline import preview_moments
from app.pipeline.stage0_import import SourceMeta


def _words(n: int = 60) -> list[Word]:
    return [Word(text=f"w{k}", start=float(k), end=float(k) + 0.8) for k in range(n)]


def _meta(job_id: str) -> SourceMeta:
    return SourceMeta(
        job_id=job_id,
        source="youtube",
        url=None,
        title="t",
        duration=60.0,
        fps=30.0,
        width=1920,
        height=1080,
    )


class _Ctx:
    def __init__(self, client: TestClient, job: str, uid: str, render_calls: list[Any]) -> None:
        self.client = client
        self.job = job
        self.uid = uid
        self.render_calls = render_calls


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    *,
    plan: str | None = "pro",
    source_ok: bool = True,
) -> _Ctx:
    """Свежая БД + строка джоба (owner=uid) + план + замоканные artifacts/loudness/рендер."""
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(store, "DATA_ROOT", tmp_path / "data")
    db.init_db()

    uid = f"u_{uuid.uuid4().hex[:8]}"
    job = f"job_{uuid.uuid4().hex[:8]}"
    db.insert_job(job, "youtube", "https://yt/x", user_id=uid)
    if plan is not None:
        db.set_user_plan(uid, plan)

    # artifacts (диск не нужен — мокаем ридеры)
    monkeypatch.setattr(
        artifacts,
        "load_transcript",
        lambda jid: Transcript(language="en", duration=60.0, words=_words()),
    )
    monkeypatch.setattr(artifacts, "load_meta", lambda jid: _meta(jid))
    if source_ok:
        src = tmp_path / "data" / job / "source.mp4"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"\x00")
        monkeypatch.setattr(artifacts, "ensure_source", lambda jid: src)
    else:

        def _gone(jid: str) -> Any:
            raise JobError("artifacts", f"no source.mp4 for {jid}")

        monkeypatch.setattr(artifacts, "ensure_source", _gone)

    # loudness — фикстура (без ffmpeg); варианс, чтобы clip_energy отдал float
    monkeypatch.setattr(
        preview_moments, "extract_loudness", lambda p, **k: [-30.0] * 100 + [-5.0] * 20
    )

    # рендер-спавн → no-op-шпион (без реального ffmpeg); фиксируем (job, clip_index, user_id)
    import app.main as main

    render_calls: list[Any] = []

    def _spy(job_id: str, clip_index: int, seg: Any, meta: Any, user_id: str | None = None) -> None:
        render_calls.append((job_id, clip_index, user_id))

    monkeypatch.setattr(main, "render_extracted_clip_job", _spy)
    # детерминизм cap/цены (независимо от .env)
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: types.SimpleNamespace(extract_free_cap=30, extract_clip_price_usd=1.0),
    )

    return _Ctx(TestClient(main.app), job, uid, render_calls)


def _seed_clips(job: str, n: int) -> None:
    """Заполнить джоб n клипами с НЕ-пересекающимися границами (диапазоны вне [0,60] источника)."""
    for i in range(1, n + 1):
        db.append_pending_clip(
            job,
            {"id": f"clip_{i:02d}", "start": 100.0 + i, "end": 120.0 + i, "video_url": ""},
        )


# ─────────────────────────── happy path (pro, under cap) ───────────────────────────


def test_pro_under_cap_appends_pending_and_spawns_render(monkeypatch, tmp_path) -> None:
    ctx = _setup(monkeypatch, tmp_path, plan="pro")
    r = ctx.client.post(f"/jobs/{ctx.job}/clips", json={"start": 10.0, "end": 40.0})
    assert r.status_code == 200
    clip = r.json()
    assert clip["id"] == "clip_01"
    assert clip["video_url"] == ""  # pending (streaming-контракт)
    assert clip["start"] < clip["end"]
    # клип реально добавлен в строку джоба
    stored = db.get_job(ctx.job)["clips"]
    assert len(stored) == 1 and stored[0]["id"] == "clip_01"
    # рендер заспавнен РОВНО на новый индекс 1, с owner-user_id
    assert ctx.render_calls == [(ctx.job, 1, ctx.uid)]


def test_studio_can_extract(monkeypatch, tmp_path) -> None:
    ctx = _setup(monkeypatch, tmp_path, plan="studio")
    r = ctx.client.post(f"/jobs/{ctx.job}/clips", json={"start": 12.0, "end": 45.0})
    assert r.status_code == 200
    assert len(ctx.render_calls) == 1


def test_appended_index_after_existing_clips(monkeypatch, tmp_path) -> None:
    # Уже 3 клипа → новый должен встать на индекс 4 (set_clip_ready адресует clips[idx-1]).
    ctx = _setup(monkeypatch, tmp_path, plan="pro")
    _seed_clips(ctx.job, 3)
    r = ctx.client.post(f"/jobs/{ctx.job}/clips", json={"start": 10.0, "end": 40.0})
    assert r.status_code == 200
    assert r.json()["id"] == "clip_04"
    assert ctx.render_calls == [(ctx.job, 4, ctx.uid)]
    assert len(db.get_job(ctx.job)["clips"]) == 4


# ─────────────────────────── plan gate (403) ───────────────────────────


def test_free_plan_forbidden(monkeypatch, tmp_path) -> None:
    ctx = _setup(monkeypatch, tmp_path, plan="free")
    r = ctx.client.post(f"/jobs/{ctx.job}/clips", json={"start": 10.0, "end": 40.0})
    assert r.status_code == 403
    assert r.json()["detail"] == {"error": "upgrade_required"}
    assert ctx.render_calls == []


def test_starter_plan_forbidden(monkeypatch, tmp_path) -> None:
    ctx = _setup(monkeypatch, tmp_path, plan="starter")
    r = ctx.client.post(f"/jobs/{ctx.job}/clips", json={"start": 10.0, "end": 40.0})
    assert r.status_code == 403
    assert r.json()["detail"] == {"error": "upgrade_required"}


def test_no_profile_defaults_free_forbidden(monkeypatch, tmp_path) -> None:
    # Нет строки profiles → get_user_plan="free" (безопасный дефолт) → 403.
    ctx = _setup(monkeypatch, tmp_path, plan=None)
    r = ctx.client.post(f"/jobs/{ctx.job}/clips", json={"start": 10.0, "end": 40.0})
    assert r.status_code == 403


# ─────────────────────────── source expired (409) ───────────────────────────


def test_source_expired_returns_409(monkeypatch, tmp_path) -> None:
    ctx = _setup(monkeypatch, tmp_path, plan="pro", source_ok=False)
    r = ctx.client.post(f"/jobs/{ctx.job}/clips", json={"start": 10.0, "end": 40.0})
    assert r.status_code == 409
    assert r.json()["detail"] == {"error": "source_expired"}
    assert ctx.render_calls == []


# ─────────────────────────── paywall over cap (402) ───────────────────────────


def test_over_cap_requires_payment(monkeypatch, tmp_path) -> None:
    ctx = _setup(monkeypatch, tmp_path, plan="pro")
    _seed_clips(ctx.job, 30)  # == cap → следующий клип платный
    r = ctx.client.post(f"/jobs/{ctx.job}/clips", json={"start": 10.0, "end": 40.0})
    assert r.status_code == 402
    assert r.json()["detail"] == {"error": "payment_required", "price_usd": 1.0}
    assert ctx.render_calls == []  # не рендерим, пока не оплатили
    assert len(db.get_job(ctx.job)["clips"]) == 30  # ничего не добавили


# ─────────────────────────── idempotency (same range → same clip) ───────────────────────────


def test_idempotent_same_range_returns_same_clip_no_double_render(monkeypatch, tmp_path) -> None:
    ctx = _setup(monkeypatch, tmp_path, plan="pro")
    r1 = ctx.client.post(f"/jobs/{ctx.job}/clips", json={"start": 10.0, "end": 40.0})
    assert r1.status_code == 200
    first = r1.json()
    # повтор ТОГО ЖЕ диапазона → тот же клип, БЕЗ второго рендера/аппенда
    r2 = ctx.client.post(f"/jobs/{ctx.job}/clips", json={"start": 10.0, "end": 40.0})
    assert r2.status_code == 200
    assert r2.json()["id"] == first["id"]
    assert r2.json()["start"] == first["start"] and r2.json()["end"] == first["end"]
    assert len(ctx.render_calls) == 1  # рендер спавнился ровно ОДИН раз
    assert len(db.get_job(ctx.job)["clips"]) == 1  # клип не задвоился


def test_idempotent_within_rounding_tolerance(monkeypatch, tmp_path) -> None:
    # segment_key округляет до 0.1с → близкий джиттер диапазона схлопывается в тот же клип.
    ctx = _setup(monkeypatch, tmp_path, plan="pro")
    r1 = ctx.client.post(f"/jobs/{ctx.job}/clips", json={"start": 10.0, "end": 40.0})
    assert r1.status_code == 200
    r2 = ctx.client.post(f"/jobs/{ctx.job}/clips", json={"start": 10.02, "end": 40.03})
    assert r2.status_code == 200
    assert r2.json()["id"] == r1.json()["id"]
    assert len(ctx.render_calls) == 1


# ─────────────────────────── job not found (404) ───────────────────────────


def test_missing_job_returns_404(monkeypatch, tmp_path) -> None:
    ctx = _setup(monkeypatch, tmp_path, plan="pro")
    r = ctx.client.post("/jobs/job_does_not_exist/clips", json={"start": 10.0, "end": 40.0})
    assert r.status_code == 404
