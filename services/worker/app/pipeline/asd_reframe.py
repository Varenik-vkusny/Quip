"""I/O active-speaker reframe: MediaPipe@fps → дорожки → crop+ASD → SpeakerTrack[].

Lean-путь (BENCHMARKS §6): наш быстрый MediaPipe-детект (не S3FD) кормит 0.84M ASD-модель.
Тяжёлые либы (torch/scipy/mediapipe/cv2/python_speech_features) — ЛЕНИВЫЙ импорт: модуль
импортируется без asd-экстры, но score_tracks_in_segment() требует её (REFRAME_SPEAKER=on).

Чистая математика (build_tracks) — в stage3_speaker (unit-тесты).
Здесь только обёртки I/O; JobError при сбое (правило №8).
"""

from __future__ import annotations

import glob
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from app.config import get_settings
from app.errors import JobError
from app.pipeline.stage3_reframe import SpeakerTrack, _phase_add
from app.pipeline.stage3_speaker import build_tracks, should_score_asd

_STAGE = "reframe"
_SILENT: float = -9.0  # speak-score для дорожки без валидного скора


def _ffmpeg(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise JobError(_STAGE, f"ffmpeg exit code {proc.returncode}: {(proc.stderr or '')[-300:]}")


def _crop_faces(
    track: dict[str, Any], get_frame: Callable[[int], Any], crop_scale: float
) -> np.ndarray:
    """Дорожка → стек кадров лица 224×224 (BGR). ``get_frame(i)`` отдаёт кадр i ЛЕНИВО (не держим
    все кадры клипа в RAM — иначе OOM на 4K/длинных). Кроп-математика 1:1 как crop_video LR-ASD."""
    import cv2  # noqa: PLC0415
    from scipy import signal  # noqa: PLC0415

    s = signal.medfilt(np.array([max(d[3] - d[1], d[2] - d[0]) / 2 for d in track["bbox"]]), 13)
    y = signal.medfilt(np.array([(d[1] + d[3]) / 2 for d in track["bbox"]]), 13)
    x = signal.medfilt(np.array([(d[0] + d[2]) / 2 for d in track["bbox"]]), 13)
    out = []
    for i, fr in enumerate(track["frame"]):
        bs = s[i]
        bsi = int(bs * (1 + 2 * crop_scale))
        pad = ((bsi, bsi), (bsi, bsi), (0, 0))
        img = np.pad(get_frame(int(fr)), pad, "constant", constant_values=110)
        my, mx = y[i] + bsi, x[i] + bsi
        face = img[
            int(my - bs) : int(my + bs * (1 + 2 * crop_scale)),
            int(mx - bs * (1 + crop_scale)) : int(mx + bs * (1 + crop_scale)),
        ]
        out.append(cv2.resize(face, (224, 224)))
    return np.array(out)


# Сколько кадров дорожки эмбеддить для отпечатка личности. Поза/ракурс скачут (профиль, наклон) →
# усредняем по разреженной выборке, чтобы отпечаток был стабильным. 12 хватает, больше — лишний CPU.
_EMBED_SAMPLES = 12


def _tight_face(crop224: np.ndarray, crop_scale: float) -> np.ndarray:
    """224-кроп LR-ASD (широкий, с полями) → ТЕСНЫЙ бокс лица. Без лишнего декода — режем то, что
    уже вырезано для ASD.

    ЗАЧЕМ: ASD-кроп намеренно широкий (crop_scale=0.55) — модель так обучена. Но для reID поля =
    ФОН (диван/стена/свет), а фон у собеседников ОБЩИЙ → SFace кодирует его и раздувает
    similarity МЕЖДУ РАЗНЫМИ людьми. Замер на реальном видео: cross 0.411 (широкий кроп) → 0.233
    (тесный), запас разделимости +0.140 → +0.211. Т.е. тесним не ради «похожести», а ради ЗАПАСА.

    Геометрия — ровно из _crop_faces: кроп = 2·bs·(1+cs) по обеим осям, лицо (2·bs) прижато к
    ВЕРХУ (y от 0) и центрировано по x."""
    denom = 2.0 * (1.0 + crop_scale)
    h, w = crop224.shape[:2]
    x0, x1 = int(w * crop_scale / denom), int(w * (crop_scale + 2.0) / denom)
    y1 = int(h * 2.0 / denom)
    face = crop224[0:y1, x0:x1]
    return face if face.size and min(face.shape[:2]) >= 20 else crop224


def _track_embedding(faces224: np.ndarray, crop_scale: float) -> tuple[float, ...]:
    """Кропы лица дорожки → ОДИН отпечаток личности (128-d L2-норм): mean по разреженной выборке."""
    from app.reid.embedder import embed_faces  # noqa: PLC0415  (ленивый: тянет reID-модель)

    n = faces224.shape[0]
    idx = np.linspace(0, n - 1, num=min(_EMBED_SAMPLES, n)).astype(int)
    embs = embed_faces([_tight_face(faces224[i], crop_scale) for i in idx])
    if embs.shape[0] == 0:
        return ()
    mean = embs.mean(axis=0)
    norm = float(np.linalg.norm(mean))
    if norm == 0:
        return ()
    return tuple(float(x) for x in (mean / norm))


def score_tracks_in_segment(
    video: Path,
    src_w: int,
    src_h: int,
    start: float,
    end: float,
    fps: float,
    *,
    crop_scale: float = 0.55,
    cuts: Sequence[int] = (),
    det_conf: float = 0.5,
    compute_embeddings: bool = False,
) -> list[SpeakerTrack]:
    """Сегмент → дорожки лиц с ASD speak-score (вход plan_regions). [] если лиц нет.

    Кадры@fps + аудио → MediaPipe-детект → build_tracks → crop+ASD на дорожку → SpeakerTrack
    (f0/f1 в КЛИП-кадрах @fps, cx per-frame, width средняя доля, speak средний скор).
    crop_scale тюним под MediaPipe-кропы (модель обучена на S3FD).

    cuts — кадры склеек в сетке ЭТОГО fps (== face_fps): дорожки не тянутся через шот-границу,
    иначе bbox интерполируется старое→новое положение и кроп «доводится» после склейки.
    """
    import cv2  # noqa: PLC0415
    import mediapipe as mp  # noqa: PLC0415
    from mediapipe.tasks import python as mp_python  # noqa: PLC0415
    from mediapipe.tasks.python import vision as mp_vision  # noqa: PLC0415
    from scipy.io import wavfile  # noqa: PLC0415

    from app.asd.scorer import score_track  # noqa: PLC0415
    from app.pipeline.stage3_reframe import _ensure_face_model  # noqa: PLC0415

    model = _ensure_face_model()
    with tempfile.TemporaryDirectory() as td:
        # Фазовые тайминги ([reframe]-строка reframe_segment): sample = ffmpeg-экстракция
        # кадров/аудио + чтение JPEG; detect = MediaPipe-детект + build_tracks; asd = crop +
        # torch-скоринг (0.0s при скипе single-track). Сбор — stage3_reframe._phase_add.
        t0 = time.monotonic()
        fdir = Path(td) / "f"
        fdir.mkdir()
        _ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-ss",
                str(start),
                "-to",
                str(end),
                "-i",
                str(video),
                "-r",
                str(fps),
                "-f",
                "image2",
                str(fdir / "%06d.jpg"),
                "-loglevel",
                "panic",
            ]
        )
        wav = str(Path(td) / "a.wav")
        _ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-ss",
                str(start),
                "-to",
                str(end),
                "-i",
                str(video),
                "-ac",
                "1",
                "-vn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                wav,
                "-loglevel",
                "panic",
            ]
        )
        # СТРИМИНГ: держим только ПУТИ к JPG (снапшот порядка ОДИН раз), НЕ пиксели → RAM детекта
        # плоская независимо от длины/разрешения (был список ВСЕХ кадров = OOM 37ГБ на 60с 4K).
        jpg_paths = sorted(glob.glob(str(fdir / "*.jpg")))
        _phase_add("sample", time.monotonic() - t0)
        if not jpg_paths:
            return []

        t0 = time.monotonic()
        opts = mp_vision.FaceDetectorOptions(
            base_options=mp_python.BaseOptions(model_asset_path=str(model)),
            min_detection_confidence=det_conf,
        )
        frame_faces: list[list[dict[str, Any]]] = []
        with mp_vision.FaceDetector.create_from_options(opts) as det:
            for i, fpath in enumerate(jpg_paths):
                img = cv2.imread(fpath)  # ОДИН кадр в полёте (не копим) — тот же decode, что и был
                if img is None:
                    raise JobError(_STAGE, f"cannot read frame {fpath}")
                rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                res = det.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
                ff: list[dict[str, Any]] = []
                for d in res.detections:
                    b = d.bounding_box
                    ff.append(
                        {
                            "frame": i,
                            "bbox": [
                                b.origin_x,
                                b.origin_y,
                                b.origin_x + b.width,
                                b.origin_y + b.height,
                            ],
                        }
                    )
                frame_faces.append(ff)

        tracks = build_tracks(frame_faces, cuts=cuts)
        _phase_add("detect", time.monotonic() - t0)
        if not tracks:
            return []

        # Perf (#2): ASD speak-скор нужен ТОЛЬКО для выбора говорящего среди 2+ дорожек. При одной
        # дорожке говорящий однозначен → crop+torch-форвард не влияет на регионы (см.
        # should_score_asd). Пропускаем его → speak=_SILENT (как «нет валидного скора»; downstream
        # _pick_target всё равно вернёт эту единственную дорожку). Аудио не читаем зря.
        # Kill-switch reframe_skip_asd_single_track=false → всегда считаем ASD (быстрый откат).
        score_asd = (not get_settings().reframe_skip_asd_single_track) or should_score_asd(
            len(tracks)
        )
        t0 = time.monotonic()
        sr, audio = wavfile.read(wav) if score_asd else (0, None)

        # Ленивый провайдер кадров для кропа: перечитываем нужный кадр с ДИСКА (JPG уже там), НЕ
        # держим весь список в RAM. _crop_faces берёт по одному → пик RAM = стек 224×224 кропов
        # дорожки (сотни МБ), а не полные кадры (десятки ГБ на 4K). Cache НЕ делаем: он держал бы
        # полные кадры = снова OOM на длинной непрерывной дорожке.
        def get_frame(idx: int) -> Any:
            img = cv2.imread(jpg_paths[idx])
            if img is None:
                raise JobError(_STAGE, f"cannot read frame {jpg_paths[idx]}")
            return img

        out: list[SpeakerTrack] = []
        for tr in tracks:
            speak_frames: tuple[float, ...] = ()
            embedding: tuple[float, ...] = ()
            if score_asd:
                assert audio is not None  # score_asd → wav прочитан выше
                faces224 = _crop_faces(tr, get_frame, crop_scale)
                a0 = int(tr["frame"][0] / fps * sr)
                a1 = int((tr["frame"][-1] + 1) / fps * sr)
                sc = score_track(faces224, audio[a0:a1])
                speak = float(np.mean(sc)) if sc.size else _SILENT
                # per-frame скоры сохраняем для turn-level binding (speaker_switch): какое лицо
                # говорит в КАЖДОЙ реплике диаризации (скаляр speak за весь шот тут бесполезен).
                speak_frames = tuple(float(x) for x in sc)
                # v2 (identity): отпечаток ЛИЧНОСТИ по тем же кропам (без доп. декода) — среднее по
                # РАЗРЕЖЕННОЙ выборке кадров (поза скачет → усреднение стабилизирует), L2-норм.
                if compute_embeddings and faces224.shape[0]:
                    embedding = _track_embedding(faces224, crop_scale)
            else:
                speak = _SILENT
            cx_series = ((tr["bbox"][:, 0] + tr["bbox"][:, 2]) / 2 / src_w).tolist()
            width = float(((tr["bbox"][:, 2] - tr["bbox"][:, 0]) / src_w).mean())
            out.append(
                SpeakerTrack(
                    f0=int(tr["frame"][0]),
                    f1=int(tr["frame"][-1]) + 1,
                    cx=tuple(min(1.0, max(0.0, c)) for c in cx_series),
                    width=width,
                    speak=speak,
                    speak_frames=speak_frames,
                    embedding=embedding,
                )
            )
        _phase_add("asd", time.monotonic() - t0)
        return out
