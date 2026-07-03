"""IDOR ownership gate on by-job / by-clip / media endpoints (security-critical).

Auth ON via HS256 (SUPABASE_URL + SUPABASE_JWT_SECRET, no network) — owner ``A`` and attacker
``B`` are both real, validly-signed JWTs. Cloud OFF (STORAGE_BACKEND unset) → SQLite; the DB is
isolated per test (``_DB_PATH`` monkeypatched) so ``GET /jobs`` returns exactly what we insert.
Ids are uniquely suffixed as well (belt-and-suspenders vs the shared persistent tmp/jobs.db).
"""

from __future__ import annotations

import time

import jwt
from fastapi.testclient import TestClient

from app import db
from app.config import get_settings
from app.editor import store

_SECRET = "test-hs256-secret-at-least-32-bytes-long!!"
_MEDIA_SECRET = "media-ticket-signing-secret-32-bytes!!"


def _token(sub: str) -> str:
    return jwt.encode(
        {"sub": sub, "aud": "authenticated", "exp": int(time.time()) + 3600},
        _SECRET,
        algorithm="HS256",
    )


def _bearer(sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(sub)}"}


def _setup(monkeypatch, tmp_path):
    """Auth ON, cloud OFF (SQLite), isolated DB + DATA_ROOT. Returns (client, A, B, suffix)."""
    monkeypatch.setenv("SUPABASE_URL", "https://ref.supabase.co")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", _SECRET)
    monkeypatch.setenv("MEDIA_TOKEN_SECRET", _MEDIA_SECRET)  # explicit media-ticket signing key
    monkeypatch.delenv("BILLING_ENABLED", raising=False)
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)  # keep cloud_state disabled → SQLite
    get_settings.cache_clear()  # storage_backend must resolve to local (deterministic media path)
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(store, "DATA_ROOT", tmp_path / "data")
    import app.main as main

    monkeypatch.setattr(main, "DATA_ROOT", tmp_path / "data")
    db.init_db()
    suffix = str(int(time.time() * 1000))
    return TestClient(main.app), f"userA_{suffix}", f"userB_{suffix}", suffix


# ─────────────────────── 1. JSON by-job endpoint (header auth) ───────────────────────


def test_get_job_owner_only(monkeypatch, tmp_path):
    client, a, b, sfx = _setup(monkeypatch, tmp_path)
    job = f"job_{sfx}_a"
    db.insert_job(job, "youtube", "https://ex/a", user_id=a)
    assert client.get(f"/jobs/{job}", headers=_bearer(a)).status_code == 200
    assert client.get(f"/jobs/{job}", headers=_bearer(b)).status_code == 403  # IDOR blocked
    assert client.get(f"/jobs/{job}").status_code == 401  # no token, auth on


# ─────────────────── 2. media endpoint (header OR scoped ?mt= ticket) ───────────────────


def _make_job_with_source(tmp_path, job: str, user: str) -> None:
    db.insert_job(job, "upload", "clip.mp4", user_id=user)
    d = tmp_path / "data" / job
    d.mkdir(parents=True, exist_ok=True)
    (d / "source.mp4").write_bytes(b"\x00\x00mp4")  # local backend → owner serves 200


def test_media_ticket_scoped_owner_only(monkeypatch, tmp_path):
    client, a, b, sfx = _setup(monkeypatch, tmp_path)
    job, other = f"job_{sfx}_a", f"job_{sfx}_a2"
    _make_job_with_source(tmp_path, job, a)
    _make_job_with_source(tmp_path, other, a)

    # (c) attacker B cannot mint a media-ticket for A's job
    assert client.get(f"/jobs/{job}/media-token", headers=_bearer(b)).status_code == 403

    # (a) owner A mints a ticket → serves the media
    tok = client.get(f"/jobs/{job}/media-token", headers=_bearer(a))
    assert tok.status_code == 200
    body = tok.json()
    assert body["expires_in"] == 300 and isinstance(body["token"], str)
    mt = body["token"]
    served = client.get(f"/jobs/{job}/source.mp4", params={"mt": mt}, follow_redirects=False)
    assert served.status_code in (200, 302)
    assert served.headers.get("referrer-policy") == "no-referrer"

    # (b) A's ticket is SCOPED to `job` — useless on a DIFFERENT job (scope check)
    r_scope = client.get(f"/jobs/{other}/source.mp4", params={"mt": mt}, follow_redirects=False)
    assert r_scope.status_code == 403


