"""POST /jobs/{job}/apply-silence-all — применить удаление пауз КО ВСЕМ клипам одного видео.

Близнец apply-style-all, но пишет ТОЛЬКО silence_config; ортогонален стилю (style-all паузы не
трогает, silence-all стиль не трогает, шаблон не несёт ни того, ни другого).
Действует только на клипы ЭТОЙ джобы, не на глобальный дефолт юзера.
"""

from __future__ import annotations

import json
import time

import jwt
from fastapi.testclient import TestClient

from app import db
from app.config import get_settings
from app.editor import store
from app.models import Segment, Transcript, Word

_SECRET = "test-hs256-secret-at-least-32-bytes-long!!"


def _seed(monkeypatch, tmp_path, *, auth_on: bool = False, owner: str | None = None):
    """Джоба с ДВУМЯ клипами: 2 сегмента на диске + clips_json. Auth off по умолчанию."""
    if auth_on:
        monkeypatch.setenv("SUPABASE_URL", "https://ref.supabase.co")
        monkeypatch.setenv("SUPABASE_JWT_SECRET", _SECRET)
    else:
        monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(store, "DATA_ROOT", tmp_path / "data")
    import app.main as main

    monkeypatch.setattr(main, "DATA_ROOT", tmp_path / "data")
    db.init_db()
    sfx = str(int(time.time() * 1_000_000))
    job = f"job_{sfx}"
    d = tmp_path / "data" / job
    d.mkdir(parents=True)
    segs = [
        Segment(start=0.0, end=3.0, reason="r", score=0.5, type="hook"),
        Segment(start=5.0, end=9.0, reason="r", score=0.5, type="hook"),
    ]
    (d / "segments.json").write_text(json.dumps([s.model_dump() for s in segs]), encoding="utf-8")
    words = [
        Word(text="a", start=0.0, end=0.4),
        Word(text="b", start=0.5, end=0.9),
        Word(text="c", start=6.0, end=6.4),
    ]
    (d / "transcript.json").write_text(
        Transcript(language="ru", duration=10.0, words=words).model_dump_json(),
        encoding="utf-8",
    )
    db.insert_job(job, "upload", "x", user_id=owner)
    db.set_clips_pending(job, [{"id": "clip_01"}, {"id": "clip_02"}])
    return TestClient(main.app), job


def _bearer(sub: str) -> dict[str, str]:
    tok = jwt.encode(
        {"sub": sub, "aud": "authenticated", "exp": int(time.time()) + 3600},
        _SECRET,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {tok}"}


def _edit(client, job, clip):
    return client.get(f"/jobs/{job}/clips/{clip}/edit").json()


def test_enables_pauses_on_every_clip(monkeypatch, tmp_path):
    client, job = _seed(monkeypatch, tmp_path)
    r = client.post(
        f"/jobs/{job}/apply-silence-all",
        json={"silence_config": {"enabled": True, "min_pause_ms": 300}},
    )
    assert r.status_code == 200
    assert r.json() == {"applied": 2, "total": 2}
    for c in ("clip_01", "clip_02"):
        e = _edit(client, job, c)
        assert e["silence_config"]["enabled"] is True
        assert e["silence_config"]["min_pause_ms"] == 300


def test_preserves_everything_else(monkeypatch, tmp_path):
    # Сначала задаём НЕ-дефолтный стиль субтитров на clip_01 через apply-style-all…
    client, job = _seed(monkeypatch, tmp_path)
    before = _edit(client, job, "clip_01")
    styled = {**before["captions"]["style"], "color": "#00FF00"}
    client.post(
        f"/jobs/{job}/apply-style-all",
        json={"style": styled, "highlight": None, "hook_style": None},
    )
    intervals_before = _edit(client, job, "clip_01")["source_intervals"]

    # …затем silence-all. Стиль и интервалы должны УЦЕЛЕТЬ — тронут только silence_config.
    client.post(f"/jobs/{job}/apply-silence-all", json={"silence_config": {"enabled": True}})
    after = _edit(client, job, "clip_01")
    assert after["silence_config"]["enabled"] is True
    assert after["captions"]["style"]["color"] == "#00FF00"  # стиль цел
    assert after["source_intervals"] == intervals_before  # интервалы целы
    assert after["version"] > before["version"]  # RENDER-REQUIRED → версия бампнулась


def test_null_config_turns_pauses_off_on_all(monkeypatch, tmp_path):
    client, job = _seed(monkeypatch, tmp_path)
    client.post(f"/jobs/{job}/apply-silence-all", json={"silence_config": {"enabled": True}})
    # выключаем на всём видео
    r = client.post(f"/jobs/{job}/apply-silence-all", json={"silence_config": None})
    assert r.status_code == 200 and r.json()["applied"] == 2
    for c in ("clip_01", "clip_02"):
        assert _edit(client, job, c)["silence_config"] is None


def test_owner_only(monkeypatch, tmp_path):
    # Массовая мутация СТРОЖЕ обычного гейта: чужой владелец → 403 (как apply-style-all).
    client, job = _seed(monkeypatch, tmp_path, auth_on=True, owner="userA")
    r = client.post(
        f"/jobs/{job}/apply-silence-all",
        json={"silence_config": {"enabled": True}},
        headers=_bearer("userB"),
    )
    assert r.status_code == 403
