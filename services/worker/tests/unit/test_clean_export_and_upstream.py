"""Прод-инцидент 2026-07-13 (OOM/503/504 на quip-api) — регрессионные тесты двух корней.

1. ``/export/clean.mp4`` был ГАРАНТИРОВАННЫМ отказом: он ВСЕГДА качал весь source в контейнер
   (на Cloud Run локальная ФС = RAM → OOM при 8 GiB) и ВСЕГДА пересчитывал CV + энкодил (дольше
   300s request-timeout → 504). captioned спасли 302-редиректом на CDN 2026-07-10 — clean остался
   без него. Пиннит: нетронутый клип → 302 на CDN и рендер НЕ ЗАПУСКАЕТСЯ ВООБЩЕ; изменённая
   геометрия → честный полный рендер (никогда не подсовываем чужие пиксели).

2. Сетевой сбой к Supabase (``httpcore.ConnectTimeout: SSL handshake timed out`` под нагрузкой) не
   ловился нигде → жёсткий 500. Пиннит: 503 + Retry-After (transient), а не 500.
"""

from __future__ import annotations

import time

import httpx
import jwt
from fastapi.testclient import TestClient

from app import db
from app.config import get_settings
from app.editor import store
from app.models import CaptionStyle, CaptionTrack, ClipEdit, SourceInterval

_SECRET = "test-hs256-secret-at-least-32-bytes-long!!"
_MEDIA_SECRET = "media-ticket-signing-secret-32-bytes!!"

SEG_START, SEG_END = 24.325, 79.655
_CDN_URL = "https://cdn.quip.ink/job_x/clip_01.mp4"


