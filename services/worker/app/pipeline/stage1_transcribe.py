"""Stage 1 (Transcribe): source.wav → transcript.json (Transcript, word-level, секунды).

Провайдеры взаимозаменяемы (Deepgram/Groq) — downstream видит только Transcript.
Deepgram отдаёт СЕКУНДЫ с diarization; Groq — секунды без diarization.

Маршрутизация:
  language=kk (казахский) + TRANSCRIPTION_PROVIDER=deepgram → авто-переключение на Groq,
  т.к. Deepgram возвращает HTTP 400 для kk. Другие языки → Deepgram (авто-детект).
  TRANSCRIPTION_PROVIDER=groq → Groq для ВСЕХ языков (ручное переключение).

Границы: deepgram_to_transcript / groq_to_transcript — pure (ответ → Transcript),
покрыты unit-тестами. Сетевые вызовы (call_deepgram / call_groq) — тонкие обёртки
над REST; при сбое кидают JobError (правило №8).
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings
from app.errors import JobError
from app.models import Transcript, Word

_STAGE = "transcribe"
_DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
_GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

# Deepgram Nova-3 pre-recorded, pay-as-you-go.
DEEPGRAM_NOVA_USD_PER_MIN = 0.0043  # ≈$0.258/hr

# Groq Whisper, pay-as-you-go (cheaper than Deepgram for supported languages).
GROQ_TURBO_USD_PER_MIN = 0.000667  # whisper-large-v3-turbo: $0.04/hr
GROQ_LARGE_USD_PER_MIN = 0.00185  # whisper-large-v3: $0.111/hr

# Languages that Deepgram doesn't support (returns HTTP 400) → auto-route to Groq.
# kk = Kazakh: misidentified as Turkish or returns 0 words on auto-detect.
DEEPGRAM_UNSUPPORTED: frozenset[str] = frozenset({"kk"})


def resolve_transcription_provider(
    language: str | None, *, configured_provider: str, groq_api_key_set: bool
) -> str:
    """PURE. Effective provider for THIS request — the same predicate `transcribe()` uses
    to auto-route kk (Kazakh) audio to Groq. Also used by run.py to key the content-addressed
    transcript cache: the cache MUST key on the effective provider, not the static
    ``configured_provider`` config, or a request that resolves to a different provider than a
    prior request (e.g. kk toggled on after a prior run without it) would hit a stale cache
    entry from the wrong provider and never actually call the right one."""
    if language in DEEPGRAM_UNSUPPORTED and configured_provider == "deepgram" and groq_api_key_set:
        return "groq"
    return configured_provider


# ─────────────────────────── pure-нормализация (unit-тесты) ───────────────────────────


def deepgram_to_transcript(resp: dict[str, Any], *, default_language: str = "en") -> Transcript:
    """Сырой ответ Deepgram REST → Transcript (секунды, punctuated_word, сортировка по start).

    JobError при неожиданной структуре или нуле слов.
    """
    try:
        channel = resp["results"]["channels"][0]
        raw_words = channel["alternatives"][0]["words"]
    except (KeyError, IndexError, TypeError) as e:
        raise JobError(_STAGE, f"unexpected Deepgram response structure: {e}") from e

    words: list[Word] = []
    for w in raw_words:
        try:
            text = w.get("punctuated_word") or w["word"]
            start = float(w["start"])
            end = float(w["end"])
        except (KeyError, TypeError, ValueError) as e:
            raise JobError(_STAGE, f"malformed word in Deepgram response: {e}") from e
        conf = w.get("confidence")
        spk = w.get("speaker")
        words.append(
            Word(
                text=text,
                start=start,
                end=end,
                confidence=float(conf) if conf is not None else None,
                speaker=int(spk) if spk is not None else None,
            )
        )
    if not words:
        raise JobError(_STAGE, "Deepgram returned 0 words")
    words.sort(key=lambda x: x.start)

    language: str = channel.get("detected_language") or default_language
    raw_dur = (resp.get("metadata") or {}).get("duration")
    if raw_dur is None:
        raise JobError(_STAGE, "Deepgram did not return metadata.duration")
    try:
        duration = float(raw_dur)
    except (TypeError, ValueError) as e:
        raise JobError(_STAGE, f"malformed metadata.duration {raw_dur!r}: {e}") from e
    return Transcript(language=language, duration=duration, words=words)


def groq_to_transcript(resp: dict[str, Any], *, default_language: str = "kk") -> Transcript:
    """Groq verbose_json → Transcript (секунды, сортировка по start).

    Groq отдаёт секунды (как Deepgram). Нет confidence и diarization — Whisper не поддерживает.
    `language` в ответе — полное английское слово ("Kazakh"), используем default_language (ISO-код).
    JobError при неожиданной структуре или нуле слов.
    """
    try:
        raw_words = resp.get("words") or []
        raw_dur = resp.get("duration")
    except (KeyError, TypeError) as e:
        raise JobError(_STAGE, f"unexpected Groq response structure: {e}") from e

    if raw_dur is None:
        raise JobError(_STAGE, "Groq did not return duration")

    words: list[Word] = []
    for w in raw_words:
        try:
            text = w["word"]
            start = float(w["start"])
            end = float(w["end"])
        except (KeyError, TypeError, ValueError) as e:
            raise JobError(_STAGE, f"malformed word in Groq response: {e}") from e
        words.append(Word(text=text, start=start, end=end))

    if not words:
        raise JobError(_STAGE, "Groq returned 0 words")
    words.sort(key=lambda x: x.start)
    return Transcript(language=default_language, duration=float(raw_dur), words=words)


# ─────────────────────────── провайдер Deepgram (REST через httpx) ───────────────────────────


def call_deepgram(
    wav: Path, *, api_key: str, model: str, language: str | None = None
) -> dict[str, Any]:
    """POST source.wav в Deepgram /v1/listen, вернуть сырой JSON. JobError при сбое/не-200.

    language=None → detect_language=true (авто-определение EN/RU/др.). Иначе фикс-язык.
    """
    params = {
        "model": model,
        "smart_format": "true",
        "punctuate": "true",
        # D2: диаризация ВКЛ → per-word speaker. Нужна, чтобы конец клипа не захватывал реплику
        # ДРУГОГО спикера (snap_end_index учитывает смену спикера). Включено в Nova-3 без доп.цены.
        "diarize": "true",
        "filler_words": "true",
    }
    if language:
        params["language"] = language
    else:
        params["detect_language"] = "true"
    headers = {"Authorization": f"Token {api_key}", "Content-Type": "audio/wav"}
    try:
        r = httpx.post(
            _DEEPGRAM_URL,
            params=params,
            headers=headers,
            content=wav.read_bytes(),
            # write=None: без лимита на upload (3h WAV ≈ 350МБ). read=600: длинное аудио Deepgram
            # обрабатывает дольше — 300s мог преждевременно оборвать обработку 3-часовика.
            timeout=httpx.Timeout(connect=30.0, write=None, read=600.0, pool=5.0),
        )
    except httpx.HTTPError as e:
        raise JobError(_STAGE, f"Deepgram network error: {e}") from e
    if r.status_code != 200:
        raise JobError(_STAGE, f"Deepgram HTTP {r.status_code}: {r.text[:300]}")
    data: dict[str, Any] = r.json()
    return data


# ─────────────────────────── провайдер Groq/Whisper (REST через httpx) ───────────────────────────


def _needs_groq_compression(size_bytes: int, max_mb: int) -> bool:
    """True if raw audio exceeds the Groq upload cap and must be re-encoded smaller.

    PURE (no I/O) → unit-tested. Groq Whisper 413s oversize uploads (free tier 25 MB /
    dev 100 MB); ``max_mb`` (config.groq_max_upload_mb) keeps a margin under the tier cap."""
    return size_bytes > max_mb * 1024 * 1024


def _prepare_audio_for_groq(wav: Path, *, max_mb: int) -> tuple[bytes, str, str]:
    """WAV → (bytes, filename, mime). Сжимает в 32 kbps MP3, когда СЫРОЙ WAV превышает лимит
    загрузки Groq (``max_mb``, зависит от тарифа). Whisper всё равно даунсемплит в 16 kHz →
    точность не страдает. Если сжатый MP3 ВСЁ РАВНО больше лимита (очень длинное аудио) —
    поднимаем понятный JobError, а НЕ отдаём Groq'у на 413 (правило №8: без тихих фолбэков)."""
    if not _needs_groq_compression(wav.stat().st_size, max_mb):
        return wav.read_bytes(), "audio.wav", "audio/wav"

    # Сырой WAV > лимита → сжать в MP3 32kbps mono через ffmpeg (~14 МБ/час → до ~1ч45м < 25 МБ).
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
        mp3_path = Path(tf.name)
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(wav),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-ab",
                "32k",
                str(mp3_path),
            ],
            capture_output=True,
            check=True,
        )
        mp3_bytes = mp3_path.read_bytes()
    except subprocess.CalledProcessError as e:
        raise JobError(_STAGE, f"ffmpeg compress for Groq failed: {e.stderr.decode()[:200]}") from e
    finally:
        mp3_path.unlink(missing_ok=True)

    if _needs_groq_compression(len(mp3_bytes), max_mb):
        raise JobError(
            _STAGE,
            f"audio too long for the current Groq tier (compressed "
            f"{len(mp3_bytes) // (1024 * 1024)} MB > {max_mb} MB cap). Enable Groq billing for the "
            "100 MB dev tier (then set GROQ_MAX_UPLOAD_MB=95), or use a shorter video.",
        )
    return mp3_bytes, "audio.mp3", "audio/mpeg"