def test_media_ticket_garbage_and_expired_rejected(monkeypatch, tmp_path):
    import app.media_token as media_token

    client, a, _b, sfx = _setup(monkeypatch, tmp_path)
    job = f"job_{sfx}_a"
    _make_job_with_source(tmp_path, job, a)

    # (d) garbage ticket → rejected
    garbage = client.get(
        f"/jobs/{job}/source.mp4", params={"mt": "not.a.ticket"}, follow_redirects=False
    )
    assert garbage.status_code == 403
    # (d) real signature but expired → rejected
    expired = media_token.mint(job, _MEDIA_SECRET.encode(), now=int(time.time()) - 10_000)
    r_exp = client.get(f"/jobs/{job}/source.mp4", params={"mt": expired}, follow_redirects=False)
    assert r_exp.status_code == 403
    # no ticket + no header → 401
    assert client.get(f"/jobs/{job}/source.mp4", follow_redirects=False).status_code == 401


def test_media_header_auth_still_works_and_raw_jwt_rejected(monkeypatch, tmp_path):
    client, a, b, sfx = _setup(monkeypatch, tmp_path)
    job = f"job_{sfx}_a"
    _make_job_with_source(tmp_path, job, a)
    # owner Authorization header (browser download via fetch with a header) → ok
    r_hdr = client.get(f"/jobs/{job}/source.mp4", headers=_bearer(a), follow_redirects=False)
    assert r_hdr.status_code in (200, 302)
    # attacker header → 403
    assert client.get(f"/jobs/{job}/source.mp4", headers=_bearer(b)).status_code == 403
    # raw session JWT via ?token= is NO LONGER accepted (credential-in-URL mechanism removed)
    raw = client.get(f"/jobs/{job}/source.mp4", params={"token": _token(a)}, follow_redirects=False)
    assert raw.status_code == 401


def test_media_token_fails_loud_without_signing_secret(monkeypatch, tmp_path):
    client, a, _b, sfx = _setup(monkeypatch, tmp_path)
    # No signing key available anywhere + auth ON → fail loud, NEVER issue an unsigned ticket.
    monkeypatch.delenv("MEDIA_TOKEN_SECRET", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("R2_SECRET_ACCESS_KEY", raising=False)
    job = f"job_{sfx}_a"
    db.insert_job(job, "upload", "clip.mp4", user_id=a)
    assert client.get(f"/jobs/{job}/media-token", headers=_bearer(a)).status_code == 500


# ─────────────────────── 3. representative write (by-clip) ───────────────────────


def test_write_endpoint_owner_only(monkeypatch, tmp_path):
    client, a, b, sfx = _setup(monkeypatch, tmp_path)
    job = f"job_{sfx}_a"
    db.insert_job(job, "youtube", "https://ex/a", user_id=a)
    r = client.post(
        f"/jobs/{job}/clips/clip_01/edit/set-interval",
        json={"version": 1, "source_start": 1.0, "source_end": 5.0},
        headers=_bearer(b),
    )
    assert r.status_code == 403  # gate fires before touching the edit


# ─────────────────────── 4. GET /jobs list scoped to caller ───────────────────────


def test_jobs_list_scoped_to_owner(monkeypatch, tmp_path):
    client, a, b, sfx = _setup(monkeypatch, tmp_path)
    ja1, ja2, jb = f"job_{sfx}_a1", f"job_{sfx}_a2", f"job_{sfx}_b"
    db.insert_job(ja1, "youtube", "https://ex/a1", user_id=a)
    db.insert_job(ja2, "upload", "vid.mp4", user_id=a)
    db.insert_job(jb, "youtube", "https://ex/b", user_id=b)

    ra = client.get("/jobs", headers=_bearer(a))
    assert ra.status_code == 200
    ids_a = {j["id"] for j in ra.json()["jobs"]}
    assert {ja1, ja2} <= ids_a
    assert jb not in ids_a  # attacker's job never leaks into A's list

    rb = client.get("/jobs", headers=_bearer(b))
    ids_b = {j["id"] for j in rb.json()["jobs"]}
    assert jb in ids_b
    assert ja1 not in ids_b and ja2 not in ids_b

    # response shape (frontend contract)
    item = next(j for j in ra.json()["jobs"] if j["id"] == ja2)
    assert item["source_kind"] == "upload"
    assert item["label"] == "vid.mp4"
    assert item["status"] == "queued"
    assert item["nclips"] == 0
    assert "created_at" in item

    assert client.get("/jobs").status_code == 401  # no token, auth on


# ─────────────────────── 5. legacy None-owner job → open ───────────────────────


def test_legacy_none_owner_job_allows_any_caller(monkeypatch, tmp_path):
    client, a, b, sfx = _setup(monkeypatch, tmp_path)
    job = f"job_{sfx}_legacy"
    db.insert_job(job, "youtube", "https://ex/legacy")  # user_id defaults to None
    # Intentional dual-mode/legacy bypass: a job owned by nobody is not IDOR-protected.
    assert client.get(f"/jobs/{job}", headers=_bearer(b)).status_code == 200