def _bearer(sub: str) -> dict[str, str]:
    tok = jwt.encode(
        {"sub": sub, "aud": "authenticated", "exp": int(time.time()) + 3600},
        _SECRET,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {tok}"}


def _setup(monkeypatch, tmp_path):
    """Auth ON (HS256, без сети), cloud OFF (SQLite), изолированные DB + DATA_ROOT."""
    monkeypatch.setenv("SUPABASE_URL", "https://ref.supabase.co")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", _SECRET)
    monkeypatch.setenv("MEDIA_TOKEN_SECRET", _MEDIA_SECRET)
    monkeypatch.delenv("BILLING_ENABLED", raising=False)
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(store, "DATA_ROOT", tmp_path / "data")
    import app.main as main

    monkeypatch.setattr(main, "DATA_ROOT", tmp_path / "data")
    db.init_db()
    sfx = str(int(time.time() * 1_000_000))
    return TestClient(main.app), f"user_{sfx}", sfx


def _edit(*, intervals: list[SourceInterval] | None = None) -> ClipEdit:
    return ClipEdit(
        id="clip_01",
        version=1,
        source_intervals=intervals or [SourceInterval(source_start=SEG_START, source_end=SEG_END)],
        captions=CaptionTrack(style=CaptionStyle()),
    )


def _wire_clean_export(
    monkeypatch,
    main,
    edit: ClipEdit,
    *,
    user_id: str | None = None,
    baked_exists: bool = True,
    baked_policy: object = "match",
) -> dict[str, bool]:
    """Подменяем I/O вокруг эндпоинта; возвращаем флаг «рендер звался».

    ``baked_exists``  — есть ли ВООБЩЕ объект ``clips/<id>.mp4`` (клип с упавшим рендером его НЕ
                        имеет: ``failed_clip_result`` → пустой ``video_url``, но строка клипа есть).
    ``baked_policy``  — политика, ПОД КОТОРОЙ клип был запечён (``job_artifacts.baked_policy``):
                        "match" = ровно текущая политика владельца; None = не записана (все старые
                        джобы); dict = произвольная (напр. тариф сменился после выпечки).
    """
    called = {"render": False}

    def _fake_render(*_a: object, **_k: object) -> None:
        called["render"] = True

    recorded = baked_policy
    if baked_policy == "match":
        recorded = main.render_policy_fingerprint(main.resolve_clip_render_policy(user_id))
    monkeypatch.setattr(main.store, "ensure_edit", lambda _j, _c: edit)
    monkeypatch.setattr(main, "segment_bounds", lambda _j, _c: (SEG_START, SEG_END))
    monkeypatch.setattr(main.storage, "clip_read_url", lambda _j, _c, **_k: _CDN_URL)
    monkeypatch.setattr(main, "render_edit_to_file", _fake_render)
    monkeypatch.setattr(main.db, "clip_has_baked_video", lambda _j, _c: baked_exists)
    monkeypatch.setattr(main.db, "get_job_artifact", lambda _j, _k: recorded)
    return called


# ─────────────────── 1. /export/clean.mp4 → 302 на CDN (api не гоняет байты) ───────────────────


def test_clean_export_redirects_to_cdn_and_never_renders(monkeypatch, tmp_path) -> None:
    client, user, sfx = _setup(monkeypatch, tmp_path)
    import app.main as main

    job = f"job_{sfx}_ok"
    db.insert_job(job, "upload", "x", user_id=user)
    called = _wire_clean_export(monkeypatch, main, _edit(), user_id=user)

    r = client.get(
        f"/jobs/{job}/clips/clip_01/export/clean.mp4",
        headers=_bearer(user),
        follow_redirects=False,
    )

    assert r.status_code == 302
    assert r.headers["location"] == _CDN_URL
    # СУТЬ ФИКСА: api-контейнер не скачал source и не запустил CV/энкод → OOM невозможен.
    assert called["render"] is False


def test_clean_export_falls_back_to_render_when_trimmed(monkeypatch, tmp_path) -> None:
    client, user, sfx = _setup(monkeypatch, tmp_path)
    import app.main as main

    job = f"job_{sfx}_trim"
    db.insert_job(job, "upload", "x", user_id=user)
    trimmed = _edit(intervals=[SourceInterval(source_start=SEG_START + 5.0, source_end=SEG_END)])
    called = _wire_clean_export(monkeypatch, main, trimmed, user_id=user)

    r = client.get(
        f"/jobs/{job}/clips/clip_01/export/clean.mp4",
        headers=_bearer(user),
        follow_redirects=False,
    )

    # Геометрия изменена → baked-пиксели НЕ те → редирект молча отдал бы НЕ ТО видео.
    assert r.status_code != 302
    assert called["render"] is True


# ── 1b. Редирект ТОЛЬКО когда объект РЕАЛЬНО есть и политика та же (ревью 2026-07-14) ──


def test_clean_export_does_not_redirect_when_baked_clip_missing(monkeypatch, tmp_path) -> None:
    """Клип с УПАВШИМ рендером (пустой ``video_url``) или ещё рендерящийся extracted-клип: объекта
    в R2 НЕТ. ``storage.clip_read_url`` просто СКЛЕИВАЕТ строку (никакой проверки существования) →
    302 туда = 404 в браузере и НИ ОДНОЙ ошибки на нашей стороне (битая скачка «молча»)."""
    client, user, sfx = _setup(monkeypatch, tmp_path)
    import app.main as main

    job = f"job_{sfx}_nobake"
    db.insert_job(job, "upload", "x", user_id=user)
    called = _wire_clean_export(monkeypatch, main, _edit(), user_id=user, baked_exists=False)

    r = client.get(
        f"/jobs/{job}/clips/clip_01/export/clean.mp4",
        headers=_bearer(user),
        follow_redirects=False,
    )

    assert r.status_code != 302
    assert "location" not in r.headers
    assert called["render"] is True  # честный рендер вместо ссылки в никуда


def test_clean_export_does_not_redirect_when_render_policy_changed(monkeypatch, tmp_path) -> None:
    """ДЕНЬГИ. baked пекли под тарифом владельца НА ТОТ МОМЕНТ (watermark/crf/preset), а полный
    рендер резолвит тариф НА МОМЕНТ ЗАПРОСА. Тариф сменился → 302 молча отдаёт НЕ ТОТ файл:
    Free→Pro скачивает клип С ВОТЕРМАРКОЙ (заплатил — и всё равно с логотипом), Pro→Free — БЕЗ
    вотермарки (обход монетизации)."""
    client, user, sfx = _setup(monkeypatch, tmp_path)
    import app.main as main

    job = f"job_{sfx}_plan"
    db.insert_job(job, "upload", "x", user_id=user)
    current = main.render_policy_fingerprint(main.resolve_clip_render_policy(user))
    stale = {**current, "watermark": not current["watermark"]}  # тариф сменился после выпечки
    called = _wire_clean_export(monkeypatch, main, _edit(), user_id=user, baked_policy=stale)

    r = client.get(
        f"/jobs/{job}/clips/clip_01/export/clean.mp4",
        headers=_bearer(user),
        follow_redirects=False,
    )

    assert r.status_code != 302
    assert "location" not in r.headers
    assert called["render"] is True


def test_clean_export_does_not_redirect_when_policy_unknown(monkeypatch, tmp_path) -> None:
    """Политика НЕ записана (все джобы до миграции 0019) → fail-safe: полный рендер, НЕ редирект.
    Худший случай — сегодняшняя медленная скачка; НИКОГДА — чужой файл."""
    client, user, sfx = _setup(monkeypatch, tmp_path)
    import app.main as main

    job = f"job_{sfx}_nopolicy"
    db.insert_job(job, "upload", "x", user_id=user)
    called = _wire_clean_export(monkeypatch, main, _edit(), user_id=user, baked_policy=None)

    r = client.get(
        f"/jobs/{job}/clips/clip_01/export/clean.mp4",
        headers=_bearer(user),
        follow_redirects=False,
    )

    assert r.status_code != 302
    assert called["render"] is True


def test_clip_has_baked_video_reads_the_clip_row(monkeypatch, tmp_path) -> None:
    """Дешёвый резолв «объект есть?» — из строки клипа (НЕ HEAD-запрос в R2 на горячем пути)."""
    _client, user, sfx = _setup(monkeypatch, tmp_path)
    job = f"job_{sfx}_row"
    db.insert_job(job, "upload", "x", user_id=user)
    db.append_pending_clip(job, {"id": "clip_01", "start": 0.0, "end": 3.0, "video_url": ""})

    assert db.clip_has_baked_video(job, "clip_01") is False  # рендер упал/не дошёл → пусто
    assert db.clip_has_baked_video(job, "clip_09") is False  # клипа нет вовсе

    db.set_clip_ready(job, 1, "r2://job/clip_01.mp4")

    assert db.clip_has_baked_video(job, "clip_01") is True


# ─────────────────── 2. Сбой транспорта к Supabase → 503, а не 500 ───────────────────


def test_upstream_connect_timeout_becomes_503_not_500(monkeypatch, tmp_path) -> None:
    client, user, sfx = _setup(monkeypatch, tmp_path)
    import app.main as main

    job = f"job_{sfx}_net"
    db.insert_job(job, "upload", "x", user_id=user)

    def _boom(_job_id: str) -> dict[str, object]:
        raise httpx.ConnectTimeout("SSL handshake timed out")

    monkeypatch.setattr(main.db, "get_job_row", _boom)

    r = client.get(f"/jobs/{job}", headers=_bearer(user))

    assert r.status_code == 503  # было 500 (клиент не ретраит) → теперь transient
    assert r.headers.get("Retry-After") == "2"
