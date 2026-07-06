"""Тесты pure-построителей форматов editor-handoff (app.editor.interchange).

Строковые билдеры EDL/CSV/JSON/VTT/FCPXML из нормализованного intermediate (HandoffClip/
HandoffMarker) — БЕЗ I/O. Проверяем СТРУКТУРУ вывода (то, что pro-NLE реально парсит), а не
пиксели. DF/NDF-таймкоды приходят из timecode.py (протестирован отдельно).
"""

import json
import xml.etree.ElementTree as ET

from app.editor.interchange import (
    HandoffClip,
    HandoffInterval,
    HandoffMarker,
    build_edl,
    build_fcpxml,
    build_manifest_json,
    build_markers_csv,
    build_vtt,
)
from app.editor.timemap import ClipTimeMap
from app.models import CaptionReply, CaptionStyle, CaptionTrack, SourceInterval, Word

_2997 = 30000.0 / 1001.0

_META = {
    "job_id": "job_test",
    "title": "My Podcast Ep 1",
    "source": "upload",
    "fps": _2997,
    "width": 1280,
    "height": 720,
    "duration": 5192.82,
}


def _clip(name="clip_01", a=111.954994, b=138.105, hook="A surprising habit") -> HandoffClip:
    return HandoffClip(
        name=name,
        source_in_s=a,
        source_out_s=b,
        reason="Reveals a surprising childhood habit.",
        hook=hook,
        why_works="Contrasts with public persona.",
        score=0.85,
        type="complete_thought",
        tone="surprising",
        key_quote="I read history books",
        intervals=[HandoffInterval(source_in_s=a, source_out_s=b)],
    )


def _marker(t=111.95, name="clip_01", kind="clip") -> HandoffMarker:
    return HandoffMarker(t_s=t, name=name, note="A surprising habit", color="Blue", kind=kind)


# ─────────────────────────── build_edl (CMX3600) ───────────────────────────


def test_edl_header_and_event_structure_df():
    edl = build_edl([_clip()], _2997, "My Podcast Ep 1")
    assert "TITLE: My Podcast Ep 1" in edl
    assert "FCM: DROP FRAME" in edl  # 29.97 → drop-frame
    assert "001" in edl  # первый event, 3-значный
    assert "* FROM CLIP NAME: A surprising habit" in edl  # hook как имя клипа
    assert "Reveals a surprising childhood habit." in edl  # reason как комментарий
    # record-таймлайн стартует с нуля (drop-frame ';')
    assert "00:00:00;00" in edl


def test_edl_ndf_flag_and_record_accumulates_across_events():
    a1, b1 = 10.0, 15.0  # 5с
    a2, b2 = 40.0, 44.0  # 4с
    clip = HandoffClip(
        name="clip_01",
        source_in_s=a1,
        source_out_s=b2,
        reason="r",
        hook="h",
        why_works="w",
        score=0.5,
        type="hook",
        tone=None,
        key_quote=None,
        intervals=[
            HandoffInterval(source_in_s=a1, source_out_s=b1),
            HandoffInterval(source_in_s=a2, source_out_s=b2),
        ],
    )
    edl = build_edl([clip], 25.0, "T")
    assert "FCM: NON-DROP FRAME" in edl
    assert "001" in edl and "002" in edl  # два интервала → два event'а
    # первый record-out = 5с @25 = 00:00:05:00, он же record-in второго event'а
    assert "00:00:05:00" in edl


# ─────────────────────────── build_markers_csv ───────────────────────────


def test_markers_csv_header_and_row():
    csv_text = build_markers_csv([_marker()], _2997)
    lines = csv_text.strip().splitlines()
    assert lines[0] == "Timecode In,Name,Comment,Color"
    assert "clip_01" in lines[1]
    assert "Blue" in lines[1]
    assert ";" in lines[1]  # DF-таймкод


# ─────────────────────────── build_manifest_json ───────────────────────────


