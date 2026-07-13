"""Face re-identification embeddings (ЛИЧНОСТЬ, а не позиция) для Speaker Focus v2.

Отпечаток лица (512-d L2-нормированный эмбеддинг) на кроп лица → кластеризация треков в ЛИЧНОСТИ
(build_identities), устойчивую к смене камер/позиций (в отличие от build_seats по cx). См. спеку
docs/superpowers/specs/2026-07-13-speaker-focus-reframe-design.md §«v2 — Identity-based».

⚠️ ВАЛИДАЦИОННАЯ реализация: InceptionResnetV1 (facenet-pytorch, веса VGGFace2). Веса =
research/NON-COMMERCIAL. Для ПРОДА нужен коммерчески-чистый reID (MIT/Apache веса или self-train
MobileFaceNet). Заменяется без изменения build_identities (тот берёт готовые эмбеддинги).

⚠️ torchvision в venv сломан (ABI vs torch 2.12) и НЕ нужен нам (это для MTCNN-детекции, у нас свои
кропы) — стабим его в sys.modules ДО импорта facenet, чтобы взять только чистый InceptionResnetV1.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from typing import Any

import numpy as np

from app.errors import JobError

_EMBED_DIM = 512


def _stub_torchvision() -> None:
    import sys
    import types

    for name in (
        "torchvision",
        "torchvision.transforms",
        "torchvision.transforms.functional",
        "torchvision.ops",
        "torchvision.ops.boxes",
    ):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["torchvision.ops.boxes"].batched_nms = lambda *a, **k: None  # type: ignore[attr-defined]
    sys.modules["torchvision"].transforms = sys.modules["torchvision.transforms"]  # type: ignore[attr-defined]
    sys.modules["torchvision.transforms"].functional = sys.modules[  # type: ignore[attr-defined]
        "torchvision.transforms.functional"
    ]
    sys.modules["torchvision"].ops = sys.modules["torchvision.ops"]  # type: ignore[attr-defined]
    sys.modules["torchvision.ops"].boxes = sys.modules["torchvision.ops.boxes"]  # type: ignore[attr-defined]


@lru_cache(maxsize=1)
def _load_net() -> Any:
    """InceptionResnetV1 (VGGFace2), CPU, eval. Кэш — грузим один раз на процесс."""
    _stub_torchvision()
    try:
        from facenet_pytorch import InceptionResnetV1  # noqa: PLC0415
    except ImportError as e:  # ЯВНАЯ ошибка, не тихий фолбэк (правило №8)
        raise JobError(
            "reframe",
            "face-reID model missing: `uv pip install facenet-pytorch` (validation-stage dep; "
            "prod needs a commercially-clean reID — see module docstring)",
        ) from e
    return InceptionResnetV1(pretrained="vggface2").eval()


def embed_faces(faces_bgr: Sequence[np.ndarray]) -> np.ndarray:
    """N кропов лица (BGR, любой размер) → (N, 512) L2-нормированных эмбеддингов. CPU.

    Пустой вход → (0, 512). Ресайз в 160×160 + fixed_image_standardization ([0,255]→[-1,1])
    как в facenet-pytorch."""
    import cv2  # noqa: PLC0415
    import torch  # noqa: PLC0415

    if len(faces_bgr) == 0:
        return np.zeros((0, _EMBED_DIM), dtype=float)
    batch = np.stack(
        [cv2.resize(cv2.cvtColor(f, cv2.COLOR_BGR2RGB), (160, 160)) for f in faces_bgr]
    ).astype(np.float32)  # N,160,160,3
    t = torch.from_numpy(batch).permute(0, 3, 1, 2)  # N,3,160,160
    t = (t - 127.5) / 128.0  # fixed_image_standardization
    net = _load_net()
    with torch.no_grad():
        emb = torch.nn.functional.normalize(net(t))
    return emb.numpy().astype(float)
