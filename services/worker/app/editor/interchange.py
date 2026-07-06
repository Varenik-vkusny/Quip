"""Pure строковые билдеры editor-handoff форматов (EDL / CSV / JSON / VTT / FCPXML).

«Скачать для редактора»: юзер уносит нарезку Quip в pro-NLE (Premiere / DaVinci Resolve /
Final Cut / CapCut) как таймкод-таймлайн в SOURCE-времени (пере-нарезка из оригинала) плюс
самодостаточные субтитры/манифест. Все функции чистые (нормализованный intermediate → строка),
без I/O и без DB — I/O и сборка zip живут в `main.py`. TDD: `tests/unit/test_interchange.py`.

Нормализованный intermediate (`HandoffClip`/`HandoffMarker`) — тонкий адаптер между моделями
пайплайна (`Segment`/`ClipEdit`/`Chapter`) и билдерами: билдеры НЕ знают про пайплайн, только
про поля хендофа. Таймкод-математика (DF/NDF, кадровая сетка) — вся в `timecode.py`.
"""

from __future__ import annotations

import csv
import io
import json
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.editor.timecode import (
    frame_duration_rational,
    frame_time_rational,
    frames_to_tc,
    is_drop_frame,
    seconds_to_frames,
    seconds_to_tc,
)
from app.editor.timemap import ClipTimeMap
from app.models import CaptionTrack, Word

# ─────────────────────────── нормализованный intermediate ───────────────────────────


@dataclass(frozen=True)
class HandoffInterval:
    """Один оставленный кусок исходника (SOURCE-секунды) → один EDL-event / FCPXML asset-clip."""

    source_in_s: float
    source_out_s: float


@dataclass(frozen=True)
class HandoffClip:
    """Клип для хендофа: одна+ SOURCE-интервал + объяснимость (hook/reason/score/…).

    `source_in_s`/`source_out_s` — общие края клипа (первый in, последний out); `intervals` —
    реальные куски (при multi-interval клипе каждый становится отдельным event'ом/asset-clip'ом).
    """

    name: str
    source_in_s: float
    source_out_s: float
    reason: str
    hook: str | None
    why_works: str | None
    score: float
    type: str
    tone: str | None
    key_quote: str | None
    intervals: list[HandoffInterval] = field(default_factory=list)


@dataclass(frozen=True)
class HandoffMarker:
    """Точка интереса на таймлайне SOURCE-времени (старт клипа / глава / момент карты видео).

    `kind` управляет представлением в FCPXML: ``"chapter"`` → ``<chapter-marker>``, иначе
    ``<marker>``. В EDL/CSV/манифесте `kind` — просто метаданные.
    """

    t_s: float
    name: str
    note: str
    color: str
    kind: str = "marker"


def _oneline(s: str | None) -> str:
    """Схлопнуть переводы строк в пробелы (комментарии EDL/CSV — однострочные). PURE."""
    return " ".join((s or "").split())


# ─────────────────────────── EDL (CMX3600) ───────────────────────────


def build_edl(clips: Sequence[HandoffClip], fps: float, title: str) -> str:
    """CMX3600 EDL в SOURCE-таймкоде (пере-нарезка из оригинала). PURE.

    Каждый SOURCE-интервал = один event: source in/out (абсолютный таймкод исходника) + record
    in/out (позиция на итоговом таймлайне, накапливается с 00:00:00). ``FCM: DROP FRAME`` для
    29.97/59.94, иначе ``NON-DROP FRAME``. ``* FROM CLIP NAME:`` = hook (что за клип), следующий
    ``*``-комментарий = reason (почему момент). Реел ``AX`` (auxiliary), канал ``AA/V`` (2 аудио
    + видео) — стандартно для пере-линковки в Resolve/Premiere.
    """
    df = is_drop_frame(fps)
    lines = [
        f"TITLE: {_oneline(title) or 'Quip Export'}",
        f"FCM: {'DROP FRAME' if df else 'NON-DROP FRAME'}",
        "",
    ]
    event = 1
    rec = 0  # record-позиция в кадрах (таймлайн стартует с нуля)
    for clip in clips:
        for i, iv in enumerate(clip.intervals):
            s_in = seconds_to_frames(iv.source_in_s, fps, rounding="floor")  # IN: не позже слова
            s_out = seconds_to_frames(iv.source_out_s, fps, rounding="ceil")  # OUT: не раньше слова
            length = max(1, s_out - s_in)
            rec_in, rec_out = rec, rec + length
            rec = rec_out
            lines.append(
                f"{event:03d}  AX       AA/V  C        "
                f"{frames_to_tc(s_in, fps)} {frames_to_tc(s_out, fps)} "
                f"{frames_to_tc(rec_in, fps)} {frames_to_tc(rec_out, fps)}"
            )
            lines.append(f"* FROM CLIP NAME: {_oneline(clip.hook) or clip.name}")
            if i == 0 and clip.reason:
                lines.append(f"* {_oneline(clip.reason)}")
            lines.append("")
            event += 1
    return "\n".join(lines) + "\n"