def test_manifest_is_catch_all_with_both_tc_and_seconds():
    data = json.loads(
        build_manifest_json(
            {"id": "job_test"},
            [_clip()],
            [_marker()],
            _META,
            chapters=[{"start": 0.0, "end": 200.0, "title": "Intro", "summary": "opening"}],
        )
    )
    src = data["source"]
    assert abs(src["fps"] - _2997) < 1e-9  # ТОЧНЫЙ fps (не округлён)
    assert src["drop_frame"] is True
    assert src["frame_duration"] == "1001/30000s"
    assert src["width"] == 1280 and src["height"] == 720
    c = data["clips"][0]
    # in/out В ОБОИХ представлениях: секунды И source-таймкод
    assert c["source_in_s"] == 111.954994
    assert ";" in c["source_in_tc"]
    assert c["hook"] == "A surprising habit"
    assert c["why_works"] == "Contrasts with public persona."
    assert c["score"] == 0.85
    assert c["type"] == "complete_thought"
    assert c["tone"] == "surprising"
    assert c["key_quote"] == "I read history books"
    assert data["markers"][0]["name"] == "clip_01"
    assert data["chapters"][0]["title"] == "Intro"


# ─────────────────────────── build_vtt ───────────────────────────


def _cmap() -> ClipTimeMap:
    return ClipTimeMap([SourceInterval(source_start=0.0, source_end=10.0)])


def test_vtt_header_dot_separator_and_natural_case():
    words = [Word(text="Hello", start=0.0, end=0.5), Word(text="world", start=0.5, end=1.0)]
    track = CaptionTrack(
        style=CaptionStyle(uppercase=True),  # видео в CAPS, но VTT — натуральный регистр
        highlight=None,
        replies=[CaptionReply(word_refs=[0, 1])],
    )
    vtt = build_vtt(track, words, _cmap())
    assert vtt.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:01.000" in vtt  # VTT = '.' для мс (не ',')
    assert "Hello world" in vtt
    assert "HELLO" not in vtt


def test_vtt_skips_hidden_and_hole_replies():
    words = [Word(text="a", start=0.0, end=0.5), Word(text="x", start=50.0, end=50.5)]
    track = CaptionTrack(
        style=CaptionStyle(),
        highlight=None,
        replies=[
            CaptionReply(word_refs=[0]),
            CaptionReply(word_refs=[1]),  # 50с вне [0,10) → дырка → пропуск
        ],
    )
    vtt = build_vtt(track, words, _cmap())
    assert "a" in vtt
    assert vtt.count("-->") == 1  # только одна реплика эмитнута


# ─────────────────────────── build_fcpxml ───────────────────────────


def test_fcpxml_is_valid_xml_with_frame_grid_and_markers():
    clips = [_clip()]
    markers = [_marker(t=120.0, name="Intro", kind="chapter")]
    xml = build_fcpxml(clips, markers, _META)
    root = ET.fromstring(xml)  # валидный XML (иначе исключение)
    assert root.tag == "fcpxml"
    fmt = root.find(".//format")
    assert fmt is not None
    assert fmt.get("frameDuration") == "1001/30000s"
    seq = root.find(".//sequence")
    assert seq.get("tcFormat") == "DF"  # 29.97 → drop-frame
    ac = root.find(".//asset-clip")
    assert ac is not None
    assert ac.get("name") == "clip_01"
    # hook → <marker>, глава → <chapter-marker>
    assert root.find(".//marker") is not None
    assert root.find(".//chapter-marker") is not None
    hook_marker = root.find(".//marker")
    assert hook_marker.get("value") == "A surprising habit"


def test_fcpxml_ndf_integer_fps():
    meta = dict(_META, fps=25.0)
    xml = build_fcpxml([_clip()], [], meta)
    root = ET.fromstring(xml)
    assert root.find(".//format").get("frameDuration") == "1/25s"
    assert root.find(".//sequence").get("tcFormat") == "NDF"
