"""Тесты POST /jobs/check-duplicate: per-user дедуп исходника (переиспользуем R2-source).

Все внешние сиды (db-поиск/persist, storage.copy_source, dispatch/фон-таск) замоканы — тест
проверяет ТОЛЬКО wiring эндпоинта (гейты → валидация отпечатка → поиск → R2-копия → спавн), без
реального R2/Supabase/пайплайна. auth off (dev), billing off → user_id из X-User-Id.
"""

from __future__ import annotations

import types

from fastapi.testclient import TestClient

_VALID_FP = "1048576:" + "a" * 64


def _r2_env(monkeypatch, backend: str = "r2"):
    monkeypatch.delenv("SUPABASE_URL", raising=False)  # auth off → X-User-Id используется
    monkeypatch.delenv("BILLING_ENABLED", raising=False)  # quota-гейт инертен
    import app.main as main

    monkeypatch.setattr(
        main, "get_settings", lambda: types.SimpleNamespace(storage_backend=backend)
    )
    return main


def test_check_duplicate_no_match_returns_false(monkeypatch) -> None:
    main = _r2_env(monkeypatch)
    monkeypatch.setattr(main.db, "find_duplicate_source", lambda uid, fp: None)

    def _no_copy(*a, **k):  # не должен быть достигнут (нет матча → нечего копировать)
        raise AssertionError("copy_source reached without a match")

    monkeypatch.setattr(main.storage, "copy_source", _no_copy)

    client = TestClient(main.app)
    r = client.post(
        "/jobs/check-duplicate",
        json={"filename": "v.mp4", "source_fingerprint": _VALID_FP},
        headers={"X-User-Id": "user_1"},
    )
    assert r.status_code == 200
    assert r.json() == {"duplicate": False}


def test_check_duplicate_match_copies_source_and_spawns(monkeypatch) -> None:
    main = _r2_env(monkeypatch)
    monkeypatch.setattr(main.db, "find_duplicate_source", lambda uid, fp: "job_original")
    copied: dict = {}
    monkeypatch.setattr(main.storage, "copy_source", lambda frm, to: copied.update(frm=frm, to=to))
    inserted: dict = {}
    monkeypatch.setattr(
        main.db,
        "insert_job",
        lambda jid, st, fn, user_id=None, remove_pauses=False: inserted.update(
            jid=jid, st=st, fn=fn, user_id=user_id, remove_pauses=remove_pauses
        ),
    )
    fp_set: dict = {}
    monkeypatch.setattr(
        main.db, "set_source_fingerprint", lambda jid, fp: fp_set.update(jid=jid, fp=fp)
    )
    spawned: list = []
    monkeypatch.setattr(main, "run_upload_job_from_r2", lambda *a: spawned.append(a))

    client = TestClient(main.app)
    r = client.post(
        "/jobs/check-duplicate",
        json={"filename": "v.mp4", "max_clips": 4, "source_fingerprint": _VALID_FP},
        headers={"X-User-Id": "user_1"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["duplicate"] is True
    new_id = body["id"]
    assert body["status"] == "queued"
    # source переиспользован: server-side copy из найденной джобы в НОВУЮ.
    assert copied == {"frm": "job_original", "to": new_id}
    # новая джоба создана на текущего юзера + отпечаток записан (для будущего дедупа).
    assert inserted["jid"] == new_id
    assert inserted["st"] == "upload"
    assert inserted["user_id"] == "user_1"
    assert fp_set == {"jid": new_id, "fp": _VALID_FP}
    # пайплайн запущен (bg.add_task run_upload_job_from_r2) на НОВУЮ джобу, max_clips проброшен.
    assert spawned and spawned[0][0] == new_id
    assert spawned[0][1] == "v.mp4" and spawned[0][2] == 4


def test_check_duplicate_source_gone_is_graceful_not_500(monkeypatch) -> None:
    # Матч есть, но R2-source уже вымело ретеншном → SourceNotFoundError → {duplicate: false},
    # НЕ 500, и новую джобу НЕ создаём (падаем в обычную загрузку клиентом).
    main = _r2_env(monkeypatch)
    monkeypatch.setattr(main.db, "find_duplicate_source", lambda uid, fp: "job_original")

    def _gone(frm, to):
        raise main.storage.SourceNotFoundError(frm)

    monkeypatch.setattr(main.storage, "copy_source", _gone)

    def _no_insert(*a, **k):
        raise AssertionError("insert_job must not run when source is gone")

    monkeypatch.setattr(main.db, "insert_job", _no_insert)

    client = TestClient(main.app)
    r = client.post(
        "/jobs/check-duplicate",
        json={"filename": "v.mp4", "source_fingerprint": _VALID_FP},
        headers={"X-User-Id": "user_1"},
    )
    assert r.status_code == 200
    assert r.json() == {"duplicate": False}


def test_check_duplicate_malformed_fingerprint_400(monkeypatch) -> None:
    main = _r2_env(monkeypatch)

    def _no_find(*a, **k):
        raise AssertionError("find_duplicate_source reached with malformed fingerprint")

    monkeypatch.setattr(main.db, "find_duplicate_source", _no_find)

    client = TestClient(main.app)
    r = client.post(
        "/jobs/check-duplicate",
        json={"filename": "v.mp4", "source_fingerprint": "not-a-real-fingerprint"},
        headers={"X-User-Id": "user_1"},
    )
    assert r.status_code == 400


def test_check_duplicate_local_backend_returns_false(monkeypatch) -> None:
    # storage_backend != r2 → нет server-side копии → {duplicate: false} ДО любого поиска.
    main = _r2_env(monkeypatch, backend="local")

    def _no_find(*a, **k):
        raise AssertionError("find_duplicate_source reached on local backend")

    monkeypatch.setattr(main.db, "find_duplicate_source", _no_find)

    client = TestClient(main.app)
    r = client.post(
        "/jobs/check-duplicate",
        json={"filename": "v.mp4", "source_fingerprint": _VALID_FP},
        headers={"X-User-Id": "user_1"},
    )
    assert r.status_code == 200
    assert r.json() == {"duplicate": False}