def call_groq(
    wav: Path, *, api_key: str, model: str, language: str, max_upload_mb: int
) -> dict[str, Any]:
    """POST audio в Groq Whisper (OpenAI-совместимый REST). Возвращает verbose_json.

    language: ISO-код (kk, ru, en…) — всегда явный (Groq-путь = у нас всегда есть хинт).
    max_upload_mb: порог сжатия под лимит тарифа Groq (см. _prepare_audio_for_groq).
    """
    audio_bytes, filename, mime = _prepare_audio_for_groq(wav, max_mb=max_upload_mb)
    try:
        r = httpx.post(
            _GROQ_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            data={
                "model": model,
                "language": language,
                "response_format": "verbose_json",
                "timestamp_granularities[]": "word",
            },
            files={"file": (filename, audio_bytes, mime)},
            timeout=httpx.Timeout(connect=30.0, write=None, read=300.0, pool=5.0),
        )
    except httpx.HTTPError as e:
        raise JobError(_STAGE, f"Groq network error: {e}") from e
    if r.status_code != 200:
        raise JobError(_STAGE, f"Groq HTTP {r.status_code}: {r.text[:300]}")
    data: dict[str, Any] = r.json()
    return data


# ─────────────────────────── dispatch + запись артефакта ───────────────────────────