# ─────────────────────────── Markers CSV (DaVinci Resolve-стиль) ───────────────────────────


def build_markers_csv(markers: Sequence[HandoffMarker], fps: float) -> str:
    """Маркеры → CSV ``Timecode In,Name,Comment,Color`` (импорт в DaVinci Resolve). PURE.

    Таймкод в SOURCE-времени (DF/NDF по fps). csv-writer квотит запятые/кавычки в тексте.
    """
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Timecode In", "Name", "Comment", "Color"])
    for m in markers:
        w.writerow([seconds_to_tc(m.t_s, fps), _oneline(m.name), _oneline(m.note), m.color])
    return buf.getvalue()


# ─────────────────────────── JSON-манифест (catch-all: всё) ───────────────────────────


def _clip_manifest(c: HandoffClip, fps: float) -> dict[str, Any]:
    return {
        "name": c.name,
        "source_in_s": c.source_in_s,
        "source_out_s": c.source_out_s,
        "source_in_tc": seconds_to_tc(c.source_in_s, fps),
        "source_out_tc": seconds_to_tc(c.source_out_s, fps),
        "duration_s": round(c.source_out_s - c.source_in_s, 3),
        "reason": c.reason,
        "hook": c.hook,
        "why_works": c.why_works,
        "score": c.score,
        "type": c.type,
        "tone": c.tone,
        "key_quote": c.key_quote,
        "intervals": [
            {
                "source_in_s": iv.source_in_s,
                "source_out_s": iv.source_out_s,
                "source_in_tc": seconds_to_tc(iv.source_in_s, fps),
                "source_out_tc": seconds_to_tc(iv.source_out_s, fps),
            }
            for iv in c.intervals
        ],
    }


def build_manifest_json(
    job: dict[str, Any],
    clips: Sequence[HandoffClip],
    markers: Sequence[HandoffMarker],
    meta: dict[str, Any],
    *,
    chapters: Sequence[dict[str, Any]] = (),
) -> str:
    """Полный JSON-манифест хендофа (catch-all: всё, что знает Quip про нарезку). PURE.

    Источник (ТОЧНЫЙ fps + drop-frame флаг + рациональная длит. кадра + размеры/длительность),
    каждый клип В ОБОИХ представлениях (SOURCE-секунды И SOURCE-таймкод) + hook/why_works/score/
    type/tone/key_quote + интервалы, маркеры и главы. Это «долгоживущая правда» пакета: даже без
    EDL/FCPXML по манифесту можно восстановить нарезку.
    """
    fps = float(meta["fps"])
    manifest: dict[str, Any] = {
        "quip_export_version": 1,
        "generator": "Quip editor handoff",
        "job": {
            "id": job.get("id"),
            "title": meta.get("title"),
            "source_kind": meta.get("source"),
        },
        "source": {
            "fps": fps,  # ТОЧНЫЙ (не округлён) — корректный drop-frame в NLE
            "drop_frame": is_drop_frame(fps),
            "frame_duration": frame_duration_rational(fps),
            "width": int(meta.get("width", 0)),
            "height": int(meta.get("height", 0)),
            "duration_s": meta.get("duration"),
            "title": meta.get("title"),
        },
        "clips": [_clip_manifest(c, fps) for c in clips],
        "markers": [
            {
                "t_s": m.t_s,
                "tc": seconds_to_tc(m.t_s, fps),
                "name": m.name,
                "note": m.note,
                "color": m.color,
                "kind": m.kind,
            }
            for m in markers
        ],
        "chapters": [
            {
                "start_s": ch.get("start"),
                "end_s": ch.get("end"),
                "start_tc": seconds_to_tc(float(ch.get("start", 0.0)), fps),
                "end_tc": seconds_to_tc(float(ch.get("end", 0.0)), fps),
                "title": ch.get("title"),
                "summary": ch.get("summary"),
            }
            for ch in chapters
        ],
    }
    return json.dumps(manifest, ensure_ascii=False, indent=2)


