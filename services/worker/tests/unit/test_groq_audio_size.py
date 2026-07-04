"""Stage 1 Groq upload-size guard (regression for the `kk` HTTP 413).

Groq Whisper rejects oversize uploads with HTTP 413 (free tier caps at 25 MB, dev at
100 MB). A 20-min Kazakh WAV (~38 MB, 16 kHz mono) sat between the old 90 MB compress
threshold and the 25 MB free cap → was sent raw → 413 → job failed at `transcribing`.

The worker now compresses to 32 kbps MP3 whenever the raw audio exceeds a tier-safe
cap (config.groq_max_upload_mb, default 24 < 25). This pins the pure threshold
predicate; the ffmpeg re-encode itself is exercised end-to-end.
"""

from app.pipeline.stage1_transcribe import _needs_groq_compression

_MB = 1024 * 1024


def test_small_file_under_cap_sent_raw() -> None:
    # A short clip's WAV is well under the cap → no re-encode (fast path).
    assert _needs_groq_compression(10 * _MB, 24) is False


def test_20min_kk_wav_over_free_cap_is_compressed() -> None:
    # The exact regression: ~38 MB WAV > 24 MB free-tier-safe cap → must compress
    # (the old code only compressed above 90 MB, so this went raw → Groq 413).
    assert _needs_groq_compression(38 * _MB, 24) is True


def test_boundary_equal_cap_sent_raw() -> None:
    assert _needs_groq_compression(24 * _MB, 24) is False


def test_just_over_cap_is_compressed() -> None:
    assert _needs_groq_compression(24 * _MB + 1, 24) is True


def test_dev_tier_cap_allows_larger_raw() -> None:
    # With Groq billing enabled (GROQ_MAX_UPLOAD_MB=95, dev tier 100 MB), a 38 MB WAV
    # is sent raw — the cap is config-driven, no code change needed to switch tiers.
    assert _needs_groq_compression(38 * _MB, 95) is False