def transcribe(wav: Path, *, language: str | None = None) -> Transcript:
    """Транскрибировать wav → Transcript.

    language — ISO-код языка пользователя (опц.):
      - kk (казахский): авто-роутинг на Groq (Deepgram вернёт HTTP 400).
      - Другие / None: Deepgram с авто-детектом или явным хинтом.
      - TRANSCRIPTION_PROVIDER=groq: всегда Groq вне зависимости от языка.
    """
    if not wav.exists():
        raise JobError(_STAGE, f"no input wav: {wav}")
    s = get_settings()

    # Авто-роутинг: языки, не поддерживаемые Deepgram → Groq (если ключ задан).
    _kk_needs_groq = language in DEEPGRAM_UNSUPPORTED and s.transcription_provider == "deepgram"
    if _kk_needs_groq and not s.groq_api_key:
        raise JobError(
            _STAGE,
            f"language '{language}' is not supported by Deepgram; "
            "set GROQ_API_KEY to enable Groq transcription for this language",
        )
    provider = resolve_transcription_provider(
        language,
        configured_provider=s.transcription_provider,
        groq_api_key_set=bool(s.groq_api_key),
    )

    if provider == "groq":
        key = s.groq_api_key
        if key is None:
            raise JobError(_STAGE, "GROQ_API_KEY is not set")
        resp = call_groq(
            wav,
            api_key=key,
            model=s.groq_model,
            language=language or "kk",
            max_upload_mb=s.groq_max_upload_mb,
        )
        return groq_to_transcript(resp, default_language=language or "kk")

    if provider == "deepgram":
        key = s.deepgram_api_key
        if key is None:
            raise JobError(_STAGE, "DEEPGRAM_API_KEY is not set")
        resp = call_deepgram(wav, api_key=key, model=s.deepgram_model, language=language)
        return deepgram_to_transcript(resp, default_language=language or "en")

    raise JobError(_STAGE, f"provider {provider} is not implemented yet")


def transcribe_to_file(wav: Path, out_path: Path, *, language: str | None = None) -> Transcript:
    """transcribe(wav) + запись transcript.json. Возвращает Transcript."""
    t = transcribe(wav, language=language)
    out_path.write_text(t.model_dump_json(indent=2), encoding="utf-8")
    return t