# ─────────────────────────── WebVTT (сиблинг compile_srt, клип-время) ───────────────────────────


def format_vtt_time(t: float) -> str:
    """Секунды → WebVTT-таймкод ``HH:MM:SS.mmm`` (ТОЧКА для мс, в отличие от SRT-запятой). PURE."""
    ms_total = round(t * 1000)
    ms = ms_total % 1000
    s_total = ms_total // 1000
    return f"{s_total // 3600:02d}:{s_total // 60 % 60:02d}:{s_total % 60:02d}.{ms:03d}"


def build_vtt(track: CaptionTrack, words: list[Word], cmap: ClipTimeMap) -> str:
    """CaptionTrack + слова + тайм-маппинг → WebVTT (клип-время). PURE.

    Сиблинг `captions_v2.compile_srt`: та же reply-итерация и тот же `cmap.source_to_clip`, но
    формат WebVTT (шапка ``WEBVTT``, ``.``-разделитель мс). Плоский текст в натуральном регистре,
    без ASS-тегов/караоке — совпадает с прожжённым видео по таймингам. Скрытые/в-дырке реплики
    пропущены без дыр в нумерации (как в SRT).
    """
    cues: list[str] = []
    idx = 1
    for reply in track.replies:
        if reply.hidden or not reply.word_refs:
            continue
        rwords = [words[i] for i in reply.word_refs]
        start_c = cmap.source_to_clip(rwords[0].start)
        if start_c is None:
            continue  # слово в дырке → пропуск (как compile_srt)
        last_c = cmap.source_to_clip(rwords[-1].start)
        end_c = (last_c if last_c is not None else start_c) + (rwords[-1].end - rwords[-1].start)
        text = (
            reply.text_override
            if reply.text_override is not None
            else " ".join(w.text for w in rwords)
        )
        cues.append(f"{idx}\n{format_vtt_time(start_c)} --> {format_vtt_time(end_c)}\n{text}")
        idx += 1
    return "WEBVTT\n\n" + "\n\n".join(cues) + ("\n" if cues else "")


# ─────────────────────────── FCPXML (минимальный валидный) ───────────────────────────


