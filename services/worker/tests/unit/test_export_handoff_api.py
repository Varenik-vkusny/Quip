"""Endpoint-смоук editor-handoff экспорта (main.py): EDL / FCPXML / markers.csv / VTT / package.zip.

I/O-склейка + zip живут в main.py (pure-билдеры протестированы в test_interchange/test_timecode).
Здесь проверяем, что эндпоинты собирают артефакты, гейтят как SRT-экспорт (attachment) и отдают
валидные форматы. fps=29.97 → drop-frame (';') сквозь весь пакет.
"""

import io
import json
import xml.etree.ElementTree as ET
import zipfile

from fastapi.testclient import TestClient

from app import db
from app.editor import store
from app.models import Segment, SourceKind, Transcript, Word
from app.pipeline.stage0_import import SourceMeta

_FPS = 30000.0 / 1001.0  # 29.97 → drop-frame


def _client(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(store, "DATA_ROOT", tmp_path / "data")
    db.init_db()
    job = "jobH"
    d = tmp_path / "data" / job
    d.mkdir(parents=True)
    (d / "meta.json").write_text(
        SourceMeta(
            job_id=job,
            source=SourceKind.upload,
            url=None,
            title="My Ep",
            duration=120.0,
            fps=_FPS,
            width=1280,
            height=720,
        ).model_dump_json(),
        encoding="utf-8",
    )
    (d / "segments.json").write_text(
        json.dumps(
            [
                Segment(
                    start=10.0, end=15.0, reason="why one", score=0.8, type="hook", hook="Hook One"
                ).model_dump(),
                Segment(
                    start=40.0, end=44.0, reason="why two", score=0.6, type="strong_quote"
                ).model_dump(),
            ]
        ),
        encoding="utf-8",
    )
    words = [
        Word(text="Hello", start=10.0, end=10.4),
        Word(text="there", start=10.4, end=10.8),
        Word(text="friend", start=41.0, end=41.4),
    ]
    (d / "transcript.json").write_text(
        Transcript(language="en", duration=120.0, words=words).model_dump_json(), encoding="utf-8"
    )
    from app.main import app

    return TestClient(app), job


def _is_attachment(resp) -> bool:
    return "attachment" in (resp.headers.get("content-disposition") or "")


def test_export_edl_endpoint(monkeypatch, tmp_path):
    client, job = _client(monkeypatch, tmp_path)
    r = client.get(f"/jobs/{job}/export.edl")
    assert r.status_code == 200
    assert _is_attachment(r)
    body = r.text
    assert "TITLE: My Ep" in body
    assert "FCM: DROP FRAME" in body  # 29.97
    assert "001" in body and "002" in body  # two clips
    assert ";" in body  # drop-frame timecode


def test_export_fcpxml_endpoint(monkeypatch, tmp_path):
    client, job = _client(monkeypatch, tmp_path)
    r = client.get(f"/jobs/{job}/export.fcpxml")
    assert r.status_code == 200
    assert _is_attachment(r)
    root = ET.fromstring(r.text)
    assert root.tag == "fcpxml"
    assert root.find(".//format").get("frameDuration") == "1001/30000s"
    assert root.find(".//sequence").get("tcFormat") == "DF"
    assert len(root.findall(".//asset-clip")) == 2


def test_export_markers_csv_endpoint(monkeypatch, tmp_path):
    client, job = _client(monkeypatch, tmp_path)
    r = client.get(f"/jobs/{job}/export/markers.csv")
    assert r.status_code == 200
    assert _is_attachment(r)
    lines = r.text.strip().splitlines()
    assert lines[0] == "Timecode In,Name,Comment,Color"
    assert "Hook One" in r.text


def test_export_clip_vtt_endpoint(monkeypatch, tmp_path):
    client, job = _client(monkeypatch, tmp_path)
    r = client.get(f"/jobs/{job}/clips/clip_01/export.vtt")
    assert r.status_code == 200
    assert _is_attachment(r)
    assert r.text.startswith("WEBVTT")
    assert "-->" in r.text and "." in r.text.split("-->")[0][-8:]  # '.' ms separator


def test_export_package_zip_endpoint(monkeypatch, tmp_path):
    client, job = _client(monkeypatch, tmp_path)
    r = client.get(f"/jobs/{job}/export/package.zip")
    assert r.status_code == 200
    assert _is_attachment(r)
    assert r.headers["content-type"].startswith("application/zip")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    assert {
        "manifest.json",
        "timeline.edl",
        "timeline.fcpxml",
        "markers.csv",
        "README.txt",
    } <= names
    assert "captions/clip_01.srt" in names
    assert "captions/clip_01.vtt" in names
    assert "captions/clip_02.vtt" in names
    # manifest is a valid, catch-all JSON with the exact fps + drop-frame flag
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["source"]["drop_frame"] is True
    assert abs(manifest["source"]["fps"] - _FPS) < 1e-9
    assert len(manifest["clips"]) == 2
    assert manifest["clips"][0]["hook"] == "Hook One"


def test_export_package_zip_include_clips_flag_no_render(monkeypatch, tmp_path):
    # include_clips=true but no rendered mp4 on disk → endpoint still succeeds, just omits clips/
    # (it never re-renders → no money/pipeline spent).
    client, job = _client(monkeypatch, tmp_path)
    r = client.get(f"/jobs/{job}/export/package.zip?include_clips=true")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert not any(n.startswith("clips/") for n in zf.namelist())
