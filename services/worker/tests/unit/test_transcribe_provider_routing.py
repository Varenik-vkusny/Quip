"""Regression: transcript cache must key on the EFFECTIVE provider, not the static config.

Bug (found 2026-07-11): `transcribe()` auto-routes kk (Kazakh) audio from Deepgram to
Groq, but the content-addressed cache in run.py was keyed on `s.transcription_provider`
(the static "deepgram" config) — never on the resolved provider. A Kazakh clip
transcribed once WITHOUT the Kazakh toggle (language=None -> Deepgram auto-detect
silently misidentifies kk as Turkish, returns 200 with garbage words) poisoned the
cache for that audio's sha forever: turning the toggle ON afterwards still hit the
same stale cache key and Groq was never called again.

`resolve_transcription_provider` is the single source of truth for "which provider
actually handles this request" — used both by `transcribe()` dispatch AND by run.py's
cache-key computation, so they can no longer drift apart.
"""

from app.pipeline.stage1_transcribe import resolve_transcription_provider


def test_kk_routes_to_groq_when_key_present() -> None:
    assert (
        resolve_transcription_provider("kk", configured_provider="deepgram", groq_api_key_set=True)
        == "groq"
    )


def test_kk_stays_deepgram_without_groq_key() -> None:
    # transcribe() raises JobError in this case (no silent fallback) — the cache-key
    # helper just mustn't crash; the pipeline never reaches a cache WRITE for this path.
    assert (
        resolve_transcription_provider("kk", configured_provider="deepgram", groq_api_key_set=False)
        == "deepgram"
    )


def test_other_languages_stay_on_configured_provider() -> None:
    assert (
        resolve_transcription_provider("ru", configured_provider="deepgram", groq_api_key_set=True)
        == "deepgram"
    )
    assert (
        resolve_transcription_provider(None, configured_provider="deepgram", groq_api_key_set=True)
        == "deepgram"
    )


def test_manual_groq_override_is_unaffected() -> None:
    # TRANSCRIPTION_PROVIDER=groq (manual, all languages) — already groq, kk-check is moot.
    assert (
        resolve_transcription_provider("en", configured_provider="groq", groq_api_key_set=True)
        == "groq"
    )
    assert (
        resolve_transcription_provider(None, configured_provider="groq", groq_api_key_set=False)
        == "groq"
    )