def build_fcpxml(
    clips: Sequence[HandoffClip],
    markers: Sequence[HandoffMarker],
    meta: dict[str, Any],
    *,
    chapters: Sequence[dict[str, Any]] = (),
) -> str:
    """Минимальный валидный FCPXML: source-asset + spine из asset-clip'ов (по интервалам). PURE.

    Рациональный `frameDuration` + `tcFormat` DF/NDF → времена на кадровой сетке (импорт в
    DaVinci Resolve / Final Cut без дрейфа). Каждый интервал клипа = asset-clip (offset = позиция
    на таймлайне, start = SOURCE in-point, оба выровнены по кадру). hook клипа → ``<marker>`` в
    начале его asset-clip; главы (`chapters`) и маркеры с ``kind="chapter"`` → ``<chapter-marker>``,
    прочие маркеры → ``<marker>``, оба привязаны к содержащему их asset-clip'у в SOURCE-времени.
    """
    fps = float(meta["fps"])
    tc_format = "DF" if is_drop_frame(fps) else "NDF"
    width = int(meta.get("width", 1920) or 1920)
    height = int(meta.get("height", 1080) or 1080)
    title = _oneline(str(meta.get("title") or "")) or "Quip Export"
    src_dur_frames = seconds_to_frames(
        float(meta.get("duration", 0.0) or 0.0), fps, rounding="ceil"
    )

    # Таймлайн: клип → интервалы → сегмент (record-offset, source-in, длина + границы в секундах
    # для привязки маркеров). record копится сквозь ВСЕ интервалы всех клипов.
    segments: list[dict[str, Any]] = []
    rec = 0
    for clip in clips:
        for i, iv in enumerate(clip.intervals):
            s_in = seconds_to_frames(iv.source_in_s, fps, rounding="floor")
            s_out = seconds_to_frames(iv.source_out_s, fps, rounding="ceil")
            length = max(1, s_out - s_in)
            segments.append(
                {
                    "offset": rec,
                    "start": s_in,
                    "length": length,
                    "clip": clip,
                    "is_first": i == 0,
                    "src_lo": iv.source_in_s,
                    "src_hi": iv.source_out_s,
                }
            )
            rec += length
    total_rec = rec

    fcpxml = ET.Element("fcpxml", version="1.9")
    resources = ET.SubElement(fcpxml, "resources")
    ET.SubElement(
        resources,
        "format",
        id="r1",
        name="QuipSourceFormat",
        frameDuration=frame_duration_rational(fps),
        width=str(width),
        height=str(height),
    )
    asset = ET.SubElement(
        resources,
        "asset",
        id="r2",
        name=title,
        start="0s",
        duration=frame_time_rational(src_dur_frames, fps),
        hasVideo="1",
        hasAudio="1",
        format="r1",
        videoSources="1",
        audioSources="1",
        audioChannels="2",
    )
    ET.SubElement(asset, "media-rep", kind="original-media", src="source.mp4")

    library = ET.SubElement(fcpxml, "library")
    event = ET.SubElement(library, "event", name="Quip Export")
    project = ET.SubElement(event, "project", name=title)
    sequence = ET.SubElement(
        project,
        "sequence",
        format="r1",
        tcStart="0s",
        tcFormat=tc_format,
        duration=frame_time_rational(total_rec, fps),
        audioLayout="stereo",
        audioRate="48k",
    )
    spine = ET.SubElement(sequence, "spine")

    for seg in segments:
        hclip: HandoffClip = seg["clip"]
        ac = ET.SubElement(
            spine,
            "asset-clip",
            name=hclip.name,
            ref="r2",
            format="r1",
            offset=frame_time_rational(seg["offset"], fps),
            start=frame_time_rational(seg["start"], fps),
            duration=frame_time_rational(seg["length"], fps),
        )
        seg["el"] = ac
        # hook → marker в начале первого интервала клипа (start = SOURCE in-point клипа).
        if seg["is_first"] and hclip.hook:
            ET.SubElement(
                ac,
                "marker",
                start=frame_time_rational(seg["start"], fps),
                duration=frame_time_rational(1, fps),
                value=_oneline(hclip.hook),
            )

    # Главы + произвольные маркеры → привязка к содержащему asset-clip'у (по SOURCE-времени).
    chapter_markers: list[HandoffMarker] = [
        HandoffMarker(
            t_s=float(ch.get("start", 0.0)),
            name=_oneline(str(ch.get("title") or "Chapter")),
            note=_oneline(str(ch.get("summary") or "")),
            color="",
            kind="chapter",
        )
        for ch in chapters
    ]
    for m in list(markers) + chapter_markers:
        host = _segment_containing(segments, m.t_s)
        if host is None:
            continue  # маркер вне всех интервалов — на таймлайне его нет (пропуск, не ошибка)
        tag = "chapter-marker" if m.kind == "chapter" else "marker"
        ET.SubElement(
            host["el"],
            tag,
            start=frame_time_rational(seconds_to_frames(m.t_s, fps, rounding="round"), fps),
            duration=frame_time_rational(1, fps),
            value=m.name or tag,
        )

    ET.indent(fcpxml, space="  ")
    body = ET.tostring(fcpxml, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n{body}\n'


def _segment_containing(segments: Sequence[dict[str, Any]], t_s: float) -> dict[str, Any] | None:
    """Первый сегмент, чей SOURCE-интервал ``[src_lo, src_hi)`` содержит `t_s`. PURE."""
    for seg in segments:
        if seg["src_lo"] <= t_s < seg["src_hi"]:
            return seg
    return None
