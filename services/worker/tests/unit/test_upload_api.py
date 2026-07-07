"""Тест endpoint POST /jobs/upload: стриминг файла на диск + постановка фон-таска.

Фон-таск (run_upload_job) замокан — тест проверяет ТОЛЬКО wiring эндпоинта
(приём multipart, запись файла чанками, создание queued-задачи), без Deepgram/Gemini/ffmpeg.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app import db


def test_upload_streams_file_and_enqueues_job(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "jobs.db")
    db.init_db()

    import app.main as main

    monkeypatch.setattr(main, "DATA_ROOT", tmp_path / "data")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)

    captured: dict[str, object] = {}

    def fake_task(
        job_id: str,
        upload_path: str,
        title: str,
        max_clips: int | None,
        user_id: str | None = None,
        language: str | None = None,
    ) -> None:
        captured.update(job_id=job_id, upload_path=upload_path, title=title, max_clips=max_clips)

    monkeypatch.setattr(main, "run_upload_job", fake_task)

    client = TestClient(main.app)
    content = b"\x00\x01fake video payload bytes" * 1000  # >1 чанк не нужен, но не пусто
    r = client.post(
        "/jobs/upload",
        files={"file": ("My Clip.mov", content, "video/quicktime")},
        data={"max_clips": "3"},
    )

    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    job_id = body["id"]

    # файл записан на диск под нормализованным именем upload.<ext>
    written = tmp_path / "data" / job_id / "upload.mov"
    assert written.exists()
    assert written.read_bytes() == content

    # фон-таск поставлен с верными аргументами
    assert captured["job_id"] == job_id
    assert captured["title"] == "My Clip.mov"
    assert captured["max_clips"] == 3
    assert str(captured["upload_path"]).endswith("upload.mov")

    # задача создана в БД
    assert db.get_job(job_id) is not None


def test_upload_without_max_clips_defaults_none(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "jobs.db")
    db.init_db()

    import app.main as main

    monkeypatch.setattr(main, "DATA_ROOT", tmp_path / "data")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)

    captured: dict[str, object] = {}
    monkeypatch.setattr(main, "run_upload_job", lambda *a: captured.update(max_clips=a[3]))

    client = TestClient(main.app)
    r = client.post("/jobs/upload", files={"file": ("v.mp4", b"abc", "video/mp4")})

    assert r.status_code == 202
    assert captured["max_clips"] is None


def test_upload_spawns_modal_job_when_enabled(monkeypatch, tmp_path: Path) -> None:
    # На Modal (MODAL_SPAWN=1): исходник заливается в R2 + spawn долгоживущей upload_job,
    # БЕЗ in-process BackgroundTask (web scale-to-zero убил бы фон-таск на полпути).
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "jobs.db")
    db.init_db()

    import app.main as main

    monkeypatch.setattr(main, "DATA_ROOT", tmp_path / "data")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)

    bg_called: list = []
    uploaded: dict = {}
    spawned: dict = {}
    monkeypatch.setattr(main, "run_upload_job", lambda *a: bg_called.append(a))
    monkeypatch.setattr(main.dispatch, "modal_spawn_enabled", lambda: True)
    monkeypatch.setattr(
        main.storage, "upload_source", lambda path, job_id: uploaded.update(job_id=job_id)
    )
    monkeypatch.setattr(main.dispatch, "spawn", lambda fn, *a: spawned.update(fn=fn, args=a))

    client = TestClient(main.app)
    r = client.post(
        "/jobs/upload",
        files={"file": ("clip.mp4", b"xyz", "video/mp4")},
        data={"max_clips": "4"},
    )

    assert r.status_code == 202
    job_id = r.json()["id"]
    assert uploaded["job_id"] == job_id  # исходник застейджен в R2
    assert spawned["fn"] == "upload_job"  # спавним именно upload_job
    assert spawned["args"][0] == job_id
    assert spawned["args"][1] == "clip.mp4"
    assert spawned["args"][2] == 4  # max_clips проброшен
    assert bg_called == []  # BackgroundTask НЕ использован на Modal


def test_upload_spawn_failure_marks_job_failed(monkeypatch, tmp_path: Path) -> None:
    # Сбой диспатча на Modal → джоб помечен failed + HTTP 500 (не вечная очередь, правило №8).
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "jobs.db")
    db.init_db()

    import app.main as main

    monkeypatch.setattr(main, "DATA_ROOT", tmp_path / "data")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main, "run_upload_job", lambda *a: None)
    monkeypatch.setattr(main.dispatch, "modal_spawn_enabled", lambda: True)
    monkeypatch.setattr(main.storage, "upload_source", lambda *a, **k: None)

    def boom(*a, **k):
        raise RuntimeError("modal down")

    monkeypatch.setattr(main.dispatch, "spawn", boom)
    failed: dict = {}
    monkeypatch.setattr(main.db, "set_failed", lambda jid, err: failed.update(job_id=jid, err=err))

    client = TestClient(main.app)
    r = client.post("/jobs/upload", files={"file": ("c.mp4", b"z", "video/mp4")})

    assert r.status_code == 500
    assert failed and "modal down" in failed["err"]


# ─────────────────── B2: POST /jobs/{id}/upload-parts (resume + URL renewal) ───────────────────


def test_upload_parts_lists_completed_and_mints_requested(monkeypatch) -> None:
    # ListParts даёт уже-залитые части (completed); parts = свежие presigned URL для ЗАПРОШЕННЫХ.
    monkeypatch.delenv("SUPABASE_URL", raising=False)  # auth off (dev)
    import app.main as main

    monkeypatch.setattr(
        main.storage,
        "list_parts",
        lambda job_id, upload_id: [
            {"part_number": 1, "etag": "e1", "size": 100},
            {"part_number": 4, "etag": "e4", "size": 50},
        ],
    )
    monkeypatch.setattr(
        main.storage,
        "presigned_upload_part_url",
        lambda job_id, upload_id, n: f"https://r2/{job_id}/{upload_id}/{n}",
    )

    client = TestClient(main.app)
    r = client.post("/jobs/job_x/upload-parts", json={"upload_id": "up1", "part_numbers": [2, 3]})
    assert r.status_code == 200
    body = r.json()
    assert body["completed"] == [
        {"part_number": 1, "etag": "e1"},
        {"part_number": 4, "etag": "e4"},
    ]
    assert [p["part_number"] for p in body["parts"]] == [2, 3]
    assert body["parts"][0]["url"].endswith("/2")
    assert body["parts"][1]["url"].endswith("/3")


def test_upload_parts_resume_derives_remaining_from_size(monkeypatch) -> None:
    # part_numbers опущен (resume) → mint'им URL для ВСЕХ частей 1..plan_part_count(size), КРОМЕ
    # уже завершённых (из list_parts). part_size = plan_part_size(size), эхом клиенту.
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    import app.main as main
    from app import storage

    monkeypatch.setattr(
        main.storage,
        "list_parts",
        lambda job_id, upload_id: [
            {"part_number": 1, "etag": "e1", "size": 100},
            {"part_number": 2, "etag": "e2", "size": 100},
        ],
    )
    monkeypatch.setattr(
        main.storage, "presigned_upload_part_url", lambda job_id, upload_id, n: f"url-{n}"
    )

    size = 3 * storage.MULTIPART_MIN_PART_SIZE + 1  # → part_size 16 МБ, 4 части
    client = TestClient(main.app)
    r = client.post("/jobs/job_y/upload-parts", json={"upload_id": "up2", "size": size})
    assert r.status_code == 200
    body = r.json()
    ps = storage.plan_part_size(size)
    n = storage.plan_part_count(size, ps)
    assert body["part_size"] == ps
    assert [p["part_number"] for p in body["parts"]] == [
        i for i in range(1, n + 1) if i not in (1, 2)
    ]


def test_upload_parts_requires_auth(monkeypatch) -> None:
    # auth включён (SUPABASE_URL) + нет токена → 401 ДО обращения к R2 (list_parts не зовём).
    monkeypatch.setenv("SUPABASE_URL", "https://ref.supabase.co")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-hs256-secret-at-least-32-bytes-long!!")
    import app.main as main

    def _boom(*a, **k):  # list_parts не должен быть достигнут (иначе тест-фейл при auth-багге)
        raise AssertionError("list_parts reached without auth")

    monkeypatch.setattr(main.storage, "list_parts", _boom)

    client = TestClient(main.app)
    r = client.post("/jobs/job_z/upload-parts", json={"upload_id": "u"})
    assert r.status_code == 401


# ─────────────────── B3: server-side upload size cap (defense-in-depth) ───────────────────


def test_upload_url_rejects_oversize(monkeypatch) -> None:
    monkeypatch.delenv("SUPABASE_URL", raising=False)  # auth off
    monkeypatch.delenv("BILLING_ENABLED", raising=False)  # quota off
    import app.main as main

    client = TestClient(main.app)
    r = client.post(
        "/jobs/upload-url", json={"filename": "big.mp4", "size": main.MAX_UPLOAD_BYTES + 1}
    )
    assert r.status_code == 413
    assert "GB" in r.json()["detail"]


def test_upload_url_accepts_within_cap(monkeypatch) -> None:
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("BILLING_ENABLED", raising=False)
    import types

    import app.main as main

    # local backend → {"local": true} (не трогаем R2); гарантируем local, не полагаясь на кэш env.
    monkeypatch.setattr(
        main, "get_settings", lambda: types.SimpleNamespace(storage_backend="local")
    )

    client = TestClient(main.app)
    r = client.post("/jobs/upload-url", json={"filename": "ok.mp4", "size": 1024})
    assert r.status_code == 200
    assert r.json().get("local") is True
