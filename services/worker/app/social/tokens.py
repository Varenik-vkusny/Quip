"""OAuth refresh-token encryption-at-rest (Fernet / AES-128-CBC + HMAC).

Refresh tokens are long-lived bearer credentials for a connected user's TikTok/IG/YouTube
account; storing them plaintext in Postgres would make the DB a single-leak game-over. We
encrypt with a symmetric key held only server-side (env ``SOCIAL_TOKEN_KEY``, a Modal secret),
so a DB dump alone is useless.

``cryptography`` ships as the ``[crypto]`` extra of the already-declared ``pyjwt[crypto]``
dependency — no new top-level requirement. Rule #8: a missing key raises, never silently
stores or returns plaintext.
"""

from __future__ import annotations

from cryptography.fernet import Fernet


def generate_key() -> str:
    """A fresh urlsafe-base64 Fernet key (the founder mints one for ``SOCIAL_TOKEN_KEY``)."""
    return Fernet.generate_key().decode("ascii")


def encrypt_token(plaintext: str, key: str) -> str:
    """Encrypt a token string → urlsafe ciphertext. Empty key raises (rule #8: never plaintext)."""
    if not key:
        raise ValueError("SOCIAL_TOKEN_KEY is required to encrypt OAuth tokens")
    return Fernet(key.encode("ascii")).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_token(ciphertext: str, key: str) -> str:
    """Decrypt ciphertext → token string. Empty key raises; tamper/wrong-key raises InvalidToken.

    Never returns garbage on a bad token — ``cryptography`` rejects it, so the caller fails
    loudly (e.g. flags the connection 'needs reconnect') instead of posting with a junk token.
    """
    if not key:
        raise ValueError("SOCIAL_TOKEN_KEY is required to decrypt OAuth tokens")
    return Fernet(key.encode("ascii")).decrypt(ciphertext.encode("ascii")).decode("utf-8")
