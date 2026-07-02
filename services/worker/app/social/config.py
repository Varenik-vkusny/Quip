"""Env-gated config for the dormant auto-posting backend.

Mirrors the ``app.cloud_state`` os.environ gate pattern (NOT the pydantic Settings) so
``app.config`` is untouched. **Default-OFF:** ``social_posting_enabled()`` is False unless
``SOCIAL_POSTING_ENABLED`` is truthy, so the whole feature is inert in prod until the founder
sets the flag + secrets (token key, YouTube OAuth client). Nothing here activates on import.
"""

from __future__ import annotations

import os


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes")


def social_posting_enabled() -> bool:
    """Master switch. False by default → the feature is dormant; routes/cron no-op when off."""
    return _truthy(os.environ.get("SOCIAL_POSTING_ENABLED", ""))


def token_key() -> str:
    """Fernet key for OAuth-token encryption-at-rest (Modal secret SOCIAL_TOKEN_KEY)."""
    return os.environ.get("SOCIAL_TOKEN_KEY", "")


def youtube_oauth() -> tuple[str, str]:
    """(client_id, client_secret) for the Google OAuth web client; empty until set."""
    return (
        os.environ.get("YOUTUBE_OAUTH_CLIENT_ID", ""),
        os.environ.get("YOUTUBE_OAUTH_CLIENT_SECRET", ""),
    )


def youtube_redirect_uri() -> str:
    """The registered OAuth callback (must match the Google client byte-for-byte)."""
    return os.environ.get("YOUTUBE_OAUTH_REDIRECT_URI", "")
