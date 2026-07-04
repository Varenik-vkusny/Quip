"""Тесты pure-логики active-speaker reframe: IOU-трекинг лиц.

Только numpy (без torch/scipy) → гоняется в базовом гейте без asd-экстры.
"""

from app.pipeline.stage3_speaker import build_tracks, should_score_asd


class TestShouldScoreASD:
    def test_skip_for_single_track(self) -> None:
        # 1 дорожка → говорящий однозначен, ASD не влияет на регионы → пропускаем torch-форвард
        assert should_score_asd(1) is False

    def test_skip_for_zero_tracks(self) -> None:
        assert should_score_asd(0) is False

    def test_score_for_two_or_more_tracks(self) -> None:
        # 2+ дорожек → нужно выбрать говорящего (max-speak / wide) → ASD обязателен
        assert should_score_asd(2) is True
        assert should_score_asd(5) is True


def _face(frame: int, x1: float, y1: float, x2: float, y2: float) -> dict:
    return {"frame": frame, "bbox": [x1, y1, x2, y2]}


class TestBuildTracks:
    def test_single_face_forms_one_track(self) -> None:
        # одно лицо в одном месте 15 кадров подряд → одна дорожка
        frames = [[_face(i, 100, 100, 200, 200)] for i in range(15)]
        tracks = build_tracks(frames, min_track=10)
        assert len(tracks) == 1
        assert len(tracks[0]["frame"]) == 15

    def test_two_faces_two_tracks(self) -> None:
        # два лица (слева и справа) во всех кадрах → две дорожки
        frames = [[_face(i, 100, 100, 200, 200), _face(i, 800, 100, 900, 200)] for i in range(15)]
        tracks = build_tracks(frames, min_track=10)
        assert len(tracks) == 2
        centers = sorted(((t["bbox"][:, 0] + t["bbox"][:, 2]) / 2).mean() for t in tracks)
        assert centers[0] < 300 and centers[1] > 700

    def test_short_track_dropped(self) -> None:
        # дорожка короче min_track отбрасывается
        frames = [[_face(i, 100, 100, 200, 200)] for i in range(5)]
        assert build_tracks(frames, min_track=10) == []

    def test_gap_within_tolerance_bridged(self) -> None:
        # пропуск детекта на 2 кадра (< num_failed_det) → дорожка не рвётся, bbox интерполируется
        frames = []
        for i in range(15):
            frames.append([] if i in (5, 6) else [_face(i, 100, 100, 200, 200)])
        tracks = build_tracks(frames, min_track=10, num_failed_det=10)
        assert len(tracks) == 1
        assert len(tracks[0]["frame"]) == 15  # дыры заполнены интерполяцией

    def _cross_cut_frames(self) -> list[list[dict]]:
        # Лицо у центра ДО склейки (P1, cx=150), пропадает на несколько кадров НА склейке,
        # затем снова у ДРУГОГО места ПОСЛЕ склейки (P2, cx=180). P1/P2 достаточно близко
        # (IOU≈0.54 > 0.5, разрыв 24-19=5 < num_failed_det) — cut-слепой трекер СЛИЛ бы их
        # в одну дорожку и проинтерполировал bbox через склейку.
        frames: list[list[dict]] = []
        for i in range(44):
            if i <= 19:
                frames.append([_face(i, 100, 0, 200, 100)])  # P1
            elif i <= 23:
                frames.append([])  # разрыв детекта ВОКРУГ склейки
            else:
                frames.append([_face(i, 130, 0, 230, 100)])  # P2
        return frames

    def test_no_cuts_arg_preserves_legacy_fusion(self) -> None:
        # БЕЗ cuts поведение прежнее: близкие лица через разрыв сливаются в одну дорожку,
        # bbox интерполируется через границу шота (это и есть источник доводки-флеша).
        tracks = build_tracks(self._cross_cut_frames(), min_track=10)
        assert len(tracks) == 1
        fr = tracks[0]["frame"]
        assert fr.min() < 20 <= fr.max()  # дорожка ПЕРЕСЕКАЕТ склейку (легаси-баг)

    def test_cut_splits_track_no_cross_cut_interpolation(self) -> None:
        # С передачей cuts дорожка НЕ должна пересекать склейку → нет интерполяции cx
        # старое↔новое положение → новый шот открывается РОВНО на новом лице (снап, не доводка).
        cut = 20
        tracks = build_tracks(self._cross_cut_frames(), min_track=10, cuts=[cut])
        assert tracks, "ожидались дорожки"
        for tr in tracks:
            fr = tr["frame"]
            assert bool((fr < cut).all()) or bool((fr >= cut).all()), (
                f"дорожка пересекает склейку {cut}: кадры {int(fr.min())}..{int(fr.max())}"
            )
        # у пост-склейкового шота первый bbox = реальный P2 (cx=180), без интерполяции к P1
        post = min((t for t in tracks if t["frame"][0] >= cut), key=lambda t: int(t["frame"][0]))
        first_cx = (post["bbox"][0][0] + post["bbox"][0][2]) / 2
        assert abs(first_cx - 180) < 1e-6, f"новый шот стартует не на P2: cx={first_cx}"
