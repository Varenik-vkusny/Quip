"""Face re-identification embeddings (ЛИЧНОСТЬ, а не позиция) для Speaker Focus v2.

Отпечаток лица (128-d L2-нормированный эмбеддинг) на кроп лица → кластеризация треков в ЛИЧНОСТИ
(build_identities), устойчивую к смене камер/позиций (в отличие от build_seats по cx). См. спеку
docs/superpowers/specs/2026-07-13-speaker-focus-reframe-design.md §«v2 — Identity-based».

Модель: **SFace** (MobileFaceNet), OpenCV Zoo `face_recognition_sface_2021dec.onnx`.
* Веса — **Apache-2.0** (LICENSE лежит В ТОЙ ЖЕ директории, что и .onnx; OpenCV Zoo назначает
  лицензию ПОКАТАЛОЖНО — у соседнего YuNet она MIT, т.е. это осознанный грант, не boilerplate).
  Коммерчески чистые — в отличие от InsightFace/EdgeFace/facenet-VGGFace2 (все non-commercial).
* Ноль новых зависимостей: `cv2.FaceRecognizerSF` уже есть в cv2 (torch/torchvision НЕ нужны).
* LFW 99.40%, ~12 мс/кроп на CPU.

Точность: SFace обучен на ArcFace-выровненных (5 точек) кропах, а мы кормим свой квадратный кроп
лица без выравнивания. Для нашей задачи (closed-set: 2–10 человек В ОДНОМ видео, один свет/сессия)
запаса разделимости с избытком — замерено на реальном видео, см. .scratch/reid_validate.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from app.errors import JobError

_EMBED_DIM = 128
_INPUT_SIZE = 112  # SFace: вход 1×3×112×112 (батч СТАТИЧЕСКИЙ = 1 → гоним по одному)
_MODEL_PATH = Path(__file__).parent / "weights" / "face_recognition_sface_2021dec.onnx"
_MODEL_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_recognition_sface/face_recognition_sface_2021dec.onnx"
)


@lru_cache(maxsize=1)
def _load_net() -> Any:
    """SFace recognizer (CPU). Кэш — грузим один раз на процесс."""
    import cv2  # noqa: PLC0415

    if not _MODEL_PATH.exists():  # ЯВНАЯ ошибка, не тихий фолбэк (правило №8)
        raise JobError(
            "reframe",
            f"face-reID weights missing: {_MODEL_PATH} (Apache-2.0, download: {_MODEL_URL})",
        )
    return cv2.FaceRecognizerSF.create(str(_MODEL_PATH), "")


def embed_faces(faces_bgr: Sequence[np.ndarray]) -> np.ndarray:
    """N кропов лица (BGR, любой размер) → (N, 128) L2-нормированных эмбеддингов. CPU.

    Пустой вход → (0, 128). Ресайз в 112×112; SFace отдаёт НЕ-нормированный вектор (‖e‖≈2.1) →
    нормируем сами, чтобы косинус был просто скалярным произведением (build_identities это ждёт).
    """
    import cv2  # noqa: PLC0415

    if len(faces_bgr) == 0:
        return np.zeros((0, _EMBED_DIM), dtype=float)
    net = _load_net()
    out = np.empty((len(faces_bgr), _EMBED_DIM), dtype=float)
    for i, face in enumerate(faces_bgr):
        emb = np.asarray(
            net.feature(cv2.resize(face, (_INPUT_SIZE, _INPUT_SIZE))), dtype=float
        ).ravel()
        norm = float(np.linalg.norm(emb))
        out[i] = emb / norm if norm > 0 else emb
    return out
