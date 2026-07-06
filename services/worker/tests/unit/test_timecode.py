"""Тесты pure-таймкод-логики (app.editor.timecode) — фундамент editor-handoff экспорта.

Drop-frame математика — самое ошибкоопасное место всего экспорта (кадр туда/сюда = рассинхрон
в pro-NLE). Поэтому TDD: известные значения DF/NDF + политика округления floor-in/ceil-out
(слово НИКОГДА не обрезается на границе кадра). См. CLAUDE.md «TDD на pure-логике».
"""

from app.editor.timecode import (
    frame_duration_rational,
    frame_time_rational,
    frames_to_tc,
    is_drop_frame,
    seconds_to_frames,
    seconds_to_tc,
)

_2997 = 30000.0 / 1001.0  # 29.97002997… (точная NTSC-дробь, как в meta.json)
_5994 = 60000.0 / 1001.0  # 59.94005994…
_23976 = 24000.0 / 1001.0  # 23.976023976…


# ─────────────────────────── is_drop_frame ───────────────────────────


def test_is_drop_frame_ntsc_2997_and_5994():
    # Drop-frame определён ТОЛЬКО для семейств 29.97 и 59.94.
    assert is_drop_frame(_2997) is True
    assert is_drop_frame(_5994) is True


def test_is_drop_frame_false_for_integer_and_2398():
    assert is_drop_frame(25.0) is False
    assert is_drop_frame(30.0) is False  # РОВНО 30 — это NDF, не DF
    assert is_drop_frame(24.0) is False
    assert is_drop_frame(60.0) is False
    assert is_drop_frame(_23976) is False  # 23.976 = 24000/1001 → NDF (не 30-семейство)


# ─────────────────────────── seconds_to_frames (floor-in / ceil-out) ───────────────────────────


def test_seconds_to_frames_exact_multiple():
    assert seconds_to_frames(1.0, 25.0) == 25
    assert seconds_to_frames(2.0, 30.0, rounding="floor") == 60  # эпсилон: не 59
    assert seconds_to_frames(2.0, 30.0, rounding="ceil") == 60  # эпсилон: не 61


def test_seconds_to_frames_floor_in_ceil_out_never_clips_word():
    # 1.019с @25 = 25.475 кадра. IN-точка (floor) → 25 (раньше слова), OUT-точка (ceil) → 26
    # (позже слова) → слово ЦЕЛИКОМ внутри [25,26]; round дал бы 25 (обрезал бы OUT).
    assert seconds_to_frames(1.019, 25.0, rounding="floor") == 25
    assert seconds_to_frames(1.019, 25.0, rounding="ceil") == 26
    assert seconds_to_frames(1.019, 25.0, rounding="round") == 25


# ─────────────────────────── frames_to_tc (NDF) ───────────────────────────


def test_frames_to_tc_ndf_zero_and_one_second():
    assert frames_to_tc(0, 25.0) == "00:00:00:00"
    assert frames_to_tc(25, 25.0) == "00:00:01:00"
    assert frames_to_tc(90, 30.0) == "00:00:03:00"


def test_frames_to_tc_ndf_uses_colon_separator():
    assert frames_to_tc(3600 * 30, 30.0) == "01:00:00:00"


# ─────────────────────────── frames_to_tc (DROP-FRAME) ───────────────────────────


def test_frames_to_tc_df_zero():
    assert frames_to_tc(0, _2997) == "00:00:00;00"  # DF использует ';'


def test_frames_to_tc_df_minute_rollover_drops_two_frames():
    # Классический drop: после 00:00:59;29 кадры ;00 и ;01 ПРОПУСКАЮТСЯ → 00:01:00;02.
    assert frames_to_tc(1799, _2997) == "00:00:59;29"
    assert frames_to_tc(1800, _2997) == "00:01:00;02"


def test_frames_to_tc_df_tenth_minute_no_drop():
    # Каждая 10-я минута — БЕЗ дропа (компенсация): кадр 17981 → 00:09:59;29, 17982 → 00:10:00;00
    # (на 10-минутной границе номера кадров ;00/;01 НЕ выкидываются).
    assert frames_to_tc(17981, _2997) == "00:09:59;29"
    assert frames_to_tc(17982, _2997) == "00:10:00;00"


# ─────────────────────────── seconds_to_tc ───────────────────────────


def test_seconds_to_tc_ndf():
    assert seconds_to_tc(0.0, 25.0) == "00:00:00:00"
    assert seconds_to_tc(1.0, 25.0) == "00:00:01:00"
    assert seconds_to_tc(3600.0, 30.0) == "01:00:00:00"


def test_seconds_to_tc_df_separator():
    tc = seconds_to_tc(0.0, _2997)
    assert tc == "00:00:00;00"
    assert ";" in tc


# ─────────────────────────── frame_duration_rational (FCPXML) ───────────────────────────


def test_frame_duration_rational_ntsc():
    assert frame_duration_rational(_2997) == "1001/30000s"
    assert frame_duration_rational(_5994) == "1001/60000s"
    assert frame_duration_rational(_23976) == "1001/24000s"


def test_frame_duration_rational_integer():
    assert frame_duration_rational(25.0) == "1/25s"
    assert frame_duration_rational(30.0) == "1/30s"
    assert frame_duration_rational(24.0) == "1/24s"


# ─────────────────── frame_time_rational (FCPXML frame-aligned time) ───────────────────


def test_frame_time_rational_zero_and_ntsc_and_integer():
    assert frame_time_rational(0, _2997) == "0s"
    assert frame_time_rational(30, _2997) == "30030/30000s"  # 30 * 1001
    assert frame_time_rational(25, 25.0) == "25/25s"
    assert frame_time_rational(48, 24.0) == "48/24s"
