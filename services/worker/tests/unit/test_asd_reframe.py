"""Регрессия рефактора `_crop_faces` на СТРИМИНГ (bounded RAM для 4K/длинных клипов).

OOM-потолок reframe = список ВСЕХ кадров клипа в RAM (`frames=[]`, 37 ГБ на 60с 4K). Фикс:
детект стримит кадры по одному, а `_crop_faces` берёт кадры ЛЕНИВО через провайдер
``get_frame(i)`` вместо материализованного списка. Кроп-математика НЕ меняется — этот тест
доказывает БАЙТ-В-БАЙТ идентичность выхода `_crop_faces` (list-доступ vs provider-доступ) на
синтетике, без mediapipe/torch/видео. Это единственный реальный риск рефактора (смена сигнатуры).
"""

from __future__ import annotations

from typing import Any

import numpy as np

from app.pipeline import asd_reframe


def _ref_crop_faces_list(track: dict[str, Any], frames: list[Any], crop_scale: float) -> np.ndarray:
    """ВЕРБАТИМ прежняя (list-based) логика `_crop_faces` — эталон для регрессии."""
    import cv2
    from scipy import signal

    s = signal.medfilt(np.array([max(d[3] - d[1], d[2] - d[0]) / 2 for d in track["bbox"]]), 13)
    y = signal.medfilt(np.array([(d[1] + d[3]) / 2 for d in track["bbox"]]), 13)
    x = signal.medfilt(np.array([(d[0] + d[2]) / 2 for d in track["bbox"]]), 13)
    out = []
    for i, fr in enumerate(track["frame"]):
        bs = s[i]
        bsi = int(bs * (1 + 2 * crop_scale))
        pad = ((bsi, bsi), (bsi, bsi), (0, 0))
        img = np.pad(frames[int(fr)], pad, "constant", constant_values=110)
        my, mx = y[i] + bsi, x[i] + bsi
        face = img[
            int(my - bs) : int(my + bs * (1 + 2 * crop_scale)),
            int(mx - bs * (1 + crop_scale)) : int(mx + bs * (1 + crop_scale)),
        ]
        out.append(cv2.resize(face, (224, 224)))
    return np.array(out)


def _synthetic(n_frames: int, n_track: int, start: int) -> tuple[list[Any], dict[str, Any]]:
    rng = np.random.default_rng(42)
    frames = [rng.integers(0, 256, (240, 320, 3), dtype=np.uint8) for _ in range(n_frames)]
    idxs = list(range(start, start + n_track))
    # bbox [x0,y0,x1,y1] — лицо гуляет по кадру (двигаем центр), размер варьируется
    bbox = np.array(
        [[90 + i * 2, 60 + i, 170 + i * 2, 150 + i] for i in range(n_track)], dtype=float
    )
    return frames, {"frame": idxs, "bbox": bbox}


def test_crop_faces_provider_matches_list_verbatim() -> None:
    # провайдер, эквивалентный прежнему frames[i]; выход ОБЯЗАН быть побитово тем же
    frames, track = _synthetic(n_frames=40, n_track=18, start=6)
    ref = _ref_crop_faces_list(track, frames, 0.4)
    got = asd_reframe._crop_faces(track, lambda i: frames[i], 0.4)
    assert got.shape == ref.shape
    assert np.array_equal(got, ref)  # БАЙТ-В-БАЙТ, не «похоже»


def test_crop_faces_provider_various_scales_and_positions() -> None:
    for scale in (0.0, 0.25, 0.4, 0.6):
        frames, track = _synthetic(n_frames=50, n_track=20, start=10)
        ref = _ref_crop_faces_list(track, frames, scale)
        got = asd_reframe._crop_faces(track, lambda i, f=frames: f[i], scale)
        assert np.array_equal(got, ref), f"crop mismatch at scale={scale}"


# ── _tight_face: ASD-кроп (широкий, обучен так) → ТЕСНЫЙ бокс лица для reID ──────────────────────
# ЗАЧЕМ тест: поля ASD-кропа = ФОН, а фон у собеседников ОБЩИЙ → эмбеддер кодирует диван/стену и
# раздувает похожесть РАЗНЫХ людей (замер: cross 0.41 → 0.23 после тесного кропа). Геометрия тесного
# бокса ВЫВЕДЕНА из математики _crop_faces — если та поедет, reID тихо деградирует. Пиним связь.


def _face_marked(crop_scale: float) -> tuple[np.ndarray, np.ndarray]:
    """Кадр, где ПИКСЕЛИ ЛИЦА помечены цветом → (224-кроп ASD, тесный кроп). Доля «цвета лица»
    показывает, сколько в кропе реального лица, а сколько фона."""
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    frame[:, :, 1] = 200  # фон (зелёный)
    x0, y0, x1, y1 = 120, 70, 200, 150  # бокс лица
    frame[y0:y1, x0:x1] = (0, 0, 255)  # лицо (красный)
    n = 15  # ≥ kernel медфильтра (13)
    track = {
        "frame": list(range(n)),
        "bbox": np.array([[x0, y0, x1, y1]] * n, dtype=float),
    }
    crop = asd_reframe._crop_faces(track, lambda i: frame, crop_scale)[n // 2]
    return crop, asd_reframe._tight_face(crop, crop_scale)


def _face_frac(img: np.ndarray) -> float:
    return float(np.mean((img[:, :, 2] > 128) & (img[:, :, 1] < 128)))  # доля красных пикселей


def test_tight_face_keeps_the_face_and_drops_the_shared_background() -> None:
    wide, tight = _face_marked(0.55)  # 0.55 = боевой reframe_speaker_crop_scale
    assert wide.shape == (224, 224, 3)
    # ASD-кроп: лицо занимает лишь ~треть — остальное фон/паддинг (модели так и надо)
    assert _face_frac(wide) < 0.5
    # тесный кроп: почти целиком лицо → фон (общий у собеседников) в эмбеддинг не попадает
    assert _face_frac(tight) > 0.9, f"tight crop is only {_face_frac(tight):.0%} face"


def test_tight_face_geometry_holds_across_crop_scales() -> None:
    for scale in (0.25, 0.4, 0.55, 0.6):
        _, tight = _face_marked(scale)
        assert _face_frac(tight) > 0.9, f"tight crop leaks background at crop_scale={scale}"


def test_tight_face_degenerate_crop_falls_back_to_input() -> None:
    # крошечный кроп: тесный бокс выродился бы (<20px) → отдаём исходный, а не пустой массив
    tiny = np.zeros((10, 10, 3), dtype=np.uint8)
    assert asd_reframe._tight_face(tiny, 0.55).shape == tiny.shape
