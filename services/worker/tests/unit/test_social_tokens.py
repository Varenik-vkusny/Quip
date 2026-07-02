"""Tests for app.social.tokens — OAuth refresh-token encryption-at-rest (Fernet).

Refresh tokens are long-lived bearer credentials; they must never sit in the DB in plaintext.
Covers round-trip, ciphertext non-determinism, tamper/wrong-key rejection, and the rule-#8
errors on a missing key (never silently store/return plaintext).
"""

from __future__ import annotations

import pytest
from cryptography.fernet import InvalidToken

from app.social import tokens


def test_round_trip_recovers_plaintext() -> None:
    key = tokens.generate_key()
    secret = "1//0grefresh-token-value"
    assert tokens.decrypt_token(tokens.encrypt_token(secret, key), key) == secret


def test_ciphertext_is_not_plaintext_and_is_non_deterministic() -> None:
    key = tokens.generate_key()
    secret = "refresh-abc"
    a = tokens.encrypt_token(secret, key)
    b = tokens.encrypt_token(secret, key)
    assert secret not in a  # not stored in the clear
    assert a != b  # Fernet embeds IV/timestamp → different ciphertext each call
    assert tokens.decrypt_token(a, key) == tokens.decrypt_token(b, key) == secret


def test_tampered_ciphertext_is_rejected() -> None:
    key = tokens.generate_key()
    token = tokens.encrypt_token("secret", key)
    tampered = token[:-2] + ("AA" if token[-2:] != "AA" else "BB")
    with pytest.raises(InvalidToken):  # never returns garbage on a bad token
        tokens.decrypt_token(tampered, key)


def test_wrong_key_cannot_decrypt() -> None:
    token = tokens.encrypt_token("secret", tokens.generate_key())
    with pytest.raises(InvalidToken):
        tokens.decrypt_token(token, tokens.generate_key())


def test_empty_key_raises_not_silent() -> None:
    with pytest.raises(ValueError):
        tokens.encrypt_token("secret", "")
    with pytest.raises(ValueError):
        tokens.decrypt_token("anything", "")
